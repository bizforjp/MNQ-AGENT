"""
Integration and migration test cases — TC32-TC35 from §5.14.

These tests exercise coordination across modules (wrapper, router, gap
recovery, staleness sweep) and cross the migration boundary (v2 legacy
payloads vs v3 Option C payloads). Not reachable by the TC1-TC23 pure
resolver tests or the TC24-TC31 outage recovery unit tests.

Conventions match test_outage_recovery.py: temp-file SQLite per test,
FakeFinnhub injected, LogCapture/EmbedCapture sinks.
"""
import sqlite3
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional

import pytest

from backend.position_resolver import (
    Bar, Direction, PositionFSMState, PositionState, Transition,
    new_position, step,
)
from backend.schema import (
    BAR_INTERVAL_MS, MAX_REPLAY_BARS, STALENESS_THRESHOLD_BARS, init_db,
)
from backend.apply_resolver_result import (
    apply_resolver_result, insert_position_row, insert_signal_row,
)
from backend.finnhub_adapter import FinnhubError
from backend.gap_recovery import (
    close_gap_clean, invoke_gap_recovery, reconcile_finnhub_bars,
)
from backend.rehydrate import (
    eod_sweep, et_to_ms, rehydrate_positions, session_adjusted_gap_bars,
)
from backend.staleness import staleness_sweep
from backend.webhook_router import (
    route_entry, route_eval, route_heartbeat_for_position,
)


# ----------------------------------------------------------------------
class FakeFinnhub:
    def __init__(self, bars=None, raises=None):
        self._bars = bars or []
        self._raises = raises
        self.call_count = 0
        self.calls: List[dict] = []

    def fetch_bars(self, *, symbol, start_ms, end_ms, interval="15m"):
        self.call_count += 1
        self.calls.append(dict(symbol=symbol, start_ms=start_ms,
                               end_ms=end_ms, interval=interval))
        if self._raises is not None:
            raise self._raises
        return list(self._bars)


class LogCapture:
    def __init__(self):
        self.lines: List[dict] = []

    def __call__(self, tag, message="", **fields):
        self.lines.append({"tag": tag, "message": message, **fields})

    def find(self, tag):
        return [ln for ln in self.lines if tag in ln["tag"]]


class EmbedCapture:
    def __init__(self):
        self.embeds: List[dict] = []

    def __call__(self, payload):
        self.embeds.append(payload)


