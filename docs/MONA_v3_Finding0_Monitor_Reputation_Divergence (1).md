# Finding #0 — Monitor Does Not Replicate Reputation Engine Gating
## MONA v3.0 Master Audit — Appendix A
## Discovered: April 12, 2026 (Sunday)
## Severity: BLOCKER — deeper than Finding #1
## Context: Found while walking the Strategist's keystone question on Finding #1's fix spec

---

## SUMMARY

The TP/SL Monitor Pine Script (`mona_v3_0_monitor.txt`) does not replicate the main indicator's Reputation Engine gating or `evalPending` gating. It opens position slots based on raw filter conditions + session + free-slot availability only. The main indicator (`mona_v3_0.txt`) only fires ENTRY alerts when the reputation tracker is in the `ELIGIBLE` state with no eval pending. These two conditions are not the same, and they diverge on exactly the days where signal quality matters most — fast trending days, post-failed-follow-through lockouts, and during the 4-bar eval windows that are the whole point of the Reputation Engine.

When the divergence occurs, the Monitor creates **phantom position slots** — slots that track, resolve, and emit `TRADE_OUTCOME` webhooks to the backend for positions the main indicator never alerted on. In the current backend, these phantom outcomes are silently grafted onto unrelated real signals via `find_parent_by_lookback`, corrupting the `trade_outcomes` table in a way that looks plausible under cursory inspection.

This finding was discovered on the way to proving the Strategist's keystone question for Finding #1 — specifically the claim that the main indicator's `bar_close_ms` and the Monitor's `entry_time_ms` would always reference the same bar. Walking the Monitor's signal-detection code to verify that claim surfaced a deeper problem: the two scripts don't reference the same bar because they don't fire on the same bar, because they don't gate on the same conditions.

---

## CODE CITATIONS

### Main indicator — ENTRY gating

`mona_v3_0.txt` line 354, trend entry block:

```
if trendTrk.repState == ELIGIBLE and not trendTrk.evalPending and not fireTrendAlert
    if trendHasLong
        ...
        fireTrendAlert  := true
        trendEntryLong  := true
        trendTrk.evalPending := true
```

`mona_v3_0.txt` line 526, squeeze entry block (symmetric):

```
if sqzTrk.repState == ELIGIBLE and not sqzTrk.evalPending and not fireSqzAlert
```

Both entry types are explicitly gated on:
- Reputation state = `ELIGIBLE` (not `GROUNDED`, not `EXTENDED`)
- No eval currently pending
- No prior fire on the same bar

This is the heart of the Reputation Engine. During GROUNDED (8 bars) and EXTENDED (16 bars), no new ENTRY alerts fire. During the 4-bar eval window after any entry, no new ENTRY alerts fire. This is by design — it's how the system says NO to bad setups.

### Monitor — slot opening gating

`mona_v3_0_monitor.txt` lines 118–143:

```
// Trend Continuation
trendLong  = vwapBull and bullStack and htfBull and trendStochBull and adxOK and volOK
trendShort = vwapBear and bearStack and htfBear and trendStochBear and adxOK and volOK

// Squeeze Detection
...
sqzLong  = sqzDetected and sqzPriceLong  and emaCrossUp   and stochBull and volOK
sqzShort = sqzDetected and sqzPriceShort and emaCrossDown and stochBear and volOK

// Final signal conditions (only fire during session)
trendEntryLong  = trendLong  and inSession
trendEntryShort = trendShort and inSession
sqzEntryLong    = sqzLong    and inSession
sqzEntryShort   = sqzShort   and inSession

hasAnySignal = trendEntryLong or trendEntryShort or sqzEntryLong or sqzEntryShort
```

And `mona_v3_0_monitor.txt` line 535, slot-opening logic:

```
if hasAnySignal
    freeIdx = findFreeSlot()
    if freeIdx >= 0
        ...
        array.set(entryBars, freeIdx, bar_index)
        array.set(entryPrices, freeIdx, close)
```

No `repState` check. No `evalPending` check. No tracker duplication anywhere in the Monitor — confirmed by grep:

```
$ grep -n "repState\|evalPending\|ELIGIBLE\|GROUNDED\|EXTENDED\|lockoutEnd\|cooldown" mona_v3_0_monitor.txt
(empty)
```

The Monitor opens slots whenever raw conditions fire, session is active, and there's a free slot in the parallel arrays.

### Workshop 3 design intent

`MONA_v3_Workshop3_Master_Context.md` line 226:

> Signal detection is duplicated from the main indicator. Pine Script doesn't allow cross-indicator state reads, so the Monitor recomputes the same filter conditions to detect new entries. During observation this is zero risk because signal logic is frozen.

