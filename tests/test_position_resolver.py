"""
Test suite for backend/position_resolver.py.

Transcribes the TC1-TC23 enumeration from MONA_v3_OptionC_Spec.md §5.12.
These tests are written BEFORE the implementation (TDD). They must all fail
(ImportError until backend/position_resolver.py exists, then assertion failures
until the implementation is correct).

Priority-rule context:
  - PF1: OPEN-state collision (TP1 + SL same bar) -> SL wins (pessimistic fill).
    See §5.6 and Waves1-5 Edit 1 (future-reader commentary on OI-03).
  - PF2: TP1_HIT collision (TP2 + BE same bar) -> BE_STOP wins.
  - PF3: Gap open beyond an exit level -> fill at bar.open (honest).
  - PF4: Entry bar is never resolved.
"""
import dataclasses

import pytest

from backend.position_resolver import (
    Bar,
    Direction,
    PositionFSMState,
    PositionState,
    ResolverInvariantError,
    StepResult,
    Transition,
    new_position,
    process_heartbeat,
    should_process,
    step,
)

from conftest import (
    BAR_1_MS, BAR_2_MS, BAR_3_MS, BAR_4_MS, BAR_5_MS,
    OPENED_AT_MS, OPENED_AT_TS,
    LONG_LEVELS, SHORT_LEVELS, EOD_FAR_FUTURE,
)


# ======================================================================
# TC1 — Normal TP1 then TP2 (LONG)
# ======================================================================
def test_TC1_long_tp1_then_tp2(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.NO_TRANSITION
    assert r1.updated_position.mae_points == -5
    assert r1.updated_position.mfe_points == 10
    assert r1.updated_position.state == PositionFSMState.OPEN

    bar2 = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP1_HIT
    assert r2.updated_position.state == PositionFSMState.TP1_HIT
    assert r2.updated_position.tp1_hit == 1
    assert r2.updated_position.tp1_hit_bar_ms == BAR_2_MS
    assert r2.updated_position.effective_sl == 25000.0   # BE stop
    assert r2.updated_position.mfe_points == 35

    bar3 = make_bar(BAR_3_MS, 25030, 25055, 25025, 25050)
    r3 = step(r2.updated_position, bar3, eod_cutoff_ms=eod_cutoff_ms)
    assert r3.transition == Transition.TP2_HIT
    assert r3.exit_reason == "TP2_HIT"
    assert r3.updated_position.state == PositionFSMState.CLOSED
    assert r3.updated_position.final_pnl_points == 2 * 30 + 1 * 50  # 110
    assert r3.updated_position.mae_points == -5
    assert r3.updated_position.mfe_points == 55
    assert r3.updated_position.post_tp1_mae_points == 5
    assert r3.updated_position.tp1_hit == 1


# ======================================================================
# TC2 — Normal TP1 then TP2 (SHORT mirror)
# ======================================================================
def test_TC2_short_tp1_then_tp2(short_open_position, make_bar, eod_cutoff_ms):
    pos = short_open_position  # entry 25000, sl 25020, tp1 24970, tp2 24950

    bar1 = make_bar(BAR_1_MS, 25000, 25005, 24990, 24995)  # mae=-5, mfe=10
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.NO_TRANSITION
    assert r1.updated_position.mae_points == -5
    assert r1.updated_position.mfe_points == 10

    bar2 = make_bar(BAR_2_MS, 24995, 25000, 24965, 24970)  # low <= tp1 24970
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP1_HIT
    assert r2.updated_position.state == PositionFSMState.TP1_HIT
    assert r2.updated_position.effective_sl == 25000.0

    bar3 = make_bar(BAR_3_MS, 24970, 24975, 24945, 24950)  # low <= tp2 24950
    r3 = step(r2.updated_position, bar3, eod_cutoff_ms=eod_cutoff_ms)
    assert r3.transition == Transition.TP2_HIT
    assert r3.exit_reason == "TP2_HIT"
    assert r3.updated_position.final_pnl_points == 2 * 30 + 1 * 50  # 110 (mirror)


# ======================================================================
# TC3 — TP1 then BE_STOP (small win, LONG)
# ======================================================================
def test_TC3_long_tp1_then_be_stop(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)

    bar2 = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP1_HIT

    bar3 = make_bar(BAR_3_MS, 25030, 25040, 24999, 25010)  # low 24999 <= entry 25000
    r3 = step(r2.updated_position, bar3, eod_cutoff_ms=eod_cutoff_ms)
    assert r3.transition == Transition.BE_STOP
    assert r3.exit_reason == "BE_STOP"
    assert r3.updated_position.final_pnl_points == 2 * 30 + 0  # 60
    assert r3.updated_position.tp1_hit == 1


# ======================================================================
# TC4 — TP1 then EOD_TIMEOUT (partial win, LONG)
# ======================================================================
def test_TC4_long_tp1_then_eod_timeout(long_open_position, make_bar):
    pos = long_open_position
    eod = BAR_5_MS  # bar 5 IS the EOD bar

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod)

    bar2 = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)  # TP1
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod)
    assert r2.transition == Transition.TP1_HIT

    bar3 = make_bar(BAR_3_MS, 25030, 25045, 25025, 25040)
    r3 = step(r2.updated_position, bar3, eod_cutoff_ms=eod)
    assert r3.transition == Transition.NO_TRANSITION

    bar4 = make_bar(BAR_4_MS, 25040, 25048, 25030, 25042)
    r4 = step(r3.updated_position, bar4, eod_cutoff_ms=eod)
    assert r4.transition == Transition.NO_TRANSITION

    bar5 = make_bar(BAR_5_MS, 25040, 25045, 25035, 25040)  # no level hit, but EOD
    r5 = step(r4.updated_position, bar5, eod_cutoff_ms=eod)
    assert r5.transition == Transition.EOD_TIMEOUT
    assert r5.exit_reason == "EOD_TIMEOUT"
    # 2 contracts at TP1 (30 pts each) + 1 runner at bar.close 25040 (+40 pts)
    assert r5.updated_position.final_pnl_points == 2 * 30 + 1 * (25040 - 25000)  # 100
    assert r5.updated_position.tp1_hit == 1


