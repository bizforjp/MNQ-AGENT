"""
Staleness sweep — §8.5, plus Revision Package Wave 2 Edit 6 (distinct tags).

Policy:
  - Iterate fsm_map for positions whose session-adjusted gap since
    last_heartbeat_bar_ms is >= threshold_bars.
  - Route them through invoke_gap_recovery with trigger="staleness".
  - Emit one of two distinct log tags so slot-overflow cases are greppable
    independently of mid-position delivery failures:
      [STALENESS_NEVER_HEARTBEATED]  heartbeats_processed == 0
      [STALENESS_GAP]                heartbeats_processed > 0

Both cases still close via GAP_CLEAN per GR1 — the trade_outcomes row's
exit_reason is the literal "GAP_CLEAN" in either case; the distinction lives
only in the log tag.
"""
from backend.gap_recovery import invoke_gap_recovery
from backend.rehydrate import session_adjusted_gap_bars
from backend.schema import BAR_INTERVAL_MS


def staleness_sweep(fsm_map, conn, current_ms,
                    *, finnhub, eod_cutoff_ms,
                    threshold_bars=2, bar_interval_ms=BAR_INTERVAL_MS,
                    post_embed=None, log=None):
    """Run one pass of the staleness check. Returns list of signal_ids routed."""
    routed = []
    for sid, pos in list(fsm_map.items()):
        last = pos.last_heartbeat_bar_ms or pos.opened_at_ms
        gap_bars = session_adjusted_gap_bars(last, current_ms, bar_interval_ms)
        if gap_bars < threshold_bars:
            continue

        # Distinct logging tag per failure mode.
        if log:
            if pos.heartbeats_processed == 0:
                log("STALENESS_NEVER_HEARTBEATED",
                    signal_id=sid,
                    bar_close_ms=pos.opened_at_ms,
                    gap_bars=gap_bars)
            else:
                log("STALENESS_GAP",
                    signal_id=sid,
                    last_heartbeat_bar_ms=pos.last_heartbeat_bar_ms,
                    heartbeats_processed=pos.heartbeats_processed,
                    gap_bars=gap_bars)

        invoke_gap_recovery(
            position=pos,
            last_seen_ms=last,
            current_bar_ms=current_ms,
            trigger="staleness",
            fsm_map=fsm_map, conn=conn,
            finnhub=finnhub, eod_cutoff_ms=eod_cutoff_ms,
            post_embed=post_embed, log=log,
        )
        routed.append(sid)
    return routed
