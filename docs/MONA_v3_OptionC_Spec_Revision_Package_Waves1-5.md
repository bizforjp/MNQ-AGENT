# MONA v3.0 Option C Spec — Complete Revision Package (Waves 1–5)

**From:** Dev Trader
**Date:** April 13, 2026
**Target:** `MONA_v3_OptionC_Spec.md` (v1.0, April 13 draft, 2,729 lines)
**Produces:** `MONA_v3_OptionC_Spec_v1.1.md` (REVISED — AWAITING FINAL LOCK)
**Status:** All waves complete. Both OI-06 and OI-07 fully resolved. Ready for Strategist final merge read.

---

## 0. Wave status

| Wave | Description | Status |
|---|---|---|
| Wave 0 | Pre-flight: OI-06 TV quota + OI-07 Finnhub | ✅ Both resolved |
| Wave 1 | Wording and framing revisions (3 edits) | ✅ Complete |
| Wave 2 | New logging/fragility commitments (3 edits) | ✅ Complete |
| Wave 3 | §7.6 alertcondition revision (Case A, strip directional-alignment) | ✅ Complete |
| Wave 4 | §5.14 Integration and Migration Test Cases (TC32–TC35) | ✅ Complete |
| Wave 5 | Top-matter status update, filename, OI-06/07 table refinements | ✅ Complete |

**OI-06 final:** Locked Case A — separate alertconditions on Essential plan (20-alert cap, 1 in use, 19 headroom). Directional-alignment argument stripped from §7.6. Heartbeat rides on its own `fireHeartbeat` alertcondition per §7.6 spec commitment.

**OI-07 final:** Free tier confirmed sufficient. Josh created fresh Finnhub account April 13. Free tier provides 60 API calls/minute against a projected worst-case of ~240 calls/month (well under 0.01% of ceiling). No upgrade needed. §8 Finnhub adapter proceeds as specified.

---

## 1. Application order

Apply the edits below to `MONA_v3_OptionC_Spec.md` in this exact order. Each edit is either REPLACE (find the exact BEFORE block and swap) or INSERT (find the anchor text and insert the block immediately after). Order matters only in that Wave 5 top-matter changes and the final filename save happen last; Waves 1–4 are position-independent and can be applied in any internal order.

1. Wave 5a: Top-matter status and date (edit at top of file)
2. Wave 1 Edit 1: §5.6 PF1 commentary
3. Wave 1 Edit 2: §9.3 `entryBarCloseMs` rewrite
4. Wave 1 Edit 3: §11 post-review decisions table (insertion)
5. Wave 2 Edit 4: §4.2 UNIQUE loud-logging commitment
6. Wave 2 Edit 5: §3.6 float-encoding fragility note
7. Wave 2 Edit 6: §8.5 distinct staleness logging
8. Wave 3: §7.6 directional-alignment argument strip
9. Wave 4: §5.14 new subsection with TC32–TC35
10. Wave 5b: Update test-count reference in §12.3 closing retirement ledger
11. Wave 5c: Save as `MONA_v3_OptionC_Spec_v1.1.md`

---

## 2. Wave 5a — Top-matter status update

**Type:** REPLACE
**Location:** Lines 3–4 of the spec (the Date and Status lines at the very top)

**BEFORE:**

```
**Date:** April 13, 2026 (Monday)
**Status:** DRAFT — awaiting Strategist review
```

**AFTER:**

```
**Date:** April 13, 2026 (Monday) — revised post-Strategist review
**Status:** REVISED per Strategist review (Waves 1–5 applied) — AWAITING FINAL MERGE READ AND LOCK
```

---

## 3. Wave 1 — Framing and wording revisions

### Edit 1 — §5.6 PF1 commentary

**Strategist source:** §6 action item 9, tying into §3 OI-03
**Type:** REPLACE
**Location:** §5.6 Priority rules — the pessimistic fill convention, the "Rule PF1" paragraph
**Rationale:** A note for the future reader who will second-guess the pessimistic convention the first time a PF1 collision costs a hypothetical win. The firewall-around-the-execution-brain thesis only holds if the convention is the explicit, locked, principled choice — not a default that gets reconsidered under pressure.

**BEFORE:**

> **Rule PF1 — OPEN state, TP1 and SL both breached on one bar: SL_HIT wins.** Intuition: if the bar's low touches the stop and the bar's high touches TP1, we cannot know which came first. In reality, a stop order at SL fires as soon as price touches it, and a limit order at TP1 fires as soon as price touches it. If the adverse move came first, the stop triggered and the TP1 order never gets a chance to fill. The pessimistic assumption is that the adverse move came first. Test case TC6.

**AFTER:**

> **Rule PF1 — OPEN state, TP1 and SL both breached on one bar: SL_HIT wins.** Intuition: if the bar's low touches the stop and the bar's high touches TP1, we cannot know which came first. In reality, a stop order at SL fires as soon as price touches it, and a limit order at TP1 fires as soon as price touches it. If the adverse move came first, the stop triggered and the TP1 order never gets a chance to fill. The pessimistic assumption is that the adverse move came first. Test case TC6.
>
> **Note for the future reader.** This convention will sometimes count a PF1 collision as a full loss when the trader's intuition is that TP1 was hit ("but I would have hit TP1 if I'd been watching"). That is the convention working as intended, not a bug to fix. See Strategist Review §3 OI-03 for the full reasoning: optimistic fills inflate Data Lab numbers in a way that creates a calibration error which only becomes visible once Josh is sized into a funded TopstepX account and reality starts disagreeing with the model. The pessimistic convention is the lower-bound-on-real-performance number that protects against blowing an evaluation on inflated expectations. It is locked from day one and is not reconsidered the first time it costs a hypothetical win.

---

### Edit 2 — §9.3 `entryBarCloseMs` "judgment not rule" rewrite

