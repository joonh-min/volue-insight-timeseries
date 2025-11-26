from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from configparser import RawConfigParser
from typing import Any, Literal
from urllib.parse import urljoin

import aiohttp
import aiohttp.web_exceptions
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from . import auth, curves_async, events, util
from .util import CurveException, DatetimeLike, _TsFreqs

RETRY_COUNT = 4    # Number of times to retry
RETRY_DELAY = 0.5  # Delay between retried calls, in seconds.
TIMEOUT = 300      # Default timeout for web calls, in seconds.
API_URLBASE = 'https://api.volueinsight.com'
AUTH_URLBASE = 'https://auth.volueinsight.com'


class ConfigException(Exception):
    pass


class MetadataException(Exception):
    pass


class Asession:
    """ Establish a connection to Wattsight API

    Creates an object that holds the state which is needed when talking to the
    Wattsight data center. To establish a session, you have to provide
    suthentication information either directly by using a ```client_id` and
    ``client_secret`` or using a ``config_file`` .

    See https://api.volueinsight.com/#documentation for information how to get
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
    timeout: float
        Timeout for REST calls, in seconds

    Returns
    -------
    session: :class:`volue_insight_timeseries.session.Session` object

    """

    def __init__(
        self,
        urlbase: str | None = None,
        config_file: str | RawConfigParser | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        auth_urlbase: str | None = None,
        timeout: float | None = None,
        retry_update_auth: bool = False,
    )->None:
        self.urlbase:str = urlbase if urlbase is not None else API_URLBASE
        self.auth:auth.OAuth | None = None
        self.timeout:float = timeout if timeout is not None else TIMEOUT
        self._session = requests.Session()
        self._asession: aiohttp.ClientSession | None = None
        self.retry_update_auth = retry_update_auth
        self._semaphore = asyncio.Semaphore(10)  # Limit concurrent connections
        if config_file is not None:
            self.read_config_file(config_file)
        elif client_id is not None and client_secret is not None:
            self.configure(client_id, client_secret, auth_urlbase)
        if timeout is not None:
            self.timeout = timeout

    def read_config_file(self, config_file:str|RawConfigParser)->None:
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
        if auth_type == 'OAuth':
            client_id = config.get(auth_type, 'id')
            client_secret = config.get(auth_type, 'secret')
            auth_urlbase = config.get(auth_type, 'auth_urlbase', fallback=AUTH_URLBASE)
            self.auth = auth.OAuth(self, client_id, client_secret, auth_urlbase)
        timeout = config.get('common', 'timeout', fallback=None)
        if timeout is not None:
            self.timeout = float(timeout)

    def configure(self, client_id:str, client_secret:str, auth_urlbase:str|None=None)->None:
        """Programmatically set authentication parameters"""
        if self.auth is not None:
            raise ConfigException('Session configuration is already done')
        auth_urlbase = auth_urlbase if auth_urlbase is not None else AUTH_URLBASE
        self.auth = auth.OAuth(self, client_id, client_secret, auth_urlbase)

    async def get_curve(self, id:int|None=None, name:str|None=None) -> curves_async.curveType:
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
            :class:`~volue_insight_timeseries.curves_async.TimeSeriesCurve`,
            :class:`~volue_insight_timeseries.curves_async.TaggedCurve`,
            :class:`~volue_insight_timeseries.curves_async.InstanceCurve`,
            :class:`~volue_insight_timeseries.curves_async.TaggedInstanceCurve`.
        """
        if id is not None:
            warnings.warn("Looking up a curve by ID will be removed in the future.", FutureWarning, stacklevel=2)
        if id is None and name is None:
            raise MetadataException('No curve specified')

        arg = util.make_arg('id', id) if id is not None else util.make_arg('name', name)
        response:curves_async.Metadata = await self.data_request('GET', self.urlbase, f'/api/curves/get?{arg}')
        return self._build_curve(response)

    async def search(
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
        frequency:_TsFreqs|list[_TsFreqs]|None=None,
        data_type:str|list[str]|None=None,
        curve_state:str|list[str]|None=None,
        modified_since:DatetimeLike|None=None,
        only_accessible:bool=False
    )->list[curves_async.curveType]:
        """
        Search for a curve matching various metadata.

        This function searches for curves that matches the given search
        parameters and returns a list of 0 or more curve objects.
        A curve object can be a
        :class:`~volue_insight_timeseries.curves_async.TimeSeriesCurve`,
        :class:`~volue_insight_timeseries.curves_async.TaggedCurve`,
        :class:`~volue_insight_timeseries.curves_async.InstanceCurve` or a
        :class:`~volue_insight_timeseries.curves_async.TaggedInstanceCurve` object.

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
            :meth:`volue_insight_timeseries.session.Session.get_commodities`

        category: str or list of str
            search for curves that match the given ``category`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_categories`

        area: str or list of str
            search for curves that match the given ``area`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_areas`

        station: str or list of str
            search for curves that match the given ``station`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_stations`

        source: str or list of str
            search for curves that match the given ``source`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_sources`

        scenario: str or list of str
            search for curves that match the given ``scenario`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_scenarios`

        unit: str or list of str
            search for curves that match the given ``unit`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_units`

        time_zone: str or list of str
            search for curves that match the given ``time_zone`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_time_zones`

        version: str or list of str
            search for curves that match the given ``version`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_versions`

        frequency: str or list of str
            search for curves that match the given ``frequency`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_frequencies`

        data_type: str or list of str
            search for curves that match the given ``data_type`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_data_types`

        curve_state: str or list of str
            search for curves that match the given ``curve_state`` attribute.
            Get valid values for this attribute with
            :meth:`volue_insight_timeseries.session.Session.get_curve_state`

        modified_since: datestring, pandas.Timestamp or datetime.datetime
            only return curves that where modified after given datetime.

        only_accessible: bool
            If True, only return curves you have (some) access to.

        Returns
        -------
        curves: list
            list of curve objects, can be one of:
            :class:`~volue_insight_timeseries.curves_async.TimeSeriesCurve`,
            :class:`~volue_insight_timeseries.curves_async.TaggedCurve`,
            :class:`~volue_insight_timeseries.curves_async.InstanceCurve`,
            :class:`~volue_insight_timeseries.curves_async.TaggedInstanceCurve`.
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
        response = await self.data_request('GET', self.urlbase, f'/api/curves{astr}')

        metadata_list:list[curves_async.Metadata] = [response] if isinstance(response, dict) else response
        return [self._build_curve(metadata) for metadata in metadata_list]

    def make_curve(self, id:int, curve_type:Literal["TIME_SERIES", "TAGGED", "INSTANCES", "TAGGED_INSTANCES"])->curves_async.curveType:
        """Return a mostly uninitialized curve object of the correct type.
        This is generally a bad idea, use get_curve or search when possible."""
        if curve_type in self._curve_types:
            return self._curve_types[curve_type](id, None, self)
        raise CurveException('Bad curve type requested')

    def events(self, curve_list, start_time=None, timeout=None):
        """Get an event listener for a list of curves_async."""
        return events.EventListener(self, curve_list, start_time=start_time, timeout=timeout)

    _attributes = {'commodities', 'categories', 'areas', 'stations', 'sources', 'scenarios',
                   'units', 'time_zones', 'versions', 'frequencies', 'data_types',
                   'curve_states', 'curve_types', 'functions', 'filters'}

    async def get_commodities(self)->list[dict]|dict:
        """
        Get valid values for the commodity attribute
        """
        return await self.get_attribute('commodities')

    async def get_categories(self)->list[dict]|dict:
        """
        Get valid values for the category attribute
        """
        return await self.get_attribute('categories')

    async def get_areas(self)->list[dict]|dict:
        """
        Get valid values for the area attribute
        """
        return await self.get_attribute('areas')

    async def get_stations(self)->list[dict]|dict:
        """
        Get valid values for the station attribute
        """
        return await self.get_attribute('stations')

    async def get_sources(self)->list[dict]|dict:
        """
        Get valid values for the source attribute
        """
        return await self.get_attribute('sources')

    async def get_scenarios(self)->list[dict]|dict:
        """
        Get valid values for the scenarios attribute
        """
        return await self.get_attribute('scenarios')

    async def get_units(self)->list[dict]|dict:
        """
        Get valid values for the unit attribute
        """
        return await self.get_attribute('units')

    async def get_time_zones(self)->list[dict]|dict:
        """
        Get valid values for the time zone attribute
        """
        return await self.get_attribute('time_zones')

    async def get_versions(self)->list[dict]|dict:
        """
        Get valid values for the version attribute
        """
        return await self.get_attribute('versions')

    async def get_frequencies(self)->list[dict]|dict:
        """
        Get valid values for the frequency attribute
        """
        return await self.get_attribute('frequencies')

    async def get_data_types(self)->list[dict]|dict:
        """
        Get valid values for the data_type attribute
        """
        return await self.get_attribute('data_types')

    async def get_curve_states(self)->list[dict]|dict:
        """
        Get valid values for the curve_state attribute
        """
        return await self.get_attribute('curve_states')

    async def get_curve_types(self)->list[dict]|dict:
        """
        Get valid values for the curve_type attribute
        """
        return await self.get_attribute('curve_types')

    async def get_functions(self)->list[dict]|dict:
        """
        Get valid values for the function attribute
        """
        return await self.get_attribute('functions')

    async def get_filters(self)->list[dict]|dict:
        """
        Get valid values for the filter attribute
        """
        return await self.get_attribute('filters')

    async def get_attribute(self, attribute:str)->dict|list[dict]:
        """Get valid values for an attribute."""
        if attribute not in self._attributes:
            raise MetadataException(f'Attribute {attribute} is not valid')
        return await self.data_request('GET', self.urlbase, f'/api/{attribute}')
        # if not response:
        #     return response
        # return response.json()

    _curve_types = {
        util.TIME_SERIES:      curves_async.TimeSeriesCurve,
        util.TAGGED:           curves_async.TaggedCurve,
        util.INSTANCES:        curves_async.InstanceCurve,
        util.TAGGED_INSTANCES: curves_async.TaggedInstanceCurve,
    }

    _meta_keys = ('id', 'name', 'frequency', 'time_zone', 'curve_type')

    def _build_curve(self, metadata:curves_async.Metadata)->curves_async.curveType:
        for key in self._meta_keys:
            if key not in metadata:
                raise MetadataException(f'Mandatory key {key} not found in metadata')
        curve_id = int(metadata['id'])
        if('curve_state' in metadata and metadata['curve_state'] == 'DEPRECATED'):
            warnings.warn("Deprecation warning for curve: {}".format(metadata['name']), DeprecationWarning, stacklevel=4)
        if metadata['curve_type'] in self._curve_types:
            return self._curve_types[metadata["curve_type"]](curve_id, metadata, self)
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

    def _validate_auth(self, data:Any, rawdata:bytes|None)->dict[str,str]:
        headers:dict[str,str] = {}

        if data is not None or rawdata is not None:
            headers['content-type'] = 'application/json'
            databytes = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        if data is None and rawdata is not None:
            databytes = rawdata
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
        data:Any=None,
        rawdata:bytes|None=None,
        headers:dict[str,str]|None=None,
        authval:tuple[str,str]|None=None,
        stream: bool = False,
        retries: int = RETRY_COUNT,
    ) -> requests.Response | None:
        if urlbase is None:
            urlbase = self.urlbase
        longurl = urljoin(urlbase, url)

        databytes = None
        if data is not None:
            databytes = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        if data is None and rawdata is not None:
            databytes = rawdata
        timeout = None
        status_code = None
        try:
            res = self._session.request(
                method=req_type, url=longurl, data=databytes, headers=headers, auth=authval, stream=stream, timeout=self.timeout
            )
            status_code = res.status_code
        except requests.exceptions.Timeout as e:
            timeout = e
            res = None
        if status_code is not None and (timeout is not None or (500 <= status_code < 600) or status_code == 408) and retries > 0:
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY)
            return self.send_data_request(req_type, urlbase, url, data, rawdata, headers, authval, stream, retries-1)
        if timeout is not None:
            raise timeout
        return res

    @retry(
        wait=wait_fixed(RETRY_DELAY),
        stop=stop_after_attempt(RETRY_COUNT),
        retry=retry_if_exception_type((aiohttp.ClientError, aiohttp.web_exceptions.HTTPRequestTimeout, asyncio.TimeoutError))
    )
    async def data_request(
        self,
        req_type: Literal["GET", "POST"],
        urlbase: str,
        url: str,
        data: Any = None,
        rawdata: bytes | None = None,
        authval:tuple[str,str]|None=None,
        stream: bool = False,
    ) -> dict|list[dict]:
        """Run a call to the backend, dealing with authentication etc."""
        headers = self._validate_auth(data, rawdata)
        if self._asession is None:
            raise MetadataException('Async session not initialized')
        if urlbase is None:
            urlbase = self.urlbase
        longurl = urljoin(urlbase, url)

        databytes = None
        if data is not None:
            databytes = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        if data is None and rawdata is not None:
            databytes = rawdata
        status_code = None

        try:
            async with self._semaphore, self._asession.request(
                method=req_type, url=longurl, data=databytes, headers=headers, auth=aiohttp.BasicAuth(*authval) if authval else None,
            ) as resp:
                status_code = resp.status

                if stream:
                    response_data = bytearray()
                    async for chunk in resp.content.iter_chunked(1024):
                        response_data.extend(chunk)
                    byte_resp = bytes(response_data)
                    # jsonify the content to mimic requests.Response behavior
                    response_content = json.loads(byte_resp.decode())
                else:
                    response_content = await resp.json()

                if status_code == 400:
                    raise aiohttp.web_exceptions.HTTPBadRequest(reason=response_content["reason"], body=str(response_content))

        except Exception as e:
            raise
        return response_content