# ======================================================================
# TC5 — SL before TP1 (full loss, LONG)
# ======================================================================
def test_TC5_long_sl_before_tp1(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24975, 24985)  # low <= SL 24980
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.SL_HIT
    assert r1.exit_reason == "SL_HIT"
    assert r1.updated_position.final_pnl_points == -3 * 20  # -60
    assert r1.updated_position.tp1_hit == 0


# ======================================================================
# TC6 — PF1 collision: TP1 + SL same bar -> SL_HIT wins (pessimistic)
# ======================================================================
def test_TC6_pf1_collision_sl_wins(long_open_position, make_bar, eod_cutoff_ms):
    """
    PF1 pessimistic-fill convention (§5.6, Waves1-5 Edit 1): when a single
    bar's range contains both SL and TP1, we cannot know intra-bar order,
    so the adverse fill is assumed to have happened first. Locked from day
    one — do not reconsider the first time a PF1 collision costs a
    hypothetical win. Rationale: protects Data Lab analysis from
    optimistic calibration error against real TopstepX execution.
    """
    pos = long_open_position
    bar1 = make_bar(BAR_1_MS, 25000, 25035, 24979, 25020)  # high>=TP1 AND low<=SL
    r = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r.transition == Transition.SL_HIT
    assert r.exit_reason == "SL_HIT"
    assert r.updated_position.final_pnl_points == -60
    assert r.updated_position.tp1_hit == 0  # never transitioned to TP1_HIT


