"""Retrying requests to the Volue Data & Forecasts API.

A urllib3 ``Retry`` decides which failures are retried, how often and how long
to wait.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, overload, override

import requests
from urllib3.exceptions import MaxRetryError
from urllib3.response import HTTPResponse
from urllib3.util import Retry

if TYPE_CHECKING:
    from .models import RawData


class _FixedDelayRetry(Retry):
    """Retry that waits the same delay, in seconds, before every attempt, with no backoff"""

    # Set after construction, because Retry.new() builds copies with only urllib3's own settings
    delay: float = 0

    @override
    def new(self, **kw: object) -> Self:
        # Retry.new passes on only urllib3's own settings
        retry = super().new(**kw)
        retry.delay = self.delay
        return retry

    @override
    def get_backoff_time(self) -> float:
        return self.delay


@overload
def make_retry(retries: int | Retry, delay: float) -> Retry: ...


@overload
def make_retry(retries: None, delay: float) -> None: ...


@overload
def make_retry(retries: int | Retry | None, delay: float) -> Retry | None: ...


def make_retry(retries: int | Retry | None, delay: float) -> Retry | None:
    """Turn a ``retries`` argument into a urllib3 Retry, or None when not given.

    An int gets delay seconds between attempts.
    """
    if retries is None or isinstance(retries, Retry):
        return retries
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise TypeError(f'retries must be an int or a urllib3 Retry, not {type(retries).__name__}')
    if retries < 0:
        raise ValueError(f'retries must be 0 or more, not {retries}')
    # Retried statuses: 408 and every 5xx
    retry = _FixedDelayRetry(total=retries, status_forcelist={408, *range(500, 600)}, allowed_methods=None,
                             raise_on_status=False, respect_retry_after_header=False)
    retry.delay = delay
    return retry


def _urllib3_error(error: requests.exceptions.RequestException) -> Exception:
    """The urllib3 error inside a requests exception, so Retry can tell connect and read errors apart"""
    cause = error.args[0] if error.args else None
    if isinstance(cause, MaxRetryError):
        cause = cause.reason
    return cause if isinstance(cause, Exception) else error


def request_with_retries(
    http_session: requests.Session,
    retry_state: Retry,
    method: str,
    url: str,
    *,
    data: RawData | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    stream: bool = False,
    timeout: float | None = None,
) -> requests.Response:
    """Send a request through http_session, retrying as retry_state allows, and return the response.

    data, headers, auth, stream and timeout are passed to http_session.request for every attempt. When retries run
    out, the last response is returned, or RetryError is raised if retry_state has
    raise_on_status. A Timeout or ConnectionError that is not retried is raised.
    SSLError and ProxyError are raised at once: they come from configuration, which
    a retry does not fix.
    """
    while True:
        try:
            res = http_session.request(method=method, url=url, data=data, headers=headers, auth=auth, stream=stream, timeout=timeout)
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError):
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            try:
                retry_state = retry_state.increment(method, url, error=_urllib3_error(e))
            except Exception:
                # Out of retries, or not retried (such as a read error for a method
                # outside allowed_methods): raise the requests exception, not urllib3's
                raise e from None
            retry_state.sleep()
            continue
        # Response headers are read only for a Retry that honours Retry-After
        response_headers = res.headers if retry_state.respect_retry_after_header else None
        if not retry_state.is_retry(method, res.status_code, response_headers is not None and 'Retry-After' in response_headers):
            return res
        response = HTTPResponse(status=res.status_code, headers=response_headers)
        try:
            retry_state = retry_state.increment(method, url, response=response)
        except MaxRetryError as e:
            if retry_state.raise_on_status:
                raise requests.exceptions.RetryError(e, request=res.request) from None
            return res
        retry_state.sleep(response)