The design note says "the same filter conditions" — not "the same conditions including reputation state." This is the precise language gap where the bug lives. Filter conditions were duplicated. The gating that sits around those filter conditions in the main indicator was not. The Monitor implementation treats "the same filter conditions" as the full firing condition, but in the main indicator those conditions are only a necessary precondition — the sufficient condition also includes the reputation state check.

I wrote both scripts. This gap is mine.

---

## WHEN THE DIVERGENCE FIRES

Three divergence scenarios, roughly in descending likelihood on a typical trading day:

**Scenario A — Eval-window retrigger (most common on trending days).** A real signal fires at bar T. Main indicator sets `evalPending = true` and locks out new entries for 4 bars. Monitor opens its slot on bar T in agreement. During bars T+1 through T+4, raw conditions re-fire (easy on a fast move — `trendStochBull` only requires `k > d` and `k > k[1]`, which wiggles on every bar of a strong trend). Monitor opens additional slots on each re-fire. Main indicator fires nothing. Every Monitor slot opened during T+1..T+4 is a phantom.

**Scenario B — GROUNDED lockout retrigger.** An eval fails follow-through at bar T. Tracker goes to `GROUNDED` with lockout to T+8. Over the next 8 bars, raw conditions can easily re-fire (the conditions that fired the failed signal may not have fundamentally changed). Monitor opens a slot on any bar where raw conditions are met. Main indicator fires nothing. Every such Monitor slot is a phantom.

**Scenario C — EXTENDED lockout retrigger.** Two failed evals in a row. Tracker goes to `EXTENDED` with lockout to T+16. Same mechanism as Scenario B, longer window, more phantoms.

**Rate estimate.** On a strong trending day with the TREND tracker spending meaningful time in non-ELIGIBLE states, I'd expect 2–5 phantom slots per real trend signal. SQUEEZE is less affected because `sqzLong` requires `ta.crossover(ema9, ema21)`, which by definition doesn't repeat in the same regime. Trend-heavy days are where the phantom rate is worst, which is also where signal quality matters most and where the Reputation Engine is working hardest — so the blind spot aligns directly with the scenarios the Engine was designed to protect against.

**Why we haven't seen it yet.** Day 1, April 10, had Signal #1 (TREND LONG 25370.25) as the only signal. That signal fired when the tracker was ELIGIBLE, and the eval window completed with no retriggers in those 4 bars. Main indicator and Monitor were in lockstep for that specific path. The observation set is N=1 and the divergence mode never fired. This finding would have manifested on the first day with a failed follow-through followed by trending conditions — probably in Week 1.

---

## WHAT HAPPENS TO PHANTOM OUTCOMES IN THE BACKEND

Current backend `find_parent_by_lookback` in `mona_v3_0_backend.py` line 350:

```python
def find_parent_by_lookback(signal_type: str, direction: str) -> int:
    """
    Lookback Matcher: find the most recent ENTRY in signals_v3 matching
    signal_type + direction within a 2-hour window. Returns signal_id or None.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute('''
                SELECT signal_id FROM signals_v3
                WHERE signal_type = ? AND signal = ?
                  AND timestamp >= strftime('%Y-%m-%d %H:%M:%S', 'now', '-2 hours')
                ORDER BY signal_id DESC LIMIT 1
            ''', (signal_type, direction)).fetchone()
            return row[0] if row else None
```

For a phantom outcome:
- `signal_type` and `direction` match whatever raw condition fired (TREND LONG, say)
- The query finds the most-recent TREND LONG ENTRY in signals_v3 within 2 hours
- That row exists — it's the previous real signal the main indicator fired
- The phantom's outcome fields (exit_code, PnL, MAE, MFE, time_in_trade) get written to `trade_outcomes` with that real signal's `signal_id` as parent
- If the real signal already had its outcome row written, the phantom either overwrites it or adds a duplicate row (depending on the write semantics of the `trade_outcomes` INSERT — worth checking in the backend)

Net effect: the `trade_outcomes` table contains rows whose `signal_id` foreign key does not identify the position that produced those values. Any Data Lab analysis that joins `trade_outcomes` to `signals_v3` and filters on signal-level fields (reputation state, volume ratio, ADX, StochRSI, time-of-day) would be analyzing a blend of real outcomes and unrelated outcomes incorrectly keyed to real signals. This is the worst class of data bug: the joins succeed, the rows look populated, the numbers look reasonable, and every conclusion drawn from the data is suspect.

---

## COMPOUND WITH FINDING #1