# ======================================================================
# TC7 — Gap open beyond SL (LONG, PF3)
# ======================================================================
def test_TC7_long_gap_below_sl(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position
    # Open 24970 (already below SL 24980) — fill at open, not at SL.
    bar1 = make_bar(BAR_1_MS, 24970, 24990, 24965, 24985)
    r = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r.transition == Transition.SL_HIT
    assert r.exit_reason == "SL_HIT"
    # 3 * (24970 - 25000) == -90. Worse than the normal -60 fill; the gap rule
    # records the honest fill.
    assert r.updated_position.final_pnl_points == 3 * (24970 - 25000)


# ======================================================================
# TC8 — Gap open beyond TP2 after TP1 (LONG, PF3)
# ======================================================================
def test_TC8_long_gap_above_tp2_after_tp1(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25005, 25035, 25000, 25030)  # TP1
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.TP1_HIT

    # Bar 2 gaps to 25060 on open (above TP2 25050) — fill at open 25060.
    bar2 = make_bar(BAR_2_MS, 25060, 25065, 25058, 25062)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP2_HIT
    assert r2.exit_reason == "TP2_HIT"
    # 2 * 30 (TP1 contracts) + 1 * (25060 - 25000) == 120. Better than 110.
    assert r2.updated_position.final_pnl_points == 2 * 30 + 1 * 60


# ======================================================================
# TC9 — Entry bar excluded (PF4)
# ======================================================================
def test_TC9_entry_bar_excluded(long_open_position, make_bar, eod_cutoff_ms):
    """
    PF4: the entry bar is never resolved. Even a bar whose range contains
    TP1, TP2, AND SL must not produce a transition because it IS the bar
    the position opened on. Defensive guard for ENTRY/HEARTBEAT same-bar
    delivery (see §5.6, §6).
    """
    pos = long_open_position
    # Entry bar heartbeat: bar_close_ms == opened_at_ms, range covers everything.
    bar0 = Bar(bar_close_ms=OPENED_AT_MS, open=25000, high=25060, low=24970, close=25000)
    r = step(pos, bar0, eod_cutoff_ms=eod_cutoff_ms)
    assert r.transition == Transition.NO_TRANSITION
    assert r.updated_position.state == PositionFSMState.OPEN
    assert r.updated_position.mae_points == 0  # unchanged from initial zero
    assert r.updated_position.mfe_points == 0
    assert r.updated_position.tp1_hit == 0


# ======================================================================
# TC10 — MAE/MFE update on transition bar
# ======================================================================
def test_TC10_mae_updates_on_transition_bar(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25000, 25020, 24985, 25015)  # mae=-15, mfe=20
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.NO_TRANSITION
    assert r1.updated_position.mae_points == -15
    assert r1.updated_position.mfe_points == 20

    # Bar 2: bar range goes to 24982 (worse than -15 -> -18) AND hits TP1 25030.
    bar2 = make_bar(BAR_2_MS, 25015, 25035, 24982, 25030)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP1_HIT
    # Crucial: MAE was updated DESPITE the transition firing.
    assert r2.updated_position.mae_points == -18
    assert r2.updated_position.mfe_points == 35


# ======================================================================
# TC11 — EOD_TIMEOUT takes priority over level checks
# ======================================================================
def test_TC11_eod_priority_over_levels(long_open_position, make_bar):
    pos = long_open_position
    eod = BAR_2_MS

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod)

    # Bar 2 breaches TP2 AND SL AND is the EOD bar — EOD wins.
    bar2 = make_bar(BAR_2_MS, 25000, 25055, 24979, 25040)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod)
    assert r2.transition == Transition.EOD_TIMEOUT
    assert r2.exit_reason == "EOD_TIMEOUT"
    # 3 contracts close at bar.close 25040 → 3 * 40 = 120
    assert r2.updated_position.final_pnl_points == 3 * (25040 - 25000)
    assert r2.updated_position.tp1_hit == 0


# ======================================================================
# TC12 — Unknown position heartbeat (wrapper-level safe no-op)
# ======================================================================
def test_TC12_unknown_position_is_safe_noop(long_open_position, make_bar, eod_cutoff_ms):
    fsm_map = {1: long_open_position}
    bar = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)

    # signal_id 999 is not in fsm_map
    result = process_heartbeat(fsm_map, signal_id=999, bar=bar, eod_cutoff_ms=eod_cutoff_ms)
    assert result is None
    # fsm_map not mutated
    assert fsm_map == {1: long_open_position}


# ======================================================================
# TC13 — Idempotency: two identical step() calls return equal StepResults
# ======================================================================
def test_TC13_idempotent_step(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position
    bar = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)  # TP1-hitting bar

    r_a = step(pos, bar, eod_cutoff_ms=eod_cutoff_ms)
    r_b = step(pos, bar, eod_cutoff_ms=eod_cutoff_ms)

    assert r_a == r_b
    # Input position must not have been mutated
    assert pos.state == PositionFSMState.OPEN
    assert pos.tp1_hit == 0


# ======================================================================
# TC14 — Replay: earlier bar_close_ms is detected and skipped by wrapper
# ======================================================================
def test_TC14_replay_earlier_bar_is_skipped(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position
    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)

    bar2 = make_bar(BAR_2_MS, 25005, 25020, 25000, 25015)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    advanced = r2.updated_position
    assert advanced.last_heartbeat_bar_ms == BAR_2_MS

    # Replay bar1 — should be detected as replay and not reprocessed.
    assert should_process(advanced, bar1) is False
    fsm_map = {advanced.signal_id: advanced}
    replay_result = process_heartbeat(
        fsm_map, signal_id=advanced.signal_id, bar=bar1, eod_cutoff_ms=eod_cutoff_ms,
    )
    assert replay_result is None
    assert fsm_map[advanced.signal_id] == advanced  # unchanged


