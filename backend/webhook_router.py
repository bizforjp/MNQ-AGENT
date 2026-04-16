"""
Webhook router — tolerance dispatcher + heartbeat splitter.

Per §10.3 Step 1 tolerance mode, the backend accepts both payload shapes
during the migration window:
  - v2.1.1 payloads (no bar_close_ms / parent_bar_close_ms) -> legacy tables
  - v3.0 payloads   (bar_close_ms populated)                -> v3 tables

Per §8.6 + §5.9, a live heartbeat whose bar_close_ms is more than one bar
past the position's last_heartbeat_bar_ms triggers gap recovery; recovery
fills the missed window, then the live bar is processed. Replays (bars at
or before last_heartbeat_bar_ms) are rejected with a REPLAY log line and
the resolver is NOT called.
"""
import json

from backend.apply_resolver_result import (
    apply_resolver_result, insert_position_row, insert_signal_row,
)
from backend.gap_recovery import invoke_gap_recovery
from backend.position_resolver import (
    Direction, PositionFSMState, new_position, step,
)
from backend.schema import BAR_INTERVAL_MS


# ----------------------------------------------------------------------
# ENTRY
# ----------------------------------------------------------------------
def route_entry(payload, *, fsm_map, conn, log=None):
    """
    ENTRY writes go to signals_v3 only. Returns the new signal_id.
    """
    bar_close_ms = payload.get("bar_close_ms")
    try:
        bar_close_ms_int = int(float(bar_close_ms))
    except (TypeError, ValueError):
        if log:
            log("SCHEMA", message="invalid bar_close_ms on v3 ENTRY",
                value=repr(bar_close_ms))
        return None

    direction = int(payload.get("sig_dir", 1))
    signal_type = int(payload.get("sig_type", 1))

    # Insert signals_v3 row first (FK target for positions).
    cur = conn.execute(
        """
        INSERT INTO signals_v3 (
            timestamp, signal, signal_type,
            entry_price, sl, tp1, tp2, stop_pts,
            bar_close_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("timestamp", ""),
            "LONG" if direction == Direction.LONG else "SHORT",
            "TREND" if signal_type == 1 else "SQUEEZE",
            float(payload.get("entry_price", 0)),
            float(payload.get("sl", 0)),
            float(payload.get("tp1", 0)),
            float(payload.get("tp2", 0)),
            abs(float(payload.get("sl", 0)) - float(payload.get("entry_price", 0))),
            bar_close_ms_int,
        ),
    )
    signal_id = cur.lastrowid

    pos = new_position(
        signal_id=signal_id,
        direction=direction,
        signal_type=signal_type,
        entry_price=float(payload.get("entry_price", 0)),
        sl=float(payload.get("sl", 0)),
        tp1=float(payload.get("tp1", 0)),
        tp2=float(payload.get("tp2", 0)),
        opened_at_ms=bar_close_ms_int,
        opened_at_ts=payload.get("opened_at_ts", payload.get("timestamp", "")),
    )
    insert_position_row(conn, pos)
    conn.commit()
    fsm_map[signal_id] = pos
    if log:
        log("ENTRY_V3", signal_id=signal_id, bar_close_ms=bar_close_ms_int)
    return signal_id


# ----------------------------------------------------------------------
# EVAL
# ----------------------------------------------------------------------
def route_eval(payload, *, conn, log=None):
    """Presence of `parent_bar_close_ms` picks v3; absence picks legacy."""
    parent_bar_close_ms = payload.get("parent_bar_close_ms")
    if parent_bar_close_ms in (None, 0, "", "0"):
        return _route_eval_legacy(payload, conn=conn, log=log)
    return _route_eval_v3(payload, parent_bar_close_ms, conn=conn, log=log)


def _route_eval_legacy(payload, *, conn, log=None):
    conn.execute(
        """
        INSERT INTO eval_results (
            timestamp, signal, signal_type, result, payload_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            payload.get("timestamp", ""),
            "LONG" if int(payload.get("sig_dir", 1)) == 1 else "SHORT",
            "TREND" if int(payload.get("sig_type", 1)) == 1 else "SQUEEZE",
            payload.get("result", "UNKNOWN"),
            json.dumps(payload),
        ),
    )
    conn.commit()
    if log:
        log("EVAL_LEGACY", timestamp=payload.get("timestamp", ""))


