#
# Various utility and conversion functions to make it easier to work with
# the data from the backend
#
from __future__ import annotations

import calendar
import datetime
import re
import warnings
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final, Literal, TypeGuard, override
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
from zoneinfo._common import ZoneInfoNotFoundError

import dateutil.parser

from .models import ArgValue, DateRange, DatetimeLike, PairedTSPayload, Point, TSCurveType, TsFreq, TSPayload

if TYPE_CHECKING:
    from types import ModuleType

    import pandas as pd

_MIN_PANDAS_VERSION = (1, 5)


def _require_pandas() -> ModuleType:
    # Pandas is an optional extra. Import it only when a pandas function runs.
    # Pip keeps a pandas that is already installed, so check the version here.
    # If pandas is missing or too old, raise ImportError with the install
    # command. An unknown version format skips the check.
    try:
        import pandas
    except ImportError as e:
        raise ImportError(
            "This function needs pandas. Install it with: "
            "pip install 'volue-insight-timeseries-async[pandas]'"
        ) from e
    match = re.match(r"(\d+)\.(\d+)", getattr(pandas, "__version__", ""))
    if match and tuple(map(int, match.groups())) < _MIN_PANDAS_VERSION:
        raise ImportError(
            "This function needs pandas>=1.5, but pandas "
            f"{pandas.__version__} is installed. Upgrade it with: "
            "pip install -U 'volue-insight-timeseries-async[pandas]'"
        )
    return pandas


# Curve types
TIME_SERIES: Final = "TIME_SERIES"
TAGGED: Final = "TAGGED"
INSTANCES: Final = "INSTANCES"
TAGGED_INSTANCES: Final = "TAGGED_INSTANCES"
PAIRED_LIST: Final = "PAIRED_LIST"

_TsFreqs = TsFreq

# Frequency mapping from TS to Pandas
_TS_FREQ_TABLE: dict[TsFreq, str] = {
    "Y": "YS",
    "S": "2QS",
    "Q": "QS",
    "M": "MS",
    "W": "W-MON",
    "H12": "12h",
    "H6": "6h",
    "H3": "3h",
    "H": "h",
    "MIN30": "30min",
    "MIN15": "15min",
    "MIN5": "5min",
    "MIN": "min",
    "D": "D",
}

# Mapping from various versions of Pandas to TS is built from map above,
# with some additions to support older versions of pandas
_PANDAS_FREQ_TABLE:dict[str,_TsFreqs] = {
    "YS-JAN": "Y",
    "AS-JAN": "Y",
    "AS": "Y",
    "2QS-JAN": "S",
    "QS-JAN": "Q",
    "12H": "H12",
    "6H": "H6",
    "3H": "H3",
    "30T": "MIN30",
    "15T": "MIN15",
    "5T": "MIN5",
    "T": "MIN",
}
for ts_freq, pandas_freq in _TS_FREQ_TABLE.items():
    _PANDAS_FREQ_TABLE[pandas_freq.upper()] = ts_freq

def _is_ts_freq(value: str) -> TypeGuard[TsFreq]:
    return value in _TS_FREQ_TABLE


# Kept for backward compatibility, use TSPayload
InputDict = TSPayload


class CurveException(Exception):
    pass

class MethodNotApplicable(Exception):
    """
    Exception raised when a method is not applicable for a specific curve type.
    """