# ======================================================================
# TC15 — Multiple concurrent positions in one heartbeat
# ======================================================================
def test_TC15_concurrent_positions_independent(make_bar, eod_cutoff_ms):
    from backend.position_resolver import new_position
    pos_a = new_position(
        signal_id=1, direction=Direction.LONG, signal_type=1,
        opened_at_ms=OPENED_AT_MS, opened_at_ts=OPENED_AT_TS, **LONG_LEVELS,
    )
    # Position B entered later, different levels so that the shared bar only
    # breaches A's TP1, not B's.
    # B's levels sit outside the shared bar's range: sl 24995 below bar.low
    # 25000, tp1 25050 above bar.high 25035 — so the bar neither stops nor
    # advances B.
    pos_b = new_position(
        signal_id=2, direction=Direction.LONG, signal_type=2,
        opened_at_ms=OPENED_AT_MS,
        opened_at_ts=OPENED_AT_TS,
        entry_price=25020.0, sl=24995.0, tp1=25050.0, tp2=25070.0,
    )

    fsm_map = {1: pos_a, 2: pos_b}
    # Bar hits TP1 for A (25030) but not for B (TP1=25050).
    bar = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)

    r_a = process_heartbeat(fsm_map, signal_id=1, bar=bar, eod_cutoff_ms=eod_cutoff_ms)
    r_b = process_heartbeat(fsm_map, signal_id=2, bar=bar, eod_cutoff_ms=eod_cutoff_ms)

    assert r_a.transition == Transition.TP1_HIT
    assert r_b.transition == Transition.NO_TRANSITION
    # Isolation: resolving A did not mutate B
    assert r_b.updated_position.signal_id == 2
    assert r_b.updated_position.state == PositionFSMState.OPEN
    assert r_a.updated_position.signal_id == 1
    assert r_a.updated_position.state == PositionFSMState.TP1_HIT


# ======================================================================
# TC16 — PF2 collision: TP2 + BE same bar -> BE_STOP wins (pessimistic)
# ======================================================================
def test_TC16_pf2_collision_be_wins(long_open_position, make_bar, eod_cutoff_ms):
    """PF2 pessimistic convention — symmetric to PF1 but post-TP1."""
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25005, 25035, 25000, 25030)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.TP1_HIT

    # Bar 2: high>=TP2 AND low<=entry_price (BE stop). BE wins.
    bar2 = make_bar(BAR_2_MS, 25020, 25055, 24999, 25040)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.BE_STOP
    assert r2.exit_reason == "BE_STOP"
    assert r2.updated_position.final_pnl_points == 2 * 30 + 0  # 60


# ======================================================================
# TC17 — post_tp1_mae tracking
# ======================================================================
def test_TC17_post_tp1_mae_tracking(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)

    bar2 = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)  # TP1
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP1_HIT

    # Bar 3: runner pulls back to 25010 below tp1 25030 (drop 20), no BE, no TP2.
    bar3 = make_bar(BAR_3_MS, 25030, 25045, 25010, 25040)
    r3 = step(r2.updated_position, bar3, eod_cutoff_ms=eod_cutoff_ms)
    assert r3.transition == Transition.NO_TRANSITION
    assert r3.updated_position.post_tp1_mae_points == 20  # 25030 - 25010

    # Bar 4: shallower pullback (25020 -> drop of 10), then TP2 hits.
    bar4 = make_bar(BAR_4_MS, 25040, 25060, 25020, 25050)
    r4 = step(r3.updated_position, bar4, eod_cutoff_ms=eod_cutoff_ms)
    assert r4.transition == Transition.TP2_HIT
    # post_tp1_mae remains at the worst seen: 20, not 10.
    assert r4.updated_position.post_tp1_mae_points == 20


# ======================================================================
# TC18 — No-transition on a normal bar
# ======================================================================
def test_TC18_no_transition_sanity(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position
    bar1 = make_bar(BAR_1_MS, 25000, 25020, 24990, 25010)  # inside corridor
    r = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)

    assert r.transition == Transition.NO_TRANSITION
    assert r.updated_position.state == PositionFSMState.OPEN
    assert r.updated_position.mae_points == -10
    assert r.updated_position.mfe_points == 20
    assert r.updated_position.last_heartbeat_bar_ms == BAR_1_MS
    assert r.updated_position.heartbeats_processed == pos.heartbeats_processed + 1
    assert r.updated_position.tp1_hit == 0


