from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from configparser import RawConfigParser
from typing import IO, TYPE_CHECKING, ClassVar, Literal, overload
from urllib.parse import urljoin, urlparse

import aiohttp
import requests

from . import auth, curves, retry, session_async, util
from ._version import __version__
from .auth import AuthType
from .events import EventListener
from .models import CURVE_METADATA_KEYS
from .session_async import Asession
from .util import CurveException

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType

    from urllib3.util.retry import Retry

    from .models import AttributeName, AttributeValue, CurveMetadata, CurveType, DatetimeLike, JSONValue, RawData, TsFreq

RETRY_COUNT = 4    # Number of times to retry
RETRY_DELAY = 0.5  # Delay between retried calls, in seconds.
TIMEOUT = 300      # Default timeout for web calls, in seconds.
API_URLBASE = 'https://data-forecasts.volue.com'
# Defined in auth, so the async session can use them too
AUTH_URLBASE = auth.AUTH_URLBASE
VOLUE_IDENTITY_AUTH_URLBASE = auth.VOLUE_IDENTITY_AUTH_URLBASE
DEFAULT_AUTH_URLBASES = auth.DEFAULT_AUTH_URLBASES

USER_AGENT = f'volue-insight-timeseries/{__version__}'


class ConfigException(Exception):
    pass


def _parse_auth_type(auth_type:AuthType|str)->AuthType:
    try:
        return AuthType(auth_type)
    except ValueError:
        raise ConfigException('Unknown auth_type {!r}, expected one of: {}'
                              .format(auth_type, ', '.join(t.value for t in AuthType))) from None


class MetadataException(Exception):
    pass


