"""
Position resolver FSM — MONA v3.0 Option C, §5 of the spec.

Pure function `step(position, bar, *, eod_cutoff_ms) -> StepResult`.
No side effects, no DB, no globals. The wrapper that commits to disk and
posts embeds lives in the backend's webhook handler (see §5.10); this
module only owns the FSM decision logic.

Behavioral order (§5.5):
  1. Invariant guard (state must be OPEN or TP1_HIT)
  2. Entry-bar exclusion (PF4 — §5.6)
  3. MAE/MFE/post_tp1_mae always update
  4. EOD timeout check (§5.8) — wins over level breaches
  5. Gap-open check (PF3 — §5.6)
  6. Intra-bar level check with pessimistic collision rules (PF1/PF2)
  7. Default NO_TRANSITION
"""
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from typing import Optional, Tuple


# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------
class Direction(IntEnum):
    LONG = 1
    SHORT = 2


class PositionFSMState(IntEnum):
    OPEN = 1
    TP1_HIT = 2
    CLOSED = 3


class Transition(str, Enum):
    NO_TRANSITION = "NO_TRANSITION"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    SL_HIT = "SL_HIT"
    BE_STOP = "BE_STOP"
    EOD_TIMEOUT = "EOD_TIMEOUT"


TERMINAL_TRANSITIONS = frozenset({
    Transition.TP2_HIT,
    Transition.SL_HIT,
    Transition.BE_STOP,
    Transition.EOD_TIMEOUT,
})


class ResolverInvariantError(Exception):
    pass


# ------------------------------------------------------------------
# Dataclasses
# ------------------------------------------------------------------
@dataclass(frozen=True)
class Bar:
    bar_close_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class PositionState:
    signal_id: int = 0
    direction: int = 0
    signal_type: int = 0
    entry_price: float = 0.0
    sl: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    opened_at_ms: int = 0
    opened_at_ts: str = ""
    state: int = PositionFSMState.OPEN
    effective_sl: float = 0.0
    tp1_hit: int = 0
    tp1_hit_bar_ms: Optional[int] = None
    mae_points: float = 0.0
    mae_bar_ms: int = 0
    mfe_points: float = 0.0
    mfe_bar_ms: int = 0
    post_tp1_mae_points: float = 0.0
    last_heartbeat_bar_ms: int = 0
    heartbeats_processed: int = 0
    last_observed_close: float = 0.0
    closed_at_ms: Optional[int] = None
    exit_reason: Optional[str] = None
    final_pnl_points: Optional[float] = None


