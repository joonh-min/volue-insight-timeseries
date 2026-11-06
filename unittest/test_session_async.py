import asyncio
import json
from types import TracebackType
from typing import Self
from unittest.mock import MagicMock

import aiohttp
import pytest
import requests_mock

import volue_insight_timeseries_async as vit
from volue_insight_timeseries_async.curves_async import InstanceCurve
from volue_insight_timeseries_async.models import JSONValue
from volue_insight_timeseries_async.session_async import Asession, MetadataException


class FakeResponse:
    def __init__(self, status: int, body: bytes | JSONValue) -> None:
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        return False


class FakeAiohttpSession:
    """Answers requests in order from a list of (status, body), and records each URL"""

    def __init__(self, *responses: tuple[int, bytes | JSONValue] | Exception) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.urls.append(url)
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(*answer)


def make_asession(
    monkeypatch: pytest.MonkeyPatch, *responses: tuple[int, bytes | JSONValue] | Exception, retries: int = 4
) -> tuple[Asession, FakeAiohttpSession]:
    a = Asession(urlbase='https://data-forecasts.volue.com', retries=retries)
    monkeypatch.setattr(vit.session_async, 'RETRY_DELAY', 0)
    fake = FakeAiohttpSession(*responses)
    # The fake only has the part of aiohttp.ClientSession that data_request uses
    monkeypatch.setattr(a, '_asession', fake)
    return a, fake


METADATA: dict[str, JSONValue] = {'id': 5, 'name': 'curve', 'frequency': 'H', 'time_zone': 'CET', 'curve_type': 'INSTANCES'}


def test_data_request_returns_json_and_none_for_204(monkeypatch: pytest.MonkeyPatch) -> None:
    a, _fake = make_asession(monkeypatch, (200, [1, 2]), (204, b''))
    assert asyncio.run(a.data_request('GET', a.urlbase, '/x')) == [1, 2]
    assert asyncio.run(a.data_request('GET', a.urlbase, '/x')) is None


def test_data_request_raises_with_status_and_body_once(monkeypatch: pytest.MonkeyPatch) -> None:
    a, fake = make_asession(monkeypatch, (403, b'You do not have access to this'))
    with pytest.raises(vit.util.ResponseError) as err:
        asyncio.run(a.data_request('GET', a.urlbase, '/x'))
    assert err.value.status == 403
    assert err.value.body == 'You do not have access to this'
    assert len(fake.urls) == 1


def test_curve_load_gives_none_for_404_and_raises_for_403(monkeypatch: pytest.MonkeyPatch) -> None:
    a, _fake = make_asession(monkeypatch, (200, METADATA), (404, b'not found'), (403, b'No access'))
    curve = asyncio.run(a.get_curve(name='curve'))
    assert isinstance(curve, InstanceCurve)
    assert asyncio.run(curve.get_latest()) is None
    with pytest.raises(vit.util.CurveException, match=r'No access \(403\)'):
        asyncio.run(curve.get_latest())


def test_get_curve_and_attribute_errors_keep_server_message(monkeypatch: pytest.MonkeyPatch) -> None:
    a, _fake = make_asession(monkeypatch, (404, b'Curve not found'), (403, b'boom'), (204, b''))
    with pytest.raises(MetadataException, match='Failed to load curve: Curve not found'):
        asyncio.run(a.get_curve(name='missing'))
    with pytest.raises(MetadataException, match='Failed loading units: boom'):
        asyncio.run(a.get_attribute('units'))
    assert asyncio.run(a.get_attribute('units')) is None


def test_async_urls_use_current_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    a, fake = make_asession(
        monkeypatch,
        (200, METADATA),
        (200, {'frequency': 'H', 'points': []}),
        (200, [{'key': 'MW', 'name': 'MW'}]),
    )
    curve = asyncio.run(a.get_curve(name='curve'))
    assert isinstance(curve, InstanceCurve)
    asyncio.run(curve.get_instance('2024-01-01'))
    asyncio.run(a.get_attribute('time_zones'))
    base = 'https://data-forecasts.volue.com/api/v1/timeseries/'
    assert fake.urls[0] == base + 'curves/get?name=curve'
    assert fake.urls[1].startswith(base + 'instances/5/at?')
    assert fake.urls[2] == base + 'curve-attributes/time-zones'


def test_retries_408_5xx_and_connection_errors_like_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    a, fake = make_asession(monkeypatch, (503, b'busy'), aiohttp.ServerDisconnectedError(), (408, b''), (200, [1]))
    assert asyncio.run(a.data_request('GET', a.urlbase, '/x')) == [1]
    assert len(fake.urls) == 4


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429])
def test_no_retry_for_other_errors(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    a, fake = make_asession(monkeypatch, (status, b'no'))
    with pytest.raises(vit.util.ResponseError):
        asyncio.run(a.data_request('GET', a.urlbase, '/x'))
    assert len(fake.urls) == 1


def test_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    a, fake = make_asession(monkeypatch, (500, b'a'), (500, b'b'), (500, b'c'), retries=2)
    with pytest.raises(vit.util.ResponseError, match='HTTP 500: c'):
        asyncio.run(a.data_request('GET', a.urlbase, '/x'))
    assert len(fake.urls) == 3


def test_ssl_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    error = aiohttp.ClientSSLError(MagicMock(), OSError('bad certificate'))
    a, fake = make_asession(monkeypatch, error)
    with pytest.raises(aiohttp.ClientSSLError):
        asyncio.run(a.data_request('GET', a.urlbase, '/x'))
    assert len(fake.urls) == 1


def test_session_retries_reach_async_client() -> None:
    token = json.dumps({'token_type': 'Bearer', 'access_token': 'secrettoken', 'expires_in': 3600})
    with requests_mock.Mocker() as m:
        m.post('https://auth.volueinsight.com/oauth2/token', text=token)
        s = vit.Session(client_id='id', client_secret='secret', retries=0)

    async def enter() -> Asession:
        async with s as a:
            return a

    assert asyncio.run(enter()).retries == 0
