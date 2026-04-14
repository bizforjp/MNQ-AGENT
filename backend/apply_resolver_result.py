"""
apply_resolver_result — the wrapper that turns a pure StepResult into side
effects. See §5.10 for commit ordering (CO1-CO5):

  CO1  DB commit precedes in-memory update.
  CO2  Terminal transitions commit positions + trade_outcomes in ONE tx.
  CO3  Memory map is updated only after the tx commits cleanly.
  CO4  Discord post happens only after DB + memory are consistent.
  CO5  Logging is last — a failed log never undoes anything else.
"""
from dataclasses import replace
from datetime import datetime, timezone

from backend.position_resolver import (
    PositionFSMState,
    Transition,
)

TERMINAL_TRANSITIONS = frozenset({
    Transition.TP2_HIT, Transition.SL_HIT,
    Transition.BE_STOP, Transition.EOD_TIMEOUT,
})


def _fmt_ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _minutes_between(start_ms, end_ms):
    if start_ms is None or end_ms is None:
        return None
    return int((end_ms - start_ms) / 60_000)


# -------------------- DB inserts --------------------
def insert_signal_row(conn, position, timestamp_iso):
    """
    Minimal row on signals_v3 so that positions / trade_outcomes FK constraint
    resolves. Populates level fields from the position; other cognition fields
    are left at 0 / NULL because they are measurement-side and the resolver's
    tests only exercise position-lifecycle columns.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO signals_v3 (
            signal_id, timestamp, signal, signal_type,
            entry_price, sl, tp1, tp2, stop_pts,
            bar_close_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position.signal_id,
            timestamp_iso,
            "LONG" if position.direction == 1 else "SHORT",
            "TREND" if position.signal_type == 1 else "SQUEEZE",
            position.entry_price,
            position.sl,
            position.tp1,
            position.tp2,
            abs(position.sl - position.entry_price),
            position.bar_close_ms if hasattr(position, "bar_close_ms") else position.opened_at_ms,
        ),
    )
    conn.commit()


def insert_position_row(conn, position):
    conn.execute(
        """
        INSERT OR REPLACE INTO positions (
            signal_id, bar_close_ms, direction, signal_type,
            entry_price, sl, tp1, tp2,
            opened_at_ms, opened_at_ts,
            state, tp1_hit, tp1_hit_bar_ms, effective_sl,
            mae_points, mae_bar_ms, mfe_points, mfe_bar_ms,
            post_tp1_mae_points,
            last_heartbeat_bar_ms, heartbeats_processed, last_observed_close,
            closed_at_ms, closed_at_ts, exit_reason, final_pnl_points
        ) VALUES (?, ?, ?, ?,   ?, ?, ?, ?,   ?, ?,
                  ?, ?, ?, ?,   ?, ?, ?, ?,   ?,
                  ?, ?, ?,   ?, ?, ?, ?)
        """,
        (
            position.signal_id,
            position.opened_at_ms,  # bar_close_ms == opened_at_ms by INV-F
            int(position.direction),
            int(position.signal_type),
            position.entry_price, position.sl, position.tp1, position.tp2,
            position.opened_at_ms, position.opened_at_ts,
            int(position.state),
            int(position.tp1_hit), position.tp1_hit_bar_ms, position.effective_sl,
            position.mae_points, position.mae_bar_ms,
            position.mfe_points, position.mfe_bar_ms,
            position.post_tp1_mae_points,
            position.last_heartbeat_bar_ms,
            position.heartbeats_processed,
            position.last_observed_close,
            position.closed_at_ms,
            None,  # closed_at_ts — filled on close
            position.exit_reason, position.final_pnl_points,
        ),
    )
    conn.commit()


def _update_position_row(conn, position):
    conn.execute(
        """
        UPDATE positions SET
            state=?, tp1_hit=?, tp1_hit_bar_ms=?, effective_sl=?,
            mae_points=?, mae_bar_ms=?, mfe_points=?, mfe_bar_ms=?,
            post_tp1_mae_points=?,
            last_heartbeat_bar_ms=?, heartbeats_processed=?, last_observed_close=?,
            closed_at_ms=?, closed_at_ts=?, exit_reason=?, final_pnl_points=?
        WHERE signal_id=?
        """,
        (
            int(position.state),
            int(position.tp1_hit), position.tp1_hit_bar_ms, position.effective_sl,
            position.mae_points, position.mae_bar_ms,
            position.mfe_points, position.mfe_bar_ms,
            position.post_tp1_mae_points,
            position.last_heartbeat_bar_ms, position.heartbeats_processed,
            position.last_observed_close,
            position.closed_at_ms,
            _fmt_ms_to_iso(position.closed_at_ms),
            position.exit_reason, position.final_pnl_points,
            position.signal_id,
        ),
    )


def _insert_trade_outcome(conn, closed_position, transition):
    opened_iso = closed_position.opened_at_ts or _fmt_ms_to_iso(closed_position.opened_at_ms)
    closed_iso = _fmt_ms_to_iso(closed_position.closed_at_ms)
    conn.execute(
        """
        INSERT INTO trade_outcomes (
            signal_id, timestamp_opened, timestamp_closed,
            tp1_hit, tp1_hit_time,
            tp2_hit, tp2_hit_time,
            sl_hit,  sl_hit_time,
            be_stop_hit, be_stop_hit_time,
            exit_reason, final_pnl_points,
            mae_points, mfe_points, mae_time_min, mfe_time_min,
            post_tp1_mae_points, time_in_trade_min,
            is_ghost
        ) VALUES (?, ?, ?,  ?, ?,  ?, ?,  ?, ?,  ?, ?,
                  ?, ?,  ?, ?, ?, ?,  ?, ?,  0)
        """,
        (
            closed_position.signal_id,
            opened_iso, closed_iso,
            1 if closed_position.tp1_hit else 0,
            _fmt_ms_to_iso(closed_position.tp1_hit_bar_ms)
                if closed_position.tp1_hit else None,
            1 if transition == Transition.TP2_HIT else 0,
            closed_iso if transition == Transition.TP2_HIT else None,
            1 if closed_position.exit_reason in ("SL_HIT",) else 0,
            closed_iso if closed_position.exit_reason == "SL_HIT" else None,
            1 if transition == Transition.BE_STOP else 0,
            closed_iso if transition == Transition.BE_STOP else None,
            closed_position.exit_reason, closed_position.final_pnl_points,
            closed_position.mae_points, closed_position.mfe_points,
            _minutes_between(closed_position.opened_at_ms, closed_position.mae_bar_ms),
            _minutes_between(closed_position.opened_at_ms, closed_position.mfe_bar_ms),
            closed_position.post_tp1_mae_points,
            _minutes_between(closed_position.opened_at_ms, closed_position.closed_at_ms),
        ),
    )


# -------------------- The wrapper --------------------
def apply_resolver_result(position, bar, result, fsm_map, conn,
                          *, post_embed=None, log=None):
    """
    DB first, then memory, then Discord, then log (CO1-CO5).
    """
    updated = result.updated_position
    transition = result.transition

    # --- CO1 + CO2: DB write inside a single transaction ---
    try:
        if transition in TERMINAL_TRANSITIONS:
            # finalize closed_at_ms/exit_reason/pnl are already on updated from
            # the resolver's close builder; we also need to set closed_at_ts.
            closed = replace(updated, state=PositionFSMState.CLOSED)
            with conn:
                _update_position_row(conn, closed)
                _insert_trade_outcome(conn, closed, transition)
            committed = closed
        else:
            with conn:
                _update_position_row(conn, updated)
            committed = updated
    except Exception:
        if log:
            log("DB_COMMIT_FAILED", signal_id=position.signal_id)
        raise

    # --- CO3: memory ---
    if transition in TERMINAL_TRANSITIONS:
        fsm_map.pop(committed.signal_id, None)
    else:
        fsm_map[committed.signal_id] = committed

    # --- CO4: Discord ---
    if transition in TERMINAL_TRANSITIONS and post_embed is not None:
        post_embed({
            "signal_id": committed.signal_id,
            "exit_reason": committed.exit_reason,
            "final_pnl_points": committed.final_pnl_points,
        })

    # --- CO5: logging ---
    if log:
        log(
            "RESOLVER_STEP",
            signal_id=committed.signal_id,
            transition=str(transition.value),
            bar_close_ms=bar.bar_close_ms,
        )
