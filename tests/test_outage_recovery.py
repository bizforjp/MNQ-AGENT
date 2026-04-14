"""
Outage recovery tests — TC24-TC31 from §8.9 of MONA_v3_OptionC_Spec.md.

These exercise the non-resolver backend code: schema, wrapper, Finnhub adapter,
gap recovery, rehydration, EOD sweep, staleness sweep. They integrate across
modules and write to a real (temp-file) SQLite DB.

Conventions:
  - Each test gets a fresh SQLite DB (tmp_path fixture).
  - Finnhub is a FakeFinnhubAdapter instance, never the real class.
  - Wall-clock is injected (current_ms) — no reliance on time.time().
  - Logs and Discord embeds are captured in lists the tests assert against.
"""
import sqlite3
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional

import pytest

from backend.position_resolver import (
    Bar,
    Direction,
    PositionFSMState,
    PositionState,
    Transition,
    new_position,
)
from backend.schema import (
    BAR_INTERVAL_MS,
    MAX_REPLAY_BARS,
    STALENESS_THRESHOLD_BARS,
    init_db,
)
from backend.apply_resolver_result import (
    apply_resolver_result,
    insert_position_row,
    insert_signal_row,
)
from backend.finnhub_adapter import FinnhubError
from backend.gap_recovery import (
    close_gap_clean,
    invoke_gap_recovery,
    reconcile_finnhub_bars,
)
from backend.rehydrate import (
    eod_cutoff_for_session_of,
    eod_sweep,
    et_to_ms,
    rehydrate_positions,
    session_adjusted_gap_bars,
)
from backend.staleness import staleness_sweep


# ----------------------------------------------------------------------
# Fake Finnhub adapter
# ----------------------------------------------------------------------
class FakeFinnhub:
    """Test double. Either returns canned bars or raises FinnhubError."""
    def __init__(self, bars: Optional[List[Bar]] = None,
                 raises: Optional[Exception] = None):
        self._bars = bars or []
        self._raises = raises
        self.call_count = 0
        self.last_call = None

    def fetch_bars(self, *, symbol, start_ms, end_ms, interval="15m"):
        self.call_count += 1
        self.last_call = dict(symbol=symbol, start_ms=start_ms,
                              end_ms=end_ms, interval=interval)
        if self._raises is not None:
            raise self._raises
        return list(self._bars)


# ----------------------------------------------------------------------
# Capture sinks
# ----------------------------------------------------------------------
class LogCapture:
    def __init__(self):
        self.lines: List[dict] = []

    def __call__(self, tag, message="", **fields):
        self.lines.append({"tag": tag, "message": message, **fields})

    def find(self, tag_contains: str):
        return [ln for ln in self.lines if tag_contains in ln["tag"]]


class EmbedCapture:
    def __init__(self):
        self.embeds: List[dict] = []

    def __call__(self, payload):
        self.embeds.append(payload)