class TS:
    """
    A class to hold a basic time series.
    """

    id: int | None
    name: str | None
    # A TS made from pandas keeps a pandas frequency that has no TS name
    frequency: TsFreq | str
    time_zone: str | None
    tz: ZoneInfo
    tag: str | None
    issue_date: DatetimeLike | None
    curve_type: TSCurveType
    points: list[Point] | None
    created: str | None = None
    modified: str | None = None

    def __init__(
        self,
        id: int|None = None,
        name:str|None=None,
        frequency:TsFreq|str|None=None,
        time_zone:str|None=None,
        tag:str|None=None,
        issue_date:DatetimeLike|None=None,
        curve_type:TSCurveType|None=None,
        points:list[Point]|None=None,
        input_dict:TSPayload|PairedTSPayload|None=None,
    )->None:
        self.id = id
        self.name = name
        self.time_zone = time_zone
        self.tag = tag
        self.issue_date = issue_date
        self.points = points

        # input_dict is the json dict from the API, its values win over the arguments
        if input_dict is not None:
            frequency = input_dict.get("frequency", frequency)
            curve_type = input_dict.get("curve_type", curve_type)
            for k, v in input_dict.items():
                setattr(self, k, v)

        if self.time_zone is not None:
            self.tz = parse_tz(self.time_zone)
        else:
            self.tz = ZoneInfo("CET")

        self.curve_type = curve_type if curve_type is not None else detect_curve_type(self.issue_date, self.tag)
        # Validation
        if frequency is None:
            raise CurveException("TS must have frequency")
        self.frequency = frequency

    def __str__(self)->str:
        size = f" size: {len(self.points)}" if self.points else ""
        return f"TS: {self.fullname}{size}"

    def __repr__(self)->str:
        name = self.name if self.name is not None else str(self.id)
        attrs = [
            f"id={self.id}" if self.id is not None else "",
            f"tag={self.tag}" if self.tag is not None else "",
            f"issue_date={self.issue_date}" if self.issue_date is not None else "",
            f"size={len(self.points)}" if self.points else ""
        ]
        attrs_str = ", ".join(filter(None, attrs))
        return f"TS(name={name}{', ' + attrs_str if attrs_str else ''})"

    def __lt__(self, other: TS)->bool:
        """
        Compare two TS objects for sorting.

        Order is: issue_date, id, name, tag, then object id.
        """
        def keys(ts: TS) -> tuple[str, float, str, str, int]:
            return (
                (
                    str(ts.issue_date) if ts.issue_date is not None else "",
                    ts.id if ts.id is not None else float("inf"),
                    ts.name if ts.name is not None else "",
                    ts.tag if ts.tag is not None else "",
                    id(ts),
                )
            )
        return keys(self) < keys(other)

    def __len__(self)->int:
        """
        Returns the number of points in the time series.
        """
        if self.points is None:
            return 0
        return len(self.points)

    @property
    def fullname(self)->str:
        attrs: list[str] = []
        if self.name:
            attrs.append(self.name)
        else:
            if self.id:
                attrs.append(str(self.id))
            attrs.extend([self.curve_type, str(self.tz), self.frequency])
        if self.tag:
            attrs.append(self.tag)
        if self.issue_date:
            attrs.append(str(self.issue_date))
        return " ".join(attrs)

    def to_pandas(self, name:str|None=None)->pd.Series[float]:
        """Converting :class:`volue_insight_timeseries_async.util.TS` object
        to a pandas.Series object

        Parameters
        ----------
        name: str, optional
            Name of the returned pandas.Series object. If not given the name
            of the curve will be used.
        Returns
        -------
        pandas.Series
        """
        pd = _require_pandas()
        if name is None:
            name = self.fullname
        if self.points is None or len(self.points) == 0:
            return pd.Series(name=name, dtype="float64", index=pd.DatetimeIndex([], tz=self.tz))

        index = []
        values = []
        for row in self.points:
            timestamp = row[0]
            if len(row) != 2 or timestamp is None:
                raise ValueError("Points have unexpected contents")
            dt = datetime.datetime.fromtimestamp(timestamp / 1000.0, self.tz)
            index.append(dt)
            values.append(row[1])
        res = pd.Series(name=name, index=index, data=values)
        mapped_freq = res.asfreq(self._map_freq(self.frequency))
        dropped = mapped_freq.dropna()

        # Warn about edge case for Gas Day timezone during DST changes.
        # Gas Day is a 24-hour period starting at 4:00 UTC in summer and 5:00 UTC in winter,
        # and finishing at 4:00 UTC (or 5:00) the next day. corresponding to 6:00 local time in Germany year-round.
        # A gas loader bug causes timestamps on the day after DST to shift to 5:00 or 7:00 instead of 6:00.
        number_of_nan = sum(1 for point in self.points if point[1] is None)
        if len(self.points) -  number_of_nan  != len(dropped):
            warnings.warn(
                f"Data length mismatch: original data length is {len(self.points)}, but mapped frequency data length is "
                f"{len(dropped)}. This may indicate data truncation.",
                RuntimeWarning,
                stacklevel=2,
            )
        return mapped_freq

    @staticmethod
    def from_pandas(pd_series:pd.Series[float])->TS:
        pd = _require_pandas()
        import numpy as np

        # Clean up some of the more common Pandas/api problems
        pd_series = pd_series.astype(np.float64).replace(np.nan, None)

        if not isinstance(pd_series.index, pd.DatetimeIndex):
            raise ValueError("Input series index must be a DatetimeIndex")

        name = pd_series.name
        frequency = TS._rev_map_freq(pd_series.index.freqstr)

        points: list[Point] = []
        for i in pd_series.index:
            t = i.astimezone(ZoneInfo("UTC"))
            timestamp = calendar.timegm(t.timetuple()) * 1000
            points.append([timestamp, pd_series[i]])

        if name is not None and isinstance(name, str) and name.isnumeric():
            return TS(id=int(name), frequency=frequency, points=points)
        return TS(name=None if name is None else str(name), frequency=frequency, points=points)

    @staticmethod
    def _map_freq(frequency:TsFreq|str) -> str:
        upper = frequency.upper()
        return _TS_FREQ_TABLE[upper] if _is_ts_freq(upper) else frequency

    @staticmethod
    def _rev_map_freq(frequency:str)->TsFreq|str:
        if frequency.upper() in _PANDAS_FREQ_TABLE:
            return _PANDAS_FREQ_TABLE[frequency.upper()]
        warnings.warn(f"Frequency is not supported: '{frequency}'", FutureWarning, stacklevel=2)
        return frequency

    @staticmethod
    def sum(ts_list:list[TS], name:str)->TS:
        """calculate the sum of a given list
        of :class:`volue_insight_timeseries_async.util.TS` objects

        Returns a :class:`~volue_insight_timeseries_async.util.TS`
        (:class:`volue_insight_timeseries_async.util.TS`) object that is
        the sum of a list of TS objects with the given name.

        Parameters
        ----------
        ts_list: list
            list of TS objects
        name: str
            Name of the returned TS object.
        Returns
        -------
        :class:`volue_insight_timeseries_async.util.TS` object
        """
        df = _ts_list_to_dataframe(ts_list)
        return _generated_series_to_TS(df.sum(axis=1), name)

    @staticmethod
    def mean(ts_list:list[TS], name:str)->TS:
        """calculate the mean of a given list of TS objects

        Returns a TS (:class:`volue_insight_timeseries_async.util.TS`) object that is
        the mean of a list of TS objects with the given name.

        Parameters
        ----------
        ts_list: list
            list of TS objects
        name: str
            Name of the returned TS object.
        Returns
        -------
        :class:`volue_insight_timeseries_async.util.TS` object
        """
        df = _ts_list_to_dataframe(ts_list)
        return _generated_series_to_TS(df.mean(axis=1), name)

    @staticmethod
    def median(ts_list:list[TS], name:str)->TS:
        """calculate the median of a given list of TS objects

        Returns a TS (:class:`volue_insight_timeseries_async.util.TS`) object that is
        the median of a list of TS objects with the given name.

        Parameters
        ----------
        ts_list: list
            list of TS objects
        name: str
            Name of the returned TS object.
        Returns
        -------
        :class:`volue_insight_timeseries_async.util.TS` object
        """
        df = _ts_list_to_dataframe(ts_list)
        return _generated_series_to_TS(df.median(axis=1), name)