# ======================================================================
# TC19 — Rehydration correctness (serialize -> deserialize -> continue)
# ======================================================================
def test_TC19_rehydration_correctness(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position

    bar1 = make_bar(BAR_1_MS, 25000, 25010, 24995, 25005)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)

    bar2 = make_bar(BAR_2_MS, 25005, 25035, 25000, 25030)  # TP1
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.TP1_HIT

    # Serialize, drop, rehydrate.
    serialized = dataclasses.asdict(r2.updated_position)
    rehydrated = PositionState(**serialized)
    assert rehydrated == r2.updated_position

    # Continue processing — bar 3 hits TP2.
    bar3 = make_bar(BAR_3_MS, 25030, 25055, 25025, 25050)
    r3 = step(rehydrated, bar3, eod_cutoff_ms=eod_cutoff_ms)
    assert r3.transition == Transition.TP2_HIT
    assert r3.updated_position.final_pnl_points == 2 * 30 + 1 * 50  # 110


# ======================================================================
# TC20 — Heartbeat on a CLOSED position raises
# ======================================================================
def test_TC20_resolver_rejects_closed_position(long_open_position, make_bar, eod_cutoff_ms):
    closed_pos = dataclasses.replace(
        long_open_position,
        state=PositionFSMState.CLOSED,
        exit_reason="SL_HIT",
        final_pnl_points=-60,
        closed_at_ms=BAR_1_MS,
    )
    bar = make_bar(BAR_2_MS, 25000, 25010, 24995, 25005)
    with pytest.raises(ResolverInvariantError):
        step(closed_pos, bar, eod_cutoff_ms=eod_cutoff_ms)


# ======================================================================
# TC21 — LONG/SHORT symmetry (SHORT mirror of a BE_STOP case)
# ======================================================================
def test_TC21_short_symmetry_be_stop(short_open_position, make_bar, eod_cutoff_ms):
    pos = short_open_position  # entry 25000, sl 25020, tp1 24970, tp2 24950

    bar1 = make_bar(BAR_1_MS, 25000, 25005, 24965, 24970)  # TP1 hit (low <= 24970)
    r1 = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r1.transition == Transition.TP1_HIT
    assert r1.updated_position.effective_sl == 25000.0  # BE at entry

    # SHORT BE stop: bar.high >= entry_price breaches BE.
    bar2 = make_bar(BAR_2_MS, 24970, 25001, 24960, 24990)
    r2 = step(r1.updated_position, bar2, eod_cutoff_ms=eod_cutoff_ms)
    assert r2.transition == Transition.BE_STOP
    assert r2.updated_position.final_pnl_points == 2 * 30 + 0  # 60


# ======================================================================
# TC22 — ResolverInvariantError on bad input
# ======================================================================
def test_TC22_invariant_errors(long_open_position, make_bar, eod_cutoff_ms):
    # 22a: CLOSED input
    closed = dataclasses.replace(long_open_position, state=PositionFSMState.CLOSED)
    with pytest.raises(ResolverInvariantError):
        step(closed, make_bar(BAR_1_MS, 25000, 25010, 24995, 25005),
             eod_cutoff_ms=eod_cutoff_ms)

    # 22b: effective_sl inconsistent with state (OPEN but effective_sl != sl) — INV-E
    inconsistent_open = dataclasses.replace(long_open_position, effective_sl=25000.0)
    with pytest.raises(ResolverInvariantError):
        step(inconsistent_open, make_bar(BAR_1_MS, 25000, 25010, 24995, 25005),
             eod_cutoff_ms=eod_cutoff_ms)

    # 22c: bar.bar_close_ms not a positive int
    with pytest.raises(ResolverInvariantError):
        step(long_open_position, Bar(bar_close_ms=0, open=1, high=1, low=1, close=1),
             eod_cutoff_ms=eod_cutoff_ms)


# ======================================================================
# TC23 — MAE/MFE start at zero; first post-entry bar updates correctly
# ======================================================================
def test_TC23_mae_mfe_initial_zero(long_open_position, make_bar, eod_cutoff_ms):
    pos = long_open_position
    assert pos.mae_points == 0
    assert pos.mfe_points == 0

    bar1 = make_bar(BAR_1_MS, 25000, 25012, 24988, 25010)  # mae=-12, mfe=12
    r = step(pos, bar1, eod_cutoff_ms=eod_cutoff_ms)
    assert r.transition == Transition.NO_TRANSITION
    assert r.updated_position.mae_points == -12
    assert r.updated_position.mfe_points == 12
    assert r.updated_position.mae_bar_ms == BAR_1_MS
    assert r.updated_position.mfe_bar_ms == BAR_1_MS