**Strategist source:** §4.1 (real pushback), §6 action item 1
**Type:** REPLACE
**Location:** §9.3 "The one thing §9 does commit", the three paragraphs from "Is this a Reputation Engine change?" through "Pass with one documented additive change."
**Rationale:** The principle "Mona's brain lives in one place" only works if we are honest about moments when we interpret it with judgment versus moments when we apply it mechanically. `entryBarCloseMs` passes the principle on the merits, but naming it as a judgment call — and fencing it with a no-precedent commitment and a four-part test any future addition must pass independently — is what keeps the principle load-bearing over the long term.

**BEFORE:**

> **Is this a Reputation Engine change?** Technically, yes — the tracker struct is part of the Reputation Engine, and adding a field to it is a structural edit. But the field is write-once at entry time, read-once at eval time, and it touches no decision logic. It is not a new gate, not a new state transition, not a new lockout rule. It is a timestamp being carried from one known point in the Reputation Engine's lifecycle to another.
>
> **§9's position:** this is the minimum viable Reputation Engine touch required by Option C, it is purely additive (one int field per tracker), and it does not alter any cognition behavior. The spec commits to this change as necessary and documents it explicitly so it is not a surprise.
>
> **The Reputation Engine's decision behavior is byte-identical pre- and post-migration.** The sequence of which signals fire, when they fire, what reputation state they produce, and what the tracker state looks like at every step is unchanged. The only observable difference is that the EVAL event's payload now carries one additional integer field. Pass with one documented additive change.

**AFTER:**

> **Is this a Reputation Engine change? This is the place in the spec where the principle from §1.4 is being interpreted with judgment, not applied mechanically.** Dev Trader wants this framing on the record explicitly, and Strategist's review §4.1 surfaced it as the one place in the spec where the principle needs to be named as interpreted rather than mechanical.
>
> The honest framing: we are adding a write-once measurement timestamp to a cognition data structure. We are accepting that as principle-compliant because the field carries no decision authority and the Reputation Engine's decision behavior is byte-identical pre- and post-migration. This is a judgment call about what constitutes a "Reputation Engine change," not a mechanical rule application. It applies here, and only here.
>
> **This single addition does not establish precedent for future tracker struct changes.** If a future change wants to add a second field to any tracker struct — for any reason, however good — the reasoning "but we already added `entryBarCloseMs`" must not be available. The principle "Mona's brain lives in one place" only survives if each proposed tracker struct addition defends itself fresh on the merits, against the same criteria applied here.
>
> **The four-part test.** Any future addition to `trendTrk`, `sqzTrk`, or any successor tracker struct must pass all four of the following criteria independently. Failing any one means the field belongs somewhere else, not in the tracker struct:
>
> 1. **Write-once.** The field is written exactly once in the tracker's lifecycle, at a known deterministic point (here: at the moment of ENTRY authorization, from `time_close`).
> 2. **Read-once.** The field is read exactly once in the tracker's lifecycle, at a known deterministic point (here: at the moment of EVAL firing, plotted into index 33 for the alertcondition message template).
> 3. **No decision authority.** The field does not participate in any gate, state transition, lockout rule, eligibility check, filter condition, or eval-pending computation. No code path that decides what the Reputation Engine should do next may read this field.
> 4. **Decision behavior byte-identical.** The sequence of which signals fire, when they fire, what reputation state they produce, and what the tracker state looks like at every step is unchanged pre- and post-addition. The only observable difference is what the event payload carries downstream.
>
> `entryBarCloseMs` passes all four. (1) It is written once at entry authorization. (2) It is read once at EVAL firing. (3) It is not referenced by `repState`, `evalPending`, lockout counting, ghost tracking, or any filter. (4) The Reputation Engine's byte-level decision trace is unchanged — the tracker's observable state transitions are identical, only the EVAL event's serialized payload carries one additional integer.
>
> **§9's position:** `entryBarCloseMs` is the minimum viable Reputation Engine touch required by Option C, it is purely additive (one int field per tracker), and it does not alter any cognition behavior. The spec commits to this change as necessary and documents it explicitly so it is not a surprise. **Pass with one documented additive change, subject to the no-precedent commitment and the four-part test above.**

---

### Edit 3 — §11 post-review decisions table (with final OI-06 and OI-07 status)

**Strategist source:** §3 (response to all 12 OIs), §6 action item 8, + Strategist carry-forward from Waves Approved memo
**Type:** INSERT AFTER
**Location:** §11 intro, immediately after the sentence "Each item has an ID, a section reference, the decision needed, and Dev Trader's current recommendation (where one exists)." — insert before the "### OI-01 — Plot limit verification (precondition, not design choice)" heading
**Rationale:** Strategist's §3 resolved all twelve open items. With Josh's answers on OI-06 (Essential plan, 19 headroom → Case A) and OI-07 (Free tier, 60 calls/min → sufficient), the table ships with all twelve items in fully-resolved state. Individual OI subsections below are preserved for historical context and tradeoff documentation.

**ANCHOR TEXT (insert immediately after this paragraph):**

> Each item has an ID, a section reference, the decision needed, and Dev Trader's current recommendation (where one exists).

**INSERT:**