Finding #1 (concurrent-position wrong-signal-id assignment) and Finding #0 (phantom outcomes silently matched to wrong parents) share the same root cause: `find_parent_by_lookback` is a fuzzy matcher and the only thing it can do when the real parent doesn't exist is return something plausible-looking. Finding #1 is about real outcomes landing on the wrong real parent. Finding #0 is about phantom outcomes landing on any real parent at all. Both are failure modes of "the lookup returns something even when it shouldn't."

The fix for both is structurally the same: **stop doing fuzzy matching, start doing exact matching, and fail cleanly when no match exists.** The Finding #1 fix spec I was writing already proposed exact-match on `bar_close_ms`. Finding #0 just adds a constraint that was implicit in my original Finding #1 spec but needs to be explicit now: **no lookback fallback retained as a safety net**. If the exact match doesn't find a parent, the outcome orphans. That's the correct behavior.

### Why this fix also neutralizes Finding #0

Under exact-match with no fallback:

- **Real outcomes.** The Monitor's `entry_time_ms` matches a real signal's `bar_close_ms` exactly (both sourced from `time_close` of the bar the position opened on, same bar in both scripts because both scripts fire on that bar). Exact integer equality. Parent found. Outcome row written with correct `signal_id`.

- **Phantom outcomes.** The Monitor's `entry_time_ms` is sourced from `time_close` of the bar the phantom slot opened on. No real signal fired on that bar. No `signals_v3` row has a `bar_close_ms` equal to that value. Exact match returns None. No fallback. Outcome orphans via `❌ [ORPHAN]` to `#mona-log`. No corruption.