def _route_eval_v3(payload, parent_bar_close_ms, *, conn, log=None):
    parent_ms = int(float(parent_bar_close_ms))
    row = conn.execute(
        "SELECT signal_id FROM signals_v3 WHERE bar_close_ms = ? LIMIT 1",
        (parent_ms,),
    ).fetchone()
    if row is None:
        if log:
            log("ORPHAN", parent_bar_close_ms=parent_ms)
        return None
    signal_id = row[0]
    conn.execute(
        """
        INSERT INTO evaluations (
            signal_id, timestamp,
            ft_target, ft_high, ft_low, ft_actual_price,
            move_points, result,
            state_before, state_after, stops_after, lockout_bars,
            is_ghost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            payload.get("timestamp", ""),
            float(payload.get("ft_target", 0)),
            float(payload.get("ft_high", 0)),
            float(payload.get("ft_low", 0)),
            float(payload.get("ft_actual_price", 0)),
            float(payload.get("move_points", 0)),
            payload.get("result", "UNKNOWN"),
            payload.get("state_before", "ELIGIBLE"),
            payload.get("state_after", "ELIGIBLE"),
            int(payload.get("stops_after", 0)),
            int(payload.get("lockout_bars", 0)),
            int(payload.get("is_ghost", 0)),
        ),
    )
    conn.commit()
    if log:
        log("EVAL_V3", signal_id=signal_id, parent_bar_close_ms=parent_ms)
    return signal_id


# ----------------------------------------------------------------------
# HEARTBEAT (per-position)
# ----------------------------------------------------------------------
def route_heartbeat_for_position(*, signal_id, bar, fsm_map, conn, finnhub,
                                 eod_cutoff_ms, post_embed=None, log=None):
    """
    Process one slot of a heartbeat for one open position.

    Returns a StepResult on successful resolver call, or None on
    unknown-position / replay / gap-recovery-that-closed-the-position.

    Gap handling (§5.9, §8.3):
      bar == last              -> REPLAY/DUPLICATE, skip
      bar <  last              -> REPLAY, skip
      bar == last + interval   -> normal path (one bar progress)
      bar >  last + interval   -> gap: recover [last+interval .. bar-interval]
                                  then process the live `bar` via step().
    """
    position = fsm_map.get(signal_id)
    if position is None:
        if log:
            log("HEARTBEAT_UNKNOWN_POSITION",
                signal_id=signal_id, bar_close_ms=bar.bar_close_ms)
        return None

    if bar.bar_close_ms <= position.last_heartbeat_bar_ms:
        if log:
            log("REPLAY",
                signal_id=signal_id,
                bar_close_ms=bar.bar_close_ms,
                last_heartbeat_bar_ms=position.last_heartbeat_bar_ms)
        return None

    expected = position.last_heartbeat_bar_ms + BAR_INTERVAL_MS
    if bar.bar_close_ms > expected:
        # Gap: replay closes at bar - interval (exclusive of live bar).
        invoke_gap_recovery(
            position=position,
            last_seen_ms=position.last_heartbeat_bar_ms,
            current_bar_ms=bar.bar_close_ms - BAR_INTERVAL_MS,
            trigger="heartbeat_gap",
            fsm_map=fsm_map, conn=conn, finnhub=finnhub,
            eod_cutoff_ms=eod_cutoff_ms,
            post_embed=post_embed, log=log,
        )
        # Recovery may have closed the position.
        position = fsm_map.get(signal_id)
        if position is None:
            return None
        # Recovery may have hit the target on a mid-replay bar and left the
        # position CLOSED — defensive guard.
        if position.state == PositionFSMState.CLOSED:
            return None

    # Normal live step.
    result = step(position, bar, eod_cutoff_ms=eod_cutoff_ms)
    apply_resolver_result(position, bar, result, fsm_map, conn,
                          post_embed=post_embed, log=log)
    return result