> ---
>
> **Post-Strategist Review Status (April 13, 2026).** All twelve open items were resolved in Strategist's peer review of this spec (see `Strategist_Review_of_OptionC_Spec.pdf` §3) and in the subsequent pre-flight checks by Josh. OI-06 was verified against Josh's TradingView plan and locked to Case A (separate alertconditions). OI-07 was verified against Josh's Finnhub account and locked to Free tier sufficient. The table below consolidates all twelve decisions. Individual OI subsections below are preserved for historical context and tradeoff documentation.
>
> | ID | Item | Decision | Notes |
> |---|---|---|---|
> | OI-01 | Plot limit verification | Mechanical — run the test | Pre-flight check. First task after spec lock, before any Pine or backend code. |
> | OI-02 | EOD cutoff time | **Lock 16:00 ET** | 15:45 biases `post_tp1_mae` and runner conversion metrics downward. Matches real flat-at-RTH-close executor behavior. |
> | OI-03 | Pessimistic fill convention | **Lock pessimistic** | Strong Strategist endorsement. Documented in §5.6 PF1 commentary (Edit 1 of this revision pass). |
> | OI-04 | Concurrent slot count N | **Lock N = 4** | §8.5 staleness check catches overflow. Raise later only if observation shows real overflow. |
> | OI-05 | UNIQUE on `bar_close_ms` | **Lock UNIQUE + loud-failure logging** | See §4.2 `[UNIQUE_VIOLATION]` commitment (Edit 4 of this revision pass). Makes the "revisit on observation data" commitment actionable by ensuring silent failures cannot happen. |
> | OI-06 | Heartbeat alertcondition | **Locked to Case A — separate alertconditions** | TradingView quota verified April 13: Josh is on Essential plan (20-alert cap, 1 alert currently in use, 19 headroom). No upgrade required. Directional-alignment argument stripped from §7.6 per Wave 3 of this revision pass — the decision wins on its own merits (failure-case elimination of same-bar ENTRY+HEARTBEAT collision per §6.2). |
> | OI-07 | Finnhub API key | **Locked — Free tier sufficient** | Verified April 13: Josh created a fresh Finnhub account on the Free tier (60 API calls/minute). Projected worst-case usage under Option C is ~240 calls/month during outage recovery events, which is far below 0.01% of the Free tier ceiling (2.6M calls/month). No upgrade needed for foreseeable observation horizon. §8 Finnhub adapter proceeds as specified. |
> | OI-08 | `MAX_REPLAY_BARS` | **Lock 16 bars** | Revisit on observation data. Covers typical position lifetime, sustainable Finnhub call volume, bounded reconciliation risk. |
> | OI-09 | Staleness cadence / threshold | **Lock 4-bar cadence (1 hour), 2-bar threshold (30 minutes)** | Tighten later only if observation shows missed gaps. |
> | OI-10 | TC24–TC31 coverage | **Accept TC24–TC31; add TC32–TC35 (§5.14)** | Strategist added four integration/migration test cases: tolerance mode coexistence, replay idempotency under live-and-replay overlap, concurrent gap recovery on multiple positions, and GAP_CLEAN fallback with unavailable `last_observed_close`. See new §5.14 added in Wave 4 of this revision pass. Total: 35 test cases. |
> | OI-11 | In-place modify vs rename of `mona_v3_0.txt` | **Lock in-place modify** | Git history handles archaeology. Option A version preserved in commit history. |
> | OI-12 | When 30-day observation clock starts | **Lock: first clean end-to-end signal cycle** | Not the deploy moment. Observation is a measurement period, not a calendar period. A deploy with zero signals firing for 5 days is 5 days of system idle, not 5 days of observation. |
>
> All twelve items are locked. No remaining pre-flight blockers. Implementation phase begins after Strategist's final merge read of this document.
>
> ---

---

## 4. Wave 2 — New logging and fragility commitments

### Edit 4 — §4.2 UNIQUE violation loud-logging commitment

**Strategist source:** §3 OI-05, §4 OI-05, §6 action item 2
**Type:** INSERT AFTER
**Location:** End of §4.2, immediately after the "Backend `translate_payload` change" paragraph, before the "### 4.3 `evaluations` — no schema changes required" heading
**Rationale:** The UNIQUE constraint's failure mode is "second INSERT fails, real signal is lost" — silent unless made loud. Without explicit logging, a cross-tracker same-bar collision would disappear into a SQLite constraint violation and only be discovered in Week 4 of Data Lab when someone notices a TREND signal that fired on the chart but has no `signals_v3` row. Loud logging is what makes the "revisit on observation data" commitment actionable.

**ANCHOR TEXT (insert immediately after this paragraph):**

