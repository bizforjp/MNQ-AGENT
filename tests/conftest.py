"""
Shared fixtures for position resolver unit tests (TC1-TC23 per §5.12).

Per §5.12 all LONG baseline cases use:
  entry_price = 25000.00
  sl          = 24980.00  (20 pts)
  tp1         = 25030.00  (30 pts = 1.5x ATR @ ATR=20)
  tp2         = 25050.00  (50 pts = 2.5x ATR)
  opened_at_ms = 1744416000000
  eod_cutoff_ms = 1744488000000 (far future unless a test overrides)
"""
import os
import sys

import pytest

# Ensure the MNQ-AGENT root is on sys.path so `backend.position_resolver` resolves.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ---- Canonical timestamps (§5.12) ----
OPENED_AT_MS = 1744416000000
OPENED_AT_TS = "2026-04-12 14:00:00"
BAR_1_MS = 1744416900000   # entry + 15 min
BAR_2_MS = 1744417800000   # entry + 30 min
BAR_3_MS = 1744418700000
BAR_4_MS = 1744419600000
BAR_5_MS = 1744420500000
EOD_FAR_FUTURE = 1744488000000  # ~20 hours out — unreachable by any TC1-23 bar


# ---- Canonical level sets ----
LONG_LEVELS = dict(entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0)
SHORT_LEVELS = dict(entry_price=25000.0, sl=25020.0, tp1=24970.0, tp2=24950.0)


# ---- Fixtures return a freshly-opened OPEN-state position ----
@pytest.fixture
def long_open_position():
    from backend.position_resolver import new_position, Direction
    return new_position(
        signal_id=1,
        direction=Direction.LONG,
        signal_type=1,
        opened_at_ms=OPENED_AT_MS,
        opened_at_ts=OPENED_AT_TS,
        **LONG_LEVELS,
    )


@pytest.fixture
def short_open_position():
    from backend.position_resolver import new_position, Direction
    return new_position(
        signal_id=2,
        direction=Direction.SHORT,
        signal_type=1,
        opened_at_ms=OPENED_AT_MS,
        opened_at_ts=OPENED_AT_TS,
        **SHORT_LEVELS,
    )


@pytest.fixture
def eod_cutoff_ms():
    return EOD_FAR_FUTURE


@pytest.fixture
def make_bar():
    from backend.position_resolver import Bar

    def _make(ms, o, h, l, c):
        return Bar(bar_close_ms=ms, open=o, high=h, low=l, close=c)

    return _make
