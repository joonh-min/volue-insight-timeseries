#
# Authentication support
#

from __future__ import annotations

import json
import os
import threading

try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    from .session import Asession, Session


class AuthFailedException(Exception):
    pass


class OAuth:
    """
    Authentication based on OAuth client ID.
    This is the main authentication mechanism for customer access to the data center.
    """

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
    def validate_auth(self)->None:
        """Check valid_until and fetch new token if needed"""
        # To avoid sending duplicated authentication requests in other threads
        with self._lock:
            if (not self.valid_until) or time.time() > self.valid_until:
                self._authenticate()

    def _authenticate(self)->None:
        # Wipe out any old values before (re-)login
        self.token = None
        self.token_type = None
        self.valid_until = None
        now = time.time()
        url = urljoin(self.auth_urlbase, '/oauth2/token')
        auth = (self.client_id, self.client_secret)
        data = {'grant_type': 'client_credentials'}
        response = self.session.send_data_request('POST', self.auth_urlbase, url, rawdata=data, authval=auth)
        if response is None:
            raise AuthFailedException('Authentication failed: No response received')
        if response.status_code != 200:
            raise AuthFailedException(f'Authentication failed: {response.content}')
        # Parse token
        rsp = json.loads(response.content.decode())
        self.token = rsp['access_token']
        self.token_type = rsp['token_type']
        self.valid_until = now + int(rsp['expires_in'] * 0.95)

    def get_headers(self)->dict[str,str]:
        """The web-token auth header is simple"""
        headers = {}
        if self.token is not None and self.token_type is not None:
            headers['Authorization'] = f'{self.token_type} {self.token}'

        wapi_request_source = os.getenv('WAPI_REQUEST_SOURCE')
        if wapi_request_source:
            headers['X-Request-Source'] = wapi_request_source

        return headers
