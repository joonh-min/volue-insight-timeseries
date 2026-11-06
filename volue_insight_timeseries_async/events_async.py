"""Curve events for Asession, read from the server-sent event stream with aiohttp."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Self

from . import curves_async, util
from .events import AnyEvent, CurveEvent, DefaultEvent, EventTimeout, ServerSentEvent

if TYPE_CHECKING:
    from types import TracebackType

    import aiohttp

    from .models import DatetimeLike
    from .session_async import Asession


class AsyncEventListener:
    """Async version of events.EventListener, use it with `async for event in listener`.

    Like the sync listener, it yields an EventTimeout when no event arrives within `timeout` seconds,
    and it reconnects after the server closes the stream. An error is raised from the loop.
    """

    def __init__(
        self,
        session: Asession,
        curve_list: curves_async.BaseCurve | int | Iterable[curves_async.BaseCurve | int],
        start_time: DatetimeLike | None = None,
        timeout: float | None = None,
    ) -> None:
        self.curve_cache: dict[int, curves_async.BaseCurve] = {}
        ids: list[int] = []
        items = curve_list if isinstance(curve_list, Iterable) and not isinstance(curve_list, str) else [curve_list]
        for curve in items:
            if isinstance(curve, curves_async.BaseCurve):
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
        self.retry = 3000  # Wait before reconnecting, in milliseconds, the server can change it
        self._closed = False

    def __aiter__(self) -> AsyncIterator[AnyEvent | EventTimeout]:
        return self._events()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop after the event that is being read now"""
        self._closed = True

    async def _events(self) -> AsyncIterator[AnyEvent | EventTimeout]:
        while not self._closed:
            async with self.session.stream(self.url) as response:
                async for item in self._read(response):
                    yield item
                    if self._closed:
                        return
            # The server closed the stream, wait before reconnecting like the sync listener
            await asyncio.sleep(self.retry / 1000.0)

    async def _read(self, response: aiohttp.ClientResponse) -> AsyncIterator[AnyEvent | EventTimeout]:
        event_type, event_id = 'message', None
        data_lines: list[str] = []
        while True:
            try:
                raw = await asyncio.wait_for(response.content.readline(), self.timeout)
            except TimeoutError:
                yield EventTimeout()
                continue
            if not raw:
                return
            line = raw.decode('utf-8').rstrip('\r\n')
            if not line:
                # A blank line ends an event
                if data_lines:
                    sse = ServerSentEvent(event=event_type, data='\n'.join(data_lines), id=event_id)
                    event = CurveEvent(sse) if sse.event == 'curve_event' else DefaultEvent(sse)
                    if isinstance(event, CurveEvent) and event.id in self.curve_cache:
                        event.curve = self.curve_cache[event.id]
                    yield event
                event_type, event_id = 'message', None
                data_lines = []
                continue
            if line.startswith(':'):
                continue
            field, _, value = line.partition(':')
            value = value.removeprefix(' ')
            if field == 'event':
                event_type = value
            elif field == 'data':
                data_lines.append(value)
            elif field == 'id':
                event_id = value
            elif field == 'retry' and value.isdigit():
                self.retry = int(value)