@dataclass(frozen=True)
class StepResult:
    updated_position: PositionState
    transition: Transition
    exit_reason: Optional[str] = None
    notes: Tuple[str, ...] = ()


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------
def new_position(*, signal_id, direction, signal_type, entry_price, sl,
                 tp1, tp2, opened_at_ms, opened_at_ts="", **extra):
    return PositionState(
        signal_id=signal_id,
        direction=int(direction),
        signal_type=signal_type,
        entry_price=entry_price,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        opened_at_ms=opened_at_ms,
        opened_at_ts=opened_at_ts,
        state=PositionFSMState.OPEN,
        effective_sl=sl,
        last_heartbeat_bar_ms=opened_at_ms,
        last_observed_close=entry_price,
        **extra,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------
def _dir_sign(direction: int) -> int:
    return 1 if direction == Direction.LONG else -1


def _favorable_breached(level: float, direction: int, bar: Bar) -> bool:
    """TP-side breach — LONG: high >= level. SHORT: low <= level."""
    if direction == Direction.LONG:
        return bar.high >= level
    return bar.low <= level


def _adverse_breached(level: float, direction: int, bar: Bar) -> bool:
    """Stop-side breach — LONG: low <= level. SHORT: high >= level."""
    if direction == Direction.LONG:
        return bar.low <= level
    return bar.high >= level


def _gap_through_adverse(level: float, direction: int, bar: Bar) -> bool:
    """Did the bar open past the adverse level (PF3)?"""
    if direction == Direction.LONG:
        return bar.open <= level
    return bar.open >= level


def _gap_through_favorable(level: float, direction: int, bar: Bar) -> bool:
    """Did the bar open past the favorable level (PF3)?"""
    if direction == Direction.LONG:
        return bar.open >= level
    return bar.open <= level


def _update_mae(pos: PositionState, bar: Bar) -> Tuple[float, int]:
    if pos.direction == Direction.LONG:
        this = bar.low - pos.entry_price     # <= 0 when bar dipped below entry
    else:
        this = pos.entry_price - bar.high    # <= 0 when bar popped above entry
    if this < pos.mae_points:
        return this, bar.bar_close_ms
    return pos.mae_points, pos.mae_bar_ms


def _update_mfe(pos: PositionState, bar: Bar) -> Tuple[float, int]:
    if pos.direction == Direction.LONG:
        this = bar.high - pos.entry_price    # >= 0 when bar rose above entry
    else:
        this = pos.entry_price - bar.low     # >= 0 when bar dropped below entry
    if this > pos.mfe_points:
        return this, bar.bar_close_ms
    return pos.mfe_points, pos.mfe_bar_ms


def _update_post_tp1_mae(pos: PositionState, bar: Bar) -> float:
    if pos.state != PositionFSMState.TP1_HIT:
        return pos.post_tp1_mae_points
    if pos.direction == Direction.LONG:
        this_drop = pos.tp1 - bar.low
    else:
        this_drop = bar.high - pos.tp1
    if this_drop > pos.post_tp1_mae_points:
        return this_drop
    return pos.post_tp1_mae_points


# ------------------------------------------------------------------
# PnL computations (§5.11)
# ------------------------------------------------------------------
def _tp1_points(pos: PositionState) -> float:
    return abs(pos.tp1 - pos.entry_price)


def _sl_points(pos: PositionState) -> float:
    return abs(pos.sl - pos.entry_price)


def _tp2_points(pos: PositionState) -> float:
    return abs(pos.tp2 - pos.entry_price)


def _signed(delta: float, direction: int) -> float:
    return delta * _dir_sign(direction)


def _pnl_sl_full(pos: PositionState, exit_price: float) -> float:
    return 3 * _signed(exit_price - pos.entry_price, pos.direction)


def _pnl_tp1_plus_runner(pos: PositionState, runner_exit_price: float) -> float:
    runner_contrib = _signed(runner_exit_price - pos.entry_price, pos.direction)
    return 2 * _tp1_points(pos) + 1 * runner_contrib


def _pnl_eod_from_open_state(pos: PositionState, close_price: float) -> float:
    return 3 * _signed(close_price - pos.entry_price, pos.direction)


# ------------------------------------------------------------------
# Invariant checks
# ------------------------------------------------------------------
def _check_invariants(position: PositionState, bar: Bar) -> None:
    if position.state == PositionFSMState.CLOSED:
        raise ResolverInvariantError(
            f"resolver called on CLOSED position signal_id={position.signal_id}"
        )
    if position.state == PositionFSMState.OPEN and position.effective_sl != position.sl:
        raise ResolverInvariantError(
            f"INV-E: OPEN state requires effective_sl == sl, got "
            f"effective_sl={position.effective_sl} sl={position.sl}"
        )
    if (position.state == PositionFSMState.TP1_HIT
            and position.effective_sl != position.entry_price):
        raise ResolverInvariantError(
            f"INV-E: TP1_HIT state requires effective_sl == entry_price, got "
            f"effective_sl={position.effective_sl} entry_price={position.entry_price}"
        )
    if not isinstance(bar.bar_close_ms, int) or bar.bar_close_ms <= 0:
        raise ResolverInvariantError(
            f"bar.bar_close_ms must be a positive int, got {bar.bar_close_ms!r}"
        )


# ------------------------------------------------------------------
# The resolver
# ------------------------------------------------------------------
def step(position: PositionState, bar: Bar, *, eod_cutoff_ms: int) -> StepResult:
    _check_invariants(position, bar)

    # PF4: entry bar is never resolved. Return position unchanged.
    if bar.bar_close_ms <= position.opened_at_ms:
        return StepResult(
            updated_position=position,
            transition=Transition.NO_TRANSITION,
            notes=("entry_bar_excluded",),
        )

    # MAE/MFE always update
    new_mae, new_mae_bar = _update_mae(position, bar)
    new_mfe, new_mfe_bar = _update_mfe(position, bar)
    new_post_tp1_mae = _update_post_tp1_mae(position, bar)

    base = replace(
        position,
        mae_points=new_mae, mae_bar_ms=new_mae_bar,
        mfe_points=new_mfe, mfe_bar_ms=new_mfe_bar,
        post_tp1_mae_points=new_post_tp1_mae,
        last_heartbeat_bar_ms=bar.bar_close_ms,
        heartbeats_processed=position.heartbeats_processed + 1,
        last_observed_close=bar.close,
    )

    # EOD wins over all level checks (§5.8, TC11)
    if bar.bar_close_ms >= eod_cutoff_ms:
        return _close_eod(base, bar)

    # Gap-open handling (PF3, TC7, TC8)
    gap_result = _maybe_close_on_gap(base, bar)
    if gap_result is not None:
        return gap_result

    # Intra-bar level checks, with pessimistic collision rules
    if base.state == PositionFSMState.OPEN:
        return _step_open(base, bar)
    if base.state == PositionFSMState.TP1_HIT:
        return _step_tp1_hit(base, bar)

    # Unreachable — invariant guard caught CLOSED already
    raise ResolverInvariantError(f"unknown state {base.state}")


def _step_open(base: PositionState, bar: Bar) -> StepResult:
    tp1_breached = _favorable_breached(base.tp1, base.direction, bar)
    sl_breached = _adverse_breached(base.effective_sl, base.direction, bar)

    # PF1: SL wins on same-bar collision (pessimistic fill).
    if sl_breached:
        return _close_sl(base, bar, fill_price=base.sl)
    if tp1_breached:
        return _transition_tp1(base, bar)
    return StepResult(updated_position=base, transition=Transition.NO_TRANSITION)


def _step_tp1_hit(base: PositionState, bar: Bar) -> StepResult:
    tp2_breached = _favorable_breached(base.tp2, base.direction, bar)
    be_breached = _adverse_breached(base.effective_sl, base.direction, bar)
    # effective_sl == entry_price in TP1_HIT state (enforced by invariant).

    # PF2: BE_STOP wins on same-bar collision.
    if be_breached:
        return _close_be(base, bar, fill_price=base.entry_price)
    if tp2_breached:
        return _close_tp2(base, bar, fill_price=base.tp2)
    return StepResult(updated_position=base, transition=Transition.NO_TRANSITION)


# ------------------------------------------------------------------
# Gap-open handling (PF3)
# ------------------------------------------------------------------
def _maybe_close_on_gap(base: PositionState, bar: Bar) -> Optional[StepResult]:
    """Returns a StepResult only if the bar opens past a level that closes
    the position at its current state. TP1 gap (OPEN -> TP1_HIT) does not
    close the position and is handled by the intra-bar path."""
    if base.state == PositionFSMState.OPEN:
        if _gap_through_adverse(base.effective_sl, base.direction, bar):
            return _close_sl(base, bar, fill_price=bar.open)
        return None

    if base.state == PositionFSMState.TP1_HIT:
        # Pessimistic ordering: if a single open price somehow implied both
        # BE and TP2 (impossible — open is a scalar — but guard anyway),
        # BE would win per PF2. In practice only one branch fires.
        if _gap_through_adverse(base.effective_sl, base.direction, bar):
            return _close_be(base, bar, fill_price=bar.open)
        if _gap_through_favorable(base.tp2, base.direction, bar):
            return _close_tp2(base, bar, fill_price=bar.open)
        return None

    return None


# ------------------------------------------------------------------
# Transition builders
# ------------------------------------------------------------------
def _transition_tp1(base: PositionState, bar: Bar) -> StepResult:
    updated = replace(
        base,
        state=PositionFSMState.TP1_HIT,
        tp1_hit=1,
        tp1_hit_bar_ms=bar.bar_close_ms,
        effective_sl=base.entry_price,
    )
    return StepResult(updated_position=updated, transition=Transition.TP1_HIT)


def _close_sl(base: PositionState, bar: Bar, *, fill_price: float) -> StepResult:
    pnl = _pnl_sl_full(base, fill_price)
    updated = replace(
        base,
        state=PositionFSMState.CLOSED,
        closed_at_ms=bar.bar_close_ms,
        exit_reason=Transition.SL_HIT.value,
        final_pnl_points=pnl,
    )
    return StepResult(
        updated_position=updated,
        transition=Transition.SL_HIT,
        exit_reason=Transition.SL_HIT.value,
    )


def _close_tp2(base: PositionState, bar: Bar, *, fill_price: float) -> StepResult:
    pnl = _pnl_tp1_plus_runner(base, fill_price)
    updated = replace(
        base,
        state=PositionFSMState.CLOSED,
        closed_at_ms=bar.bar_close_ms,
        exit_reason=Transition.TP2_HIT.value,
        final_pnl_points=pnl,
    )
    return StepResult(
        updated_position=updated,
        transition=Transition.TP2_HIT,
        exit_reason=Transition.TP2_HIT.value,
    )


def _close_be(base: PositionState, bar: Bar, *, fill_price: float) -> StepResult:
    pnl = _pnl_tp1_plus_runner(base, fill_price)
    updated = replace(
        base,
        state=PositionFSMState.CLOSED,
        closed_at_ms=bar.bar_close_ms,
        exit_reason=Transition.BE_STOP.value,
        final_pnl_points=pnl,
    )
    return StepResult(
        updated_position=updated,
        transition=Transition.BE_STOP,
        exit_reason=Transition.BE_STOP.value,
    )


def _close_eod(base: PositionState, bar: Bar) -> StepResult:
    # EOD uses bar.close. If position already had TP1, the runner closes at
    # bar.close; the two TP1 contracts contribute their locked-in points.
    if base.tp1_hit:
        pnl = _pnl_tp1_plus_runner(base, bar.close)
    else:
        pnl = _pnl_eod_from_open_state(base, bar.close)
    updated = replace(
        base,
        state=PositionFSMState.CLOSED,
        closed_at_ms=bar.bar_close_ms,
        exit_reason=Transition.EOD_TIMEOUT.value,
        final_pnl_points=pnl,
    )
    return StepResult(
        updated_position=updated,
        transition=Transition.EOD_TIMEOUT,
        exit_reason=Transition.EOD_TIMEOUT.value,
    )


# ------------------------------------------------------------------
# Thin wrapper helpers for TC12 / TC14
# ------------------------------------------------------------------
def should_process(position: PositionState, bar: Bar) -> bool:
    """Replay guard (§5.9). True iff this bar should be handed to step()."""
    return bar.bar_close_ms > position.last_heartbeat_bar_ms


def process_heartbeat(fsm_map, *, signal_id, bar, eod_cutoff_ms):
    """
    Safe-no-op wrapper:
      - Unknown signal_id  -> return None (TC12)
      - Replay / duplicate -> return None (TC14)
      - Otherwise          -> delegate to step() and return StepResult
    """
    position = fsm_map.get(signal_id)
    if position is None:
        return None
    if not should_process(position, bar):
        return None
    return step(position, bar, eod_cutoff_ms=eod_cutoff_ms)
