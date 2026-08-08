"""HTTP clients for the services unfuckarr talks to."""

from .arr import ArrClient, ArrError
from .emby import EmbyClient, EmbyError

__all__ = ["ArrClient", "ArrError", "EmbyClient", "EmbyError"]
