import sys

import volue_insight_timeseries_async.curves_async as _real
from volue_insight_timeseries_async.curves_async import *

# Swap in the real module, so state, patches and private names are shared
sys.modules[__name__] = _real
