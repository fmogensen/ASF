"""asf.feeder — rows for the tick: what to start next, read off ``index.json`` and the sessions
in flight. ``rows`` builds them (footprint and stalemate gates), ``tiers`` orders and cuts them
(the S1 lane), ``render`` prints them and the incident clock and wires ``asf next``."""
from asf.feeder.render import register  # noqa: F401