> **Backend translate_payload change.** The `translate_payload` function in the backend needs to extract `bar_close_ms` from the parsed payload (where it arrives as a float via Pine's plot coercion) and coerce it to int: `int(float(data.get("bar_close_ms", 0)))`. If the value is 0 or missing on an ENTRY, that is a schema violation and the ENTRY is rejected with `log_error("SCHEMA", "missing bar_close_ms on ENTRY")`. This check is a single new line in the ENTRY route.

**INSERT:**

> **UNIQUE violation loud-logging commitment (post-Strategist review).** The UNIQUE constraint above treats cross-tracker same-bar entries as a theoretical-but-rare case to revisit on observation data. The failure mode under UNIQUE is "second INSERT fails, real signal is silently lost" — which is invisible unless we make it loud. Strategist review §3 OI-05 and §4 OI-05 require defense-in-depth on this edge case.
>
> **Spec commitment.** Any UNIQUE constraint violation on `signals_v3.bar_close_ms` or `positions.bar_close_ms` MUST trigger both:
>
> 1. An explicit `❌ [UNIQUE_VIOLATION]` log line at ERROR level, naming:
>    - the `bar_close_ms` value,
>    - the `signal_type` and `direction` of the failed INSERT,
>    - the `signal_id` of the row already occupying that `bar_close_ms` (from a pre-INSERT SELECT on failure), and
>    - which table the collision occurred on (`signals_v3` or `positions`).
> 2. An embed to the `#system-log` Discord channel carrying the same fields, so Josh sees the event within minutes rather than discovering it weeks later in a Data Lab anomaly.
>
> **Why this matters.** Without the loud-failure path, a real cross-tracker same-bar entry (TREND and SQUEEZE both authorized on the same bar from independent reputation states) would disappear silently into a SQLite constraint violation. The second signal's row would not be written. The Discord embed for the second signal would not fire. The only observable trace would be a TREND signal that fired on the chart but has no corresponding `signals_v3` row — discoverable only by cross-referencing TradingView alert history against the database weeks later. Loud logging is what makes the "revisit on observation data if the constraint fails in practice" commitment above actually actionable: if we hit this edge case in observation, we know within minutes.
>
> This commitment is required by OI-05's post-review decision (see §11 summary table) and is what permits the UNIQUE constraint to be locked on merit.

---

### Edit 5 — §3.6 float-encoding fragility note

**Strategist source:** §4.3, §6 action item 5
**Type:** INSERT AFTER
**Location:** End of §3.6, immediately after the "Commitment" paragraph ending "...should only be taken if the straightforward path is blocked.", before the "### 3.7 Alertcondition message template" heading
**Rationale:** The plot-as-float coercion is fine for 13-digit ms-since-epoch values (float64 has ~15–16 decimal digits of precision), but this is a fragility that bites later. Any future field requiring more than 15 digits of precision would silently lose precision and fail in edge cases — the worst class of bug. Future-proofing the spec so no future schema change treats plot-as-float as a general-purpose integer transport.

**ANCHOR TEXT (insert immediately after this paragraph):**

> **Commitment.** The main indicator gains 10 new plot slots (indexes 32-41) to support Option C's event contract. If the verification task above fails (plot limit below 42), the backend can alternatively read the new fields from a JSON structure passed through a single plot — but that path is uglier and should only be taken if the straightforward path is blocked.

**INSERT:**

> **Float-encoding fragility note (post-Strategist review).** The plot-as-float coercion described above is sufficient for the current use case: 13-digit millisecond-since-epoch values fit comfortably within double-precision float's ~15–16 decimal digits of precision, and the backend's `int(float(value))` coercion recovers the exact integer. This is fine for Option C and the current v3.0 schema.
>
> **This encoding does not generalize.** Any future event payload field that requires more than 15 decimal digits of precision — microsecond timestamps, nanoseconds, composite encodings, high-precision numeric identifiers, or any value that could exceed `10^15` — MUST NOT use the plot-as-float transport. Silent precision loss on values exceeding float64's decimal precision would produce the worst class of bug: correct in 99% of cases and subtly wrong on the edge cases, with no error raised and no obvious diagnostic.
>
> **Commitment for future schema changes.** Any future field requiring more than 15 digits of decimal precision must use an alternative mechanism: a JSON-encoded string in the message template, a separate event channel, a composite encoded across multiple plots, or a different serialization entirely. This note exists to block the failure mode from ever being introduced by a future schema change that treats plot-as-float as a general-purpose integer transport without checking the precision bound.
>
> Future-proofing the spec, not a code change for Option C itself.

---

### Edit 6 — §8.5 distinct staleness logging

**Strategist source:** §4.5, §6 action item 6
**Type:** INSERT AFTER
**Location:** End of §8.5, immediately after the "Staleness and slot overflow" paragraph ending "...(cheaply — a short window) or closes GAP_CLEAN.", before the "### 8.6 Gap recovery — the `invoke_gap_recovery` function" heading
**Rationale:** The current `log_stale(position, gap_bars)` catches a stale position but cannot distinguish three different failure modes: slot overflow (Pine never assigned a slot), mid-position gap (delivery or state failure mid-lifecycle), and orphaned restart. All three hit the same handler. Distinguishing slot-overflow GAP_CLEANs from mid-position GAP_CLEANs in the log makes slot-overflow events grep-able without changing the `trade_outcomes` exit_reason.

**ANCHOR TEXT (insert immediately after this paragraph):**

> **Staleness and slot overflow.** §5.13 promised that a Pine slot overflow (Pine has 5+ positions but only 4 slots) would be caught by the staleness check. This is how: the 5th position is written to `signals_v3` and `positions` on ENTRY, added to `fsm_map`, but then never receives heartbeats because it is not in Pine's slot array. Its `last_heartbeat_bar_ms` stays at its initial value (0 or the opened_at_ms). Within 2 bars, the staleness check fires, detects the position has never been updated, invokes gap recovery, and either replays (cheaply — a short window) or closes GAP_CLEAN.

**INSERT:**

> **Distinct staleness logging commitment (post-Strategist review).** The staleness check's current `log_stale(position, gap_bars)` catches a stale position but does not distinguish between three architecturally distinct failure modes that all arrive at the same handler. Strategist review §4.5 requires these be distinguishable in the log so slot-overflow events can be grepped for independently.
>
> **The three failure modes:**
>
> 1. **Slot overflow** — the position exists in `signals_v3`, `positions`, and `fsm_map`, but Pine never assigned it a slot (because Pine's `N=4` slots were already occupied when this ENTRY fired), so Pine has never sent a heartbeat for it. Detection signature: `position.heartbeats_processed == 0` at the time the staleness check fires. Failure mode: Pine-side slot contention (§5.13 overflow case).
> 2. **Mid-position gap** — the position received heartbeats for some number of bars, then heartbeats stopped arriving before natural resolution. Detection signature: `position.heartbeats_processed > 0` at staleness-check time. Failure mode: TradingView delivery failure, Pine slot corruption mid-lifecycle, Pine closure heuristic incorrectly releasing the slot, or an unknown Pine-side state bug.
> 3. **Orphaned restart** — the position exists in `positions` on disk but `fsm_map` rehydration on restart failed to load it correctly, leaving it in an inconsistent state. Rare; indicates a backend rehydration bug. Caught by §8.4 restart recovery under normal operation; reaches the staleness handler only if restart recovery itself is broken.
>
> **Spec commitment.** The staleness handler MUST emit distinct log tags based on `position.heartbeats_processed` at the time of the check:
>
> - `⚠️ [STALENESS_NEVER_HEARTBEATED]` when `position.heartbeats_processed == 0`. Indicates slot overflow or complete delivery failure from the ENTRY bar onward. Includes `signal_id`, `bar_close_ms` of the ENTRY, and `gap_bars` since ENTRY.
> - `⚠️ [STALENESS_GAP]` when `position.heartbeats_processed > 0`. Indicates mid-position delivery or state failure after some successful heartbeats. Includes `signal_id`, `last_heartbeat_bar_ms`, `heartbeats_processed` count, and `gap_bars` since last heartbeat.
>
> **What stays the same.** Both cases still close via `GAP_CLEAN` per §8.8's single-exit-reason convention. The `trade_outcomes.exit_reason` value is `GAP_CLEAN` in both cases. The distinction is in the log tag only, not in the database row. This preserves Data Lab's clean category query (`WHERE exit_reason = 'GAP_CLEAN'`) for aggregate outage-cost analysis while giving `#system-log` a grep-able signal for slot-overflow events specifically.
>
> **Why this matters.** Slot overflow is a signal that Pine's `N` is sized wrong for actual concurrent-position pressure. If `[STALENESS_NEVER_HEARTBEATED]` fires repeatedly in observation, that's the specific trigger to raise `N` from 4 to 6 or 8 in Pine (§3.4, OI-04). If `[STALENESS_GAP]` fires repeatedly, that points at delivery reliability or Pine state corruption and requires a different investigation. One log tag could not tell these apart; two can. Two lines of code in the staleness handler, one paragraph in the spec.
>
> TC35 in §5.14 (added in Wave 4) includes assertions that the distinct tags are emitted on the correct condition; TC29 in §8.9 already covers the slot-overflow-to-GAP_CLEAN path end-to-end.

---

## 5. Wave 3 — §7.6 directional-alignment argument strip

**Strategist source:** §3 OI-06, §6 action item 4
**Type:** REPLACE
**Location:** §7.6, the "Rationale, in descending weight" numbered list (items 1–4)
**Rationale:** Strip the "one step toward the long-term goal of separated alertconditions per signal type" argument because it's the shape of "we're shipping a thing we wanted anyway" reasoning, which quietly accumulates scope under any architectural change. The decision wins on its own merits from the failure-case-elimination argument (same-bar ENTRY+HEARTBEAT collision from §6.2). With Josh's quota verified (Essential plan, 19 alerts of headroom), Case A is locked unconditionally. Future separation of ENTRY and EVAL into their own alertconditions earns itself separately when the time comes.

**BEFORE:**

> **Spec commitment: Option B — separate alertcondition for HEARTBEAT.**
>
> Rationale, in descending weight:
>
> 1. Eliminating the same-bar collision between ENTRY and HEARTBEAT removes a failure case from §6.2 (the measurement gap on ENTRY bars for other open positions). One less case to think about and one less log line category to watch for is worth the user-facing setup burden.
> 2. The architectural direction for The Mona is toward more-separated alertconditions, not fewer. The pre-observation accepted risk about the single-alertcondition bottleneck specifically anticipates "separate alertconditions planned pre-production." Option C is an opportunity to take one step in that direction, cheaply.
> 3. The second alertcondition is purely measurement. It does not change signal generation. The discipline rule applies cleanly.
> 4. The added user burden is a one-time setup cost (Josh creates one additional alert in TradingView after market hours). The wasted-payload-bytes cost of Option A is recurring.

**AFTER:**

> **Spec commitment: Option B — separate alertcondition for HEARTBEAT. Locked post-review to Case A (quota verified).**
>
> Rationale, in descending weight:
>
> 1. **Failure-case elimination (the load-bearing reason).** Eliminating the same-bar collision between ENTRY and HEARTBEAT removes a failure case from §6.2 — the measurement gap on ENTRY bars for other open positions. One less case to think about and one less log line category to watch for. This is the reason the decision stands on its own merits; all other considerations below are supporting, not load-bearing.
> 2. **Purely measurement, ships under the discipline rule.** The second alertcondition does not change signal generation. It does not change what Mona thinks, only what we can see about what she thinks. The observation-discipline rule applies cleanly — the edit ships on merit without requiring observation data to justify.
> 3. **Low user-burden cost.** The added setup cost is a one-time operation (Josh creates one additional alert in TradingView after market hours). The wasted-payload-bytes cost of Option A is recurring. The one-time cost is cheaper than the recurring cost over any non-trivial observation window.
>
> **Quota verification (post-Strategist review, April 13).** Per Strategist Review §3 OI-06, before locking Option B the spec requires verification that Josh's TradingView plan can support a second alertcondition. Verified: Josh is on the Essential plan (20 technical alerts cap) with 1 alert currently in use. Nineteen alerts of headroom is sufficient for Option C's two alerts (the existing `anySignalFired` and the new `fireHeartbeat`) with no upgrade required. Option B Case A is locked.
>
> **Note on the previous "architectural direction" argument.** Earlier drafts of this section included a fourth rationale item arguing that separating HEARTBEAT from ENTRY/EVAL was "one step toward the long-term goal of separated alertconditions per signal type." That argument is stripped in the post-review revision. Strategist §3 OI-06 flagged it as slippery — it is the shape of "we're shipping a thing we wanted anyway," which is the kind of reasoning that quietly accumulates scope under any architectural change. The decision wins on its own merits from item 1 (failure-case elimination); future separation of ENTRY and EVAL into their own alertconditions earns itself separately on its own merits when the time comes, not by piggy-backing on the Option C heartbeat work.

---

## 6. Wave 4 — New §5.14 Integration and Migration Test Cases

**Strategist source:** Review §5 (TC32–TC35)
**Type:** INSERT NEW SUBSECTION
**Location:** After §5.13 "Concurrent positions handling" (which ends with "...starts small (N=4) and grows on data, not on theory.") and before the "---" separator line that precedes the "## 6. KEYSTONES WALKED" heading
**Rationale:** Strategist Review §5 identified four integration and migration test cases that the §5.12 (resolver unit tests TC1–TC23) and §8.9 (outage recovery tests TC24–TC31) enumerations did not cover. These new cases test cross-component coordination: the tolerance mode migration window, replay idempotency against live heartbeats, concurrent gap recovery on multiple positions, and GAP_CLEAN fallback with unavailable `last_observed_close`. Placed in their own subsection rather than folded into §5.12 so that the TDD sequencing stays clean: write resolver unit tests first, implement resolver, then write integration tests against the wrapper.

**ANCHOR TEXT (insert immediately after this paragraph, then the new §5.14 heading follows on its own section):**

> **This overflow mode is observable.** Every instance where Pine overflows a slot is a log line (on the TradingView side, visible in the alert log; on the backend side, visible as a staleness event). Observation will tell us whether overflow is a real risk or a theoretical one. If observation shows overflows happening, N is raised from 4 to 8 (one-line Pine change per additional slot). The spec deliberately starts small (N=4) and grows on data, not on theory.

**INSERT (full new subsection):**

> ### 5.14 Integration and Migration Test Cases (TC32–TC35)
>
> §5.12's TC1–TC23 cover the pure resolver function as a black box. §8.9's TC24–TC31 cover the wrapper's outage recovery paths. Strategist Review §5 added four additional test cases that cover inter-component coordination not reachable by either of the above enumerations: the tolerance mode migration window, replay idempotency against overlapping live delivery, concurrent gap recovery on multiple positions, and the defensive GAP_CLEAN fallback when `last_observed_close` is unavailable.
>
> These are integration tests, not unit tests. They exercise the wrapper (`apply_resolver_result`), the migration layer (§10.3 Step 1 tolerance mode), the gap recovery dispatcher (§8.6), and the GAP_CLEAN path (§8.8). They are placed in their own subsection rather than folded into §5.12 so the TDD order stays clean during implementation: resolver unit tests first (§5.12 TC1–TC23), then resolver implementation, then outage recovery unit tests (§8.9 TC24–TC31), then outage recovery implementation, then finally these integration tests (§5.14 TC32–TC35) which cannot run until the full wrapper and migration layer are in place.
>
> **Total test count after §5.14:** 35 cases — TC1–TC23 (resolver), TC24–TC31 (outage recovery), TC32–TC35 (integration and migration). The 3:1 test-to-implementation ratio commitment from §1.7 holds: ~150–200 lines of resolver implementation against ~600–900 lines of tests.
>
> ---
>
> **TC32 — Tolerance mode coexistence during migration Step 1.**
>
> **Setup.** Backend running new Option C code in tolerance mode (per §10.3 Step 1). Database has v2.x tables (`signals`, `eval_results`) and v3.0 tables (`signals_v3`, `evaluations`, `trade_outcomes`, `positions`) coexisting. No positions in `fsm_map` at test start.
>
> **Test sequence.**
>
> 1. Send a v2.1.1-shaped ENTRY payload with no `bar_close_ms`, no `parent_bar_close_ms`, and no slot fields (i.e., the payload shape the current production Pine script emits).
>    - Assert: row written to `signals` (legacy table), no row in `signals_v3`, no row in `positions`, no entry in `fsm_map`. Legacy code path took the payload.
> 2. Send a v3.0-shaped ENTRY payload with `bar_close_ms` populated.
>    - Assert: row written to `signals_v3`, row created in `positions`, entry added to `fsm_map`, no row in `signals`. New code path took the payload.
> 3. Send a v3.0-shaped HEARTBEAT referencing the v3.0 position's `bar_close_ms` in slot 1.
>    - Assert: resolver runs, position state updates correctly, `last_heartbeat_bar_ms` advances, `heartbeats_processed` increments.
> 4. Send a v2.1.1-shaped EVAL payload referencing the v2.1.1 ENTRY's `signal_id` via the legacy lookup path.
>    - Assert: writes to `eval_results` via legacy code path, no attempt to look up parent in `signals_v3` or touch v3.0 tables.
>
> **Why this matters.** Step 1 of the migration (§10.3) is the critical deployment window where signal loss is most likely if tolerance mode silently fails. The backend must handle both payload shapes correctly during the interval between backend deploy and Pine deploy. If TC32 fails, the entire migration plan fails and the failure manifests only in production during the deploy window — when signals are live and delivery reliability matters most. This test is the mechanical guarantee that the two code paths coexist cleanly before the production migration runs.
>
> **Assertion invariants:** no cross-contamination between legacy and new tables; no exceptions on either payload shape; both tables accumulate rows in their respective schemas without interference; the `fsm_map` contains only v3.0 positions and never legacy ones.
>
> ---
>
> **TC33 — Replay idempotency under live-and-replay overlap.**
>
> **Setup.** Position open in `fsm_map`. Backend has processed heartbeats for bars T, T+1, T+2 live. A heartbeat gap is detected between T+2 and T+5 — the backend missed bars T+3 and T+4 (perhaps due to a brief Railway hiccup). Gap recovery invokes Finnhub replay for T+3 and T+4.
>
> **Test sequence.**
>
> 1. Run the gap recovery. Finnhub replay is mocked to return reconciled bars for T+3 and T+4.
>    - Assert: bars T+3 and T+4 are replayed through the resolver successfully. `last_heartbeat_bar_ms` advances to T+4. Position state reflects the replayed data.
> 2. Simulate a delayed TradingView delivery of the original T+3 heartbeat (the webhook was just slow, not dropped — TV sent it and it's arriving after recovery completed). The HEARTBEAT arrives at the backend with `bar_close_ms = T+3`.
>    - Assert: the wrapper's idempotency guard at §5.9 detects `bar.bar_close_ms < position.last_heartbeat_bar_ms` (T+3 < T+4) and rejects the delivery as a replay.
>    - Assert: the log emits `⚠️ [REPLAY] bar=T+3 last=T+4 signal_id=X`.
>    - Assert: the resolver is NOT called for this delayed delivery.
>    - Assert: position state is unchanged from after the recovery replay.
> 3. Simulate a live T+5 heartbeat arriving next.
>    - Assert: `last_heartbeat_bar_ms` was T+4 going in, T+5 coming out. Resolver runs normally.
>
> **Why this matters.** The §5.9 idempotency commitment is the property that makes Finnhub replay safe to combine with live heartbeat resumption. If a live heartbeat for an already-replayed bar is processed a second time, MAE/MFE values could double-update (depending on implementation) or state transitions could fire twice, producing phantom `trade_outcomes` rows or corrupted excursion statistics. The guard is supposed to prevent this. TC33 asserts the guard fires correctly in the specific scenario where replay and live delivery overlap — which is the most likely real-world manifestation of the race because TV delivery latency is variable and gap recovery can easily finish before a slow-delivered heartbeat arrives.
>
> ---
>
> **TC34 — Concurrent gap recovery on multiple positions in a single heartbeat.**
>
> **Setup.** Two positions open, A and B, both in `fsm_map`. Both have `last_heartbeat_bar_ms == T`. Both have missed heartbeats for bars T+1 through T+3. The next live heartbeat arrives carrying both slots (A in `pos_slot_1_time` and B in `pos_slot_2_time`) with `bar_close_ms == T+4`.
>
> **Test sequence.**
>
> 1. The HEARTBEAT handler receives the incoming bar. Gap check fires for both positions (each is 3 bars behind).
> 2. Gap recovery is invoked for position A first. Finnhub replay runs for A; three bars are processed through the resolver for A. Position A's `last_heartbeat_bar_ms` advances to T+3.
> 3. Gap recovery is then invoked for position B. Finnhub replay runs for B; three bars are processed through the resolver for B. Position B's `last_heartbeat_bar_ms` advances to T+3.
> 4. After both recoveries complete, the normal slot-iteration code path in the HEARTBEAT handler processes the live T+4 bar for both positions.
>    - Assert: both positions end up with `last_heartbeat_bar_ms == T+4`.
>    - Assert: both positions' state transitions across the replayed window are correct against the mocked Finnhub data.
>    - Assert: neither position's recovery interferes with the other's — position A's state is exactly what it would be if recovered in isolation, and the same for B.
>    - Assert: no race condition or cross-contamination of `fsm_map` during the recovery loop.
>
> **Why this matters.** §8.6 rule GR5 states that "recovery holds no FSM locks across the Finnhub network call" and notes that concurrent heartbeats for other positions are unaffected. The spec is otherwise silent on what happens when a single heartbeat triggers gap recovery for multiple positions in the same heartbeat handler invocation. This is probably fine because each position's recovery operates serially against its own state object and has no read/write contention with another position's recovery. But "probably fine" is not "tested fine." TC34 asserts the concurrency assumption explicitly rather than asserting it in prose. If this test fails, the spec's §8.6 needs a stronger locking or serialization rule before implementation proceeds.
>
> ---
>
> **TC35 — GAP_CLEAN fallback with unavailable `last_observed_close`.**
>
> **Setup.** Position open in OPEN state (never hit TP1). Backend has processed zero heartbeats for it since ENTRY — this is the slot-overflow scenario from §5.13. Staleness check fires after 2 bars without heartbeats (`heartbeats_processed == 0` at staleness-check time, triggering the `[STALENESS_NEVER_HEARTBEATED]` log per Edit 6 of this revision pass). Gap recovery is invoked with `trigger="staleness"`. Finnhub replay is attempted but returns an error (e.g., the Finnhub service is unreachable at this moment, or returns malformed data that fails reconciliation).
>
> **Test sequence.**
>
> 1. `close_gap_clean` is called with `exit_bar_ms = position.opened_at_ms` (the only bar_close_ms we know for this position) and `reason = "FINNHUB_UNAVAILABLE"`.
> 2. The function checks whether `exit_bar_ms == position.last_heartbeat_bar_ms`.
>    - For a position with zero heartbeats processed, `last_heartbeat_bar_ms` is at its initial value (null or equal to `opened_at_ms`, depending on how the column was initialized on INSERT).
>    - The branch detects that there is no `last_observed_close` available (no heartbeat has ever been processed to populate it).
> 3. The defensive fallback fires: `exit_price = position.entry_price`. This is the "we have no observation data at all" zero-PnL mark per §8.8.
> 4. `final_pnl_points` is computed for an OPEN position with exit at entry price: `3 * (exit_price - entry_price) * direction_sign == 0`.
> 5. The function writes `trade_outcomes` with `exit_reason = "GAP_CLEAN"`, `final_pnl_points = 0`, `closed_at_ms = opened_at_ms`, `mae_points = 0`, `mfe_points = 0`.
> 6. The function updates `positions` to `state = 3` (CLOSED).
> 7. The function removes the position from `fsm_map`.
> 8. The function posts the OUTCOME embed to `#trade-journal` with the `⚠️ GAP_CLEAN (FINNHUB_UNAVAILABLE)` annotation.
>
> **Assertions.**
> - `trade_outcomes` row exists with `signal_id = X`, `exit_reason = 'GAP_CLEAN'`, `final_pnl_points = 0`.
> - No exception raised during the fallback.
> - No null-pointer or AttributeError on the missing `last_observed_close`.
> - `positions.state == 3`, `positions.closed_at_ms == positions.opened_at_ms`.
> - `fsm_map` no longer contains the position.
> - OUTCOME embed posted with the correct annotation.
> - `#system-log` contains both `[STALENESS_NEVER_HEARTBEATED]` (from the staleness detection) and `[GAP_CLEAN]` (from the close) log entries.
>
> **Why this matters.** §8.8's defensive fallback path — "exit_price = position.entry_price when last_observed_close is unavailable" — is the hardest case in the spec to construct in production and the one most likely to crash if the fallback code path has a null-handling or default-value bug. It executes only when three rare conditions all fire together: (a) a position exists with zero heartbeats processed, (b) the staleness sweep catches it before any heartbeat can arrive, and (c) Finnhub replay fails or is unavailable. In production, this combined scenario might occur once in a thousand positions. TC35 exercises it explicitly so the fallback is verified before it is needed, not discovered broken when a real edge case finally triggers it. Defensive paths that are rarely taken are where bugs live; testing them is how bugs are killed before they bite.

---

## 7. Wave 5b — Update test count reference in §12.3

**Strategist source:** Implicit in TC32–TC35 addition
**Type:** REPLACE
**Location:** §12.3 "What changes under Option C (the net diff)", the "Test suite" line in the bullet list
**Rationale:** The closing retirement ledger references "31 test cases (TC1–TC31)" but with TC32–TC35 added in Wave 4, the correct count is 35. Minor fix; no prose change beyond the number.

**BEFORE:**

> - **Test suite:** 31 test cases (TC1–TC31) covering the resolver FSM, the wrapper, and outage recovery. Written before implementation, failing first, passing after.

**AFTER:**

> - **Test suite:** 35 test cases (TC1–TC35) covering the resolver FSM (TC1–TC23), outage recovery (TC24–TC31), and integration/migration cases (TC32–TC35 in §5.14). Written before implementation, failing first, passing after. Test-to-implementation ratio ~3:1 against an estimated 150–200 lines of resolver code, per §1.7's TDD commitment.

---

## 8. Wave 5c — Filename

**Action:** Save the revised document as `MONA_v3_OptionC_Spec_v1.1.md`.

The April 13 v1.0 draft (`MONA_v3_OptionC_Spec.md`) is preserved as the pre-revision baseline for the paper trail, per OI-11's "in-place modify" commitment for the `mona_v3_0.txt` Pine script — by analogy, the spec itself also keeps both versions on disk so the revision history is inspectable without relying solely on git. The v1.1 filename is unambiguous about which version is current.

---

## 9. Summary of changes

**Wave 1 (3 edits — wording and framing):**
- Edit 1: §5.6 PF1 commentary added (1 new paragraph)
- Edit 2: §9.3 `entryBarCloseMs` rewritten as judgment-not-rule with no-precedent commitment and four-part test (~35 new lines)
- Edit 3: §11 post-review decisions table inserted (1 new table with 12 resolved items, ~20 new lines)

**Wave 2 (3 edits — new logging and fragility commitments):**
- Edit 4: §4.2 `[UNIQUE_VIOLATION]` loud-logging commitment (~25 new lines)
- Edit 5: §3.6 float-encoding fragility note (~15 new lines)
- Edit 6: §8.5 distinct staleness logging commitment (~40 new lines)

**Wave 3 (1 edit — §7.6 alertcondition rationale revision):**
- Stripped the directional-alignment argument (item 2 of the original 4-item rationale list)
- Reorganized remaining rationale from 4 items to 3
- Added quota verification paragraph documenting Josh's TradingView plan
- Added note-on-the-previous-argument paragraph documenting the strip and Strategist's reasoning

**Wave 4 (1 new subsection — 4 new test cases):**
- New §5.14 "Integration and Migration Test Cases" with TC32–TC35 (~180 new lines total)
- TC32: Tolerance mode coexistence
- TC33: Replay idempotency under live-and-replay overlap
- TC34: Concurrent gap recovery on multiple positions
- TC35: GAP_CLEAN fallback with unavailable `last_observed_close`

**Wave 5 (3 edits — packaging):**
- 5a: Top-matter status updated (DRAFT → REVISED awaiting final merge read)
- 5b: §12.3 test count reference updated (31 → 35)
- 5c: Saved as `MONA_v3_OptionC_Spec_v1.1.md`

**Total new lines added:** approximately 315 lines of prose across 6 existing sections + 1 new subsection. Zero lines deleted. Zero existing sections restructured. Zero architectural changes. Zero cognition touches. Zero schema changes. Zero resolver logic changes.

**Spec growth:** from ~2,729 lines (v1.0) to ~3,044 lines (v1.1), an 11.5% increase driven entirely by logging commitments, test case additions, and the §9.3 principle-integrity framing.

**Review loop integrity held:** every edit in this revision pass is traceable to either (a) a specific Strategist review item, or (b) a pre-flight quota check that Josh ran. No edit is a Dev Trader-initiated scope expansion.

---

## 10. Handoff to Strategist

Per Strategist Response §5 ("Proceed order for Waves 3, 4, 5"), the merged v1.1 document is staged for Strategist's final merge read. No further Strategist review is expected between the waves unless something surprising surfaces during the merge read itself.

**What Strategist will verify in the merge read:**

1. All 9 revision items from Strategist Review §6 action list landed in v1.1.
2. The OI-06 §11 table row reflects the resolved Case A state with quota verification, not the pre-quota "Wave 0 check required" language.
3. The directional-alignment argument is stripped from §7.6's rationale list, with only the failure-case-elimination argument remaining as load-bearing.
4. TC32–TC35 are placed in §5.14 as new subsection, not folded into §5.12.
5. The §9.3 `entryBarCloseMs` rewrite includes the four-part test and the no-precedent commitment.
6. No line of the Reputation Engine was touched. The §9.2 per-section audit passes.
7. The `grep` test from §9.4 still holds: the diff against `mona_v3_0.txt` should show only the additive changes named in §9.4, with no modifications to `repState`, `evalPending`, lockout counting, ghost tracking, or filter conditions.

**After the merge read:** spec is locked and implementation phase begins with OI-01 (plot limit verification) as the first hands-on-keyboard task, followed by TDD on the resolver (TC1–TC35 written first as failing tests, implementation written to pass them), then backend non-resolver work, then Pine §7 additions after market hours, then dry-run smoke test, then production deploy per §10.3 after market hours, then Day 1 of observation with the clock starting on the first clean end-to-end signal cycle per OI-12.

**Ferrari mode holds throughout.** No deadline on implementation. Quality bar held by Dev Trader; clock held by Josh; review integrity held by Strategist. The three-seat review triangle remains open and reachable throughout implementation per Strategist Review §7.

---

*End of revision package. See you on the other side of v1.1 final lock.*

*— Dev Trader, The Mona Project, April 13, 2026*
