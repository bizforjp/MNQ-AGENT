"""
Rehydration + EOD sweep + session-time helpers.

§4.9 — rehydration reads positions WHERE state != 3.
§8.4 — startup ordering: rehydrate -> EOD sweep -> gap check -> accept webhooks.
§8.7 — EOD timer uses a synthetic bar (OHLC = last_observed_close) for the
       16:00 ET close moment when a live EOD heartbeat never arrived.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.apply_resolver_result import (
    _insert_trade_outcome,
    _update_position_row,
)
from backend.position_resolver import (
    Bar,
    PositionFSMState,
    PositionState,
    Transition,
    step,
)
from backend.schema import BAR_INTERVAL_MS, SESSION_START_ET, SESSION_END_ET

ET = ZoneInfo("America/New_York")


# ----------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------
def et_to_ms(year, month, day, hour, minute) -> int:
    return int(datetime(year, month, day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def _ms_to_et(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ET)


def eod_cutoff_for_session_of(current_ms: int) -> int:
    """
    16:00 ET of the session date containing current_ms. If current_ms is
    already past 16:00 of its date, return that same date's 16:00 (the EOD
    that was missed).
    """
    et = _ms_to_et(current_ms)
    cutoff = et.replace(hour=SESSION_END_ET[0], minute=SESSION_END_ET[1],
                        second=0, microsecond=0)
    return int(cutoff.timestamp() * 1000)


def market_is_open(current_ms: int) -> bool:
    et = _ms_to_et(current_ms)
    if et.weekday() >= 5:  # Sat/Sun
        return False
    open_t = et.replace(hour=SESSION_START_ET[0], minute=SESSION_START_ET[1],
                        second=0, microsecond=0)
    close_t = et.replace(hour=SESSION_END_ET[0], minute=SESSION_END_ET[1],
                         second=0, microsecond=0)
    return open_t <= et < close_t


def session_adjusted_gap_bars(last_ms: int, current_ms: int,
                              bar_interval_ms: int = BAR_INTERVAL_MS) -> int:
    """
    Count 15-minute bars between last_ms and current_ms, excluding time
    that falls outside the RTH session window (9:30-16:00 ET, weekdays).
    Weekends are entirely outside the window. Simple minute-by-minute-style
    accumulation — fine for gaps of at most a day or two.
    """
    if current_ms <= last_ms:
        return 0

    # Walk forward from last_ms by bar_interval_ms, count each step that
    # starts inside the session window on a weekday.
    in_session_ms = 0
    cursor = last_ms
    step_ms = bar_interval_ms
    while cursor + step_ms <= current_ms:
        et = _ms_to_et(cursor + step_ms)
        if et.weekday() < 5:  # Mon-Fri
            open_t = et.replace(hour=SESSION_START_ET[0],
                                minute=SESSION_START_ET[1],
                                second=0, microsecond=0)
            close_t = et.replace(hour=SESSION_END_ET[0],
                                 minute=SESSION_END_ET[1],
                                 second=0, microsecond=0)
            if open_t < et <= close_t:
                in_session_ms += step_ms
        cursor += step_ms
    return in_session_ms // step_ms


# ----------------------------------------------------------------------
# Rehydration
# ----------------------------------------------------------------------
def rehydrate_positions(conn):
    """
    Load all non-closed positions into an in-memory fsm_map keyed by signal_id.
    """
    fsm_map = {}
    rows = conn.execute(
        """
        SELECT signal_id, bar_close_ms, direction, signal_type,
               entry_price, sl, tp1, tp2,
               opened_at_ms, opened_at_ts,
               state, tp1_hit, tp1_hit_bar_ms, effective_sl,
               mae_points, mae_bar_ms, mfe_points, mfe_bar_ms,
               post_tp1_mae_points,
               last_heartbeat_bar_ms, heartbeats_processed, last_observed_close,
               closed_at_ms, exit_reason, final_pnl_points
        FROM positions
        WHERE state != 3
        ORDER BY opened_at_ms ASC
        """
    ).fetchall()
    for row in rows:
        (signal_id, bar_close_ms, direction, signal_type,
         entry_price, sl, tp1, tp2,
         opened_at_ms, opened_at_ts,
         state, tp1_hit, tp1_hit_bar_ms, effective_sl,
         mae_points, mae_bar_ms, mfe_points, mfe_bar_ms,
         post_tp1_mae_points,
         last_heartbeat_bar_ms, heartbeats_processed, last_observed_close,
         closed_at_ms, exit_reason, final_pnl_points) = row
        pos = PositionState(
            signal_id=signal_id,
            direction=int(direction),
            signal_type=int(signal_type),
            entry_price=entry_price, sl=sl, tp1=tp1, tp2=tp2,
            opened_at_ms=opened_at_ms, opened_at_ts=opened_at_ts or "",
            state=int(state),
            tp1_hit=int(tp1_hit),
            tp1_hit_bar_ms=tp1_hit_bar_ms,
            effective_sl=effective_sl,
            mae_points=mae_points or 0.0, mae_bar_ms=mae_bar_ms or 0,
            mfe_points=mfe_points or 0.0, mfe_bar_ms=mfe_bar_ms or 0,
            post_tp1_mae_points=post_tp1_mae_points or 0.0,
            last_heartbeat_bar_ms=last_heartbeat_bar_ms or 0,
            heartbeats_processed=heartbeats_processed or 0,
            last_observed_close=last_observed_close or 0.0,
            closed_at_ms=closed_at_ms,
            exit_reason=exit_reason,
            final_pnl_points=final_pnl_points,
        )
        fsm_map[signal_id] = pos
    return fsm_map


# ----------------------------------------------------------------------
# EOD sweep (§8.7)
# ----------------------------------------------------------------------
def eod_sweep(fsm_map, conn, current_ms,
              *, post_embed=None, log=None):
    """
    For each open position whose session has ended before current_ms, close
    it via synthetic EOD bar at 16:00 ET of that position's session date.

    Uses direct DB write (not apply_resolver_result) because the EOD transition
    is manufactured, not driven by a real bar arrival — the spec allows the
    synthetic bar to route through the resolver, which is what we do: we
    construct a synthetic Bar with OHLC = last_observed_close and let the
    resolver's EOD branch fire.
    """
    to_close = []
    zombies = []
    for sid, pos in list(fsm_map.items()):
        if pos.state == PositionFSMState.CLOSED:
            continue
        # Zombie guard: positions with opened_at_ms <= 0 came from an
        # unresolved {{plot}} template (bar_close_ms=0). The session
        # cutoff for epoch/pre-epoch dates is negative, which crashes
        # the resolver invariant check. Close these directly as
        # GAP_CLEAN without going through the resolver.
        if pos.opened_at_ms <= 0:
            zombies.append((sid, pos))
            continue
        # 16:00 ET of the session date the position was opened on.
        session_cutoff = eod_cutoff_for_session_of(pos.opened_at_ms)
        if current_ms > session_cutoff:
            to_close.append((sid, pos, session_cutoff))

    from backend.apply_resolver_result import apply_resolver_result
    from backend.gap_recovery import close_gap_clean

    # Close zombie positions (bar_close_ms=0) as GAP_CLEAN.
    for sid, pos in zombies:
        print(f"⚠️ [ZOMBIE] signal_id={sid} opened_at_ms={pos.opened_at_ms} "
              "— closing as GAP_CLEAN (invalid bar_close_ms)")
        close_gap_clean(
            pos, exit_bar_ms=current_ms,
            reason="ZOMBIE_INVALID_BAR_CLOSE_MS",
            fsm_map=fsm_map, conn=conn,
            post_embed=post_embed, log=log,
        )

    for sid, pos, session_cutoff in to_close:
        price = pos.last_observed_close if pos.last_observed_close else pos.entry_price
        synth = Bar(
            bar_close_ms=session_cutoff,
            open=price, high=price, low=price, close=price,
        )
        # Force the resolver to see this as an EOD bar: eod_cutoff_ms equals
        # the bar's bar_close_ms so §5.5 step 4 fires.
        result = step(pos, synth, eod_cutoff_ms=session_cutoff)
        apply_resolver_result(
            pos, synth, result, fsm_map, conn,
            post_embed=post_embed, log=log,
        )
        if log:
            log("EOD_SWEEP_CLOSED",
                signal_id=sid,
                session_cutoff_ms=session_cutoff,
                exit_price=price)