def _ms(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


# Canonical moments for integration tests. Using spec's "T / T+1 / ..."
# notation where T is the entry bar (bar_close_ms == opened_at_ms).
OPEN_AT_MS = _ms(2026, 4, 13, 10, 0)
T   = OPEN_AT_MS
T_1 = OPEN_AT_MS + 1 * BAR_INTERVAL_MS   # 10:15
T_2 = OPEN_AT_MS + 2 * BAR_INTERVAL_MS   # 10:30
T_3 = OPEN_AT_MS + 3 * BAR_INTERVAL_MS   # 10:45
T_4 = OPEN_AT_MS + 4 * BAR_INTERVAL_MS   # 11:00
T_5 = OPEN_AT_MS + 5 * BAR_INTERVAL_MS   # 11:15
EOD_DAY_1 = _ms(2026, 4, 13, 16, 0)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    init_db(conn)
    yield conn
    conn.close()


# ======================================================================
# TC32 — Tolerance mode coexistence during migration Step 1
# ======================================================================
def test_TC32_tolerance_mode_coexistence(db):
    """
    Two payload shapes arrive at a backend running new Option C code in
    tolerance mode. Legacy payloads write to v2 tables via the legacy path;
    v3 payloads write to v3 tables via the new path. No cross-contamination.
    """
    fsm_map: dict = {}
    log = LogCapture()

    # Step 1 — v2.1.1 ENTRY (no bar_close_ms)
    legacy_entry = {
        "status": "ENTRY",
        "sig_dir": 1, "sig_type": 1,
        "entry_price": 25000.0, "sl": 24980.0,
        "tp1": 25030.0, "tp2": 25050.0,
        "timestamp": "2026-04-13 10:00:00",
    }
    route_entry(legacy_entry, fsm_map=fsm_map, conn=db, log=log)

    signals_count = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    signals_v3_count = db.execute("SELECT COUNT(*) FROM signals_v3").fetchone()[0]
    positions_count = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert signals_count == 1, "legacy ENTRY must write to signals"
    assert signals_v3_count == 0, "legacy ENTRY must NOT touch signals_v3"
    assert positions_count == 0, "legacy ENTRY must NOT create a position"
    assert fsm_map == {}

    # Step 2 — v3.0 ENTRY (has bar_close_ms)
    v3_entry = {
        "status": "ENTRY",
        "sig_dir": 1, "sig_type": 1,
        "entry_price": 25000.0, "sl": 24980.0,
        "tp1": 25030.0, "tp2": 25050.0,
        "timestamp": "2026-04-13 10:00:00",
        "bar_close_ms": OPEN_AT_MS,
        "opened_at_ts": "2026-04-13 10:00:00",
    }
    v3_signal_id = route_entry(v3_entry, fsm_map=fsm_map, conn=db, log=log)
    assert v3_signal_id is not None

    assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM signals_v3").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    assert v3_signal_id in fsm_map
    pos = fsm_map[v3_signal_id]
    assert pos.opened_at_ms == OPEN_AT_MS
    assert pos.state == PositionFSMState.OPEN

    # Step 3 — v3.0 HEARTBEAT for the v3 position (one bar past entry).
    bar = Bar(T_1, 25000, 25010, 24995, 25005)
    finnhub = FakeFinnhub(bars=[])
    route_heartbeat_for_position(
        signal_id=v3_signal_id, bar=bar,
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1, log=log,
    )
    pos_after = fsm_map[v3_signal_id]
    assert pos_after.last_heartbeat_bar_ms == T_1
    assert pos_after.heartbeats_processed == 1

    # Step 4 — v2.1.1 EVAL (no parent_bar_close_ms)
    legacy_eval = {
        "status": "EVAL_RESULT",
        "sig_dir": 1, "sig_type": 1,
        "result": "PASS",
        "ft_target": 25020.0, "move_points": 5.0,
        "state_before": "ELIGIBLE", "state_after": "ELIGIBLE",
        "stops_after": 0,
        "timestamp": "2026-04-13 10:30:00",
    }
    route_eval(legacy_eval, conn=db, log=log)

    assert db.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0


# ======================================================================
# TC33 — Replay idempotency under live-and-replay overlap
# ======================================================================
def test_TC33_replay_idempotency_vs_live_overlap(db):
    """
    After gap recovery fills bars T+3 and T+4 via Finnhub, a delayed live
    heartbeat for T+3 arrives. Guard must reject it, log REPLAY, and NOT
    call the resolver. A subsequent live T+5 proceeds normally.
    """
    # Seed: position has processed T+1, T+2, T+3 live (so last == T+3).
    pos = new_position(
        signal_id=33, direction=Direction.LONG, signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=T, opened_at_ts="2026-04-13 10:00:00",
    )
    insert_signal_row(db, pos, timestamp_iso="2026-04-13 10:00:00")
    insert_position_row(db, pos)
    fsm_map = {pos.signal_id: pos}
    for b in [
        Bar(T_1, 25000, 25010, 24995, 25005),
        Bar(T_2, 25005, 25015, 25000, 25010),
        Bar(T_3, 25010, 25020, 25005, 25015),
    ]:
        result = step(fsm_map[pos.signal_id], b, eod_cutoff_ms=EOD_DAY_1)
        apply_resolver_result(fsm_map[pos.signal_id], b, result, fsm_map, db)

    assert fsm_map[pos.signal_id].last_heartbeat_bar_ms == T_3

    # Live T+5 heartbeat arrives — router detects gap (T+4 was missed),
    # invokes Finnhub for exactly T+4, then processes T+5 live.
    replay = [Bar(T_4, 25015, 25025, 25010, 25020)]
    finnhub = FakeFinnhub(bars=replay)
    log = LogCapture()
    live_t5 = Bar(T_5, 25020, 25025, 25015, 25022)
    res = route_heartbeat_for_position(
        signal_id=pos.signal_id, bar=live_t5,
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1, log=log,
    )
    assert res is not None
    # After gap fill + live step, last_heartbeat is the live T+5.
    assert fsm_map[pos.signal_id].last_heartbeat_bar_ms == T_5

    # Delayed live delivery of T+4 (earlier than last_heartbeat_bar_ms)
    pre_state = fsm_map[pos.signal_id]
    late_bar = Bar(T_4, 25015, 25025, 25010, 25020)
    replay_log = LogCapture()
    res_late = route_heartbeat_for_position(
        signal_id=pos.signal_id, bar=late_bar,
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1, log=replay_log,
    )
    # Resolver was not called
    assert res_late is None
    assert replay_log.find("REPLAY")
    # Position state unchanged by the rejected late delivery.
    assert fsm_map[pos.signal_id] == pre_state


# ======================================================================
# TC34 — Concurrent gap recovery on two positions
# ======================================================================
def test_TC34_concurrent_gap_recovery_two_positions(db):
    """
    Two positions opened at T with no heartbeats yet. A single live
    heartbeat for T+4 triggers router gap recovery for each: fill T+1..T+3,
    then process T+4 live. Both end at T+4 with independent state.
    """
    pos_a = new_position(
        signal_id=101, direction=Direction.LONG, signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=T, opened_at_ts="2026-04-13 10:00:00",
    )
    # B opens one bar later (different bar_close_ms per INV-G UNIQUE).
    pos_b_opened = T_1
    pos_b = new_position(
        signal_id=102, direction=Direction.LONG, signal_type=2,
        entry_price=25100.0, sl=25080.0, tp1=25130.0, tp2=25150.0,
        opened_at_ms=pos_b_opened, opened_at_ts="2026-04-13 10:15:00",
    )
    insert_signal_row(db, pos_a, timestamp_iso="2026-04-13 10:00:00")
    insert_position_row(db, pos_a)
    insert_signal_row(db, pos_b, timestamp_iso="2026-04-13 10:00:00")
    insert_position_row(db, pos_b)
    fsm_map = {pos_a.signal_id: pos_a, pos_b.signal_id: pos_b}

    # Finnhub replay windows (T+4 is live in both; recovery fills up to T+3).
    # A opened at T, so recovery fills T+1..T+3 (3 bars).
    # B opened at T+1, so recovery fills T+2..T+3 (2 bars).
    a_replay = [
        Bar(T_1, 25000, 25010, 24995, 25005),
        Bar(T_2, 25005, 25015, 25000, 25010),
        Bar(T_3, 25010, 25020, 25005, 25015),
    ]
    b_replay = [
        Bar(T_2, 25105, 25115, 25100, 25110),
        Bar(T_3, 25110, 25120, 25105, 25115),
    ]

    class SequencedFinnhub:
        def __init__(self, sequences):
            self._seqs = list(sequences)
            self.call_count = 0

        def fetch_bars(self, **kw):
            self.call_count += 1
            return list(self._seqs.pop(0))

    finnhub = SequencedFinnhub([a_replay, b_replay])
    log = LogCapture()

    # Live T+4 heartbeat, slot 1 = A, slot 2 = B. Router processes per slot.
    live_a = Bar(T_4, 25015, 25025, 25010, 25020)
    live_b = Bar(T_4, 25115, 25125, 25110, 25120)
    route_heartbeat_for_position(
        signal_id=101, bar=live_a,
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1, log=log,
    )
    # A finished; B not yet started — still at opened state.
    assert fsm_map[101].last_heartbeat_bar_ms == T_4
    assert fsm_map[101].last_observed_close == 25020
    assert fsm_map[102].last_heartbeat_bar_ms == pos_b_opened
    assert fsm_map[102].last_observed_close == 25100.0

    route_heartbeat_for_position(
        signal_id=102, bar=live_b,
        fsm_map=fsm_map, conn=db, finnhub=finnhub,
        eod_cutoff_ms=EOD_DAY_1, log=log,
    )
    assert fsm_map[102].last_heartbeat_bar_ms == T_4
    assert fsm_map[102].last_observed_close == 25120
    # A's state unchanged by B's recovery — no cross-contamination.
    assert fsm_map[101].last_heartbeat_bar_ms == T_4
    assert fsm_map[101].last_observed_close == 25020
    assert finnhub.call_count == 2


# ======================================================================
# TC35 — GAP_CLEAN fallback with unavailable last_observed_close
# ======================================================================
def test_TC35_gap_clean_fallback_no_observation(db):
    """
    Position in OPEN state, heartbeats_processed == 0. Staleness check fires
    NEVER_HEARTBEATED tag. Finnhub raises. close_gap_clean falls back to
    entry_price → final_pnl == 0. trade_outcomes row written cleanly; no
    null-handling error on the missing last_observed_close.
    """
    pos = new_position(
        signal_id=201, direction=Direction.LONG, signal_type=1,
        entry_price=25000.0, sl=24980.0, tp1=25030.0, tp2=25050.0,
        opened_at_ms=OPEN_AT_MS, opened_at_ts="2026-04-13 10:00:00",
    )
    # Force the "never heartbeated" shape: no observation data at all.
    pos = replace(pos, heartbeats_processed=0, last_observed_close=0.0,
                  last_heartbeat_bar_ms=pos.opened_at_ms)
    insert_signal_row(db, pos, timestamp_iso="2026-04-13 10:00:00")
    insert_position_row(db, pos)
    fsm_map = {pos.signal_id: pos}

    finnhub = FakeFinnhub(raises=FinnhubError("unreachable"))
    log = LogCapture()
    embeds = EmbedCapture()

    # Staleness sweep: 2+ bars have elapsed since opened_at_ms
    current_ms = pos.opened_at_ms + 3 * BAR_INTERVAL_MS
    staleness_sweep(
        fsm_map=fsm_map, conn=db, current_ms=current_ms,
        finnhub=finnhub, eod_cutoff_ms=EOD_DAY_1,
        threshold_bars=STALENESS_THRESHOLD_BARS,
        bar_interval_ms=BAR_INTERVAL_MS,
        post_embed=embeds, log=log,
    )

    # trade_outcomes row: GAP_CLEAN, final_pnl == 0, mae/mfe == 0
    outcome = db.execute(
        "SELECT exit_reason, final_pnl_points, mae_points, mfe_points "
        "FROM trade_outcomes WHERE signal_id=?", (201,),
    ).fetchone()
    assert outcome is not None
    assert outcome[0] == "GAP_CLEAN"
    assert outcome[1] == 0
    assert outcome[2] == 0
    assert outcome[3] == 0

    # positions: state=3, closed_at_ms == opened_at_ms
    pos_row = db.execute(
        "SELECT state, closed_at_ms, opened_at_ms FROM positions WHERE signal_id=?",
        (201,),
    ).fetchone()
    assert pos_row[0] == int(PositionFSMState.CLOSED)
    assert pos_row[1] == pos_row[2]

    # fsm_map emptied
    assert 201 not in fsm_map

    # OUTCOME embed posted with GAP_CLEAN annotation
    gap_clean_embeds = [
        e for e in embeds.embeds if e.get("exit_reason") == "GAP_CLEAN"
    ]
    assert gap_clean_embeds
    assert gap_clean_embeds[0].get("gap_clean_reason") == "FINNHUB_UNAVAILABLE"

    # Log carries both the distinct staleness tag and GAP_CLEAN
    assert log.find("STALENESS_NEVER_HEARTBEATED")
    assert log.find("GAP_CLEAN")


# ======================================================================
# Regression: ENTRY → HEARTBEAT → second ENTRY must not collide
# ======================================================================
def test_entry_heartbeat_entry_no_collision(db):
    """
    Bug 1 regression: heartbeats must never write to signals_v3, and two
    entries on different bars must both succeed. Also verifies that
    bar_close_ms=0 is rejected as a likely unresolved template tag.
    """
    fsm_map: dict = {}
    log = LogCapture()
    finnhub = FakeFinnhub()
    embeds = EmbedCapture()

    entry1_ms = T
    entry2_ms = T_5  # different bar

    # --- ENTRY 1: TREND SHORT ---
    e1 = {
        "sig_dir": 2, "sig_type": 1,
        "entry_price": 25050.0, "sl": 25080.0,
        "tp1": 25020.0, "tp2": 24990.0,
        "bar_close_ms": entry1_ms,
        "timestamp": "2026-04-13 10:00:00",
    }
    sid1 = route_entry(e1, fsm_map=fsm_map, conn=db, log=log)
    assert sid1 is not None, "first ENTRY must succeed"

    # --- HEARTBEAT on bars T+1 through T+4 ---
    for i in range(1, 5):
        hb_ms = entry1_ms + i * BAR_INTERVAL_MS
        bar = Bar(bar_close_ms=hb_ms, open=25045, high=25055, low=25035, close=25040)
        eod = _ms(2026, 4, 13, 16, 0)
        route_heartbeat_for_position(
            signal_id=sid1, bar=bar, fsm_map=fsm_map, conn=db,
            finnhub=finnhub, eod_cutoff_ms=eod,
            post_embed=embeds, log=log,
        )

    # signals_v3 must still have exactly 1 row — heartbeats must not write here
    sv3_count = db.execute("SELECT COUNT(*) FROM signals_v3").fetchone()[0]
    assert sv3_count == 1, f"heartbeats should not write to signals_v3, got {sv3_count}"

    # --- ENTRY 2: SQUEEZE LONG on a different bar ---
    e2 = {
        "sig_dir": 1, "sig_type": 2,
        "entry_price": 25060.0, "sl": 25040.0,
        "tp1": 25090.0, "tp2": 25120.0,
        "bar_close_ms": entry2_ms,
        "timestamp": "2026-04-13 11:15:00",
    }
    sid2 = route_entry(e2, fsm_map=fsm_map, conn=db, log=log)
    assert sid2 is not None, "second ENTRY on a different bar must succeed"

    # Both entries are in signals_v3
    sv3_count = db.execute("SELECT COUNT(*) FROM signals_v3").fetchone()[0]
    assert sv3_count == 2

    # Both positions in fsm_map
    assert sid1 in fsm_map
    assert sid2 in fsm_map

    # --- ENTRY 3: bar_close_ms=0 must be rejected ---
    e3 = {
        "sig_dir": 1, "sig_type": 1,
        "entry_price": 25070.0, "sl": 25050.0,
        "tp1": 25100.0, "tp2": 25130.0,
        "bar_close_ms": 0,
        "timestamp": "2026-04-13 12:00:00",
    }
    sid3 = route_entry(e3, fsm_map=fsm_map, conn=db, log=log)
    assert sid3 is None, "bar_close_ms=0 must be rejected"

    # signals_v3 still has 2 rows, not 3
    sv3_count = db.execute("SELECT COUNT(*) FROM signals_v3").fetchone()[0]
    assert sv3_count == 2


def test_same_bar_trend_and_squeeze_both_succeed(db):
    """
    With the composite UNIQUE(bar_close_ms, signal_type), TREND and
    SQUEEZE entries on the same bar must both succeed.
    """
    fsm_map: dict = {}
    log = LogCapture()
    same_bar_ms = T

    e_trend = {
        "sig_dir": 1, "sig_type": 1,
        "entry_price": 25000.0, "sl": 24980.0,
        "tp1": 25030.0, "tp2": 25050.0,
        "bar_close_ms": same_bar_ms,
        "timestamp": "2026-04-13 10:00:00",
    }
    sid_t = route_entry(e_trend, fsm_map=fsm_map, conn=db, log=log)
    assert sid_t is not None

    e_sqz = {
        "sig_dir": 2, "sig_type": 2,
        "entry_price": 25000.0, "sl": 25020.0,
        "tp1": 24970.0, "tp2": 24940.0,
        "bar_close_ms": same_bar_ms,
        "timestamp": "2026-04-13 10:00:00",
    }
    sid_s = route_entry(e_sqz, fsm_map=fsm_map, conn=db, log=log)
    assert sid_s is not None, "SQUEEZE on same bar as TREND must succeed"
    assert sid_t != sid_s
