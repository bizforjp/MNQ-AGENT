"""
Gap recovery — §8.6 / §8.8.

Decision tree (summarised from §8.6):
  gap_bars == 0                                  -> no-op
  gap_bars > MAX_REPLAY_BARS                     -> close GAP_CLEAN, GAP_EXCEEDS_MAX
  Finnhub raises, heartbeat_gap + gap_bars <= 2  -> fail-soft (position lives)
  Finnhub raises, otherwise                      -> close GAP_CLEAN, FINNHUB_UNAVAILABLE
  reconcile fails                                -> close GAP_CLEAN, RECONCILE_FAIL
  replay feeds resolver;
  if a replayed bar closes pos                   -> apply_resolver_result writes outcome
  replay completes still-open                    -> continue (position lives on)

Fail-soft is gated on trigger=="heartbeat_gap" because the heartbeat path has
a live bar queued up in route_heartbeat_for_position that will call step() and
advance last_heartbeat_bar_ms. The staleness path has no following live bar,
so failing soft there would leak a stuck OPEN position. Motivation: TradingView
occasionally drops a single heartbeat; killing the position when Finnhub is
also unavailable is worse than accepting the MAE/MFE blind spot for one bar.

All GAP_CLEAN exit_reasons on trade_outcomes are the literal string "GAP_CLEAN"
(GR1). The specific cause lives only in the log line.
"""
from dataclasses import replace
from datetime import datetime, timezone
from typing import List, Optional

from backend.apply_resolver_result import (
    TERMINAL_TRANSITIONS,
    _fmt_ms_to_iso,
    _insert_trade_outcome,
    _update_position_row,
    apply_resolver_result,
)
from backend.finnhub_adapter import FinnhubError
from backend.position_resolver import (
    Bar,
    Direction,
    PositionFSMState,
    Transition,
    step,
)
from backend.schema import BAR_INTERVAL_MS, MAX_REPLAY_BARS

SMALL_GAP_FAIL_SOFT_BARS = 2


# ----------------------------------------------------------------------
def reconcile_finnhub_bars(
    bars: List[Bar], expected_start_ms: int,
    bar_interval_ms: int = BAR_INTERVAL_MS,
) -> Optional[List[Bar]]:
    """
    Return bars if every bar_close_ms lands exactly on the expected 15-minute
    grid offset from expected_start_ms; else None.
    """
    if not bars:
        return None
    for i, bar in enumerate(bars):
        expected = expected_start_ms + i * bar_interval_ms
        if bar.bar_close_ms != expected:
            return None
    return bars


# ----------------------------------------------------------------------
def close_gap_clean(position, exit_bar_ms, reason,
                    *, fsm_map, conn, post_embed=None, log=None):
    """
    Defensive close. exit_reason on trade_outcomes is always the literal
    "GAP_CLEAN"; the `reason` arg (GAP_EXCEEDS_MAX, FINNHUB_UNAVAILABLE,
    RECONCILE_FAIL) is recorded only in the log.
    """
    # Decide exit price.
    if (position.last_heartbeat_bar_ms
            and position.last_heartbeat_bar_ms >= position.opened_at_ms
            and position.last_observed_close):
        exit_price = position.last_observed_close
    else:
        exit_price = position.entry_price

    dir_sign = 1 if position.direction == Direction.LONG else -1
    if position.state == PositionFSMState.OPEN:
        final_pnl = 3 * (exit_price - position.entry_price) * dir_sign
    elif position.state == PositionFSMState.TP1_HIT:
        tp1_pts = abs(position.tp1 - position.entry_price)
        runner_pts = (exit_price - position.entry_price) * dir_sign
        final_pnl = 2 * tp1_pts + 1 * runner_pts
    else:
        # Already CLOSED — nothing to do (defensive).
        return

    closed = replace(
        position,
        state=PositionFSMState.CLOSED,
        closed_at_ms=exit_bar_ms,
        exit_reason="GAP_CLEAN",
        final_pnl_points=final_pnl,
    )

    with conn:
        _update_position_row(conn, closed)
        _insert_trade_outcome(conn, closed, Transition.NO_TRANSITION)
        # Patch the exit_reason on trade_outcomes to GAP_CLEAN (the helper
        # used Transition.NO_TRANSITION to skip the TP/SL/BE flags but writes
        # exit_reason from closed.exit_reason which is already GAP_CLEAN).

    fsm_map.pop(position.signal_id, None)

    if post_embed:
        post_embed({
            "signal_id": position.signal_id,
            "exit_reason": "GAP_CLEAN",
            "gap_clean_reason": reason,
            "final_pnl_points": final_pnl,
        })
    if log:
        log(reason, signal_id=position.signal_id, exit_bar_ms=exit_bar_ms)
        log("GAP_CLEAN", signal_id=position.signal_id, reason=reason)


