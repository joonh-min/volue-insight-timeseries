#
# Volue Insight API access library
#

import os

from . import auth, curves, curves_async, events, session, session_async, util
from .session import Session
from .session_async import Asession

__all__ = ['VERSION', 'Asession', 'Session', 'auth', 'curves', 'curves_async', 'events', 'session', 'session_async', 'util']
here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, 'VERSION')) as fv:
    VERSION = __version__ = fv.read().strip()
