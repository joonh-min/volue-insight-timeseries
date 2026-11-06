#
# Authentication support
#

from __future__ import annotations

import json
import os
import threading
import time
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    import requests

    from .session import Asession, Session


class AuthFailedException(Exception):
    pass


class AuthType(StrEnum):
    """
    Supported values for ``auth_type``.
    """

    OAUTH = 'OAuth'
    VOLUE_IDENTITY = 'VolueIdentity'


class OAuth:
    """
    Authentication based on OAuth client ID.
    This is the main authentication mechanism for customer access to the data center.
    """

    auth_type: AuthType = AuthType.OAUTH
    token_path: str = '/oauth2/token'
    scope: str | None = None
    mismatch_hint: str = ('Check that the client ID and secret are your Volue Data & Forecasts credentials. '
                     'If they are Volue Identity credentials, set auth_type = VolueIdentity.')

    def __init__(self, session:Session|Asession, client_id:str, client_secret:str, auth_urlbase:str)->None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_urlbase = auth_urlbase
        self.token:str|None = None
        self.token_type:str|None = None
        self.valid_until:float|None = None
        self.session = session
        self._lock = threading.Lock()
        self._authenticate()

    def token_expired(self)->bool:
        """True when there is no token yet, or it is past valid_until"""
        return (not self.valid_until) or time.time() > self.valid_until

    def validate_auth(self)->None:
        """Check valid_until and fetch new token if needed"""
        # To avoid sending duplicated authentication requests in other threads
        with self._lock:
            if self.token_expired():
                self._authenticate()

    def _authenticate(self)->None:
        # Wipe out any old values before (re-)login
        self.token = None
        self.token_type = None
        self.valid_until = None
        now = time.time()
        url = urljoin(self.auth_urlbase, self.token_path)
        auth = (self.client_id, self.client_secret)
        data = {'grant_type': 'client_credentials'}
        if self.scope is not None:
            data['scope'] = self.scope
        response = self.session.send_data_request('POST', self.auth_urlbase, url, rawdata=data, authval=auth)
        if response is None:
            raise AuthFailedException('Authentication failed: No response received')
        if response.status_code != 200:
            raise AuthFailedException(self._failure_message(url, response))
        # Parse token
        rsp = json.loads(response.content.decode())
        self.token = rsp['access_token']
        self.token_type = rsp['token_type']
        self.valid_until = now + int(rsp['expires_in'] * 0.95)

    def _failure_message(self, url:str, response:requests.Response)->str:
        """Explain a failed token request, so it is not mistaken for an API error"""
        message = f'Authentication failed ({self.auth_type}) when requesting a token from {url}'
        error = None
        try:
            body = json.loads(response.content.decode())
            error = body.get('error')
            message += f': {error}'
            if body.get('error_description'):
                message += f' ({body["error_description"]})'
        except (ValueError, AttributeError):
            message += f': HTTP {response.status_code} {response.content!r}'
        if error == 'invalid_client':
            message += '. ' + self.mismatch_hint + ' The credentials may also have been revoked or have expired.'
        elif error == 'invalid_scope':
            message += '. The client is not allowed to access the Volue Data & Forecasts API. Contact support.'
        elif response.status_code == 404:
            message += '. Check that auth_type and auth_urlbase belong together.'
        return message

    def get_headers(self, data:object=None)->dict[str,str]:
        """The web-token auth header is simple"""
        headers = {}
        if self.token is not None and self.token_type is not None:
            headers['Authorization'] = f'{self.token_type} {self.token}'

        wapi_request_source = os.getenv('REPORTED_DATA_SOURCE')
        if wapi_request_source:
            headers['X-Model-Name'] = wapi_request_source

        return headers


class VolueIdentity(OAuth):
    """
    Authentication with a Volue Identity client ID (client credentials).
    """

    auth_type = AuthType.VOLUE_IDENTITY
    token_path = '/connect/token'
    scope = 'in.dnf:api'
    mismatch_hint = ('Check that the client ID and secret are your Volue Identity credentials. '
                     'If they are your existing Volue Data & Forecasts credentials, set auth_type = OAuth.')


AUTH_URLBASE = 'https://auth.volueinsight.com'
VOLUE_IDENTITY_AUTH_URLBASE = 'https://auth.identity.volue.com'

DEFAULT_AUTH_URLBASES: dict[AuthType, str] = {
    AuthType.OAUTH: AUTH_URLBASE,
    AuthType.VOLUE_IDENTITY: VOLUE_IDENTITY_AUTH_URLBASE,
}