class Session:
    """ Establish a connection to Wattsight API

    Creates an object that holds the state which is needed when talking to the
    Wattsight data center. To establish a session, you have to provide
    suthentication information either directly by using a ```client_id` and
    ``client_secret`` or using a ``config_file`` .

    See https://docs.volue.com/data-and-forecasts/volue-insight-timeseries/getting-started/connection for information how to get
    your authentication data.

    Parameters
    ----------

    urlbase: url
        Location of Wattsight service
    config_file: path
        path to the config.ini file which contains your authentication
        information.
    client_id: str
        Your client ID
    client_secret:
        Your client secret.
    auth_urlbase: url
        Location of Wattsight authentication service
    auth_type: AuthType or str
        ``AuthType.OAUTH`` / ``'OAuth'`` (default) for Volue Data & Forecasts credentials, or
        ``AuthType.VOLUE_IDENTITY`` / ``'VolueIdentity'`` for Volue Identity credentials.
    timeout: float
        Timeout for REST calls, in seconds
    requests_session: requests.Session
        Your own ``requests.Session``, for proxies, CA bundles, client
        certificates or connection pool size. All requests, authentication
        included, are sent through it.
    retries: int or urllib3.util.Retry
        By default, a request that timed out, failed to connect or got a 408
        or 5xx response is retried 4 times, with 0.5 seconds between attempts.
        Pass an int to change the number, or a ``urllib3.util.Retry`` for other
        rules, such as backoff. If ``requests_session`` already retries
        through its adapters, pass 0 so requests are not retried twice.

    Returns
    -------
    session: :class:`volue_insight_timeseries_async.session.Session` object

    """

    def __init__(
        self,
        urlbase: str | None = None,
        config_file: str | IO[str] | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        auth_urlbase: str | None = None,
        timeout: float | None = None,
        retry_update_auth: bool = False,
        auth_type: AuthType | str = AuthType.OAUTH,
        requests_session: requests.Session | None = None,
        retries: int | Retry = RETRY_COUNT,
    )->None:
        self.urlbase = API_URLBASE
        self.auth:auth.OAuth | None = None
        self.timeout:float = TIMEOUT
        if retries is None:
            raise TypeError('retries must be an int or a urllib3 Retry, not NoneType')
        self._retries = retry.make_retry(retries, RETRY_DELAY)
        if requests_session is None:
            requests_session = requests.Session()
        elif not isinstance(requests_session, requests.Session):
            raise TypeError(f'requests_session must be a requests.Session, not {type(requests_session).__name__}')
        self._session = requests_session
        self._session.headers['User-Agent'] = f'{USER_AGENT} {requests.utils.default_user_agent()}'
        self.retry_update_auth = retry_update_auth
        self._asession:aiohttp.ClientSession | None = None
        if config_file is not None:
            self.read_config_file(config_file)
        elif client_id is not None and client_secret is not None:
            self.configure(client_id, client_secret, auth_urlbase, auth_type)
        if urlbase is not None:
            self.urlbase = urlbase
        if timeout is not None:
            self.timeout = timeout

        # Made on the first `async with`, so a sync-only Session does not log in twice
        self._async_client: Asession | None = None
        # Open `async with` blocks, the aiohttp session is closed when the last one exits
        self._async_users = 0

    @property
    def urlbase(self)->str:
        return self._urlbase

    @urlbase.setter
    def urlbase(self, value:str)->None:
        # Every path that sets urlbase passes here, subclasses included.
        # Any host other than the API_URLBASE host gives a FutureWarning.
        if value and urlparse(value).hostname != urlparse(API_URLBASE).hostname:
            warnings.warn(
                f'urlbase {value} is not the default host. Other hosts can stop working. '
                f'Remove urlbase from your config, or set it to {API_URLBASE}.',
                FutureWarning, stacklevel=2)
        self._urlbase = value

    def read_config_file(self, config_file:str|IO[str])->None:
        """Set up according to configuration file with hosts and access details"""
        if self.auth is not None:
            raise ConfigException('Session configuration is already done')
        config = RawConfigParser()
        # Support being given a file-like object or a file path:
        if hasattr(config_file, 'read'):
            config.read_file(config_file)
        else:
            files_read = config.read(config_file)
            if not files_read:
                raise ConfigException(f'Configuration file with name {config_file} '
                                      'was not found.')
        urlbase = config.get('common', 'urlbase', fallback=None)
        if urlbase is not None:
            self.urlbase = urlbase
        auth_type = config.get('common', 'auth_type')
        auth_type = _parse_auth_type(auth_type)
        client_id = config.get(auth_type.value, 'id')
        client_secret = config.get(auth_type.value, 'secret')
        auth_urlbase = config.get(auth_type.value, 'auth_urlbase', fallback=None)
        self.configure(client_id, client_secret, auth_urlbase, auth_type)
        timeout = config.get('common', 'timeout', fallback=None)
        if timeout is not None:
            self.timeout = float(timeout)

    def configure(
        self,
        client_id:str,
        client_secret:str,
        auth_urlbase:str|None=None,
        auth_type:AuthType|str=AuthType.OAUTH,
    )->None:
        """Programmatically set authentication parameters"""
        if self.auth is not None:
            raise ConfigException('Session configuration is already done')
        auth_type = _parse_auth_type(auth_type)
        if auth_urlbase is None:
            auth_urlbase = DEFAULT_AUTH_URLBASES[auth_type]
        auth_classes = {AuthType.OAUTH: auth.OAuth, AuthType.VOLUE_IDENTITY: auth.VolueIdentity}
        self.auth = auth_classes[auth_type](self, client_id, client_secret, auth_urlbase)

    def get_curve(self, id:int|None=None, name:str|None=None) -> curves.curveType:
        """Getting a curve object

        Return a curve object of the correct type.  Name should be specified.
        While it is possible to get a curve by id, this is not guaranteed to be
        long-term stable and will be removed in future versions.

        Parameters
        ----------

        id: int
            curve id (deprecated)
        name: str
            curve name

        Returns
        -------
        curve object
            Curve objects, can be one of:
            :class:`~volue_insight_timeseries_async.curves.TimeSeriesCurve`,
            :class:`~volue_insight_timeseries_async.curves.TaggedCurve`,
            :class:`~volue_insight_timeseries_async.curves.InstanceCurve`,
            :class:`~volue_insight_timeseries_async.curves.TaggedInstanceCurve`.
        """
        if id is not None:
            warnings.warn("Looking up a curve by ID will be removed in the future.", FutureWarning, stacklevel=2)
        if id is not None:
            arg = util.make_arg('id', id)
        elif name is not None:
            arg = util.make_arg('name', name)
        else:
            raise MetadataException('No curve specified')
        response = self.data_request('GET', self.urlbase, f'/api/v1/timeseries/curves/get?{arg}')
        return self.handle_single_curve_response(response)

    def search(
        self,
        query:str|None=None,
        id:int|list[int]|None=None,
        name:str|list[str]|None=None,
        commodity:str|list[str]|None=None,
        category:str|list[str]|None=None,
        area:str|list[str]|None=None,
        station:str|list[str]|None=None,
        source:str|list[str]|None=None,
        scenario:str|list[str]|None=None,
        unit:str|list[str]|None=None,
        time_zone:str|list[str]|None=None,
        version:str|list[str]|None=None,
        frequency:TsFreq|list[TsFreq]|None=None,
        data_type:str|list[str]|None=None,
        curve_state:str|list[str]|None=None,
        modified_since:DatetimeLike|None=None,
        only_accessible:bool=False
    )->list[curves.curveType]:
        """
        Search for a curve matching various metadata.

        This function searches for curves that matches the given search
        parameters and returns a list of 0 or more curve objects.
        A curve object can be a
        :class:`~volue_insight_timeseries_async.curves.TimeSeriesCurve`,
        :class:`~volue_insight_timeseries_async.curves.TaggedCurve`,
        :class:`~volue_insight_timeseries_async.curves.InstanceCurve` or a
        :class:`~volue_insight_timeseries_async.curves.TaggedInstanceCurve` object.

        The search will return those curves matching all supplied parameters
        (logical AND). For most parameters, a list of values may be supplied.
        The search will match any of these values (logical OR).  If a single
        value contains a string with comma-separated values, these will be
        treated as a list but will match with logical AND. (This only makes
        sense for parameters where a curve may have multiple values:
        area (border curves), category, source and scenario.)

        For more details, see the REST documentation.

        Parameters
        ----------

        query: str
            A query string used for a language-aware text search on both names
            and descriptions of the various attributes in the curve.

        id: int or lits of int
            search for one or more specific id's (deprecated)

        name: str or list of str
            search for one or more curve names, you can use the ``*`` as
            a wildcard for patter matching.

        commodity: str or list of str
            search for curves that match the given ``commodity`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_commodities`

        category: str or list of str
            search for curves that match the given ``category`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_categories`

        area: str or list of str
            search for curves that match the given ``area`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_areas`

        station: str or list of str
            search for curves that match the given ``station`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_stations`

        source: str or list of str
            search for curves that match the given ``source`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_sources`

        scenario: str or list of str
            search for curves that match the given ``scenario`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_scenarios`

        unit: str or list of str
            search for curves that match the given ``unit`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_units`

        time_zone: str or list of str
            search for curves that match the given ``time_zone`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_time_zones`

        version: str or list of str
            search for curves that match the given ``version`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_versions`

        frequency: str or list of str
            search for curves that match the given ``frequency`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_frequencies`

        data_type: str or list of str
            search for curves that match the given ``data_type`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_data_types`

        curve_state: str or list of str
            search for curves that match the given ``curve_state`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries_async.session.Session.get_curve_state`

        modified_since: datestring, pandas.Timestamp or datetime.datetime
            only return curves that where modified after given datetime.

        only_accessible: bool
            If True, only return curves you have (some) access to.

        Returns
        -------
        curves: list
            list of curve objects, can be one of:
            :class:`~volue_insight_timeseries_async.curves.TimeSeriesCurve`,
            :class:`~volue_insight_timeseries_async.curves.TaggedCurve`,
            :class:`~volue_insight_timeseries_async.curves.InstanceCurve`,
            :class:`~volue_insight_timeseries_async.curves.TaggedInstanceCurve`.
        """
        search_terms = {
            'query': query,
            'id': id,
            'name': name,
            'commodity': commodity,
            'category': category,
            'area': area,
            'station': station,
            'source': source,
            'scenario': scenario,
            'unit': unit,
            'time_zone': time_zone,
            'version': version,
            'frequency': frequency,
            'data_type': data_type,
            'curve_state': curve_state,
            'modified_since': modified_since,
            'only_accessible': only_accessible,
        }
        if id is not None:
            warnings.warn("Searching for curves by ID will be removed in the future.", FutureWarning, stacklevel=2)
        args:list[str] = []
        astr:str = ''
        for key, val in search_terms.items():
            if val is None:
                continue
            args.append(util.make_arg(key, val))
        if args:
            astr = "?{}".format("&".join(args))
        # Now run the search, and try to produce a list of curves
        response = self.data_request('GET', self.urlbase, f'/api/v1/timeseries/curves{astr}')
        return self.handle_multi_curve_response(response)

    @overload
    def make_curve(self, id:int, curve_type:Literal["TIME_SERIES"])->curves.TimeSeriesCurve: ...

    @overload
    def make_curve(self, id:int, curve_type:Literal["TAGGED"])->curves.TaggedCurve: ...

    @overload
    def make_curve(self, id:int, curve_type:Literal["INSTANCES"])->curves.InstanceCurve: ...

    @overload
    def make_curve(self, id:int, curve_type:Literal["TAGGED_INSTANCES"])->curves.TaggedInstanceCurve: ...

    @overload
    def make_curve(self, id:int, curve_type:CurveType)->curves.curveType: ...

    def make_curve(self, id:int, curve_type:CurveType)->curves.curveType:
        """Return a mostly uninitialized curve object of the correct type.
        This is generally a bad idea, use get_curve or search when possible."""
        if curve_type in self._curve_types:
            return self._curve_types[curve_type](id, None, self)
        raise CurveException('Bad curve type requested')

    def events(
        self,
        curve_list:curves.BaseCurve|int|Iterable[curves.BaseCurve|int],
        start_time:DatetimeLike|None=None,
        timeout:float|None=None,
    )->EventListener:
        """Get an event listener for a list of curves."""
        return EventListener(self, curve_list, start_time=start_time, timeout=timeout)

    _attributes: ClassVar[set[str]] = {'commodities', 'categories', 'areas', 'stations', 'sources', 'scenarios',
                   'units', 'time_zones', 'versions', 'frequencies', 'data_types',
                   'curve_states', 'curve_types', 'functions', 'filters'}

    def get_commodities(self)->list[AttributeValue]|None:
        """
        Get valid values for the commodity attribute
        """
        return self.get_attribute('commodities')

    def get_categories(self)->list[AttributeValue]|None:
        """
        Get valid values for the category attribute
        """
        return self.get_attribute('categories')

    def get_areas(self)->list[AttributeValue]|None:
        """
        Get valid values for the area attribute
        """
        return self.get_attribute('areas')

    def get_stations(self)->list[AttributeValue]|None:
        """
        Get valid values for the station attribute
        """
        return self.get_attribute('stations')

    def get_sources(self)->list[AttributeValue]|None:
        """
        Get valid values for the source attribute
        """
        return self.get_attribute('sources')

    def get_scenarios(self)->list[AttributeValue]|None:
        """
        Get valid values for the scenarios attribute
        """
        return self.get_attribute('scenarios')

    def get_units(self)->list[AttributeValue]|None:
        """
        Get valid values for the unit attribute
        """
        return self.get_attribute('units')

    def get_time_zones(self)->list[AttributeValue]|None:
        """
        Get valid values for the time zone attribute
        """
        return self.get_attribute('time_zones')

    def get_versions(self)->list[AttributeValue]|None:
        """
        Get valid values for the version attribute
        """
        return self.get_attribute('versions')

    def get_frequencies(self)->list[AttributeValue]|None:
        """
        Get valid values for the frequency attribute
        """
        return self.get_attribute('frequencies')

    def get_data_types(self)->list[AttributeValue]|None:
        """
        Get valid values for the data_type attribute
        """
        return self.get_attribute('data_types')

    def get_curve_states(self)->list[AttributeValue]|None:
        """
        Get valid values for the curve_state attribute
        """
        return self.get_attribute('curve_states')

    def get_curve_types(self)->list[AttributeValue]|None:
        """
        Get valid values for the curve_type attribute
        """
        return self.get_attribute('curve_types')

    def get_functions(self)->list[AttributeValue]|None:
        """
        Get valid values for the function attribute
        """
        return self.get_attribute('functions')

    def get_filters(self)->list[AttributeValue]|None:
        """
        Get valid values for the filter attribute
        """
        return self.get_attribute('filters')

    def get_attribute(self, attribute:AttributeName)->list[AttributeValue]|None:
        """Get valid values for an attribute."""
        if attribute not in self._attributes:
            raise MetadataException(f'Attribute {attribute} is not valid')
        response = self.data_request('GET', self.urlbase, f'/api/v1/timeseries/curve-attributes/{attribute.replace("_", "-")}')
        if response is None:
            return response
        if response.status_code == 200:
            return response.json()
        if response.status_code == 204:
            return None
        raise MetadataException(f'Failed loading {attribute}: {response.content.decode()}')

    _curve_types: ClassVar[dict[str, type[curves.curveType]]] = {
        util.TIME_SERIES:      curves.TimeSeriesCurve,
        util.TAGGED:           curves.TaggedCurve,
        util.INSTANCES:        curves.InstanceCurve,
        util.TAGGED_INSTANCES: curves.TaggedInstanceCurve,
    }

    _meta_keys = CURVE_METADATA_KEYS

    def _build_curve(self, metadata:CurveMetadata)->curves.curveType:
        for key in self._meta_keys:
            if key not in metadata:
                raise MetadataException(f'Mandatory key {key} not found in metadata')
        curve_id = int(metadata['id'])
        if('curve_state' in metadata and metadata['curve_state'] == 'DEPRECATED'):
            warnings.warn("Deprecation warning for curve: {}".format(metadata['name']), DeprecationWarning, stacklevel=4)
        if metadata['curve_type'] in self._curve_types:
            c = self._curve_types[metadata['curve_type']](curve_id, metadata, self)
            if isinstance(c, curves.TaggedInstanceCurve):
                categories = getattr(c, 'categories', []) or []
                if 'PAIRED-LIST' in categories:
                    c.__class__ = curves.PairedListCurve
            return c
        raise CurveException('Unknown curve type ({})'.format(metadata['curve_type']))

    def _get_auth_header_with_retry(self, retries:int=RETRY_COUNT)-> dict[str, str]:
        if self.auth is None:
            raise MetadataException('No authentication configured for this session')

        try:
            self.auth.validate_auth()
            return self.auth.get_headers()
        except Exception:
            if retries <= 0:
                raise
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY)
            return self._get_auth_header_with_retry(retries - 1)

    def _validate_auth(self, data:JSONValue, rawdata:RawData|None)->dict[str,str]:
        headers:dict[str,str] = {}

        if data is not None or rawdata is not None:
            headers['content-type'] = 'application/json'
        if self.auth is not None:
            # Beta-feature: Only update auth with retry if explicitly requested
            if self.retry_update_auth:
                auth_header = self._get_auth_header_with_retry()
                headers.update(auth_header)
            else:
                self.auth.validate_auth()
                headers.update(self.auth.get_headers())

        return headers

    def send_data_request(
        self,
        req_type: Literal["GET", "POST"],
        urlbase: str | None,
        url: str,
        data:JSONValue=None,
        rawdata:RawData|None=None,
        headers:dict[str,str]|None=None,
        authval:tuple[str,str]|None=None,
        stream: bool = False,
        retries: int | Retry | None = None,
    ) -> requests.Response:
        if not urlbase:
            urlbase = self.urlbase
        longurl = urljoin(urlbase, url)

        databytes: RawData | None = None
        if data is not None:
            databytes = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        if data is None and rawdata is not None:
            databytes = rawdata
        retry_setting = retry.make_retry(retries, RETRY_DELAY)
        if retry_setting is None:
            retry_setting = self._retries
        return retry.request_with_retries(http_session=self._session, retry_state=retry_setting,
                                          method=req_type, url=longurl, data=databytes, headers=headers,
                                          auth=authval, stream=stream, timeout=self.timeout)

    def data_request(
        self,
        req_type: Literal["GET", "POST"],
        urlbase: str,
        url: str,
        data: JSONValue = None,
        rawdata: RawData | None = None,
        authval:tuple[str,str]|None=None,
        stream: bool = False,
        retries: int | Retry | None = None,
    ) -> requests.Response:
        """Run a call to the backend, dealing with authentication etc."""
        headers = self._validate_auth(data, rawdata)
        res = self.send_data_request(req_type, urlbase, url, data, rawdata, headers, authval, stream, retries)
        if res.status_code == 401 and getattr(self.auth, 'auth_type', None) == AuthType.VOLUE_IDENTITY:
            raise auth.AuthFailedException(
                'Volue Identity issued a token, but the Volue Data & Forecasts API rejected it (HTTP 401). '
                'Volue Identity credentials may not be accepted for your account yet: keep using '
                'your current credentials with auth_type = OAuth until you are told to switch.')
        return res

    def handle_single_curve_response(self, response:requests.Response|None)-> curves.curveType:
        if response is None:
            raise MetadataException('Failed to load curve: No response received') from None
        if not response.ok:
            raise MetadataException(f'Failed to load curve: {response.content.decode()}') from None
        metadata:CurveMetadata = response.json()
        return self._build_curve(metadata)

    def handle_multi_curve_response(self, response:requests.Response|None)-> list[curves.curveType]:
        if response is None:
            raise MetadataException('Curve search failed: No response received') from None
        if not response.ok:
            raise MetadataException(f'Curve search failed: {response.content.decode()}') from None
        metadata_list:list[CurveMetadata] = response.json()

        return [self._build_curve(metadata) for metadata in metadata_list]

    async def __aenter__(self)->Asession:
        """
        Enter async context, returning an async session object.

        :param self: Description
        :return: Description
        :rtype: Asession
        """
        if self._async_client is None:
            self._async_client = Asession(urlbase=self.urlbase, timeout=self.timeout, retry_update_auth=self.retry_update_auth)
        # Copy the settings on every entry, so later changes to this session reach the async client
        self._async_client.urlbase = self.urlbase
        self._async_client.timeout = self.timeout
        self._async_client.retry_update_auth = self.retry_update_auth
        # The async client counts retries only, so a Retry without an int total falls back to the default
        total = self._retries.total
        self._async_client.retries = 0 if total is False else total if isinstance(total, int) else RETRY_COUNT
        # Share this session's login, so the async client needs no token request of its own
        self._async_client.auth = self.auth
        if self._async_client._asession is None or self._async_client._asession.closed:
            # Fails early when no login is set up; the token itself is sent per request, so it never goes stale here
            await asyncio.to_thread(self._async_client._get_auth_header_with_retry)
            # A new semaphore with each aiohttp session, so both belong to the event loop that is running now
            self._async_client._semaphore = asyncio.Semaphore(session_async.CONCURRENT_REQUESTS)
            # Same User-Agent as the sync requests, with aiohttp's own part in place of requests'
            user_agent = f'{USER_AGENT} {aiohttp.http.SERVER_SOFTWARE}'
            self._async_client._asession = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), headers={'User-Agent': user_agent}
            )
        self._async_users += 1
        return self._async_client

    async def __aexit__(self, exc_type:type[BaseException]|None, exc_val: BaseException|None, exc_tb: TracebackType|None)->None:
        """
        Exit async context, closes session if needed.

        Args:
            exc_type: Exception type.
            exc_val: Exception value.
            exc_tb: Traceback.
        """
        logging.info("Closing ASYNC session")
        self._async_users -= 1
        if self._async_users > 0:
            return
        if self._async_client is not None and self._async_client._asession is not None and not self._async_client._asession.closed:
            await self._async_client._asession.close()
