#
# Various utility and conversion functions to make it easier to work with
# the data from the backend
#
from __future__ import annotations

import calendar
import datetime
import warnings
from typing import Any, Literal, TypeVar, TypedDict, Union
from urllib.parse import quote_plus

import dateutil.parser
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo
from zoneinfo._common import ZoneInfoNotFoundError

DatetimeLike = Union[str, pd.Timestamp, datetime.datetime, datetime.date]

# Curve types
TIME_SERIES = "TIME_SERIES"
TAGGED = "TAGGED"
INSTANCES = "INSTANCES"
TAGGED_INSTANCES = "TAGGED_INSTANCES"

_TsFreqs = Literal["Y", "S", "Q", "M", "W", "H12", "H6", "H3", "H", "MIN30", "MIN15", "MIN5", "MIN", "D"]

# Frequency mapping from TS to Pandas
_TS_FREQ_TABLE = {
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

class InputDict(TypedDict):
    frequency: _TsFreqs
    time_zone: str
    id: int
    name: str
    issue_date: str
    created: str
    modified: str
    points: list[list[float]]


class CurveException(Exception):
    pass


class TS:
    """
    A class to hold a basic time series.
    """

    def __init__(
        self,
        id: int|None = None,
        name:str|None=None,
        frequency:_TsFreqs|None=None,
        time_zone:str|None=None,
        tag:str|None=None,
        issue_date:str|None=None,
        curve_type:Literal["TAGGED_INSTANCES", "INSTANCES", "TAGGED", "TIME_SERIES"]|None=None,
        points:list[list[float]]|None=None,
        input_dict:InputDict|None=None,
    ):
        self.id = id
        self.name = name
        self.frequency = frequency
        self.time_zone = time_zone
        self.tag = tag
        self.issue_date = issue_date
        self.curve_type = curve_type
        self.points = points

        # input_dict is the json dict from the API
        if input_dict is not None:
            for k, v in input_dict.items():
                setattr(self, k, v)

        if self.time_zone is not None:
            self.tz = parse_tz(self.time_zone)
        else:
            self.tz = ZoneInfo("CET")

        if self.curve_type is None:
            self.curve_type = detect_curve_type(self.issue_date, self.tag)
        # Validation
        if self.frequency is None:
            raise CurveException("TS must have frequency")

    def __str__(self)->str:
        size = " size: {}".format(len(self.points)) if self.points else ""
        return "TS: {}{}".format(self.fullname, size)

    def __repr__(self) -> str:
        name = self.name if self.name is not None else str(self.id)
        attrs = [
            f"id={self.id}" if self.id is not None else "",
            f"tag={self.tag}" if self.tag is not None else "",
            f"issue_date={self.issue_date}" if self.issue_date is not None else "",
            f"size={len(self.points)}" if self.points else ""
        ]
        attrs_str = ", ".join(filter(None, attrs))
        return f"TS(name={name}{', ' + attrs_str if attrs_str else ''})"

    def __lt__(self, other: TS) -> bool:
        """
        Compare two TS objects for sorting.

        Order is: issue_date, id, name, tag, then object id.
        """
        def keys(ts):
            return (
                (
                    ts.issue_date if ts.issue_date is not None else "",
                    ts.id if ts.id is not None else float("inf"),
                    ts.name if ts.name is not None else "",
                    ts.tag if ts.tag is not None else "",
                    id(ts),
                )
            )
        return keys(self) < keys(other)

    def __len__(self) -> int:
        """
        Returns the number of points in the time series.
        """
        if self.points is None:
            return 0
        return len(self.points)

    @property
    def fullname(self)->str:
        attrs = []
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
        """Converting :class:`volue_insight_timeseries.util.TS` object
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
        if name is None:
            name = self.fullname
        if self.points is None or len(self.points) == 0:
            return pd.Series(name=name, dtype="float64", index=pd.DatetimeIndex([], tz=self.tz))

        index = []
        values = []
        for row in self.points:
            if len(row) != 2:
                raise ValueError("Points have unexpected contents")
            dt = datetime.datetime.fromtimestamp(row[0] / 1000.0, self.tz)
            index.append(dt)
            values.append(row[1])
        res = pd.Series(name=name, index=index, data=values)
        mapped_freq = res.asfreq(self._map_freq(self.frequency))
        dropped = mapped_freq.dropna()

        # Warn about edge case for Gas Day timezone during DST changes.
        # Gas Day is a 24-hour period starting at 4:00 UTC in summer and 5:00 UTC in winter,
        # and finishing at 4:00 UTC (or 5:00) the next day. corresponding to 6:00 local time in Germany year-round.
        # A gas loader bug causes timestamps on the day after DST to shift to 5:00 or 7:00 instead of 6:00.
        if len(self.points) != len(dropped):
            warnings.warn(
                f"Data length mismatch: original data length is {len(self.points)}, but mapped frequency data length is "
                f"{len(dropped)}. This may indicate data truncation.",
                RuntimeWarning,
                stacklevel=2,
            )
        return mapped_freq

    @staticmethod
    def from_pandas(pd_series:pd.Series[float])->TS:
        # Clean up some of the more common Pandas/api problems
        pd_series = pd_series.astype(np.float64).replace({np.nan: None})

        if not isinstance(pd_series.index, pd.DatetimeIndex):
            raise ValueError("Input series index must be a DatetimeIndex")

        name = pd_series.name
        frequency = TS._rev_map_freq(pd_series.index.freqstr)

        points = []
        for i in pd_series.index:
            t = i.astimezone(ZoneInfo("UTC"))
            timestamp = int(calendar.timegm(t.timetuple()) * 1000)
            points.append([timestamp, pd_series[i]])

        if name is not None and isinstance(name, str) and name.isnumeric():
            return TS(id=int(name), frequency=frequency, points=points)
        return TS(name=name, frequency=frequency, points=points)

    @staticmethod
    def _map_freq(frequency: _TsFreqs|str) -> str:
        if frequency.upper() in _TS_FREQ_TABLE:
            frequency = _TS_FREQ_TABLE[frequency.upper()]
        return frequency

    @staticmethod
    def _rev_map_freq(frequency:str)->_TsFreqs:
        if frequency.upper() in _PANDAS_FREQ_TABLE:
            frequency = _PANDAS_FREQ_TABLE[frequency.upper()]
        else:
            warnings.warn(f"Frequency is not supported: '{frequency}'", FutureWarning, stacklevel=2)
        return frequency

    @staticmethod
    def sum(ts_list:list[TS], name:str)->TS:
        """calculate the sum of a given list
        of :class:`volue_insight_timeseries.util.TS` objects

        Returns a :class:`~volue_insight_timeseries.util.TS`
        (:class:`volue_insight_timeseries.util.TS`) object that is
        the sum of a list of TS objects with the given name.

        Parameters
        ----------
        ts_list: list
            list of TS objects
        name: str
            Name of the returned TS object.
        Returns
        -------
        :class:`volue_insight_timeseries.util.TS` object
        """
        df = _ts_list_to_dataframe(ts_list)
        return _generated_series_to_TS(df.sum(axis=1), name)

    @staticmethod
    def mean(ts_list:list[TS], name:str)->TS:
        """calculate the mean of a given list of TS objects

        Returns a TS (:class:`volue_insight_timeseries.util.TS`) object that is
        the mean of a list of TS objects with the given name.

        Parameters
        ----------
        ts_list: list
            list of TS objects
        name: str
            Name of the returned TS object.
        Returns
        -------
        :class:`volue_insight_timeseries.util.TS` object
        """
        df = _ts_list_to_dataframe(ts_list)
        return _generated_series_to_TS(df.mean(axis=1), name)

    @staticmethod
    def median(ts_list:list[TS], name:str)->TS:
        """calculate the median of a given list of TS objects

        Returns a TS (:class:`volue_insight_timeseries.util.TS`) object that is
        the median of a list of TS objects with the given name.

        Parameters
        ----------
        ts_list: list
            list of TS objects
        name: str
            Name of the returned TS object.
        Returns
        -------
        :class:`volue_insight_timeseries.util.TS` object
        """
        df = _ts_list_to_dataframe(ts_list)
        return _generated_series_to_TS(df.median(axis=1), name)


def _generated_series_to_TS(series:pd.Series[float], name:str)->TS:
    series.name = name
    return TS.from_pandas(series)


def _ts_list_to_dataframe(ts_list:list[TS])->pd.DataFrame:
    return pd.concat([ts.to_pandas() for ts in ts_list], axis=1)


def tags_to_DF(tagged_list:list[TS])->pd.DataFrame:
    """
    Given a list of tagged series/instances, create a DataFrame with the tag of
    each as column name
    """
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


def parserange(rangeobj, tz=None):
    """
    Parse a range object (a pair of date strings, which may each be None)
    """
    if rangeobj.get("empty") is True:
        return None
    begin = rangeobj.get("begin")
    end = rangeobj.get("end")
    if begin is not None:
        begin = parsetime(begin, tz=tz)
    if end is not None:
        end = parsetime(end, tz=tz)
    return (begin, end)


_tzmap = {
    "CEGT": "CET",
    "WEGT": "WET",
    "PST": "US/Pacific",
    "TRT": "Turkey",
    "MSK": "Europe/Moscow",
    "ART": "America/Argentina/Buenos_Aires",
    "JST": "Asia/Tokyo",
}


def parse_tz(time_zone:str):
    try:
        if time_zone in _tzmap:
            time_zone = _tzmap[time_zone]
        return ZoneInfo(time_zone)
    except ZoneInfoNotFoundError:
        warnings.warn(f"ZoneInfo `{time_zone}` is invalid, setting timezone to `CET`.", Warning, 2)
        return ZoneInfo("CET")


def detect_curve_type(issue_date:str|None, tag:str|None)->Literal["TIME_SERIES", "TAGGED", "INSTANCES", "TAGGED_INSTANCES"]:
    if issue_date is None and tag is None:
        return TIME_SERIES
    if issue_date is None:
        return TAGGED
    if tag is None:
        return INSTANCES
    return TAGGED_INSTANCES

def make_arg(key:str, value:Any):
    if hasattr(value, "__iter__") and not isinstance(value, str):
        return "&".join([make_arg(key, v) for v in value])

    tmp = value.isoformat() if isinstance(value, datetime.date) else str(value)
    v = quote_plus(tmp)
    return f"{key}={v}"