class PairedTS(TS):
    """
    A class to hold paired data from a PairedListCurve, extending the standard TS object.
    Used for data like price-volume pairs, where each point is a tuple/list.
    """

    tags: list[str] | None = None

    def __init__(
        self,
        id: int|None = None,
        name:str|None=None,
        frequency:TsFreq|str|None=None,
        time_zone:str|None=None,
        tag:str|None=None,
        issue_date:DatetimeLike|None=None,
        curve_type:TSCurveType|None=None,
        points:list[Point]|None=None,
        input_dict:TSPayload|PairedTSPayload|None=None,
    )->None:
        super().__init__(id, name, frequency, time_zone, tag, issue_date, curve_type, points, input_dict)

        if not isinstance(self.tags, list) or not self.tags:
            raise ValueError(
                "PairedTS requires 'tags' as a non-empty list to label the DataFrame columns."
            )

        # Validate points structure
        if self.points:
            if not isinstance(self.points[0], (list, tuple)):
                raise ValueError("Each point in PairedTS must be a list or tuple.")

            values_per_point = len(self.points[0])
            if values_per_point != len(self.tags):
                raise ValueError(
                    f"Point structure mismatch: ({len(self.tags)}) tags provided but each point has ({values_per_point}) values."
                )


    @override
    def to_pandas(self, name: str | None = None) -> pd.DataFrame:  # type: ignore[override]  # returns a DataFrame, TS returns a Series
        """
        Convert the PairedTS object to a pandas.DataFrame.
        Rows = paired points, columns = tags.

        Returns:
            pandas.DataFrame
            A pandas.DataFrame representing the paired data, with curve metadata
            stored in the `.attrs` dictionary.
        """
        pd = _require_pandas()
        df = pd.DataFrame(self.points or [], columns=self.tags)

        # Curve Metadata in DataFrame attributes
        df.attrs["name"] = self.fullname
        df.attrs["frequency"] = self.frequency
        df.attrs["issue_date"] = self.issue_date
        df.attrs["length"] = len(self.points or [])

        return df