- **Concurrent real outcomes (Finding #1 original case).** Both real signals have distinct `bar_close_ms` values because they opened on different bars. Monitor emits the correct `entry_time_ms` for each resolution. Exact match on each. No cross-contamination.

Finding #0 is worse than Finding #1 — it's a larger surface area of corruption and it's invisible to the Monitor Bench Test and the Week 1 Validation Protocol for the same reasons Finding #1 was. But the fix is the same fix, with one clarification: **the lookback fallback must be removed, not demoted.** My original Finding #1 fix sketch kept the lookback as a "warn-and-fallback" path. That path protects Finding #1 somewhat but enables Finding #0 completely. The fallback has to go.

### Observability bonus

With phantom outcomes orphaning cleanly to `#mona-log`, we get free measurement of the divergence rate. Every `❌ [ORPHAN]` log line in observation is a phantom, and counting them per day tells us exactly how severe the Monitor/main-indicator divergence is under real market conditions. If phantoms are rare (tracker is usually ELIGIBLE), the orphan-clean approach is a permanent solution. If phantoms are frequent, that's data telling us the Monitor needs its own Reputation Engine before production — and we'd schedule that for v3.1 with a bench test and a clean gate, not under deploy pressure on a Sunday.

This is exactly the kind of observability the Workshop 3 thesis wants: data deciding architecture, not intuition.

---

## WHY THIS ISN'T A "DUPLICATE THE REPUTATION ENGINE IN THE MONITOR" EMERGENCY

The intuitive fix to Finding #0 is to port the Reputation Engine state machine (`trendTrk`, `sqzTrk`, `evalPending`, `lockoutEnd`, ghost tracking, follow-through checks) into the Monitor so that Monitor and main indicator fire on exactly the same bars by construction. This is the "correct" fix in the sense that it eliminates the divergence at its source. It is also the wrong fix for Sunday because:

1. **Lock-step duplication is a maintainability tax.** Every future change to the Reputation Engine would have to land in both scripts simultaneously or the divergence comes back in a new form. The Workshop 3 context already flagged the filter-condition duplication as "maintainability debt acknowledged and accepted" — doubling the surface of duplicated logic doubles the debt and the lockstep risk.

2. **A hand-ported state machine under deploy pressure is a bug factory.** The Reputation Engine in the main indicator is roughly 200 lines of stateful Pine Script with ghost tracking, lockout counting, eval pending transitions, and follow-through evaluation. Porting it on a Sunday with Monday as the deploy gate, while simultaneously fixing Finding #1 and Finding #2, is exactly the condition under which I would expect to introduce a new bug that escapes the existing tests.

3. **The orphan-clean approach via exact-match is simpler, measurable, and correct.** Phantom outcomes orphan. Real outcomes match. We get observability into the divergence rate. If the rate is low, we're done. If the rate is high, we have the data to justify a Reputation Engine port for v3.1 with its own bench test and a Week 1 of its own. Phase the complexity.

4. **The orphan path already exists in the backend.** The `❌ [ORPHAN]` log prefix is already defined at line 389 of `mona_v3_0_backend.py`. The handling path exists. We just need to make sure the lookup function uses it correctly — which is a change in the lookup function, not a change in the overall architecture.

The recommendation is: orphan-clean approach in v3.0, data-justified Reputation Engine port in v3.1 if observation shows the divergence rate is painful.

---

## ONE THING I STILL NEED TO VERIFY BEFORE THE SPEC

The main indicator fires three kinds of alerts on the same `alertcondition` line:

1. **ENTRY alerts** (sig_status = 1) — gated on `repState == ELIGIBLE and not evalPending`
2. **EVAL alerts** (sig_status = 2) — fire when an eval completes, regardless of tracker state; includes ghost eval completions
3. (Plus OUTCOME alerts come from the Monitor's own `alertcondition`, sig_status = 3)

The Monitor only emits TRADE_OUTCOME alerts — it doesn't emit ENTRY or EVAL alerts. So the only thing the backend matches Monitor outcomes against is `signals_v3` rows of type ENTRY. EVAL rows and ghost EVAL rows are in `evaluations`, not `signals_v3`, and they're not parents to anything.

**Before I finalize the spec, I want to verify one more thing:** ghost evals in the main indicator (lines 309–339 in `mona_v3_0.txt`) fire on the ghost tracker's completion logic. I need to confirm that ghost evals **never write to signals_v3** and **never create a parent row that could be a match target for a Monitor outcome**. If ghost evals somehow do leak into `signals_v3`, that's an adjacent failure surface that needs separate handling in the spec. This check is 10 minutes of reading once I'm back at the keyboard.

---

## PROPOSED RE-PLAN

**Today (Sunday):**

1. Finish the ghost-eval verification above.
2. Write the combined Finding #0 + Finding #1 + Finding #2 fix spec as a single document (`MONA_v3_0_Fix_Spec.md`). Explicit enumeration of every touch point. Explicit removal of the lookback fallback. Explicit orphan-clean behavior for phantoms. Explicit walkthrough of all three failure cases (concurrent real outcomes, single-bar phantom, multi-bar phantom during lockout) against the fixed logic.
3. Expand the Layer 1 unit test scope to cover phantom outcome orphaning. The test now needs:
   - Two real `signals_v3` rows with distinct `bar_close_ms` values
   - Three `TRADE_OUTCOME` payloads: two real (resolved in reverse order — Finding #1 case), one phantom with an `entry_time_ms` that doesn't match any real signal
   - Assert: real outcomes match their real parents by `signal_id`; phantom outcome orphans (returns None from the lookup, logs `❌ [ORPHAN]`, does not write to `trade_outcomes`)
4. Loop Strategist in on Finding #0 before writing code. The keystone review he asked for cycled back a bigger finding and he should see it before the spec becomes authoritative.

**Sunday night or Monday morning:**

5. Strategist response on Finding #0 and the orphan-clean path. If he agrees, proceed to fix implementation. If he pushes back on the "no fallback" approach, we talk it out before code.

**Monday afternoon (if everything above lands clean):**

6. Write the backend fix, the main indicator `bar_close_ms` plot addition, the Monitor `entry_time_ms` plot addition, the schema addition on `signals_v3`, the MAE/MFE loop restructure for Finding #2.
7. Run Layer 1 and Layer 2 tests.
8. If tests pass, deploy Monday evening or Tuesday morning.

**If anything in steps 1–7 slips:**

9. Slip deploy to Tuesday afternoon or Wednesday. Explicitly accept the timeline cost rather than deploying a fix we haven't walked all the way through.

---

## CLOSING

This finding is mine and it's one I signed off on in both the Workshop 3 design phase and the first pass of the Master Audit. I walked the Monitor code in the Master Audit and didn't catch that it was missing the reputation gating — I looked at the MAE/MFE loop structure, the resolution priority, the slot-free logic, the edge cases around TP1/TP2/BE, and I didn't look at whether the slot-open path gated on the same conditions as the main indicator's ENTRY path. That was a blind spot. The spec-before-code discipline the Strategist flagged as a pattern is the same discipline that would have caught this finding in Workshop 3 — if I'd written a fix spec for the Monitor's position-detection logic that explicitly enumerated every condition it needed to mirror from the main indicator, the reputation gating would have been on that list and its absence would have been visible.

I'm treating this as a confirming data point on the pattern commitment, not as a separate failure. Same reflex, same fix: write the spec first, enumerate the surface, prove the keystone before assuming it.

The review loop worked. Strategist pushed the keystone question, the push forced the walkthrough, the walkthrough found the finding. Four days of slow walking and we're still finding real things. Alertness, not relief.

*— Dev Trader, The Mona Project*
*April 12, 2026*
