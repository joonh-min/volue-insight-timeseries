from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Self

import sseclient

from . import curves, util
from .models import DatetimeLike, JSONValue, is_curve_event_payload

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from .session import Session


class EventListener:
    def __init__(
        self,
        session: Session,
        curve_list: curves.BaseCurve | int | Iterable[curves.BaseCurve | int],
        start_time: DatetimeLike | None = None,
        timeout: float | None = None,
    ) -> None:
        self.curve_cache: dict[int, curves.BaseCurve] = {}
        ids: list[int] = []
        items = curve_list if isinstance(curve_list, Iterable) and not isinstance(curve_list, str) else [curve_list]
        for curve in items:
            if isinstance(curve, curves.BaseCurve):
                ids.append(curve.id)
                self.curve_cache[curve.id] = curve
            else:
                ids.append(curve)
        args = [util.make_arg('id', ids)]
        if start_time is not None:
            args.append(util.make_arg('start_time', start_time))
        self.url: str = '/api/v1/timeseries/events?{}'.format('&'.join(args))
        self.session = session
        self.timeout = timeout
        self.retry = 3000 # Retry time in milliseconds
        self.client: sseclient.SSEClient | None = None
        self.queue: queue.Queue[AnyEvent | EventError] = queue.Queue()
        self.do_shutdown = False
        self.worker = threading.Thread(target=self.fetch_events)
        self.worker.daemon = True
        self.worker.start()

    def get(self) -> AnyEvent | EventTimeout:
        try:
            val = self.queue.get(timeout=self.timeout)
            if isinstance(val, EventError):
                raise val.exception
            return val
        except queue.Empty:
            return EventTimeout()

    def fetch_events(self) -> None:
        while not self.do_shutdown:
            try:
                with self.session.data_request("GET", self.session.urlbase, self.url, stream=True) as stream:
                    self.client = sseclient.SSEClient(stream)
                    for sse_event in self.client.events():
                        event: AnyEvent = CurveEvent(sse_event) if sse_event.event == 'curve_event' else DefaultEvent(sse_event)
                        if isinstance(event, CurveEvent) and event.id in self.curve_cache:
                            event.curve = self.curve_cache[event.id]
                        self.queue.put(event)
                        if sse_event.retry is not None:
                            with contextlib.suppress(ValueError, TypeError):
                                self.retry = int(sse_event.retry)
                        if self.do_shutdown:
                            break
                    # Session was closed by server/network, wait for retry before looping.
                    time.sleep(self.retry / 1000.0)
            except Exception as e:
                self.queue.put(EventError(e))
                break

    def close(self, timeout: float = 1) -> None:
        self.do_shutdown = True
        if self.client is not None:
            self.client.close()
        self.worker.join(timeout)

    def __iter__(self) -> EventListener:
        return self

    def __next__(self) -> AnyEvent | EventTimeout:
        return self.get()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


class EventError:
    def __init__(self, exception: BaseException) -> None:
        self.exception = exception

    def __str__(self) -> str:
        return f"{self.exception}"


class EventTimeout:
    """Returned on timeout, etc."""


class DefaultEvent:
    json_data: JSONValue  # parsed data of the event, None when it is not JSON

    def __init__(self, sse_event: sseclient.Event) -> None:
        self._raw_event = sse_event
        try:
            self.json_data = json.loads(sse_event.data)
        except (json.JSONDecodeError, TypeError):
            self.json_data = None


class CurveEvent(DefaultEvent):
    id: int
    curve: curves.BaseCurve | None
    created: datetime.datetime
    operation: str
    tag: str | None
    issue_date: datetime.datetime | None
    range: tuple[datetime.datetime | None, datetime.datetime | None] | None

    def __init__(self, sse_event: sseclient.Event) -> None:
        super().__init__(sse_event)
        payload = self.json_data
        if not is_curve_event_payload(payload):
            raise ValueError(f'curve_event without id, created and operation: {sse_event.data!r}')
        self.id = payload['id']
        self.curve = None
        self.created = util.parsetime(payload['created'])
        self.operation = payload['operation']
        self.tag = None
        self.issue_date = None
        self.range = None
        if 'tag' in payload:
            self.tag = payload['tag']
        if 'issue_date' in payload:
            self.issue_date = util.parsetime(payload['issue_date'])
        if 'range' in payload:
            self.range = util.parserange(payload['range'])

AnyEvent = CurveEvent | DefaultEvent