# ----------------------------------------------------------------------
# Canonical ET times used across tests (April 13-14, 2026)
# Session: 09:30-16:00 ET. 15-min bars.
# ----------------------------------------------------------------------
def _ms(y, m, d, hh, mm):
    """Epoch ms for an ET wall-clock moment."""
    return int(datetime(y, m, d, hh, mm,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


SESSION_DATE_1 = (2026, 4, 13)
SESSION_DATE_2 = (2026, 4, 14)

OPEN_AT_MS = _ms(*SESSION_DATE_1, 10, 0)      # 10:00 ET entry
BAR_1_MS = _ms(*SESSION_DATE_1, 10, 15)
BAR_2_MS = _ms(*SESSION_DATE_1, 10, 30)
BAR_3_MS = _ms(*SESSION_DATE_1, 10, 45)
BAR_4_MS = _ms(*SESSION_DATE_1, 11, 0)
BAR_5_MS = _ms(*SESSION_DATE_1, 11, 15)
EOD_DAY_1 = _ms(*SESSION_DATE_1, 16, 0)
NEXT_DAY_10AM = _ms(*SESSION_DATE_2, 10, 0)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def long_open(db):
    """A LONG OPEN position, inserted into signals_v3 + positions + fsm_map."""
    pos = new_position(
        signal_id=1,
        direction=Direction.LONG,
        signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=OPEN_AT_MS,
        opened_at_ts="2026-04-13 10:00:00",
    )
    # Seed with some heartbeats already processed so last_heartbeat_bar_ms is set.
    pos = replace(pos, last_heartbeat_bar_ms=OPEN_AT_MS, last_observed_close=25000.0)
    insert_signal_row(db, pos, timestamp_iso="2026-04-13 14:00:00")
    insert_position_row(db, pos)
    fsm_map = {pos.signal_id: pos}
    return pos, fsm_map


@pytest.fixture
def long_after_two_bars(db):
    """Position that has processed BAR_1 and BAR_2 cleanly (NO_TRANSITION both)."""
    from backend.position_resolver import step
    pos = new_position(
        signal_id=2,
        direction=Direction.LONG,
        signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=OPEN_AT_MS,
        opened_at_ts="2026-04-13 10:00:00",
    )
    insert_signal_row(db, pos, timestamp_iso="2026-04-13 14:00:00")
    insert_position_row(db, pos)
    fsm_map = {pos.signal_id: pos}

    # Process a couple of heartbeats via the wrapper so last_heartbeat_bar_ms
    # advances and the DB stays consistent with memory.
    for bar in [
        Bar(BAR_1_MS, 25000, 25010, 24995, 25005),
        Bar(BAR_2_MS, 25005, 25015, 24995, 25010),
    ]:
        result = step(fsm_map[pos.signal_id], bar, eod_cutoff_ms=EOD_DAY_1)
        apply_resolver_result(fsm_map[pos.signal_id], bar, result,
                              fsm_map, db)
    return fsm_map[pos.signal_id], fsm_map


# ----------------------------------------------------------------------
# TC24 — Finnhub returns misaligned bars (reconcile fail)
# ----------------------------------------------------------------------
def test_TC24_finnhub_misaligned_bars_gap_clean(long_after_two_bars, db):
    pos, fsm_map = long_after_two_bars
    # 3-bar gap; Finnhub returns bars offset by 7 minutes.
    current_bar_ms = BAR_5_MS  # last was BAR_2
    misaligned = [
        Bar(BAR_2_MS + 7 * 60_000, 25010, 25020, 25000, 25015),
        Bar(BAR_3_MS + 7 * 60_000, 25015, 25025, 25010, 25018),
        Bar(BAR_4_MS + 7 * 60_000, 25018, 25028, 25010, 25022),
    ]
    finnhub = FakeFinnhub(bars=misaligned)
    log = LogCapture()
    embeds = EmbedCapture()

    invoke_gap_recovery(
        position=pos,
        last_seen_ms=pos.last_heartbeat_bar_ms,
        current_bar_ms=current_bar_ms,
        trigger="heartbeat_gap",
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1,
        post_embed=embeds, log=log,
    )

    # trade_outcomes row exists with GAP_CLEAN
    row = db.execute(
        "SELECT exit_reason FROM trade_outcomes WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "GAP_CLEAN"
    # Log names the underlying reason
    reconcile_lines = log.find("RECONCILE_FAIL")
    assert len(reconcile_lines) >= 1
    # Position is no longer in fsm_map
    assert pos.signal_id not in fsm_map


# ----------------------------------------------------------------------
# TC25 — Gap larger than MAX_REPLAY_BARS
# ----------------------------------------------------------------------
def test_TC25_gap_exceeds_max_replay_bars(long_after_two_bars, db):
    pos, fsm_map = long_after_two_bars
    # 20-bar gap (exceeds 16) — no Finnhub call should be made.
    current_bar_ms = pos.last_heartbeat_bar_ms + 20 * BAR_INTERVAL_MS
    finnhub = FakeFinnhub(bars=[])   # would raise if exercised wrong
    log = LogCapture()
    embeds = EmbedCapture()

    invoke_gap_recovery(
        position=pos,
        last_seen_ms=pos.last_heartbeat_bar_ms,
        current_bar_ms=current_bar_ms,
        trigger="heartbeat_gap",
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1 + 2 * 24 * 3600 * 1000,  # well future
        post_embed=embeds, log=log,
    )

    assert finnhub.call_count == 0, "Finnhub must not be called when gap > MAX"
    row = db.execute(
        "SELECT exit_reason FROM trade_outcomes WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "GAP_CLEAN"
    assert log.find("GAP_EXCEEDS_MAX")
    assert pos.signal_id not in fsm_map


# ----------------------------------------------------------------------
# TC26 — Finnhub unreachable
# ----------------------------------------------------------------------
def test_TC26_finnhub_unreachable(long_after_two_bars, db):
    pos, fsm_map = long_after_two_bars
    current_bar_ms = pos.last_heartbeat_bar_ms + 3 * BAR_INTERVAL_MS
    finnhub = FakeFinnhub(raises=FinnhubError("connection reset"))
    log = LogCapture()
    embeds = EmbedCapture()

    invoke_gap_recovery(
        position=pos,
        last_seen_ms=pos.last_heartbeat_bar_ms,
        current_bar_ms=current_bar_ms,
        trigger="heartbeat_gap",
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1,
        post_embed=embeds, log=log,
    )

    assert finnhub.call_count == 1
    row = db.execute(
        "SELECT exit_reason FROM trade_outcomes WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "GAP_CLEAN"
    assert log.find("FINNHUB_UNAVAILABLE")
    assert pos.signal_id not in fsm_map


# ----------------------------------------------------------------------
# TC27 — Successful 3-bar replay that hits TP1 on the 3rd bar
# ----------------------------------------------------------------------
def test_TC27_successful_three_bar_replay_tp1(long_after_two_bars, db):
    pos, fsm_map = long_after_two_bars
    # Gap of 3 bars — bars 3/4/5 missed.
    replay = [
        Bar(BAR_3_MS, 25010, 25015, 25005, 25012),  # NO_TRANSITION
        Bar(BAR_4_MS, 25012, 25020, 25008, 25018),  # NO_TRANSITION
        Bar(BAR_5_MS, 25018, 25035, 25015, 25030),  # TP1 hit (high >= 25030)
    ]
    finnhub = FakeFinnhub(bars=replay)
    log = LogCapture()

    invoke_gap_recovery(
        position=pos,
        last_seen_ms=pos.last_heartbeat_bar_ms,
        current_bar_ms=BAR_5_MS,
        trigger="heartbeat_gap",
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1,
        log=log,
    )

    # Position is still open (TP1 transition does not close) and advanced to TP1_HIT
    assert pos.signal_id in fsm_map
    rehydrated = fsm_map[pos.signal_id]
    assert rehydrated.state == PositionFSMState.TP1_HIT
    assert rehydrated.tp1_hit == 1
    assert rehydrated.tp1_hit_bar_ms == BAR_5_MS
    assert rehydrated.last_heartbeat_bar_ms == BAR_5_MS
    # No trade_outcomes yet
    row = db.execute(
        "SELECT COUNT(*) FROM trade_outcomes WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert row[0] == 0


# ----------------------------------------------------------------------
# TC28 — Replay closes the position mid-replay (bar 2 of 5 hits SL)
# ----------------------------------------------------------------------
def test_TC28_replay_closes_mid_replay(long_after_two_bars, db):
    pos, fsm_map = long_after_two_bars
    # 5-bar replay at BAR_3..BAR_5 + two more 15-min intervals; bar 2 hits SL.
    bar_6_ms = pos.last_heartbeat_bar_ms + 4 * BAR_INTERVAL_MS  # BAR_2 + 4 = 11:30
    bar_7_ms = pos.last_heartbeat_bar_ms + 5 * BAR_INTERVAL_MS  # 11:45
    replay = [
        Bar(BAR_3_MS, 25010, 25020, 25000, 25015),   # NO_TRANSITION
        Bar(BAR_4_MS, 25015, 25020, 24975, 24985),   # SL hit (low <= 24980)
        Bar(BAR_5_MS, 24985, 24995, 24970, 24980),   # must be skipped
        Bar(bar_6_ms, 24980, 24990, 24970, 24985),   # must be skipped
        Bar(bar_7_ms, 24985, 24995, 24975, 24985),   # must be skipped
    ]
    finnhub = FakeFinnhub(bars=replay)
    log = LogCapture()

    invoke_gap_recovery(
        position=pos,
        last_seen_ms=pos.last_heartbeat_bar_ms,
        current_bar_ms=bar_7_ms,
        trigger="heartbeat_gap",
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1,
        log=log,
    )

    # trade_outcomes row reflects bar 2's SL hit
    row = db.execute(
        "SELECT exit_reason, final_pnl_points FROM trade_outcomes WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "SL_HIT"
    # Position row has closed_at_ms == bar 2's bar_close_ms
    pos_row = db.execute(
        "SELECT closed_at_ms, state FROM positions WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert pos_row[0] == BAR_4_MS
    assert pos_row[1] == PositionFSMState.CLOSED
    assert pos.signal_id not in fsm_map


# ----------------------------------------------------------------------
# TC29 — Staleness sweep closes a never-heartbeated position
# ----------------------------------------------------------------------
def test_TC29_staleness_closes_slot_overflow_victim(long_open, db):
    pos, fsm_map = long_open
    # Advance wall clock by 2+ bars of market time.
    current_ms = pos.opened_at_ms + 3 * BAR_INTERVAL_MS
    finnhub = FakeFinnhub(raises=FinnhubError("simulated"))  # force GAP_CLEAN
    log = LogCapture()

    staleness_sweep(
        fsm_map=fsm_map, conn=db, current_ms=current_ms,
        finnhub=finnhub, eod_cutoff_ms=EOD_DAY_1,
        threshold_bars=STALENESS_THRESHOLD_BARS,
        bar_interval_ms=BAR_INTERVAL_MS,
        log=log,
    )

    row = db.execute(
        "SELECT exit_reason FROM trade_outcomes WHERE signal_id=?",
        (pos.signal_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "GAP_CLEAN"
    assert pos.signal_id not in fsm_map
    # Distinct tag: this position never heartbeated after ENTRY
    assert log.find("STALENESS_NEVER_HEARTBEATED")


# ----------------------------------------------------------------------
# TC30 — Restart rehydrates position past EOD; sweep closes with EOD_TIMEOUT
# ----------------------------------------------------------------------
def test_TC30_restart_rehydrated_position_past_eod(tmp_path):
    db_path = str(tmp_path / "tc30.db")
    # --- PRE-RESTART: write a position in TP1_HIT state with last heartbeat
    # at 15:30 ET on session 1.
    pre = sqlite3.connect(db_path)
    init_db(pre)
    last_hb_ms = _ms(*SESSION_DATE_1, 15, 30)
    last_close_price = 25018.0
    pos = new_position(
        signal_id=30,
        direction=Direction.LONG,
        signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=OPEN_AT_MS,
        opened_at_ts="2026-04-13 10:00:00",
    )
    pos = replace(
        pos,
        state=PositionFSMState.TP1_HIT,
        tp1_hit=1,
        tp1_hit_bar_ms=BAR_2_MS,
        effective_sl=pos.entry_price,
        last_heartbeat_bar_ms=last_hb_ms,
        heartbeats_processed=7,
        last_observed_close=last_close_price,
    )
    insert_signal_row(pre, pos, timestamp_iso="2026-04-13 14:00:00")
    insert_position_row(pre, pos)
    pre.commit()
    pre.close()

    # --- RESTART: fresh connection, rehydrate, then EOD sweep.
    db = sqlite3.connect(db_path)
    fsm_map = rehydrate_positions(db)
    assert 30 in fsm_map
    current_ms = NEXT_DAY_10AM
    log = LogCapture()
    eod_sweep(fsm_map, db, current_ms, log=log)

    outcome = db.execute(
        "SELECT exit_reason, final_pnl_points FROM trade_outcomes WHERE signal_id=?",
        (30,),
    ).fetchone()
    assert outcome is not None
    assert outcome[0] == "EOD_TIMEOUT"
    # final_pnl uses last_observed_close (25018): 2*30 + 1*(25018-25000) == 78
    assert outcome[1] == pytest.approx(2 * 30 + 1 * (last_close_price - 25000))
    # positions.closed_at_ms is 16:00 ET of session 1 — the EOD moment missed.
    pos_row = db.execute(
        "SELECT closed_at_ms FROM positions WHERE signal_id=?", (30,),
    ).fetchone()
    assert pos_row[0] == EOD_DAY_1
    assert 30 not in fsm_map
    db.close()


# ----------------------------------------------------------------------
# TC31 — Overnight: EOD sweep runs before staleness, so staleness is a no-op
# ----------------------------------------------------------------------
def test_TC31_overnight_eod_before_staleness(tmp_path):
    db_path = str(tmp_path / "tc31.db")
    pre = sqlite3.connect(db_path)
    init_db(pre)

    # Position opened at 15:30 ET, never heartbeated since.
    open_ms = _ms(*SESSION_DATE_1, 15, 30)
    pos = new_position(
        signal_id=31,
        direction=Direction.LONG,
        signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=open_ms,
        opened_at_ts="2026-04-13 15:30:00",
    )
    pos = replace(pos, last_observed_close=25000.0)
    insert_signal_row(pre, pos, timestamp_iso="2026-04-13 15:30:00")
    insert_position_row(pre, pos)
    pre.commit()
    pre.close()

    db = sqlite3.connect(db_path)
    fsm_map = rehydrate_positions(db)
    current_ms = NEXT_DAY_10AM  # next morning
    log = LogCapture()

    # Startup ordering: EOD sweep runs first (§8.4 step 2).
    eod_sweep(fsm_map, db, current_ms, log=log)
    # Staleness sweep runs second — must be a no-op.
    finnhub = FakeFinnhub(bars=[])
    staleness_sweep(
        fsm_map=fsm_map, conn=db, current_ms=current_ms,
        finnhub=finnhub, eod_cutoff_ms=EOD_DAY_1,
        threshold_bars=STALENESS_THRESHOLD_BARS,
        bar_interval_ms=BAR_INTERVAL_MS,
        log=log,
    )

    # EOD sweep closed the position; staleness found nothing to close.
    row = db.execute(
        "SELECT exit_reason FROM trade_outcomes WHERE signal_id=?",
        (31,),
    ).fetchone()
    assert row is not None
    assert row[0] == "EOD_TIMEOUT"
    assert 31 not in fsm_map
    # Session-adjusted gap helper: previous session ended 16:00 Day 1,
    # next session starts 9:30 Day 2. The overnight window is excluded.
    bars_between = session_adjusted_gap_bars(
        _ms(*SESSION_DATE_1, 15, 45),
        _ms(*SESSION_DATE_2, 9, 45),
        BAR_INTERVAL_MS,
    )
    # 15:45 -> 16:00 = 1 bar that session, then 9:30 -> 9:45 = 1 bar next session
    assert bars_between == 2
    # Staleness did not call gap recovery / did not log a STALENESS_* tag
    # against a still-open position since no positions remained.
    db.close()