# ----------------------------------------------------------------------
def invoke_gap_recovery(position, last_seen_ms, current_bar_ms, trigger,
                        *, fsm_map, conn, finnhub, eod_cutoff_ms,
                        post_embed=None, log=None):
    gap_bars = (current_bar_ms - last_seen_ms) // BAR_INTERVAL_MS
    if gap_bars <= 0:
        return

    if log:
        log("GAP_RECOVERY_INVOKED",
            signal_id=position.signal_id,
            gap_bars=gap_bars, trigger=trigger)

    # -- 1. Too far behind: defensive close, no Finnhub call --
    if gap_bars > MAX_REPLAY_BARS:
        close_gap_clean(
            position, exit_bar_ms=last_seen_ms,
            reason="GAP_EXCEEDS_MAX",
            fsm_map=fsm_map, conn=conn,
            post_embed=post_embed, log=log,
        )
        return

    # -- 2. Try Finnhub --
    try:
        bars = finnhub.fetch_bars(
            symbol="MNQ",
            start_ms=last_seen_ms + BAR_INTERVAL_MS,
            end_ms=current_bar_ms,
            interval="15m",
        )
    except FinnhubError:
        # Fail-soft only on the heartbeat path: a live bar is about to call
        # step() and advance last_heartbeat_bar_ms. The staleness path has no
        # live bar following — failing soft there would leak a stuck OPEN
        # position that re-trips on every sweep.
        if (trigger == "heartbeat_gap"
                and gap_bars <= SMALL_GAP_FAIL_SOFT_BARS):
            print(
                f"⚠️ [GAP_RECOVERY] signal_id={position.signal_id} "
                f"FINNHUB_UNAVAILABLE on {gap_bars}-bar gap "
                f"(<= {SMALL_GAP_FAIL_SOFT_BARS}) — failing soft, "
                f"position remains OPEN, live bar will advance state"
            )
            if log:
                log("FINNHUB_UNAVAILABLE_FAIL_SOFT",
                    signal_id=position.signal_id,
                    gap_bars=gap_bars,
                    last_seen_ms=last_seen_ms,
                    trigger=trigger)
            return
        close_gap_clean(
            position, exit_bar_ms=last_seen_ms,
            reason="FINNHUB_UNAVAILABLE",
            fsm_map=fsm_map, conn=conn,
            post_embed=post_embed, log=log,
        )
        return

    # -- 3. Reconcile --
    reconciled = reconcile_finnhub_bars(
        bars, expected_start_ms=last_seen_ms + BAR_INTERVAL_MS,
    )
    if reconciled is None:
        close_gap_clean(
            position, exit_bar_ms=last_seen_ms,
            reason="RECONCILE_FAIL",
            fsm_map=fsm_map, conn=conn,
            post_embed=post_embed, log=log,
        )
        return

    # -- 4. Replay --
    for bar in reconciled:
        current = fsm_map.get(position.signal_id)
        if current is None:
            # Closed mid-replay on the previous bar.
            return
        result = step(current, bar, eod_cutoff_ms=eod_cutoff_ms)
        apply_resolver_result(
            current, bar, result, fsm_map, conn,
            post_embed=post_embed, log=log,
        )
        if result.transition in TERMINAL_TRANSITIONS:
            if log:
                log("REPLAY_CLOSED_MID",
                    signal_id=position.signal_id,
                    at_bar_ms=bar.bar_close_ms,
                    reason=str(result.transition.value))
            return

    if log:
        log("REPLAY_COMPLETE",
            signal_id=position.signal_id,
            bars=len(reconciled))
