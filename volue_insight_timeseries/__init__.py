#
# Volue Insight API access library
#

import os

from . import auth, curves, events, session, util
from .session import Session

__all__ = [ 'VERSION', 'Session', 'auth', 'curves', 'events', 'session', 'util', ]

here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, 'VERSION')) as fv:
    VERSION = __version__ = fv.read().strip()
