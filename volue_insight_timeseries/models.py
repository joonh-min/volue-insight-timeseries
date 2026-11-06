"""
Type aliases and TypedDict models for the data sent by the API.

Nothing here is checked at run time, these types are only for type checkers and editors.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, TypeGuard

if TYPE_CHECKING:
    import pandas as pd

# One value, or a list of values (the API takes both)
type OneOrMany[T] = T | Sequence[T]

type DatetimeLike = str | pd.Timestamp | datetime.datetime | datetime.date

TsFreq = Literal["Y", "S", "Q", "M", "W", "H12", "H6", "H3", "H", "MIN30", "MIN15", "MIN5", "MIN", "D"]

# Curve types the API sends in the curve metadata
CurveType = Literal["TIME_SERIES", "TAGGED", "INSTANCES", "TAGGED_INSTANCES"]

# Curve types a TS object can have, a paired list curve is a tagged instance curve in the metadata
TSCurveType = Literal["TIME_SERIES", "TAGGED", "INSTANCES", "TAGGED_INSTANCES", "PAIRED_LIST"]

# The `| str` keeps values the API adds later valid, the Literal part gives editor completion
FilterType = Literal["PEAK", "OFFPEAK", "OFFPEAK1", "OFFPEAK2", "FUTUREPEAK", "FUTUREOFFPEAK", "WORKDAYS", "WEEKENDS"] | str
FunctionType = Literal["AVERAGE", "SAVERAGE", "MAX", "MIN", "SUM", "SSUM", "LAST", "SAME", "DIVIDE"] | str

# ISO 8601 duration, for example "P1D" or "PT6H"
Duration = str

# Tags given as a list, a single tag is a plain str
TagList = list[str] | tuple[str, ...]

AttributeName = Literal[
    "commodities", "categories", "areas", "stations", "sources", "scenarios", "units", "time_zones",
    "versions", "frequencies", "data_types", "curve_states", "curve_types", "functions", "filters",
]

# Body of a request, bytes are sent as they are and a mapping is sent as form data
RawData = bytes | Mapping[str, str]

# Any value that json.dumps can write
type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None

# A value util.make_arg can put in a URL query, a list gives the key once per item
type ArgValue = str | int | float | datetime.date | Iterable[ArgValue]

# One point is [timestamp in milliseconds since epoch, value], the value is None when it is missing.
# Points of a paired list curve have one value per tag instead of one value.
Point = list[float | None]


class DateRange(TypedDict):
    """A time range, `begin` and `end` are date strings or None, an empty range only has `empty`"""

    begin: NotRequired[str | None]
    end: NotRequired[str | None]
    empty: NotRequired[bool]


class AttributeValue(TypedDict):
    """One valid value of a curve attribute, as sent by the curve attribute endpoints"""

    key: str
    name: str
    description: NotRequired[str]


class CurveMetadata(TypedDict):
    """Metadata of a curve, as sent by the curve get and search endpoints"""

    id: int
    name: str
    frequency: TsFreq
    time_zone: str
    curve_type: CurveType
    curve_state: NotRequired[Literal["PUBLIC"] | str]
    created: NotRequired[str]
    modified: NotRequired[str]
    issue_frequency: NotRequired[str]
    area: NotRequired[str]
    categories: NotRequired[list[str]]
    commodity: NotRequired[str]
    unit: NotRequired[str]
    station: NotRequired[str]
    sources: NotRequired[list[str]]
    scenarios: NotRequired[list[str]]
    hasAccess: NotRequired[bool]
    accessRange: NotRequired[DateRange]
    data_type: NotRequired[str]
    description: NotRequired[str]


class TSPayload(TypedDict):
    """One time series, as sent by the series and instance endpoints"""

    frequency: TsFreq
    time_zone: NotRequired[str]
    id: NotRequired[int]
    name: NotRequired[str]
    tag: NotRequired[str]
    issue_date: NotRequired[str]
    created: NotRequired[str]
    modified: NotRequired[str]
    curve_type: NotRequired[TSCurveType]
    points: NotRequired[list[Point]]  # missing when the instances are listed without data


class PairedTSPayload(TSPayload):
    """One instance of a paired list curve, `tags` names the values of every point"""

    tags: list[str]


class CurveEventPayload(TypedDict):
    """The data of a `curve_event` server-sent event"""

    id: int
    created: str
    operation: str
    tag: NotRequired[str]
    issue_date: NotRequired[str]
    range: NotRequired[DateRange]


# Checks for the JSON the API sends, so a payload gets its TypedDict without a cast.
# They only check the outer shape that the code relies on, not every key.


def is_ts_payload(value: JSONValue) -> TypeGuard[TSPayload]:
    return isinstance(value, dict) and "frequency" in value


def is_ts_payload_list(value: JSONValue) -> TypeGuard[list[TSPayload]]:
    return isinstance(value, list) and all(is_ts_payload(item) for item in value)


def is_str_list(value: JSONValue) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def is_date_range_list(value: JSONValue) -> TypeGuard[list[DateRange]]:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def is_curve_metadata(value: JSONValue) -> TypeGuard[CurveMetadata]:
    return isinstance(value, dict) and all(key in value for key in ("id", "name", "frequency", "time_zone", "curve_type"))


def is_attribute_values(value: JSONValue) -> TypeGuard[list[AttributeValue]]:
    return isinstance(value, list) and all(isinstance(item, dict) and "key" in item and "name" in item for item in value)


def is_curve_event_payload(value: JSONValue) -> TypeGuard[CurveEventPayload]:
    return isinstance(value, dict) and all(key in value for key in ("id", "created", "operation"))


def is_curve_metadata_list(value: JSONValue) -> TypeGuard[list[CurveMetadata]]:
    return isinstance(value, list) and all(is_curve_metadata(item) for item in value)