def _generated_series_to_TS(series:pd.Series[float], name:str)->TS:
    series.name = name
    return TS.from_pandas(series)


def _ts_list_to_dataframe(ts_list:list[TS])->pd.DataFrame:
    pd = _require_pandas()
    return pd.concat([ts.to_pandas() for ts in ts_list], axis=1)


def tags_to_DF(tagged_list:list[TS])->pd.DataFrame:
    """
    Given a list of tagged series/instances, create a DataFrame with the tag of
    each as column name
    """
    pd = _require_pandas()
    return pd.DataFrame({s.tag: s.to_pandas() for s in tagged_list})


#
# Some parsing helpers
#


def parsetime(datestr:str, tz:str|datetime.tzinfo|None=None)->datetime.datetime:
    """
    Parse the input date and optionally convert to correct time zone
    """

    d = dateutil.parser.parse(datestr)

    if tz is not None:
        if not isinstance(tz, datetime.tzinfo):
            tz = parse_tz(tz)

        d = d.astimezone(tz) if d.tzinfo is not None else d.replace(tzinfo=tz)

    # If datestr does not have tzinfo and no tz given, assume CET
    elif d.tzinfo is None:
        d = d.replace(tzinfo=ZoneInfo("CET"))
    return d


def parserange(rangeobj:DateRange, tz:str|datetime.tzinfo|None=None)->tuple[datetime.datetime|None, datetime.datetime|None]|None:
    """
    Parse a range object (a pair of date strings, which may each be None)
    """
    if rangeobj.get("empty") is True:
        return None
    begin = rangeobj.get("begin")
    end = rangeobj.get("end")
    return (
        parsetime(begin, tz=tz) if begin is not None else None,
        parsetime(end, tz=tz) if end is not None else None,
    )


_tzmap = {
    "CEGT": "CET",
    "WEGT": "WET",
    "PST": "US/Pacific",
    "TRT": "Turkey",
    "MSK": "Europe/Moscow",
    "ART": "America/Argentina/Buenos_Aires",
    "JST": "Asia/Tokyo",
}


def parse_tz(time_zone:str)->ZoneInfo:
    try:
        if time_zone in _tzmap:
            time_zone = _tzmap[time_zone]
        return ZoneInfo(time_zone)
    except ZoneInfoNotFoundError:
        warnings.warn(f"ZoneInfo `{time_zone}` is invalid, setting timezone to `CET`.", Warning, 2)
        return ZoneInfo("CET")


def detect_curve_type(issue_date:DatetimeLike|None, tag:str|None)->Literal["TIME_SERIES", "TAGGED", "INSTANCES", "TAGGED_INSTANCES"]:
    if issue_date is None and tag is None:
        return TIME_SERIES
    if issue_date is None:
        return TAGGED
    if tag is None:
        return INSTANCES
    return TAGGED_INSTANCES


def is_integer(s:str | float)->bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def make_arg(key:str, value:ArgValue)->str:
    if isinstance(value, Iterable) and not isinstance(value, str):
        return "&".join([make_arg(key, v) for v in value])

    tmp = value.isoformat() if isinstance(value, datetime.date) else str(value)
    v = quote_plus(tmp)
    return f"{key}={v}"
