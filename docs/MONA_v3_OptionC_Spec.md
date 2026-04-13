# The Mona v3.0 — Option C Architectural Specification

**Author:** Dev Trader (Senior Quant Dev)
**Date:** April 13, 2026 (Monday)
**Status:** DRAFT — awaiting Strategist review after all five parts land
**Supersedes:** `MONA_v3_Master_Audit.md`, `MONA_v3_Migration_Plan.md`, `MONA_v3_Test_Checklist.md`, `MONA_v3_Rollback_Plan.md`, `MONA_v3_Monitor_Bench_Test.md`, `MONA_v3_DevTrader_Response_to_Strategist.md` (all Option A paper trail — retained as historical record, no longer current direction)
**Builds on:** `MONA_v3_Architectural_Rethesis_Option_C.md` (Sunday pre-spec), `MONA_v3_Finding0_Monitor_Reputation_Divergence.md`, `MONA_v3_OptionC_Going_In_Notes.md`, Strategist's Sunday-night closeout handoff

---

## 0. HOW TO READ THIS SPEC

This document is the architectural specification for v3.0 of The Mona under Option C — the direction Josh decided on Sunday night, April 12, after the Strategist review of the Master Audit surfaced Finding #0 and the implication that the v3.0-with-Monitor architecture had a structural problem, not a patchable bug.

It is delivered in five parts because it needs to be thorough enough to survive a cold read by someone who was not in the Sunday-night conversation, and that length does not fit in one response without compression. The parts are logically one document. Section numbers run continuously from §1 in Part 1 to §12 in Part 5. When a later part references an earlier section it does so by section number, not by filename.

**Table of contents, all parts:**

| Part | Sections | Topic |
|---|---|---|
| 1 (this file) | §1, §2 | Framing; System Architecture |
| 2 | §3, §4 | Event Contract; Database Schema |
| 3 | §5 | Resolver FSM — states, transitions, test cases, edge cases |
| 4 | §6, §7 | Keystone Walked; Heartbeat Plot in Main Indicator |
| 5 | §8, §9, §10, §11, §12 | Railway Outage Recovery; Reputation Engine Integration; Migration and Rollback; Open Items; What Dies, What Survives |

No part depends on later parts for correctness of its own content. Later parts fill in depth on areas that earlier parts identify as needing it.

**Reading order for a cold reader:** §1 gives the why. §2 gives the new picture. §3 and §4 give the wire format and the shape of persisted state. §5 gives the heart of the system — the resolver FSM and the test cases it has to pass. §6 walks the failure modes against that FSM explicitly. §7 gives the one place Option C touches Pine Script. §8 handles outages. §9 confirms the Reputation Engine stays exactly where it is. §10 says how we get from production to this and back if it breaks. §11 flags the items that still need a decision. §12 is the clean ledger of what this spec retires and what it preserves.

---

## 1. FRAMING

### 1.1 Why this spec exists

This spec exists because on Sunday April 12, Strategist pushed a keystone question on Finding #1's fix spec: *prove that the main indicator's `bar_close_ms` and the Monitor's `entry_time_ms` always reference the same bar, don't assume it.* Walking that proof with both scripts open side-by-side surfaced a deeper problem than the one being proved. The proof cracked because the two scripts don't fire on the same bar, because they don't gate on the same conditions, because the Monitor Pine Script does not replicate the main indicator's Reputation Engine gating at all. That finding is written up in full in `MONA_v3_Finding0_Monitor_Reputation_Divergence.md` and is summarized below in §1.2. It matters here because it is the specific bug that exposed a structural pattern — and the structural pattern, not the specific bug, is what this spec addresses.

The Sunday-afternoon rethesis document (`MONA_v3_Architectural_Rethesis_Option_C.md`) laid out three responses to the finding: Option A patches the Monitor in place with exact-match lookups and an orphan-clean rule; Option B folds the Monitor into the main indicator so one Pine Script owns both signal generation and position resolution; Option C retires the Monitor entirely and moves position resolution into the Python backend, using a lightweight heartbeat from the main indicator to feed bar data forward as positions evolve.

Sunday night, after Strategist review, Josh decided Option C. His principle is recorded in §1.4 and is the design constraint this spec is written under. The decision was not primarily about the bug — it was about eliminating the class of architectural weakness the bug came from, and about aligning Mona's structure with what Mona actually is.

This spec is what Option C looks like in detail: the event contract, the schema, the finite state machine that replaces the Monitor's position arrays, the keystone walked at the intra-script level, the Pine Script heartbeat plot, the outage recovery mechanism, the migration path, and the ledger of what ships and what retires.

### 1.2 Layer 1 — The specific bug

The Monitor Pine Script (`mona_v3_0_monitor.txt`, 706 lines, currently retired) opens position tracking slots based on raw filter conditions plus session window plus free-slot availability. Concretely, its slot-opening test is `trendLong and inSession` or the symmetric squeeze variants, where `trendLong` is the AND of `vwapBull`, `bullStack`, `htfBull`, `trendStochBull`, `adxOK`, and `volOK`. Those are the filter conditions that make an entry *possible* in the main indicator. They are not the conditions that make one *fire*.

The main indicator's ENTRY path (`mona_v3_0.txt` line 354 for trend, line 526 for squeeze) wraps those filter conditions in an additional gate: `if trendTrk.repState == ELIGIBLE and not trendTrk.evalPending and not fireTrendAlert`. The Reputation Engine is the whole point of that gate. During the 4-bar eval window that follows any real entry, `evalPending` is true and no new entries fire. After a failed follow-through, `repState` is `GROUNDED` and no new entries fire for 8 bars. After a second failed follow-through, `repState` is `EXTENDED` and no new entries fire for 16 bars. That lockout behavior is the mechanism by which Mona says NO to bad setups — it is the core cognitive behavior the system was built for.

The Monitor contains no `repState` tracking, no `evalPending` tracking, no lockout logic, no ghost eval handling. A grep against the Monitor source for `repState`, `evalPending`, `ELIGIBLE`, `GROUNDED`, `EXTENDED`, `lockoutEnd`, or `cooldown` returns empty. The Workshop 3 design note written at the time the Monitor was built said the Monitor would duplicate "the same filter conditions" — and that language is technically accurate, but the accuracy is where the bug lives. Filter conditions were duplicated. The gating around those filter conditions was not.

The operational consequence is that on any bar where raw filter conditions fire *and* the main indicator's tracker is in any non-eligible state (eval window active, GROUNDED, or EXTENDED), the Monitor opens a position slot that the main indicator never alerted on. The slot tracks, resolves to TP1 or SL or EOD like any other slot, and emits a `TRADE_OUTCOME` webhook to the backend when it resolves. These are phantom positions — they exist in the Monitor's state but not in Mona's cognition.

The backend's current parent-lookup function, `find_parent_by_lookback` (line 350 of `mona_v3_0_backend.py`), grafts those phantom outcomes onto the most-recent real `signals_v3` row matching the same `signal_type` and `direction` within a two-hour window. There is almost always such a row on a trending day. The phantom's PnL, MAE, MFE, exit reason, and time-in-trade get written to `trade_outcomes` keyed by a `signal_id` that does not identify the position that produced them. The join into `signals_v3` succeeds. The row looks populated. Every Data Lab analysis that keys off it is suspect.

**Three scenarios drive the phantom rate**, in descending likelihood on a typical trading day:

*Scenario A — Eval-window retrigger.* A real signal fires at bar T. `evalPending` becomes true for 4 bars. During bars T+1 through T+4, trending conditions easily re-fire — `trendStochBull` only requires `k > d` and `k > k[1]`, which wiggles on every bar of a strong trend. The main indicator fires nothing in those 4 bars. The Monitor opens a slot on every bar where raw conditions refire and a free slot exists. Every one of those is a phantom.

*Scenario B — GROUNDED lockout retrigger.* An eval fails follow-through at bar T. Tracker goes to `GROUNDED` with lockout to T+8. Raw conditions can and do re-fire during that 8-bar window because the conditions that failed the first eval have not fundamentally changed. Main indicator fires nothing. Monitor opens slots. Phantoms.

*Scenario C — EXTENDED lockout retrigger.* Two consecutive failed evals. 16-bar lockout. Same mechanism as B, wider window, more phantoms.

The rate estimate from Finding #0 is 2–5 phantom slots per real trend signal on a strong trending day. Squeeze signals are less affected because the squeeze entry condition requires `ta.crossover(ema9, ema21)`, which by definition does not repeat inside the same regime. Trend-heavy days are where the phantom rate is worst. Trend-heavy days are also where signal quality matters most and where the Reputation Engine is working hardest. The blind spot aligns with the scenarios the Engine exists to protect against.

The reason this has not yet been observed in production is that Day 1 (April 10, v2.1.1 baseline) had one signal, that signal fired while the tracker was ELIGIBLE, the eval window completed cleanly, and no retriggers occurred. N=1 is insufficient to express the bug. It would have surfaced in Week 1, probably on the first day with a failed follow-through followed by a continuing trend.

### 1.3 Layer 2 — The bug class

The specific bug in §1.2 is one instance of a structural pattern. The pattern is worth naming because it is the reason this spec exists, and because the pattern is what Option C structurally eliminates — not the instance.

**The pattern: any time two independently-maintained pieces of state are supposed to agree by construction, with agreement enforced by convention rather than mechanism, you have a bug farm.**

The main indicator's `trendTrk` / `sqzTrk` tracker state and the Monitor's `entryBars` / `entryPrices` parallel arrays are that pattern exactly. They are supposed to agree by construction — the design document said so, the Workshop 3 discussion assumed so, the initial audit passes treated the agreement as invariant. Agreement was enforced by the convention that both scripts would compute their conditions from the same inputs via duplicated logic. The duplication was incomplete. Filter conditions got duplicated. Gating conditions did not. Finding #0 is the observable consequence of the pattern failing.

Option A (fix in place with exact-match) addresses the observable consequence. Phantom outcomes would orphan cleanly to `#mona-log` as `❌ [ORPHAN]` instead of silently corrupting `trade_outcomes`. That is a real improvement over the status quo. What it does not do is eliminate the pattern. A year from now, when we change the Reputation Engine to add a third signal type or adjust the lockout durations or tune the ghost redemption rule, that change has to land in both scripts in lockstep or a new divergence class opens. Every audit pass going forward has to re-verify that the two sides still match. Every feature that touches shared conditions carries the lockstep tax. The pattern stays; the pattern just gets watched more carefully.

Option B (fold Monitor into main indicator) eliminates the pattern by making there be only one script holding state, which means there is nothing to agree by construction because there is only one construction. That is better than A. It leaves position resolution logic in Pine Script, which means every future iteration on TP/SL placement, trailing stops, timeout rules, or intrabar conventions still happens in a language where unit testing is effectively impossible, historical replay requires TradingView bar-replay and a human watching, and the scripting environment imposes constraints on loop structure and same-bar state variable ordering that generated Finding #2 in the first place. B solves the pattern and leaves the environment that made the pattern likely.

Option C eliminates the pattern the same way B does — one source of truth, nothing to reconcile — and additionally moves the domain where resolution logic lives to a language (Python) where the logic can be exhaustively unit-tested, replayed against synthetic bar sequences, version-controlled, diffed across changes, and iterated on without a deploy cycle. The test suite for the resolver becomes the authoritative statement of how the system resolves positions, because it is runnable, assertable, and versioned alongside the code it tests. None of those properties are available in Pine Script.

The test Dev Trader applied to Option C when writing the rethesis, and which Strategist confirmed in review: *do the existing bugs become impossible under the new architecture, or do they just get patched?* Under Option C:

- **Finding #0** cannot occur. There is no second script with independent gating. There is nothing to gate.
- **Finding #1** (concurrent position wrong-signal-id assignment under `find_parent_by_lookback`) cannot occur. `signal_id` is assigned at the moment the position is created, by the backend, at the same time the `signals_v3` row is written. The position record carries its `signal_id` from creation forward. Nothing is ever reconstructed via fuzzy lookup.
- **Finding #2** (MAE/MFE under-reporting caused by Pine Script's same-bar state variable ordering) cannot occur. MAE/MFE is computed in a Python function that runs on each incoming bar with full control over loop ordering and variable lifetimes, with unit tests asserting the exact update sequence.

Three findings retired by construction. Not patched. Retired.

### 1.4 The principle — Mona's brain lives in one place

Josh's decision on Sunday night was driven by a principle that Strategist elevated from Josh's reaction to a proposal earlier in the weekend to install a second Reputation Engine inside the Monitor Pine Script for a different reason. Josh's language, recorded in the Going-In Notes:

> *"As soon as he started talking about a 2nd Reputation Engine I was out. What we have so far is too good."*

Strategist elevated that instinct into the design principle this spec is written under:

> **Mona's brain lives in one place.** The Reputation Engine is the system's cognition — the part that decides what Mona thinks, when she is allowed to think it, and when she has to be silent. Any architectural decision that would fragment cognition across multiple processes, scripts, or services bumps against this principle and loses by default. **Measurement can be distributed. State can be replicated for resilience. Cognition is singular.**

The distinction this principle draws — between cognition, measurement, and state — is load-bearing throughout the rest of the spec and should be made explicit here:

**Cognition** is the part of the system that decides what a signal is, whether it is allowed to fire right now, and what its reputation state becomes afterward. In Mona, that is the Reputation Engine plus the filter conditions plus the signal-firing logic, and all of it lives in the main indicator Pine Script. Under this principle, cognition is not allowed to exist in more than one place. There is exactly one Reputation Engine. There is exactly one authority on "is Mona allowed to say something right now."

**Measurement** is the part of the system that observes what happened after Mona said something. Under this principle, measurement can be distributed — the main indicator can emit bar data via a heartbeat, the backend can compute MAE/MFE/TP1/SL outcomes, a Data Lab analysis script can run offline against historical `signals.db` snapshots, and a future Finnhub recovery path can replay missing bars after a Railway outage. All of these are measurement activities. None of them fragment cognition because none of them are allowed to decide what Mona thinks.

**State** is the data on disk that records what cognition produced and what measurement observed — the `signals_v3` table, the `evaluations` table, the `trade_outcomes` table, and the new `positions` table this spec introduces. Under this principle, state can be replicated for resilience (WAL mode on SQLite is one such replication, persisted positions as introduced in §4 is another). Replicating state is not the same as fragmenting cognition, because state is a record, not a decider.

The Monitor Pine Script, as built, violated the principle in a specific way: it was pitched as measurement ("a hypothetical execution tracker") but it implemented its own entry-detection logic, which made it a decider. By deciding when to open a slot, it was participating in cognition — it was saying "Mona would have entered here" at bars where Mona, in fact, was locked out and said no. Once a piece of software is deciding when Mona thinks something, it is cognition, regardless of what the design document labels it.

Option C resolves this by removing the Monitor's decision authority entirely. The backend under Option C never decides when an entry happens. Entries are announced to the backend by the main indicator's ENTRY webhook and no other path. The backend observes bar data via heartbeats and updates position state based on observed data against the levels that the entry webhook stamped. The backend is pure measurement and pure state. Cognition stays in the main indicator, exactly where it was, exactly as it was, unchanged by a single line.

### 1.5 Ghost-eval verification (confirmed isolated)

Finding #0 flagged one adjacent concern that had to be resolved before this spec could specify the positions schema: can ghost evaluations leak into `signals_v3` by any code path, such that the new FSM might consume a ghost event as a real entry?

A ghost evaluation is an EVAL_RESULT emitted during a lockout when no real position exists — it is the Reputation Engine's accounting for signals that would have happened but were suppressed, used to compute redemption paths back to ELIGIBLE. Ghost evals are written with `is_ghost=1` to the `evaluations` table. If any code path were to promote a ghost eval into a `signals_v3` row, the FSM would see a phantom entry and Option C would have its own version of the Finding #0 bug, only now in Python instead of Pine.

This was verified twice. First, informally, by reading the backend on April 13 (recorded in the Workshop opening message). Second, formally, while writing this spec, by grepping and tracing the webhook handler in `mona_v3_0_backend.py`.

**Verification result: isolated.** The backend routes webhooks by `status` field, which is the top-level discriminator in the payload. Three routes exist: `status == "ENTRY"` writes to `signals_v3` and nothing else (line 812). `status == "EVAL_RESULT"` writes to `evaluations` and nothing else (line 850). `status == "TRADE_OUTCOME"` writes to `trade_outcomes` and nothing else (line 895 in the current v3.0 backend; this route is retired under Option C). The `is_ghost` column exists only on the `evaluations` table schema (line 286 of `init_db`); `signals_v3` has no `is_ghost` column at all. There is no code path — no fallthrough, no secondary insert, no reconciliation loop — that moves data from `evaluations` to `signals_v3`.

**Consequence for the spec.** The new resolver FSM defined in §5 consumes exactly one event class as its entry trigger: ENTRY events routed into the backend via the `status == "ENTRY"` path. That path produces rows in `signals_v3`. Those rows cannot, by construction, represent ghost evaluations. The FSM therefore does not need ghost-eval handling as an edge case. The new `positions` table defined in §4 does not need an `is_ghost` column. The resolver test suite in §5 does not need a ghost-eval test case for the positions side of the system.

This does not mean ghost evaluations go away — they still fire, still get written to `evaluations` with `is_ghost=1`, still inform the Reputation Engine's redemption logic on the Pine side, and still show up in Data Lab analyses of the Reputation Engine's behavior. It means they never enter the resolver's scope.

**Spec commitment:** the FSM consumes ENTRY events only; ghost evaluations are out of scope for position resolution by construction, verified against the routing code in `mona_v3_0_backend.py` on April 13, 2026.

### 1.6 What this spec is and is not

**This spec IS:**

- The architectural specification for the Option C version of v3.0
- The authoritative statement of the resolver FSM's states, transitions, edge cases, and test obligations
- The definition of the new `positions` table schema and its invariants
- The definition of the heartbeat event contract between the main indicator and the backend
- The enumeration of the keystone failure cases under Option C and the spec's answer to each
- The migration path from current production (v2.1.1: `PreObFinal.py` + `mona_v2_1.txt`) to Option C v3.0
- The rollback path if Option C goes wrong
- A ledger of what this direction retires and what it preserves
- A statement of open items requiring Strategist review before code is written

**This spec IS NOT:**

- Code. No line of Python or Pine Script in this spec is executable as-delivered. Code sketches appear only where they are needed to make an architectural point unambiguous, and those sketches are always labeled as illustrative.
- A deploy plan. Josh removed the deploy pressure variable Sunday night. This spec does not contain a timeline, does not estimate days of work, does not propose a deploy target date, and does not compress scope to hit any external calendar constraint. The spec is done when it is thorough enough to pass Strategist review and cover the edge cases it needs to cover. The code is done when its tests pass. The deploy happens when the code is done and Josh says go.
- A signal-logic change. This spec does not touch the Reputation Engine, the filter conditions, StochRSI, the volume floor, the ADX threshold, the session window, the squeeze detection, or any other element of what Mona thinks. By the observation-discipline rule Strategist laid down — "does this change what Mona thinks, or what we can see about what she thinks?" — every architectural change in this spec is on the measurement side and ships on merit without observation data to justify it. The signal side is frozen. The 30-day observation clock has not started and does not start until Option C ships and a clean first webhook lands in `#trade-journal`.
- A redesign of peripheral systems. Discord reorganization from Workshop 3 stays. The embed format stays. The layer-specific logging convention (📥 📝 📤) stays. The `#trade-journal` user experience is byte-identical post-migration.

If any section of the spec appears to drift into scope beyond this list while being written or reviewed, the drift gets caught and cut. Noted, not in this spec.

### 1.7 The spec-before-code commitment

One procedural note, elevated here because it is load-bearing for how this spec should be reviewed:

This document is being written before any code is modified. The rule is the one Dev Trader committed to in the Finding #1 retrospective and Strategist codified as pattern commitment: spec first, then the test suite that the spec demands, then the implementation that the test suite guards. If during spec-writing an ambiguity surfaces that cannot be resolved without reading code, Dev Trader reads the code, records the resolution as a spec commitment, and keeps writing — but does not write new code as a result of the ambiguity. If during test-writing an edge case surfaces that the spec did not anticipate, Dev Trader updates the spec first and only then writes the test. Implementation comes last.

This ordering is the mechanism by which the architectural win of Option C actually gets delivered. If the implementation is written first and the tests are backfilled to match, the tests become a checkbox and the architectural advantage evaporates into the same kind of "trust me, it works" state Pine Script produced. Strategist flagged this explicitly in the closeout: *the test suite should take longer than the implementation, not shorter.* That ratio is the deliverable.

---

## 2. SYSTEM ARCHITECTURE

### 2.1 The new picture

Under Option C, The Mona is a single-cognition system with measurement distributed across two processes. The boundary between cognition and measurement is sharp and the spec will return to it throughout.

**Components:**

1. **Main indicator (Pine Script, `mona_v3_0.txt` with additions).** Runs on TradingView against the MNQ1! 15-minute chart. Contains the Reputation Engine, the filter conditions, the signal-firing logic, and under Option C gains a lightweight heartbeat plot that fires on each confirmed bar close while at least one position is open in its local tracking state. Produces three webhook event types: ENTRY (a signal fired), EVAL (a past signal's 4-bar follow-through window completed), and HEARTBEAT (a bar closed and at least one position is still open). All three are emitted via a single `alertcondition` — this is the single-alertcondition bottleneck flagged as an accepted risk in the Pre-Observation Report; the spec addresses its interaction with heartbeat in §7.

2. **Backend (Python / FastAPI, `mona_v3_0_backend.py` with substantial additions).** Runs on Railway. Receives webhooks from the main indicator, parses and validates them, routes them to three handlers: ENTRY (writes `signals_v3`, creates a `positions` row, acknowledges), EVAL (writes `evaluations`, posts to Discord `#trade-journal`, acknowledges), HEARTBEAT (runs the resolver FSM against every open position using the bar data in the heartbeat payload, updates `positions` state, writes `trade_outcomes` when a position resolves, posts to `#trade-journal` on close). Also runs startup-time rehydration from persisted `positions` rows after a restart, and a Finnhub-based gap recovery path when it detects a heartbeat gap. See §5 for the FSM, §8 for outage recovery.

3. **SQLite (`signals.db` on Railway's persistent volume, schema in §4).** Holds four tables: `signals_v3` (unchanged), `evaluations` (unchanged), `trade_outcomes` (unchanged in shape, unchanged in who consumes it, but now written by the backend resolver instead of by the Monitor), and a **new `positions` table** that represents open and closed position state. The positions table is the persistence layer for the FSM; see §4 for schema and §5 for how the FSM uses it.

4. **Discord (unchanged from Workshop 3).** Receives embeds for ENTRY, EVAL, and OUTCOME events in their respective channels. The user-facing experience is byte-identical to the v3.0-with-Monitor plan: an ENTRY embed when a signal fires, an EVAL embed when follow-through evaluates, an OUTCOME embed when a position resolves. What changes is who writes the OUTCOME embed — the backend resolver now does it when a position closes, where previously the Monitor emitted a TRADE_OUTCOME webhook and the backend built the embed from the Monitor's payload.

**Components that no longer exist under Option C:**

- **Monitor Pine Script (`mona_v3_0_monitor.txt`, 706 lines).** Retired entirely. The file stays on disk as reference material for the resolver test suite — its priority rules and edge-case handling translate into test cases in §5 — but the script is never deployed and never runs in production again.
- **`find_parent_by_lookback` in the backend.** Retired. Positions know their `signal_id` from creation; nothing needs to reconstruct it via fuzzy lookup. The function and its call sites (currently used by the EVAL and OUTCOME routes in the v3.0 backend) are removed. The EVAL route still needs a parent lookup, but under Option C it can use an exact lookup via `bar_close_ms` against `signals_v3` because the main indicator's EVAL events now carry the parent signal's `bar_close_ms` explicitly — this is addressed in §3 (event contract).

### 2.2 Component roles, stated as responsibilities

Stating each component's responsibilities in one-line form makes the cognition/measurement boundary audit-able: each responsibility is either cognition, measurement, or state, and a reviewer can point at any line and ask which it is.

**Main indicator responsibilities:**

- [Cognition] Compute filter conditions for TREND and SQUEEZE on each bar
- [Cognition] Maintain Reputation Engine state (`trendTrk`, `sqzTrk`, `evalPending`, lockouts, ghost tracking, follow-through evaluation)
- [Cognition] Decide whether an ENTRY fires on this bar by gating filter conditions on reputation state
- [Cognition] Compute and stamp entry levels (`entry_price`, `sl`, `tp1`, `tp2`, `stop_pts`, `atr` values) at the moment an ENTRY fires
- [Measurement] Emit ENTRY webhook payload when cognition has decided an entry fires
- [Measurement] Emit EVAL webhook payload when the 4-bar follow-through window for a past signal completes
- [Measurement] Track locally which entry bars' positions are "open from Pine's point of view" in a small array of `bar_close_ms` values
- [Measurement] Emit HEARTBEAT webhook payload on each confirmed bar close while the local openPositionTimes array is non-empty
- [State] None — the main indicator holds transient Pine state but persists nothing. Its state is re-derivable from historical bars on reload.

**Backend responsibilities:**

- [Measurement] Receive, authenticate, parse, sanitize, and route webhook payloads
- [State] Write `signals_v3` rows on ENTRY events, creating the definitive record of what Mona decided to say
- [State] Write `positions` rows on ENTRY events, creating the record the resolver FSM consumes
- [State] Write `evaluations` rows on EVAL events, linked to the parent signal via exact `bar_close_ms` match
- [Measurement] Run the resolver FSM on each HEARTBEAT event: iterate open positions, update MAE/MFE per position, check each position against its TP1/SL/BE/TP2/EOD transition criteria, update the `positions` row to reflect the new state
- [State] Write `trade_outcomes` rows when the resolver closes a position, carrying the full outcome record
- [Measurement] Post ENTRY, EVAL, and OUTCOME embeds to Discord
- [State/Resilience] Rehydrate in-memory FSM state from `positions` rows on startup, so a Railway restart mid-position does not lose state
- [Measurement/Resilience] Detect heartbeat gaps, trigger Finnhub replay on gaps within recoverable windows, mark positions `❌ [GAP]` on unrecoverable gaps
- [Cognition] **None.** The backend never decides when an entry happens. Never decides when Mona should be silent. Never participates in the Reputation Engine. The backend is strictly measurement and state.

The absence of a cognition line in the backend's responsibilities is the architectural guarantee this spec is making. Every FSM edge case in §5 and every outage recovery rule in §8 will be tested against this guarantee: *does this rule require the backend to decide something that Mona's cognition is supposed to decide?* If yes, the rule is wrong. If no, the rule may still be wrong for other reasons, but it does not violate the principle.

### 2.3 Event flow — happy path

This subsection describes the end-to-end flow for a single real position from entry to resolution, with no errors, no gaps, no out-of-order delivery. It exists to give a reader a concrete sense of how the parts fit before §3 and §5 go into detail. Error and edge cases are deferred to later sections.

**T+0: Signal fires.** At the close of bar T, the main indicator's cognition determines that all filter conditions are met for a TREND LONG entry and that `trendTrk.repState == ELIGIBLE and not trendTrk.evalPending`. The indicator computes `entry_price`, `sl`, `tp1`, `tp2` from the current bar's close and ATR. It sets `trendTrk.evalPending = true`, increments its local `openPositionTimes` array by one element equal to the current bar's `bar_close_ms`, and fires the ENTRY webhook payload.

**T+0 + ~1 second: Backend receives ENTRY.** The webhook handler parses the payload, validates the `status == "ENTRY"` route, and:

1. Writes a new row to `signals_v3` with all filter and level values from the payload. SQLite assigns `signal_id = N`.
2. Writes a new row to `positions` with `signal_id = N`, `bar_close_ms` (from the payload's `bar_close_ms` field), `entry_price`, `sl`, `tp1`, `tp2`, `direction`, `signal_type`, `state = OPEN`, `tp1_hit = 0`, and MAE/MFE initialized to zero or null per the schema in §4.
3. Builds and posts the ENTRY embed to Discord `#alerts`.
4. Acknowledges the webhook with HTTP 200.

At this point the system's state is consistent: Pine knows about the open position (in `openPositionTimes`), the backend knows about the open position (in `signals_v3` and `positions`), and the backend's in-memory FSM state holds a reference to the `positions` row.

**T+1: First heartbeat.** At the close of bar T+1, the main indicator's heartbeat logic checks `array.size(openPositionTimes) > 0`. It is — the array has one element. The indicator fires the HEARTBEAT webhook with this bar's OHLC, the bar's `bar_close_ms`, and the `openPositionTimes` array serialized as a comma-separated string.

**T+1 + ~1 second: Backend receives HEARTBEAT.** The handler parses, routes on `status == "HEARTBEAT"`, and:

1. Parses `open_position_entry_times` into a list of integers.
2. For each integer, looks up the corresponding row in `positions` by `bar_close_ms` (which is unique because it is the primary key of this lookup; see §4). If no match, logs `⚠️ [HEARTBEAT]` with "unknown position" and skips — this is the safe no-op case.
3. For each matched position, calls the resolver function with the current position state and the heartbeat bar's OHLC. The resolver:
   - Updates MAE and MFE based on the bar's high/low vs. entry price.
   - Checks the bar's high/low against TP1 and SL. Assume bar T+1 is a normal bar that breaks neither.
   - Returns an updated position state with MAE/MFE updated and no transition.
4. Writes the updated `positions` row to disk.
5. Acknowledges the webhook with HTTP 200.

**T+2 through T+M: Repeat.** Each subsequent bar's heartbeat repeats the above. MAE and MFE are tracked on every bar. No transitions fire.

**T+M: TP1 hits.** At bar T+M, the bar's high exceeds `tp1`. The resolver, on processing this heartbeat:

1. Updates MAE and MFE for the bar as usual.
2. Detects that `high >= tp1` and the position's state is `OPEN` (not yet `TP1_HIT`).
3. Transitions the position to state `TP1_HIT`, setting `tp1_hit = 1`, `tp1_hit_bar_ms` to the current bar's `bar_close_ms`, and moving the effective SL to breakeven (`entry_price`). The runner now has TP2 as its target and `entry_price` as its stop.
4. Writes the updated `positions` row.
5. (Does not yet write `trade_outcomes`. The position is still open — the runner is live.)
6. (Does not yet emit a Discord embed. The embed fires on final resolution, not on TP1 specifically. Discord experience is unchanged from the Workshop 3 plan where the OUTCOME embed fires at close.)

**T+N: Runner resolves.** At bar T+N, one of three things happens:

- *TP2 hit:* the bar's high exceeds `tp2`. Resolver transitions `state = CLOSED`, exit_reason = `TP2_HIT`, writes `trade_outcomes` with the full PnL, MAE, MFE, post-TP1 MAE, and time-in-trade, removes the position from the in-memory FSM state and from the `openPositionTimes` array concept (see §7 for how Pine is notified), posts the OUTCOME embed to `#trade-journal`.
- *BE stop hit:* the bar's low touches or crosses `entry_price`. Resolver transitions `state = CLOSED`, exit_reason = `BE_STOP`, writes `trade_outcomes` with final_pnl equal to the TP1 portion only (small win), posts the OUTCOME embed.
- *EOD timeout:* the heartbeat arriving on the bar whose `bar_close_ms` crosses the session close threshold finds the position still open. Resolver transitions `state = CLOSED`, exit_reason = `EOD_TIMEOUT`, writes `trade_outcomes` with PnL computed against the session close price, posts the OUTCOME embed.

In all three cases, the resolver's exit path:

1. Writes the `trade_outcomes` row with `signal_id = N` (the `signal_id` assigned when the position was created — no lookup, no lookback, no fuzzy match).
2. Updates the `positions` row to `state = CLOSED` and sets the close timestamp. (The row is retained, not deleted — history is preserved.)
3. Removes the position from the in-memory FSM's active-position map so that subsequent heartbeats don't reprocess it.
4. The main indicator's `openPositionTimes` array drops the corresponding entry via Pine-side session logic or its own resolution detection — see §7 for the detail on how Pine and backend stay in sync on position closure given the one-way nature of alerts.
5. Builds and posts the OUTCOME embed to `#trade-journal` with the full outcome summary.

**End state.** `signals_v3` has one row for this signal. `evaluations` has at most one row for this signal (the 4-bar follow-through eval that the main indicator emits independently of position resolution — this is the Reputation Engine's own accounting, not the resolver's). `positions` has one row showing the full history of this position from open through TP1 to final close. `trade_outcomes` has one row keyed by `signal_id = N` with the PnL, MAE, MFE, timing, and exit reason. Every row is correct by construction, because every row was written by a code path that knew the `signal_id` at the moment it wrote the row.

### 2.4 What changes versus v3.0-with-Monitor (Option A)

This subsection is an explicit delta against the Option A architecture so a reviewer can see what moves and what does not. It is not a replacement for §12's retirement/preservation ledger — that ledger lists the files and structures retired. This subsection is conceptual.

**Changes:**

1. **Monitor Pine Script retired.** Position resolution moves from a second TradingView script to a Python function in the backend.
2. **`find_parent_by_lookback` retired for outcomes.** The fuzzy parent-lookup used by the Monitor's TRADE_OUTCOME path is deleted because there are no TRADE_OUTCOME webhooks from Pine anymore. (The function may still be used by the EVAL route in the short term — the spec's §3 addresses whether it gets replaced with exact-match on `bar_close_ms` for EVAL at the same time, or whether that is a separate item.)
3. **New `positions` table added to schema.** Persisted from day one, per Strategist's resolution of open question 6.3. Schema in §4.
4. **New heartbeat event added to the webhook contract.** Status value `HEARTBEAT`. Payload in §3.
5. **New resolver FSM module in the backend.** Pure function, exhaustively unit-tested before implementation. §5.
6. **Main indicator gains a small amount of position-tracking state.** A local array of `bar_close_ms` values for positions Pine believes are open, with add/remove logic. §7.
7. **Main indicator gains a new `alertcondition` for heartbeats, or reuses the existing one.** This interacts with the single-alertcondition bottleneck; §7 resolves which.
8. **Backend gains startup rehydration.** On boot, the backend reads any `positions` rows with `state != CLOSED` and loads them back into the FSM's in-memory active-position map. A Railway restart mid-position is survivable.
9. **Backend gains Finnhub replay logic.** On heartbeat gap detection, the backend pulls missing bars from Finnhub and replays them through the resolver for open positions. §8.
10. **Week 1 Validation Protocol simplified.** The "provisional vs. trusted Monitor phase" no longer exists. The resolver is tested exhaustively before deploy via its unit test suite; Week 1 of observation becomes validation of signal-side behavior, embed formatting, and Pine-backend end-to-end integrity.

**Unchanged:**

1. **Reputation Engine.** Every line. Not touched.
2. **Filter conditions, StochRSI, volume floor, ADX, session window.** Not touched.
3. **`signals_v3` schema.** Not touched. (The spec may add fields if the implementation surfaces a real need, but that gets called out explicitly in §4 and the default is "unchanged.")
4. **`evaluations` schema.** Not touched.
5. **`trade_outcomes` schema.** Not touched in shape. Written by a different code path, same columns, same meanings.
6. **Discord embeds — ENTRY, EVAL, OUTCOME.** Byte-identical to the Workshop 3 plan.
7. **Discord channel organization.** Unchanged.
8. **Layer-specific logging convention (📥 📝 📤).** Unchanged.
9. **Observation discipline and the 30-day clock.** Clock has not started and does not start until Option C ships with a clean first webhook.

### 2.5 Where the cognition/measurement boundary sits in code

One final orientation before Part 2 goes into the event contract and schema. The spec will return to this boundary repeatedly, so locating it in concrete code terms makes later references unambiguous.

- Every line of the main indicator that touches `trendTrk`, `sqzTrk`, `repState`, `evalPending`, lockout counting, ghost tracking, or the decision to set `fireTrendAlert` / `fireSqzAlert` is **cognition**. It stays exactly where it is.

- Every line of the main indicator that emits an alertcondition, stamps a level, computes an ATR, writes a plot value, or manages the `openPositionTimes` local array is **measurement**. Option C adds to this category (the heartbeat) but does not remove from it.

- Every line of the backend that writes a row, updates a row, reads a row, computes an MAE/MFE delta, checks a TP/SL threshold against a bar's high/low, posts an embed, or handles a Finnhub reconnect is **measurement or state**. The backend contains no cognition lines under Option C.

- Every line of the resolver FSM in §5 is **measurement**. The FSM takes observed bar data as input and emits state transitions as output. It never decides when to open a position (the main indicator does that). It never decides when a signal is valid (the Reputation Engine does that). It never decides whether a signal should fire (gating does that). It decides only what happens to a position that has already been opened, given bars that have already been observed.

If at any point during implementation a line of code crosses this boundary — if the backend finds itself deciding when to open a position, if the resolver finds itself second-guessing whether a signal was "really" a signal, if the heartbeat handler finds itself inferring Pine state from its own bar stream — that line is wrong. The design principle is violated and the spec's guarantees break. Catch the drift at review, not at runtime.

---

## End of Part 1

Part 2 picks up at §3 (Event Contract) and §4 (Database Schema). The positions table schema in §4 is the first place this spec commits to new on-disk structure, and is the thing the FSM in §5 (Part 3) is written against. Nothing in Part 1 is contingent on Parts 2–5; Part 1 can be reviewed standalone for framing and architecture soundness before the detail work in later parts is checked.

*— Dev Trader, The Mona Project*
*April 13, 2026*

**Author:** Dev Trader (Senior Quant Dev)
**Date:** April 13, 2026 (Monday)
**Status:** DRAFT — awaiting Strategist review after all five parts land
**Continues:** `MONA_v3_OptionC_Spec_Part1.md`

---

## 3. EVENT CONTRACT

### 3.1 Overview

Option C introduces one new event type (`HEARTBEAT`) and tightens the discriminator on two existing event types (`ENTRY`, `EVAL`). The backend already routes webhooks by a top-level `status` field (verified in §1.5 against the backend source). Option C keeps that discriminator and adds one new value.

Under the single-alertcondition bottleneck that the current main indicator uses — one `alertcondition` line, one shared message template built from numeric plot slots — all three events come through the same webhook URL and the same message template. Different events set different plot values and the `status` discriminator tells the backend which event it is looking at. The implication is that any new field the heartbeat needs has to be expressed as one or more numeric plot slots added to the main indicator's plot block, because Pine Script's `plot()` primitive only accepts numeric series. This constraint is load-bearing for §3 and §7 and is addressed explicitly in §3.6.

**The three event types and their routing:**

| Status | Event | Destination table | Discord channel | New under Option C |
|---|---|---|---|---|
| `ENTRY` (or sig_status = 1) | Signal fired | `signals_v3` + new `positions` | `#alerts` | No, but writes `positions` too |
| `EVAL` (or sig_status = 2) | Follow-through window completed | `evaluations` | `#trade-journal` (suppressed for ghost) | No, but parent lookup changes |
| `HEARTBEAT` (or sig_status = 3) | Bar closed, positions still open | (no table write by default) | None | **Yes — new** |

The `TRADE_OUTCOME` status value used by the current v3.0-with-Monitor backend route (line 895 of `mona_v3_0_backend.py`) is **retired** under Option C. No webhook from Pine Script ever carries `status = TRADE_OUTCOME` again. Trade outcomes are *produced* by the backend FSM during HEARTBEAT processing and written directly to `trade_outcomes` without going through the webhook handler's routing. The `TRADE_OUTCOME` route is removed from the webhook handler as part of the migration.

### 3.2 ENTRY event

**Semantics.** An ENTRY webhook is fired by the main indicator at the close of a bar on which the Reputation Engine has authorized a new position to open. This is the only event type under which `signals_v3` gets a new row, and the only event type under which `positions` gets a new row. ENTRY is the event that creates a position. No other event creates a position.

**When it fires (unchanged from current v3.0).** The main indicator's cognition determines that all filter conditions are met for a TREND or SQUEEZE entry AND the relevant tracker is in `repState == ELIGIBLE` with `evalPending == false` AND no other signal has already fired on this bar. Cognition stamps `entry_price`, `sl`, `tp1`, `tp2` from the current bar's close and ATR, sets `evalPending = true`, fires the alertcondition.

**What the payload carries.** The full filter-state and level block that the current v3.0 script already emits (see lines 670 of `mona_v3_0.txt` for the existing plot map) remains in place unchanged. Option C adds exactly one new required field to the ENTRY payload:

- `bar_close_ms` — integer, milliseconds-since-epoch timestamp of the just-closed bar. Sourced from Pine's `time_close` on the bar the entry fires on. This is the field that keys the new `positions` row and that subsequent HEARTBEAT events will reference to identify which positions the heartbeat is for.

No other ENTRY payload fields change. The existing 32 plot slots (indexes 0-31 in the current script) continue to carry `sig_dir`, `sig_type`, `sig_status`, `sig_sl`, `sig_tp1`, `sig_tp2`, filter state, VWAP/EMA/ADX/StochRSI values, reputation state, and so on. The `status` discriminator for ENTRY is derived from `sig_status == 1` as it is today.

**Backend routing for ENTRY.** When the webhook handler sees `status = ENTRY`:

1. Parse and sanitize the payload (unchanged from current behavior).
2. Write a row to `signals_v3`. All current columns unchanged. **One new column: `bar_close_ms` populated from the payload.** See §4.2 for the schema change.
3. Capture the autoincrement `signal_id` returned by SQLite.
4. Write a row to `positions` with `signal_id`, `bar_close_ms` (from the payload), `direction`, `signal_type`, `entry_price`, `sl`, `tp1`, `tp2`, `state = OPEN`, `tp1_hit = 0`, MAE/MFE initialized to zero, `opened_at_ms = bar_close_ms`. See §4.5 for schema.
5. Insert the new position into the backend's in-memory active-position map keyed by `signal_id` (the FSM's runtime state — see §5).
6. Build and post the ENTRY embed to Discord `#alerts` (unchanged from current behavior).
7. Log `📥 ENTRY` → `📝 signals_v3 + positions` → `📤 #alerts` per the layer-specific logging convention.
8. Acknowledge the webhook with HTTP 200.

**Invariants on ENTRY handling.**

- I1. Every successful ENTRY write produces exactly one `signals_v3` row and exactly one `positions` row, both with the same `signal_id` and both committed in the same SQLite transaction. Either both are written or neither is.
- I2. The `bar_close_ms` written to `signals_v3` and to `positions` for a given `signal_id` is identical — both come from the same field of the same parsed payload.
- I3. The in-memory active-position map gains an entry if and only if the database write succeeded. The order is: write DB first, commit, then add to in-memory map. If the DB write fails, the in-memory state is not polluted.
- I4. An ENTRY event that the backend fails to process (DB error, validation error, unknown field) does not partially persist. The Pine side does not retry — the ENTRY is lost from the backend's record if the write fails, and §8 addresses what to do about that.

### 3.3 EVAL event

**Semantics.** An EVAL webhook is fired by the main indicator when the 4-bar follow-through window on a past signal completes. It carries the outcome of the follow-through check (did price move at least 0.5× ATR in the expected direction within 4 bars) and the resulting Reputation Engine state transition (remained ELIGIBLE, went to GROUNDED, went to EXTENDED, or a redemption pathway for ghost evals). This event is orthogonal to position resolution — it is the Reputation Engine's own accounting, and it fires whether or not the underlying position has resolved at the time the eval window closes.

**When it fires (unchanged).** The Pine-side eval-window countdown reaches zero. The tracker's `lastFollowTarget` is checked against the bars that elapsed during the window. `state_before`, `state_after`, `result`, `move_points`, `ft_high`, `ft_low`, `ft_actual_price` are all computed and emitted via the shared alertcondition with `sig_status == 2`.

**What changes under Option C.** The EVAL payload needs to carry the parent signal's `bar_close_ms` — the `bar_close_ms` of the entry bar this eval is evaluating, not the current bar. This is a new field: **`parent_bar_close_ms`**. Its purpose is to let the backend find the parent `signals_v3` row by exact match instead of by `find_parent_by_lookback`.

The reason this matters: under the current backend, `find_parent_by_lookback` is used for both EVAL parent lookup (line 854 of `mona_v3_0_backend.py`) and OUTCOME parent lookup (line 897). Option C retires the OUTCOME path because the backend FSM writes `trade_outcomes` directly with a known `signal_id`. But the EVAL path still needs to match an eval event to its parent signal — and under Option C we want that match to also be exact, so Finding #1's class of bug is retired on the EVAL side too, not just on the outcomes side.

Adding `parent_bar_close_ms` to the EVAL payload requires Pine to remember which bar's entry this eval corresponds to. The Reputation Engine already does this — `trendTrk` and `sqzTrk` each retain the entry bar's state so they can compute the follow-through. The main indicator needs to additionally store `entry_bar_close_ms` on each tracker at the moment of entry (a single new field per tracker) and plot it when the eval fires.

**Backend routing for EVAL.**

1. Parse and sanitize (unchanged).
2. Exact-match lookup: `SELECT signal_id FROM signals_v3 WHERE bar_close_ms = ? LIMIT 1` where `?` is `parent_bar_close_ms` from the payload.
3. If no row returned: log `❌ [ORPHAN]` with full payload to `#mona-log`, return HTTP 200 with `{"status": "rejected", "reason": "no_parent"}`. This is the orphan-clean behavior from the original Finding #0 analysis, applied to the EVAL path.
4. If row returned: write the eval to `evaluations` with that `signal_id`.
5. If not a ghost eval, build the EVAL embed with parent context and post to `#trade-journal`. Ghost evals are silent to users and emit only a 👻 log line (unchanged from current behavior).
6. Acknowledge with HTTP 200.

**Invariants on EVAL handling.**

- I5. An EVAL event that cannot find its parent by exact `bar_close_ms` match is never written to `evaluations`. No fallback lookup. No heuristic matching.
- I6. The `find_parent_by_lookback` function is deleted from the backend. Its call sites are replaced with exact-match `bar_close_ms` lookups. This applies to both the EVAL path and (formerly) the OUTCOME path.
- I7. Ghost eval handling is unchanged in semantics — `is_ghost=1` rows go to `evaluations`, no Discord post, single 👻 log line. The orphan-clean rule (I5) applies to ghost evals too: a ghost eval whose parent cannot be found by exact match is an orphan and does not get written.

**Note on EVAL events that arrive for positions the FSM has already closed.** It is possible and normal for an EVAL to fire several bars after a position has already resolved. The 4-bar follow-through window runs independently of TP/SL resolution — a position could hit TP1 on bar T+1, hit TP2 on bar T+2, be fully closed, and still generate an EVAL event on bar T+4 when the follow-through window completes. Under Option C this is fine: the EVAL goes to `evaluations` keyed to `signal_id`, the corresponding `positions` row already exists in `state = CLOSED`, nothing is inconsistent. The backend does not need to reconcile EVAL with position state — they are two independent records about the same `signal_id`.

### 3.4 HEARTBEAT event

**Semantics.** A HEARTBEAT webhook is fired by the main indicator at the close of each confirmed bar on which at least one position is tracked as "still open" in Pine's local state. It carries the bar's OHLC values, the bar's `bar_close_ms`, and the list of `bar_close_ms` values for positions Pine believes are still open. The backend uses this event to walk the resolver FSM forward — updating MAE/MFE, checking TP1/SL/BE/TP2 levels, and transitioning positions to new states or closing them.

**This is the new event type.** Everything about the heartbeat is new to Option C. §3.4 specifies what the event carries and how the backend processes it. §5 specifies the FSM behavior the processing invokes. §7 specifies how Pine maintains its local "positions still open" state and what mechanism it uses to fire the heartbeat.

**Fire condition (Pine-side).** The alertcondition fires on a bar when `barstate.isconfirmed == true` (the bar has closed, not an intrabar tick) AND the local openPositions tracker is non-empty. The "local openPositions tracker" is a fixed-size array of integer `bar_close_ms` slots the main indicator maintains. §7 specifies exactly how slots are added (on ENTRY fire) and how slots are released (the conservative closure heuristic). For §3's purposes, the contract is: *the heartbeat carries a non-empty set of `bar_close_ms` values representing Pine's best-effort "positions still potentially active" list*.

**What the payload carries.** The HEARTBEAT payload needs the following fields:

- `status` or equivalent discriminator — `"HEARTBEAT"` or `sig_status == 3`
- `bar_close_ms` — integer, the just-closed bar's `time_close`
- `open` — float, bar open
- `high` — float, bar high
- `low` — float, bar low
- `close` — float, bar close
- `pos_slot_1_time` through `pos_slot_N_time` — N integer fields, one per slot, with `0` indicating "slot empty" and any non-zero value indicating "position with this `bar_close_ms` as entry bar is tracked open"
- `ticker` — string, `"MNQ"` (unchanged)
- `version` — string, `"3.0"` (unchanged)

**The N question.** How many slots does Pine track? This is a concrete design commitment, not a philosophical question. The constraints:

- Each slot costs one numeric plot index in the main indicator. Plots are a bounded resource; the current v3.0 script uses 32, and adding slots means adding plots.
- The Reputation Engine gates new trend entries on `not trendTrk.evalPending`, which means at most one trend entry can fire inside any 4-bar window. Same for squeeze. So new entries are paced.
- But positions persist beyond the eval window. A trend TP1-and-runner can be open for 20+ bars, during which a new trend eval window can complete and a new trend entry can fire. Squeeze can fire concurrently. So realistic concurrent-position counts are bounded but not by 2.
- Observational estimate for 15-minute MNQ during a 6.25-hour session: concurrent open position count is typically 0-2, occasionally 3-4, very rarely higher.

**Commitment: N = 4.** Four slots. This accommodates the realistic 3-4 concurrent case with one slot of headroom, costs four new plots, and is small enough to enumerate in the alertcondition message template without bloat. If observation data shows that concurrent-position counts exceed 4 in practice, N can be increased in a subsequent Pine update — but the resolver FSM is written to handle any N, and the backend parsing is written to accept any N ≥ 1, so raising N later is a Pine-side one-liner change per slot.

**Slot semantics.** Slots are a fixed-size array indexed 1 through N. A slot holds either `0` (empty) or a `bar_close_ms` value (occupied by a position whose entry bar had that `bar_close_ms`). When a new ENTRY fires, Pine finds the lowest-indexed empty slot and writes the new `bar_close_ms` there. When Pine's closure heuristic determines a position is no longer active (see §7), it zeros the slot. If all slots are occupied when a new ENTRY fires, Pine fires the ENTRY anyway — the backend gets the ENTRY via its own route regardless — and logs a ⚠️ "slot overflow" condition. The backend still records the position in `signals_v3` and `positions`, but no heartbeats will flow for it until a slot frees up. This is a degradation mode, not a correctness violation, and the slot overflow is observable.

**Backend routing for HEARTBEAT.**

1. Parse and sanitize.
2. Extract `bar_close_ms`, `open`, `high`, `low`, `close`, and the N slot values.
3. Check for heartbeat gaps: is the incoming `bar_close_ms` exactly one bar-interval (900,000 ms for 15M) ahead of the last processed heartbeat's `bar_close_ms`? If not — either a bar was skipped, or this is the first heartbeat after a restart, or Railway was down during an intervening bar — the gap handler in §8 is invoked before normal processing.
4. For each non-zero slot value:
   - Look up the position in the in-memory active-position map by its `bar_close_ms`. (The map is keyed by `signal_id` but also indexed by `bar_close_ms` for the heartbeat lookup — see §5.)
   - If the position is not found, log `⚠️ [HEARTBEAT]` with "unknown position, bar_close_ms = X" and skip this slot. This is the safe no-op case from the resolver edge case list.
   - If the position is found, call the resolver function with the current position state and the heartbeat bar's OHLC.
5. The resolver may return an updated position state (no transition), a TP1 transition (updates `tp1_hit`, moves effective SL to breakeven), or a final-close transition (TP2 hit, BE stop hit, SL hit, or EOD timeout).
6. For each transition the resolver produces, update the in-memory map and write through to the `positions` table. For final closes, additionally write to `trade_outcomes` and post the OUTCOME embed to Discord `#trade-journal`.
7. Log `📥 HEARTBEAT bar=X slots=[a,b,c,d]` → for each resolved slot `📝 positions (update) | trade_outcomes (insert) | 📤 #trade-journal`.
8. Acknowledge with HTTP 200.

**Invariants on HEARTBEAT handling.**

- I8. A HEARTBEAT event never writes to `signals_v3`. HEARTBEAT is strictly a resolver driver; it cannot create positions.
- I9. A HEARTBEAT event for a `bar_close_ms` slot that does not match any known position is a safe no-op. It logs and skips. It does not error. It does not refuse the webhook.
- I10. The resolver is called at most once per position per heartbeat. Even if the same `bar_close_ms` appears in multiple slots (which would be an upstream bug), the in-memory map lookup returns one position and the resolver runs once.
- I11. A HEARTBEAT event that arrives with a `bar_close_ms` equal to or earlier than the last-processed heartbeat's `bar_close_ms` is a duplicate or a replay. The handler detects this and either (a) idempotently re-processes it if the resolver is designed to be idempotent (preferred), or (b) logs and skips. See §5 for the resolver's idempotency commitment.
- I12. The resolver FSM never has hidden state outside the in-memory active-position map plus the persisted `positions` table. Everything needed to process a heartbeat is either in memory, on disk, or in the heartbeat payload itself.

### 3.5 Status code reference table

For unambiguous reference across the rest of the spec and the implementation:

| sig_status numeric | status string | Event | Backend route | Writes |
|---|---|---|---|---|
| 1 | `ENTRY` | Signal fires | ENTRY handler | `signals_v3`, `positions` |
| 2 | `EVAL` | Follow-through window completes | EVAL handler | `evaluations` |
| 3 | `HEARTBEAT` | Bar closed, positions open | HEARTBEAT handler | `positions` (update), `trade_outcomes` (on close) |
| ~~4~~ | ~~`TRADE_OUTCOME`~~ | ~~Monitor resolves a position~~ | **RETIRED** | — |

The numeric encoding matters because Pine's `sig_status` is a plot slot (currently plot index 2) carrying an integer, and the backend's current `translate_payload` function reads it. The string encoding matters because the current `translate_payload` function promotes numeric `sig_status` into a string `status` field for routing. Both encodings stay consistent: 1=ENTRY, 2=EVAL, 3=HEARTBEAT. Value 4 is permanently reserved as "retired, do not reuse" to prevent a future schema change from accidentally routing something to a code path that used to mean TRADE_OUTCOME.

### 3.6 New Pine plots required

Concretely, the following new plots must be added to the main indicator's plot block to support Option C's event contract. These are new integer or float plot indices beyond the current 31 (indexes 0-31 exist today; 32 and up are new).

| Plot index (proposed) | Pine variable | Type | Populated on | Purpose |
|---|---|---|---|---|
| 32 | `barCloseMs` | int (via float coerce) | every bar | `bar_close_ms` field in all three event payloads |
| 33 | `parentBarCloseMs` | int (via float coerce) | EVAL events only | `parent_bar_close_ms` field in EVAL payload |
| 34 | `barOpen` | float | HEARTBEAT events | `open` field in HEARTBEAT payload |
| 35 | `barHigh` | float | HEARTBEAT events | `high` field in HEARTBEAT payload |
| 36 | `barLow` | float | HEARTBEAT events | `low` field in HEARTBEAT payload |
| 37 | `barClose` | float | HEARTBEAT events (redundant with existing close but explicit) | `close` field in HEARTBEAT payload |
| 38 | `posSlot1Time` | int | HEARTBEAT events | Slot 1's tracked `bar_close_ms` or 0 |
| 39 | `posSlot2Time` | int | HEARTBEAT events | Slot 2's tracked `bar_close_ms` or 0 |
| 40 | `posSlot3Time` | int | HEARTBEAT events | Slot 3's tracked `bar_close_ms` or 0 |
| 41 | `posSlot4Time` | int | HEARTBEAT events | Slot 4's tracked `bar_close_ms` or 0 |

**Note on the int-via-float-coerce problem.** Pine Script's `plot()` primitive only accepts `series float`. Integer values (like `bar_close_ms` which is ~1.7e12) have to be plotted as floats. Float precision is sufficient for 13-digit millisecond timestamps (double-precision float gives ~15-16 decimal digits of precision), and the backend parses them as integers via `int(float(value))` on receipt. This is a known Pine quirk; the current script already does this for `session_minute` and the ghost flag.

**On {{plot_N}} limits.** The current v3.0 indicator uses `{{plot_N}}` syntax up to plot index 31 without apparent issue, contradicting the legacy comment at line 615 that suggests a ~20-plot limit. The limit in modern Pine is higher than that comment indicates (the comment appears to be stale from an older Pine version). The spec assumes plot indices 0-41 are all addressable via `{{plot_N}}` in the alertcondition message. **Verification task before implementation:** confirm by compiling a test Pine script that plots 42 slots and references `{{plot_41}}` in an alertcondition message. This is a 5-minute check, done before §7's Pine changes are written.

**Commitment.** The main indicator gains 10 new plot slots (indexes 32-41) to support Option C's event contract. If the verification task above fails (plot limit below 42), the backend can alternatively read the new fields from a JSON structure passed through a single plot — but that path is uglier and should only be taken if the straightforward path is blocked.

### 3.7 Alertcondition message template

The updated shared alertcondition message template, with new fields added and retired fields noted. This is illustrative — the final exact string is written during Pine implementation in §7. Formatted for readability:

```
{
  "sig_dir": {{plot_0}},
  "sig_type": {{plot_1}},
  "sig_status": {{plot_2}},
  "entry_price": {{close}},
  "price": {{close}},
  "sl": {{plot_3}},
  "tp1": {{plot_4}},
  "tp2": {{plot_5}},
  ... (existing plots 6-31 unchanged from current v3.0) ...

  "bar_close_ms": {{plot_32}},
  "parent_bar_close_ms": {{plot_33}},
  "bar_open": {{plot_34}},
  "bar_high": {{plot_35}},
  "bar_low": {{plot_36}},
  "bar_close": {{plot_37}},
  "pos_slot_1_time": {{plot_38}},
  "pos_slot_2_time": {{plot_39}},
  "pos_slot_3_time": {{plot_40}},
  "pos_slot_4_time": {{plot_41}},

  "ticker": "MNQ",
  "version": "3.0"
}
```

**Field population by event type.** Most fields are populated on every event; some are meaningful only for specific events. The backend is tolerant of unused fields being `0` because `translate_payload` already normalizes numerics:

- On an **ENTRY event**, the existing level fields (`sl`, `tp1`, `tp2`, etc.) and `bar_close_ms` are meaningful. `parent_bar_close_ms` is 0 (unused). Bar OHLC fields are 0 or na. Slot fields are 0 unless a heartbeat is firing on the same bar (see §6 for the same-bar keystone case).
- On an **EVAL event**, `parent_bar_close_ms` is meaningful. `bar_close_ms` is the current bar's close time. Bar OHLC and slot fields are 0.
- On a **HEARTBEAT event**, `bar_close_ms`, bar OHLC, and slot fields are meaningful. Level fields carry stale values from the last ENTRY (backend ignores them). `parent_bar_close_ms` is 0.

This is not elegant — the shared message template means each event carries fields irrelevant to itself — but it is correct, and it is constrained by the single-alertcondition bottleneck. §7 revisits whether the heartbeat should go to a *second* alertcondition instead of sharing the existing one. For the current spec, assume shared.

### 3.8 Backend routing decision tree

Stated as pseudocode for unambiguous reference by §5 and the test suite:

```
on webhook_received(payload):
    validate_auth(payload)
    raw = parse_and_sanitize(payload)
    status = resolve_status(raw)          # maps sig_status numeric to string

    match status:
        case "ENTRY":
            signal_id = insert_signals_v3(raw)
            position = insert_positions(signal_id, raw)
            fsm_map[signal_id] = position
            post_entry_embed(raw, signal_id)
            log(RECEIVED, WRITTEN_signals_v3_positions, POSTED_alerts)
            return 200

        case "EVAL":
            parent_id = lookup_signal_id_by_bar_close_ms(raw.parent_bar_close_ms)
            if parent_id is None:
                log_orphan(raw)
                return {"status": "rejected", "reason": "no_parent"}
            insert_evaluations(parent_id, raw)
            if not raw.is_ghost:
                post_eval_embed(raw, parent_id)
                log(RECEIVED, WRITTEN_evaluations, POSTED_trade_journal)
            else:
                log_ghost(raw)
            return 200

        case "HEARTBEAT":
            check_heartbeat_gap(raw.bar_close_ms)        # §8 invoked if gap
            for slot_value in raw.slot_values:
                if slot_value == 0:
                    continue
                position = fsm_map.find_by_bar_close_ms(slot_value)
                if position is None:
                    log_heartbeat_unknown(slot_value)
                    continue
                result = resolver.step(position, raw.bar_ohlc)
                apply_resolver_result(position, result)
            return 200

        case _:
            log_error("UNKNOWN_STATUS", raw)
            return 400
```

This pseudocode is the specification for the webhook handler's top-level structure. The resolver (`resolver.step`) is specified in §5. The `apply_resolver_result` helper is specified in §5.4 — it is the function that handles writing through to `positions` and to `trade_outcomes` depending on the transition type.

---

## 4. DATABASE SCHEMA

### 4.1 Principles

The schema for Option C obeys four principles. Each is testable — a reviewer can point at any schema choice and ask "does this obey principle X?"

**P1 — The positions table is persisted from day one.** This is Strategist's resolved answer to open question 6.3 from the rethesis doc. The in-memory-only alternative was considered and rejected because outage recovery (§8) depends on rehydration from a persisted source, and "we'll add persistence in v3.1" is the kind of architectural corner-cut that the principle "Mona's brain lives in one place" exists to block. State is persisted. If Railway restarts mid-position, the FSM rehydrates and continues.

**P2 — signal_id is the primary key of the position, assigned at creation, never reconstructed.** This is the mechanical guarantee that retires Finding #1. A position's identity is its `signal_id`. Every row the resolver writes to `trade_outcomes` uses this identity. Nothing is ever looked up by fuzzy matching after the position exists.

**P3 — bar_close_ms is the interoperability key between Pine and the backend.** Where the two systems need to agree about "which bar" or "which entry bar," they use `bar_close_ms`. Pine sources it from `time_close`, backend parses it as an integer, both treat it as an opaque identifier. Exact-match only. No fuzzy lookups. No windowed queries. An exact bar_close_ms either matches a row or it doesn't.

**P4 — Schema changes are additive.** Existing `signals_v3`, `evaluations`, and `trade_outcomes` tables get new columns (where needed) but no existing columns are removed, no types change, no meanings shift. This preserves rollback safety: a rollback to v2.1.1 sees columns it doesn't recognize and ignores them; it does not fail on missing columns. The migration is `ALTER TABLE ... ADD COLUMN`, never `DROP` or `ALTER TYPE`.

### 4.2 `signals_v3` — changes

**Current schema.** See `init_db` at line 236 of `mona_v3_0_backend.py` (Part 1 §1.5 referenced this). Contains `signal_id`, `timestamp`, `signal`, `signal_type`, level fields, filter-state fields, `session_minute`, `reputation`, `consecutive_stops`, `conditions`. Does not currently have a `bar_close_ms` column.

**Required addition: `bar_close_ms INTEGER`.**

```sql
ALTER TABLE signals_v3 ADD COLUMN bar_close_ms INTEGER;
CREATE UNIQUE INDEX idx_signals_v3_bar_close_ms
  ON signals_v3(bar_close_ms)
  WHERE bar_close_ms IS NOT NULL;
```

**Why `INTEGER` and not `BIGINT`.** SQLite does not distinguish `INTEGER` from `BIGINT`; all integer types map to the same 64-bit dynamic-typed storage class. `INTEGER` is the conventional name and is sufficient to hold 13-digit millisecond timestamps through year 294276.

**Why the partial UNIQUE index (`WHERE bar_close_ms IS NOT NULL`).** The ALTER adds the column with default NULL. Existing rows written under v3.0 pre-migration will have NULL. The partial UNIQUE index enforces uniqueness on populated values without conflicting on legacy NULL rows. Pre-migration rows remain valid; post-migration rows must have unique, non-null `bar_close_ms`. After a grace period (or with a one-time backfill if desired), the index can be upgraded to a full UNIQUE NOT NULL constraint.

**Why UNIQUE.** The EVAL lookup and the HEARTBEAT slot lookup both use `bar_close_ms` as the match key into `signals_v3` and `positions`. Two signals cannot legitimately share a `bar_close_ms` because `bar_close_ms` is the close time of a bar, and the main indicator's single-fire-per-bar rule (`fireTrendAlert`/`fireSqzAlert` guards) means at most one ENTRY fires per bar from a single tracker. Even cross-tracker (trend and squeeze firing on the same bar), the Reputation Engine gating makes that rare — and the spec's conservative choice is: two entries on the same bar share a `bar_close_ms`, which violates UNIQUE and fails at the INSERT boundary. This is a correctness-over-permissive choice. If observation shows cross-tracker same-bar entries happen in practice (SQUEEZE and TREND firing on the same bar from different trackers with independent reputation states), the UNIQUE constraint is too strict and gets replaced with a non-unique index plus a compound lookup key. This is flagged as an open item in §11.

**Backend translate_payload change.** The `translate_payload` function in the backend needs to extract `bar_close_ms` from the parsed payload (where it arrives as a float via Pine's plot coercion) and coerce it to int: `int(float(data.get("bar_close_ms", 0)))`. If the value is 0 or missing on an ENTRY, that is a schema violation and the ENTRY is rejected with `log_error("SCHEMA", "missing bar_close_ms on ENTRY")`. This check is a single new line in the ENTRY route.

### 4.3 `evaluations` — no schema changes required

The `evaluations` table does not need new columns. The `signal_id` foreign key already links each eval to its parent signal. Under Option C, the EVAL handler resolves the parent via exact-match lookup on `bar_close_ms` (from the EVAL payload's `parent_bar_close_ms` field) into `signals_v3`, gets the `signal_id`, and writes to `evaluations` with that `signal_id` as it does today.

**What changes is the lookup code, not the schema.** The call to `find_parent_by_lookback(signal_type, direction)` at line 854 of the backend is replaced with a call to `find_parent_by_exact_bar_close_ms(parent_bar_close_ms)`. The new function is a single query:

```python
def find_parent_by_exact_bar_close_ms(bar_close_ms: int) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT signal_id FROM signals_v3 WHERE bar_close_ms = ? LIMIT 1',
            (bar_close_ms,)
        ).fetchone()
        return row[0] if row else None
```

No fallback. No lookback window. No fuzzy matching. Either the exact `bar_close_ms` matches a `signals_v3` row or the EVAL is an orphan.

### 4.4 `trade_outcomes` — no schema changes required in shape

The `trade_outcomes` table is not modified in shape. Every column that exists today still exists and means the same thing. What changes is the write path: instead of being written by the `TRADE_OUTCOME` webhook route (which is retired), it is written by the resolver FSM directly when a position closes. The resolver's write goes through a helper function `write_trade_outcome(position)` that constructs the row from the closed position's accumulated state.

**`is_ghost` column on `trade_outcomes`.** The existing schema has `is_ghost INTEGER NOT NULL DEFAULT 0` as a v3.1 provision (line 320). Under Option C this column remains at its default of 0 because the resolver does not track ghost positions — ghost evaluations exist only in `evaluations`, and the resolver's input stream (ENTRY events) cannot carry ghosts per the verification in §1.5. The column is harmless and stays for rollback compatibility. It is not used.

### 4.5 `positions` — the new table

This is the new on-disk artifact of Option C. It holds one row per position from creation through final close. Rows are not deleted on close — the table is append-and-update, not append-and-delete — so it serves as the history of every position the system has ever tracked.

**Full schema:**

```sql
CREATE TABLE IF NOT EXISTS positions (
    signal_id              INTEGER PRIMARY KEY,
    bar_close_ms           INTEGER NOT NULL,

    direction              INTEGER NOT NULL,    -- 1=LONG, 2=SHORT
    signal_type            INTEGER NOT NULL,    -- 1=TREND, 2=SQUEEZE

    entry_price            REAL NOT NULL,
    sl                     REAL NOT NULL,
    tp1                    REAL NOT NULL,
    tp2                    REAL NOT NULL,

    opened_at_ms           INTEGER NOT NULL,    -- == bar_close_ms of entry bar
    opened_at_ts           TEXT NOT NULL,       -- ET timestamp string for human reads

    state                  INTEGER NOT NULL,    -- 1=OPEN, 2=TP1_HIT, 3=CLOSED
    tp1_hit                INTEGER NOT NULL DEFAULT 0,
    tp1_hit_bar_ms         INTEGER,

    effective_sl           REAL NOT NULL,       -- = sl until TP1 hit, then = entry_price

    mae_points             REAL NOT NULL DEFAULT 0,
    mae_bar_ms             INTEGER,
    mfe_points             REAL NOT NULL DEFAULT 0,
    mfe_bar_ms             INTEGER,
    post_tp1_mae_points    REAL NOT NULL DEFAULT 0,

    last_heartbeat_bar_ms  INTEGER,             -- bar of most recently processed heartbeat
    heartbeats_processed   INTEGER NOT NULL DEFAULT 0,

    closed_at_ms           INTEGER,             -- null until state = CLOSED
    closed_at_ts           TEXT,                -- null until state = CLOSED
    exit_reason            TEXT,                -- TP2_HIT, BE_STOP, SL_HIT, EOD_TIMEOUT, GAP_CLEAN
    final_pnl_points       REAL,

    FOREIGN KEY (signal_id) REFERENCES signals_v3(signal_id)
);

CREATE UNIQUE INDEX idx_positions_bar_close_ms
  ON positions(bar_close_ms);

CREATE INDEX idx_positions_state
  ON positions(state)
  WHERE state != 3;    -- index only open and TP1-hit rows (rehydration query hot path)
```

**Column-by-column notes:**

- **`signal_id`** — PRIMARY KEY. Matches the `signal_id` of the parent `signals_v3` row. Assigned at ENTRY time, never reconstructed. P2 guarantee.

- **`bar_close_ms`** — UNIQUE. The heartbeat slot identifier. The backend looks up positions by this value when processing HEARTBEAT events. Also matches the `bar_close_ms` on the corresponding `signals_v3` row (P3).

- **`direction`, `signal_type`** — integer-encoded (1=LONG, 2=SHORT; 1=TREND, 2=SQUEEZE). Matches the encoding used in the Pine plot fields. Stored as integer rather than string for consistency with the plot encoding and smaller storage.

- **`entry_price`, `sl`, `tp1`, `tp2`** — immutable after insert. The resolver never updates these. They are the position's contract.

- **`opened_at_ms`** — duplicates `bar_close_ms` in value; retained as a separate column for clarity at query time ("when was this opened" is a more natural question than "what was the entry bar's close time"). If storage efficiency ever matters, one can be dropped — they are equal by construction. Until then, having both makes the schema self-documenting.

- **`opened_at_ts`** — human-readable ET timestamp string. Matches the format used by `get_et_now()` / `fmt_et` in the current backend. Used for Discord embeds and Data Lab SQL queries that want to filter by date.

- **`state`** — the FSM state. 1=OPEN (no TP1 yet), 2=TP1_HIT (runner active, stop moved to breakeven), 3=CLOSED (final). This is the integer the resolver reads and writes. The rehydration query in §4.9 uses the partial index on this column.

- **`tp1_hit`, `tp1_hit_bar_ms`** — redundant with `state == 2`, but explicit. When `tp1_hit = 1`, `tp1_hit_bar_ms` records the bar on which it happened. Useful for MAE/MFE analysis and for Data Lab queries asking "how long did positions sit before hitting TP1."

- **`effective_sl`** — the stop that the resolver is currently testing against. Starts equal to `sl`. On TP1 transition, is set to `entry_price` (the BE stop). The resolver always checks `low <= effective_sl` (for longs) rather than re-deriving the stop from state. This simplifies resolver logic and makes one column the single source of truth for the current stop level.

- **`mae_points`, `mae_bar_ms`** — maximum adverse excursion in points (signed, negative = worse than entry for a long), and the bar on which the worst was seen. Updated on every heartbeat.

- **`mfe_points`, `mfe_bar_ms`** — maximum favorable excursion. Same semantics, positive.

- **`post_tp1_mae_points`** — the depth of adverse excursion below TP1 after TP1 is hit. Zero if TP1 hasn't hit. Non-negative. Used to measure "how close did the runner come to a BE stop." Key Data Lab metric.

- **`last_heartbeat_bar_ms`** — idempotency guard. The resolver checks this before processing a heartbeat: if the incoming `bar_close_ms` is ≤ this value, the resolver short-circuits and skips, because this position has already seen that bar. I11 implementation.

- **`heartbeats_processed`** — counter, for diagnostics. Incremented on every successful resolver step.

- **`closed_at_ms`, `closed_at_ts`, `exit_reason`, `final_pnl_points`** — populated on close. Null while open. Exit reason is one of: `TP2_HIT`, `BE_STOP`, `SL_HIT`, `EOD_TIMEOUT`, `GAP_CLEAN` (the §8 outage case). The same five-value enum is used consistently in `trade_outcomes.exit_reason`.

### 4.6 Invariants across tables

The schema above implies invariants that the implementation must preserve. Stating them here gives the test suite in §5 and the migration/rollback logic in §10 an authoritative list to assert against.

- **INV-A.** For every `positions.signal_id`, there exists exactly one `signals_v3.signal_id` with the same value. Enforced by FOREIGN KEY.

- **INV-B.** For every `positions.signal_id`, `positions.bar_close_ms == signals_v3.bar_close_ms` where the `signals_v3` row has the matching `signal_id`. Not enforced by SQL constraint (SQLite doesn't do cross-row constraints cheaply), but enforced by the ENTRY handler writing both values from the same payload in the same transaction. Assertable in test.

- **INV-C.** Once `positions.state == 3` (CLOSED), the row is never modified again except by a human audit operation. The resolver never transitions out of CLOSED. The rehydration logic in §4.9 never loads CLOSED rows into the FSM.

- **INV-D.** For every row in `trade_outcomes`, there exists a corresponding `positions` row with `state == 3` and the same `signal_id`. Enforced by the resolver: the function that writes `trade_outcomes` is the same function that sets `state = 3` on the position, within one transaction.

- **INV-E.** `effective_sl` equals `sl` when `state == 1` (OPEN), and equals `entry_price` when `state == 2` (TP1_HIT). Checked by the resolver's transition function; assertable in test.

- **INV-F.** `opened_at_ms == bar_close_ms` for all rows. True by construction in the ENTRY handler.

- **INV-G.** No two rows in `positions` share a `bar_close_ms`. Enforced by UNIQUE index. Same rationale and same caveat as §4.2's UNIQUE on `signals_v3.bar_close_ms`.

### 4.7 Indexes

Rationale stated for each index beyond the primary key:

| Index | Purpose | Used by |
|---|---|---|
| `idx_signals_v3_bar_close_ms` (partial UNIQUE) | EVAL parent lookup by exact bar_close_ms | `find_parent_by_exact_bar_close_ms` |
| `idx_positions_bar_close_ms` (UNIQUE) | HEARTBEAT slot-to-position lookup | in-memory map rebuild; FSM's by-bar lookups during rehydration |
| `idx_positions_state` (partial, state != 3) | Startup rehydration query; efficient "which positions are still open" reads | Startup init; diagnostic tooling |
| `idx_signal_timestamp` (existing) | Data Lab time-range queries | Unchanged |
| `idx_eval_signal` (existing) | Join from evaluations to parent | Unchanged |
| `idx_eval_timestamp` (existing) | Data Lab time-range queries | Unchanged |
| `idx_outcome_signal` (existing) | Join from trade_outcomes to parent | Unchanged |

No index becomes unused under Option C. No index needs to be dropped. The three new indexes support exactly the three new lookup patterns Option C introduces (exact parent lookup on signals_v3, slot lookup on positions, rehydration scan on positions).

### 4.8 Relationships

```
signals_v3
    signal_id (PK) ───────────────────────────┐
    bar_close_ms (UNIQUE when set)            │
                                              │
         ┌────────────────────────────────────┤
         │                                    │
         ▼                                    ▼
    evaluations                          positions
        eval_id (PK)                        signal_id (PK, FK → signals_v3)
        signal_id (FK)                      bar_close_ms (UNIQUE)
        is_ghost                            state
        ...                                 ... (lifecycle columns)
                                              │
                                              ▼
                                          trade_outcomes
                                              outcome_id (PK)
                                              signal_id (FK → signals_v3)
                                              ...
```

**Shape of the relationships:**

- One `signals_v3` row → at most one `positions` row (by PK equality on signal_id).
- One `signals_v3` row → zero or more `evaluations` rows (a signal can have one real eval, or one ghost eval from a lockout path, or technically both in unusual cases; `evaluations.signal_id` is a regular FK, not unique).
- One `positions` row → at most one `trade_outcomes` row. The resolver writes `trade_outcomes` exactly once per position close, and INV-D guarantees the 1:1 relationship at the state = 3 boundary.
- One `signals_v3` row → zero or one `trade_outcomes` rows, transitively: a signal has a position, and the position eventually closes into a trade_outcomes row.

### 4.9 Rehydration query

On Railway startup, before the backend accepts its first webhook, the FSM rehydrates from persisted state. The query:

```sql
SELECT *
FROM positions
WHERE state != 3                    -- open or TP1_hit, not closed
ORDER BY opened_at_ms ASC;
```

The ORDER BY is for deterministic order (useful for logging and reproducibility; not functionally required). The partial index `idx_positions_state` makes this query cheap even if `positions` accumulates many thousands of historical rows — the index holds only non-closed rows.

**For each row returned:**

1. Construct an in-memory position object with all the persisted fields.
2. Insert into `fsm_map` keyed by `signal_id`.
3. Insert into a secondary `fsm_map_by_bar_close_ms` keyed by `bar_close_ms` (the heartbeat slot lookup structure).
4. Log `🔄 REHYDRATE signal_id=X state=Y last_heartbeat=Z` to `#system-log`.

**After rehydration, the backend checks for heartbeat gaps.** If `last_heartbeat_bar_ms` on any rehydrated row is more than one bar interval behind the current wall clock (accounting for market hours — a gap during the overnight session is expected, not a failure), the §8 gap recovery path is invoked for that position: Finnhub is queried for the missing bars and the resolver is replayed. If the gap is larger than the recoverable window (defined in §8), the position is marked `GAP_CLEAN` and closed defensively.

Rehydration completes before the first live webhook is served. Startup logs indicate how many positions were rehydrated and how many required gap recovery.

**Why this works for Railway restart.** Railway restarts are the failure mode Strategist's resolution of open question 6.3 targets. A restart mid-bar, or mid-position, or mid-trading-day, is survivable because every position-state transition commits to disk before the in-memory state updates. The write-through commit is the durability guarantee (see §5 for the commit ordering rules within the resolver). On the next startup, the FSM reads the exact state the pre-restart FSM had committed, and resumes.

---

## End of Part 2

Part 2 leaves three items threaded through to later parts:

- **§7 owes a verification and commitment** on the Pine plot limit being ≥ 42 (§3.6's note), on how Pine tracks and releases slot entries (§3.4's "see §7"), and on whether heartbeat shares the existing alertcondition or gets its own.
- **§5 owes the resolver function specification** that §3 and §4 were written against — the state machine, transitions, idempotency rules, test cases. §5 is the heart of Part 3.
- **§6 owes the walk through of the five keystone failure cases** named in the Going-In Notes §3, applied against the event contract and schema defined in Parts 1 and 2.
- **§8 owes the outage recovery mechanism** that §4.9's rehydration query invokes on gap detection.
- **§11 owes a flagged open item** on whether the UNIQUE constraint on `bar_close_ms` is too strict for cross-tracker same-bar entries.


**Author:** Dev Trader (Senior Quant Dev)
**Date:** April 13, 2026 (Monday)
**Status:** DRAFT — awaiting Strategist review after all five parts land
**Continues:** `MONA_v3_OptionC_Spec_Part2.md`

---

## 5. RESOLVER FINITE STATE MACHINE

This section is the heart of the spec. Everything in Parts 1 and 2 was infrastructure for what §5 defines: the function that decides what happens to an open position when a new bar arrives.

The ratio Strategist committed to in the closeout handoff — "the test suite should take longer than the implementation, not shorter" — applies here most of all. The resolver implementation, when it is eventually written, will be a small module. The test case enumeration in §5.12 is what makes that small module correct. The tests come first. The implementation is written against the tests. That ordering is the mechanism by which Option C's architectural advantage actually gets delivered; any drift back to "implement first, then backfill" collapses the advantage.

§5.1 through §5.11 define the resolver's contract: what it is, what it takes in, what it produces, what its rules are. §5.12 is the test case enumeration. §5.13 handles concurrent-position edge cases. The test cases in §5.12 are written in a format directly translatable into Python unit tests — input state, input bar sequence, expected assertions — and it is intentional that the TDD pass consists of literally transcribing §5.12 into `tests/test_position_resolver.py` before writing `backend/position_resolver.py`.

### 5.1 Overview and design principles

The resolver is a **pure function** that takes a position's current state and a new bar's OHLC data, and returns either an updated state or a terminal close-out decision. It has no side effects. It does not touch the database. It does not post to Discord. It does not read global state. Everything it needs is in its arguments; everything it produces is in its return value. The wrapper that actually commits transitions to disk and posts embeds is `apply_resolver_result`, specified in §5.10 — but that wrapper is a separate, simpler function whose only job is to take the resolver's pure output and turn it into side effects in the correct order.

**Why pure.** A pure function is exhaustively testable. Given an input, a pure function produces exactly one output; the test case is "assert output equals expected." No mocking of databases, no fake webhook bodies, no teardown of previous state. Every edge case in §5.12 becomes a one-function call with deterministic assertions, and the full test suite runs in under a second. The Monitor Pine Script's correctness depended on bar-replay sessions in the TradingView UI with a human watching; the resolver's correctness will depend on `pytest` exiting 0, which is auditable, reproducible, and diffable across changes.

**Why a finite state machine.** Position resolution under the 3/2/1 trading plan has exactly three states: OPEN (no TP1 yet, testing original SL and TP1), TP1_HIT (2 of 3 contracts exited at TP1, runner testing breakeven stop and TP2), and CLOSED (terminal). Every bar either leaves the state unchanged (a "no transition" step, most common) or advances it (OPEN → TP1_HIT, OPEN → CLOSED, or TP1_HIT → CLOSED). There is no path that goes backward. There is no path that goes from CLOSED to anything. FSMs with this structure are trivial to reason about, easy to unit-test by state, and impossible to get into "what state am I actually in" ambiguity — a class of bug Pine Script is prone to because it mixes variable lifetimes with bar indexing in ways that are easy to misread.

**Why small.** The resolver does one thing: given a position and a bar, decide what the position looks like after the bar. Everything else is someone else's job:
- Deciding whether a position should exist at all — the main indicator's cognition, not the resolver.
- Deciding whether a heartbeat is "real" — the webhook parser and I11 idempotency guard, not the resolver.
- Deciding what to do when no heartbeat arrives — the §8 staleness check, not the resolver.
- Writing to `positions` and `trade_outcomes` — `apply_resolver_result`, not the resolver.
- Posting to Discord — the wrapper, not the resolver.

Every line the resolver doesn't own is a line that can't fail in a subtle way. The goal of the §5 spec is to make the resolver as small as it can be while still being the source of truth for "what did this bar do to this position."

### 5.2 Function signature

```python
def step(position: PositionState, bar: Bar, *, eod_cutoff_ms: int) -> StepResult:
    """
    Pure function. Given an open or TP1_hit position and a newly-arrived bar,
    return an updated position state and a transition tag describing what
    happened on this bar.

    position:
        The current PositionState dataclass. Contains entry levels, direction,
        signal_type, current state (OPEN or TP1_HIT), effective_sl, MAE/MFE
        state, tp1_hit flag, last_heartbeat_bar_ms, etc. See §5.3.

    bar:
        A Bar dataclass carrying bar_close_ms, open, high, low, close.

    eod_cutoff_ms:
        Millisecond-of-day cutoff for EOD timeout. Keyword-only to prevent
        accidental positional misuse. Default set by the backend's config;
        the resolver is agnostic to its source.

    returns:
        A StepResult dataclass with:
          - updated_position: the post-bar PositionState
          - transition: one of NO_TRANSITION, TP1_HIT, TP2_HIT, SL_HIT,
            BE_STOP, EOD_TIMEOUT
          - exit_reason: str | None — same as transition when terminal
          - notes: list[str] — debug annotations for tests and logs

    Raises:
        ResolverInvariantError if the input position is in CLOSED state
        (the resolver is never called on closed positions; apply_resolver_result
        removes them from the active map before the next heartbeat).
    """
```

**Commitments this signature makes:**

1. The resolver never sees a CLOSED position. If it does, it raises — this is an invariant violation and the test suite has a specific case asserting it. The wrapper code is responsible for removing closed positions from the active map; the resolver is allowed to assume its inputs are always OPEN or TP1_HIT.

2. The resolver never sees a bar it has already processed. `apply_resolver_result` is responsible for checking `bar.bar_close_ms > position.last_heartbeat_bar_ms` before calling `step`. If a duplicate or replay arrives (I11 from §3), the wrapper skips the call. The resolver itself does not have to be defensive about replays, which keeps its logic focused on the forward-progress case. Test case TC-REPLAY in §5.12 asserts this at the wrapper level.

3. The resolver is passed `eod_cutoff_ms` explicitly rather than reading a global constant. This is what makes the test suite robust — a test can set `eod_cutoff_ms` to a value that makes the bar under test the EOD bar, without having to patch global state. This is a small decision with big testability payoff.

4. The resolver returns a `StepResult` that always contains an updated position. Even the NO_TRANSITION case returns an updated position — because MAE/MFE might have updated, or `last_heartbeat_bar_ms` incremented, even if no state transition fired. This keeps the write-through path in `apply_resolver_result` uniform: every return is committed.

### 5.3 States and state representation

There are three states. They are encoded as integers in the `positions.state` column per §4.5, and as an enum in the resolver module for readability:

```python
class PositionFSMState(IntEnum):
    OPEN = 1       # No TP1 yet. effective_sl == original sl. Targets: tp1, sl.
    TP1_HIT = 2   # TP1 taken. effective_sl == entry_price (BE stop). Targets: tp2, entry_price.
    CLOSED = 3    # Terminal. Resolver never sees this state on input.
```

**Semantic invariants on each state:**

**OPEN** — The position is alive, nothing has happened yet beyond the entry bar. The resolver is checking whether the current bar has breached `tp1` (transition to TP1_HIT), has breached `sl` (transition to CLOSED with exit_reason SL_HIT), both (priority rule: SL_HIT wins — see §5.6), or neither (NO_TRANSITION with MAE/MFE updates). In OPEN state, `effective_sl == sl` and `tp1_hit == 0`. INV-E from §4.6.

**TP1_HIT** — Two of the three contracts have exited at TP1. The remaining runner is targeting TP2 with a breakeven stop at `entry_price`. The resolver is checking whether the current bar has breached `tp2` (transition to CLOSED with TP2_HIT), has breached `entry_price` (transition to CLOSED with BE_STOP), both (priority rule: BE_STOP wins — see §5.6), or neither (NO_TRANSITION with MFE and post_tp1_mae updates). In TP1_HIT state, `effective_sl == entry_price` and `tp1_hit == 1`.

**CLOSED** — Terminal. The position has a `closed_at_ms`, `exit_reason`, and `final_pnl_points`. The `trade_outcomes` row has been written. The resolver does not see CLOSED positions because `apply_resolver_result` removes them from the active-position map before the next heartbeat arrives.

**Transitions allowed:**

```
                  ┌──── SL_HIT / EOD_TIMEOUT ────┐
                  │                              │
   OPEN ──────────┴── TP1_HIT ──── TP2_HIT ──────┴── CLOSED
                  │                BE_STOP      │
                  │                EOD_TIMEOUT  │
                  └──────────────────────────────┘
```

Seven total transition types from the two input states:

- OPEN → TP1_HIT (intermediate, not terminal)
- OPEN → CLOSED via SL_HIT
- OPEN → CLOSED via EOD_TIMEOUT (timed out before ever reaching TP1)
- TP1_HIT → CLOSED via TP2_HIT
- TP1_HIT → CLOSED via BE_STOP
- TP1_HIT → CLOSED via EOD_TIMEOUT (timed out with partial win)
- (NO_TRANSITION — state unchanged, but MAE/MFE and `last_heartbeat_bar_ms` updated)

The GAP_CLEAN exit reason from §4.5 is not produced by the resolver — it is produced by the §8 outage recovery path when a position is closed defensively on unrecoverable gap. The resolver only produces the six transition reasons above.

### 5.4 Direction-awareness

The resolver handles both LONG and SHORT positions. The logic is symmetric but the comparison operators flip. Rather than write two code paths, the implementation uses a small helper that returns a direction-aware set of tests:

```python
def _breached(level: float, direction: int, bar_high: float, bar_low: float) -> bool:
    """
    For a LONG position, a level is breached upward when bar_high >= level
    (TP1/TP2 hit) and downward when bar_low <= level (SL hit).
    For a SHORT position, the comparisons invert: bar_low <= level for TP1/TP2,
    bar_high >= level for SL.

    This helper is used for individual level checks; the full test is
    direction-aware throughout.
    """
```

The spec does not enumerate SHORT test cases separately from LONG test cases in §5.12 except where a direction-specific edge case exists (gap opens are direction-sensitive). The test suite, as a policy, has a LONG and a SHORT version of every test case. The §5.12 enumeration lists each test once with a note that the mirror case exists.

### 5.5 The step function — behavioral pseudocode

This is the resolver's logic stated in order of operations. It is not final code; it is the unambiguous statement of what the code does for the test cases in §5.12 to reference.

```
step(position, bar, eod_cutoff_ms):

    # 1. Invariant: position is OPEN or TP1_HIT, never CLOSED.
    if position.state == CLOSED:
        raise ResolverInvariantError("resolver called on closed position")

    # 2. Entry bar exclusion: if this bar IS the entry bar, no resolution.
    #    The entry bar's OHLC is irrelevant to the position; the position
    #    opened on this bar's close, so only subsequent bars count.
    if bar.bar_close_ms <= position.opened_at_ms:
        return NO_TRANSITION with MAE/MFE unchanged

    # 3. MAE/MFE update (always, regardless of transitions).
    #    For LONG: adverse = low, favorable = high. Sign convention from §4.5.
    #    For SHORT: adverse = high, favorable = low.
    new_mae_points, new_mae_bar_ms = update_mae(position, bar)
    new_mfe_points, new_mfe_bar_ms = update_mfe(position, bar)
    if position.state == TP1_HIT:
        new_post_tp1_mae = update_post_tp1_mae(position, bar)

    # 4. EOD timeout check (before level checks — a position at EOD exits
    #    at the close price, not at a stop or target).
    if bar.bar_close_ms >= eod_cutoff_ms:
        return CLOSED with exit_reason=EOD_TIMEOUT, final_pnl from bar.close

    # 5. Gap-open handling: if this bar opened outside the "alive" corridor
    #    for the current state, the position resolved at the open price.
    #    §5.6 defines the full priority rules; this step is their
    #    implementation.
    if _opened_beyond_exit(position, bar):
        return CLOSED with exit_reason=<computed>, final_pnl from bar.open

    # 6. Intra-bar level tests, with priority per §5.6.
    if position.state == OPEN:
        tp1_breached = _breached(position.tp1, position.direction, bar.high, bar.low)
        sl_breached  = _breached(position.effective_sl, position.direction, bar.low, bar.high)

        if sl_breached and tp1_breached:
            # Same-bar collision: SL_HIT wins (pessimistic fill). §5.6.
            return CLOSED with exit_reason=SL_HIT, final_pnl from position.sl
        if sl_breached:
            return CLOSED with exit_reason=SL_HIT, final_pnl from position.sl
        if tp1_breached:
            return TP1_HIT with effective_sl=entry_price, MAE/MFE updated
        return NO_TRANSITION with MAE/MFE updated

    if position.state == TP1_HIT:
        tp2_breached = _breached(position.tp2, position.direction, bar.high, bar.low)
        be_breached  = _breached(position.entry_price, position.direction, bar.low, bar.high)

        if tp2_breached and be_breached:
            # Same-bar collision: BE_STOP wins (pessimistic fill). §5.6.
            return CLOSED with exit_reason=BE_STOP, final_pnl from tp1_only
        if be_breached:
            return CLOSED with exit_reason=BE_STOP, final_pnl from tp1_only
        if tp2_breached:
            return CLOSED with exit_reason=TP2_HIT, final_pnl from full_tp2
        return NO_TRANSITION with MFE and post_tp1_mae updated
```

The behavioral structure is: **(1) guard, (2) entry bar exclusion, (3) always-update MAE/MFE, (4) EOD check, (5) gap-open check, (6) intra-bar level check with same-bar priority rule, (7) default no-transition**. Each test case in §5.12 exercises one or more of these steps and asserts the corresponding branch of logic.

### 5.6 Priority rules — the pessimistic fill convention

When two events could resolve on the same bar, the resolver has to pick one. The convention adopted here is called the **pessimistic fill rule**: when the bar's range contains both a favorable and an unfavorable level, the resolver assumes the unfavorable level was touched first. This is the conservative-against-the-trader convention and it matches how real markets behave under order-flow uncertainty — we don't know intra-bar sequence, and assuming the bad thing happened first gives a lower bound on performance that Data Lab analyses can trust.

**Rule PF1 — OPEN state, TP1 and SL both breached on one bar: SL_HIT wins.** Intuition: if the bar's low touches the stop and the bar's high touches TP1, we cannot know which came first. In reality, a stop order at SL fires as soon as price touches it, and a limit order at TP1 fires as soon as price touches it. If the adverse move came first, the stop triggered and the TP1 order never gets a chance to fill. The pessimistic assumption is that the adverse move came first. Test case TC6.

**Rule PF2 — TP1_HIT state, TP2 and BE stop both breached on one bar: BE_STOP wins.** Same intuition applied post-TP1. If the runner's bar range contains both TP2 and the breakeven stop, the BE stop is assumed to have triggered first, the runner gets stopped at BE, and the TP2 target is never reached. Small win, not a full win. Test case TC-BE-TP2.

**Rule PF3 — Gap open beyond any exit level: fill at the open price.** If the bar *opens* beyond a level that would close the position, the position is filled at the bar's open price, not at the level itself. For a LONG position:
- Gap down beyond SL: close at `bar.open`, exit reason SL_HIT, `final_pnl = bar.open - entry_price` (a worse fill than SL, correctly recorded).
- Gap up beyond TP2 (post-TP1): close at `bar.open`, exit reason TP2_HIT, `final_pnl = tp1_contribution + (bar.open - entry_price)` for the runner (a better fill than TP2, correctly recorded).
- Gap down beyond entry_price (post-TP1): close at `bar.open`, exit reason BE_STOP, `final_pnl = tp1_contribution + (bar.open - entry_price)` for the runner (a worse fill than breakeven — the runner loses on the BE stop).

For a SHORT position, the inverse comparisons apply.

The pessimistic fill convention on gaps says: record the actual fill at the actual open, whether that is better or worse than the level. Unlike the intra-bar collision rules (PF1, PF2), which are pessimistic because intra-bar sequence is unknowable, the gap rule is *realistic* because the gap open is observable — it is what any real order would have filled at. Test cases TC7 (gap beyond SL) and TC8 (gap beyond TP2).

**Rule PF4 — Entry bar is never resolved.** The bar on which a position opened cannot be used to resolve the position. A position opens on a bar's close, and the bar's high/low are already in the past by the time the entry is recorded. Using the same bar's high to claim TP1 would be using information the position did not have when it opened — the level was set by the same close price that the bar's high already exceeded. The resolver enforces this with a `bar.bar_close_ms > position.opened_at_ms` check before any level test. Entry bars are passed through with MAE/MFE unchanged (they cannot have adverse or favorable excursion that the position experienced, by construction). Test case TC9.

In practice, Pine sends the heartbeat on the bar *after* the entry, not on the entry bar itself — the entry fired on the close of bar T, and the first heartbeat fires on the close of bar T+1. So the entry bar exclusion is a defensive guard rather than a common path. It exists because §6 has to walk the case where ENTRY and HEARTBEAT arrive out of order, and the test suite asserts the defense is in place.

**Rule PF5 — Multiple concurrent positions in one heartbeat are resolved independently.** A single heartbeat may contain up to 4 populated slots (§3.4). Each slot corresponds to one position. The resolver is called once per slot. The positions do not interact; one position resolving does not affect another position in the same heartbeat. The order in which slots are processed is irrelevant to correctness (each call is on a different `position` object) but is deterministic for logging: slot 1 first, then slot 2, etc. Test case TC15.

### 5.7 MAE/MFE update rules

Maximum Adverse Excursion and Maximum Favorable Excursion are the two non-transition updates the resolver performs on every bar. They are computed against the entry price, in points, signed consistently with the position's direction.

**Sign convention for LONG positions:**
- MAE is the most negative (worst) `low - entry_price` value seen across all bars processed. MAE is zero or negative.
- MFE is the most positive (best) `high - entry_price` value seen across all bars processed. MFE is zero or positive.

**Sign convention for SHORT positions:**
- MAE is the most negative `entry_price - high` value seen across all bars. A SHORT's adverse excursion is to the upside, so `entry_price - high` is negative when the bar's high exceeded entry. MAE is zero or negative.
- MFE is the most positive `entry_price - low` value seen across all bars. A SHORT's favorable excursion is to the downside. MFE is zero or positive.

Both LONG and SHORT MAE are signed negative; both MFE are signed positive. A reader can always ask "how bad did it get" (more negative = worse) and "how good did it get" (more positive = better) without thinking about direction.

**The update rule on each bar:**

```
update_mae(position, bar):
    if LONG:
        this_bar_adverse = bar.low - position.entry_price    # ≤ 0 if bar dropped below entry
    else:  # SHORT
        this_bar_adverse = position.entry_price - bar.high   # ≤ 0 if bar rose above entry
    if this_bar_adverse < position.mae_points:
        return (this_bar_adverse, bar.bar_close_ms)
    return (position.mae_points, position.mae_bar_ms)  # unchanged
```

MFE is symmetric with `bar.high` (LONG) / `bar.low` (SHORT) and the inverse comparison.

**Crucial rule: MAE/MFE updates happen on every bar the resolver processes, including the bar on which a transition fires.** If a bar hits TP1 AND also posts a deeper MAE than any previous bar, both updates are recorded. The final `positions` row reflects the worst adverse excursion observed across the full position's lifetime, even if that worst excursion happened on the same bar the position made money. Test case TC10 exercises this: a bar with a range that both dips below the previous MAE and closes at TP1 produces an update to both `mae_points` (worse) and `tp1_hit` (yes). The MAE update is committed whether or not a transition occurs.

**post_tp1_mae.** After TP1 is hit, the resolver additionally tracks `post_tp1_mae_points` — the depth of adverse excursion below TP1 while the runner is active. This is a separate metric from overall MAE and has a different interpretation: it measures "how close did the runner come to being stopped out at breakeven." Zero means the runner never pulled back below TP1; a large positive number means the runner gave back most of TP1's gains before hitting TP2 or BE. Data Lab uses this for runner-quality analysis.

```
update_post_tp1_mae(position, bar):
    if position.state != TP1_HIT:
        return position.post_tp1_mae_points  # unchanged
    if LONG:
        this_bar_drop = position.tp1 - bar.low
    else:
        this_bar_drop = bar.high - position.tp1
    if this_bar_drop > position.post_tp1_mae_points:
        return this_bar_drop
    return position.post_tp1_mae_points
```

`post_tp1_mae_points` is non-negative. It starts at 0 on TP1 transition and only grows. Test case TC-POST-TP1-MAE.

### 5.8 EOD timeout rule

A position still open when the resolver receives a heartbeat whose `bar.bar_close_ms >= eod_cutoff_ms` is closed at the bar's close price with exit_reason EOD_TIMEOUT. The final_pnl is computed from `bar.close`, not from any level.

**The cutoff value.** `eod_cutoff_ms` is a millisecond-of-day value matching the session close time the main indicator defines. The current Pine session window ends at 15:45 ET (`"0930-1545"` in the v3 script), which is the last-entry cutoff, not the last-resolution cutoff. The resolver needs a separate EOD cutoff, nominally 16:00 ET, to give any still-open runner 15 minutes of headroom past the entry window for natural resolution before forcing a timeout. This is an open item in §11 — Josh and Strategist get the call — but the default for this spec is 16:00 ET (4:00 PM Eastern).

Encoding: the resolver receives `eod_cutoff_ms` as an argument, the backend computes it from wall-clock date plus the cutoff-of-day constant, and the tests set it explicitly. The resolver does not compute the date; it compares the heartbeat bar's `bar_close_ms` against the value it was passed. This keeps the function pure and the test isolation clean.

**Why EOD resolution at `bar.close` and not at some other price.** The EOD bar's close is the last observed price of the trading session for this instrument's RTH window. A real mechanical executor on the 3/2/1 plan with a hard session-close flat rule would exit any still-open position at the close. The resolver models this. An alternative would be to exit at the level the runner was testing (BE stop for a post-TP1 position, original SL for an OPEN position that never hit TP1) — but that mis-models what a flat-at-close rule actually does. The close price is the honest answer.

**EOD timeout takes priority over all level checks on the same bar.** If the EOD bar would have hit TP2 and also hit the BE stop, the EOD rule wins — the position closes at `bar.close`, not at either level. The spec chooses this ordering because a flat-at-close execution by a mechanical system happens regardless of where intra-bar levels were touched; the flat is deterministic, the intra-bar sequence is not. Test case TC11.

### 5.9 Idempotency and replay safety

A core design promise of §3's HEARTBEAT handling (invariant I11) is that the resolver is safe against replay: the same heartbeat processed twice produces the same final state as if it had been processed once. This is the property that makes §8's Finnhub replay mechanism work — the replay can reprocess bars the backend already saw without corrupting position state.

The resolver achieves idempotency by **(a) never mutating its input position**, **(b) being driven by a monotonically-advancing `last_heartbeat_bar_ms` guard in the wrapper**, and **(c) producing outputs that are a pure function of inputs**.

**(a)** — The `position` argument is not modified in place. The resolver returns a new `PositionState` object with the updated fields. Python's dataclasses make this `dataclasses.replace(position, ...)` in practice. The caller can call the resolver twice with the same inputs and get the same outputs, because nothing on the input side has changed between calls.

**(b)** — `apply_resolver_result` (§5.10) checks `bar.bar_close_ms > position.last_heartbeat_bar_ms` before calling the resolver. If the check fails, the wrapper logs `⚠️ REPLAY bar=X position=Y last=Z` and skips. This means the resolver itself never sees a replay — the wrapper filters them out. The resolver is therefore guaranteed to see bars in strictly increasing order per position, which simplifies its internal logic and means the test cases can assume strict ordering.

**(c)** — No random numbers, no wall-clock reads, no global lookups. The resolver's output is a pure function of its three arguments.

**Consequence.** The test suite can call `step(position, bar, eod_cutoff_ms=X)` twice in a row on the same inputs and assert that both return the same `StepResult`. This is a property test in the test suite: "for any valid input, two calls return equal results." Test case TC-IDEMPOTENT in §5.12.

### 5.10 Commit ordering and `apply_resolver_result`

The pure resolver is called by a thin wrapper that applies the result to persistent state. The wrapper is the only place in the backend where the `positions` table and the `trade_outcomes` table are written for position events. Its job is to get commit ordering right.

```python
def apply_resolver_result(
    position: PositionState,
    bar: Bar,
    result: StepResult,
    fsm_map: ActivePositionMap,
    conn: sqlite3.Connection,
) -> None:

    # 1. Update the in-memory position FIRST? No — DB first, then memory.
    #    Rationale: if we crash between memory update and DB write,
    #    the on-disk state is the source of truth and memory gets rehydrated.
    #    We want on-disk to lead memory, not trail it.

    with conn:  # transactional context — rollback on any exception
        if result.transition == TRANSITION.NO_TRANSITION:
            # Update positions row: MAE/MFE, last_heartbeat_bar_ms, heartbeats_processed
            conn.execute(
                'UPDATE positions SET mae_points=?, mae_bar_ms=?, mfe_points=?, '
                'mfe_bar_ms=?, post_tp1_mae_points=?, last_heartbeat_bar_ms=?, '
                'heartbeats_processed=heartbeats_processed+1 '
                'WHERE signal_id=?',
                (result.updated_position.mae_points,
                 result.updated_position.mae_bar_ms,
                 ...,
                 result.updated_position.signal_id)
            )

        elif result.transition == TRANSITION.TP1_HIT:
            # Update positions row: state=2, tp1_hit=1, tp1_hit_bar_ms,
            # effective_sl=entry_price, plus MAE/MFE updates.
            conn.execute('UPDATE positions SET state=?, tp1_hit=?, ...', (...))

        elif result.transition in (TRANSITION.TP2_HIT, TRANSITION.BE_STOP,
                                    TRANSITION.SL_HIT, TRANSITION.EOD_TIMEOUT):
            # Terminal transition: update positions to state=3, write trade_outcomes,
            # post OUTCOME embed.
            conn.execute('UPDATE positions SET state=3, closed_at_ms=?, '
                         'closed_at_ts=?, exit_reason=?, final_pnl_points=?, ... '
                         'WHERE signal_id=?', (...))
            conn.execute('INSERT INTO trade_outcomes (signal_id, timestamp_opened, '
                         'timestamp_closed, tp1_hit, tp1_hit_time, ...) VALUES (?, ?, ...)',
                         (...))

        # Transaction commits at the end of `with conn:` block.

    # 2. NOW update in-memory state (after DB commit succeeded).
    fsm_map[position.signal_id] = result.updated_position

    # 3. If terminal, remove from active map.
    if result.transition in TERMINAL_TRANSITIONS:
        del fsm_map[position.signal_id]
        # Rebuild the by-bar-close-ms secondary index to remove this position.
        fsm_map.by_bar_close_ms.pop(position.bar_close_ms, None)

    # 4. Post Discord embed for terminal transitions.
    if result.transition in TERMINAL_TRANSITIONS:
        post_outcome_embed(result.updated_position, result.transition)

    # 5. Log the transition per layer convention.
    log_transition(position, bar, result)
```

**Commit ordering rules, stated explicitly:**

- **CO1.** DB commit precedes in-memory update. A Railway crash between DB commit and memory update is recoverable — rehydration (§4.9) reads the committed row. A crash between memory update and DB commit would leave memory ahead of disk; that ordering is forbidden.

- **CO2.** For a terminal transition, the `positions` update and the `trade_outcomes` insert happen in the same SQL transaction. Either both commit or neither does. INV-D is enforced at the transaction boundary.

- **CO3.** The in-memory map is updated after the transaction commits successfully. If the commit raises (SQLite lock contention, disk full, etc.), the in-memory map is untouched and the exception propagates to the webhook handler, which logs `❌ [DB_COMMIT_FAILED]` and returns HTTP 500. TradingView will log an alert delivery failure but will not retry, so the heartbeat is lost from the backend's perspective. §8's gap recovery will detect the missed heartbeat on the next successful delivery and recover.

- **CO4.** The Discord post happens only after both the DB commit and the in-memory update succeeded. If the Discord post fails, the state is already consistent on disk and in memory; the embed is logged-and-skipped and the user sees no message, but the data is correct. (This is a known quirk — a position can have a `trade_outcomes` row but no Discord embed if Discord was down at the close moment. A diagnostic endpoint can re-post missed embeds from the database if desired. Not in scope for v3.0.)

- **CO5.** Logging happens last. If logging fails, nothing else is undone.

The ordering — *DB → memory → Discord → log* — is the durability hierarchy: the thing most important to preserve (on-disk state) happens first, the thing least important (log line) happens last, and each step is a clean point at which a failure leaves the system in a well-defined state.

### 5.11 Constructing the `trade_outcomes` row on close

When the resolver produces a terminal transition, `apply_resolver_result` constructs the `trade_outcomes` row from the closed position's state. This is a mechanical mapping with one non-obvious part.

**The mechanical mapping:**

```
trade_outcomes.signal_id         = position.signal_id
trade_outcomes.timestamp_opened  = position.opened_at_ts
trade_outcomes.timestamp_closed  = fmt_et(bar.bar_close_ms or now)
trade_outcomes.tp1_hit           = 1 if position.tp1_hit else 0
trade_outcomes.tp1_hit_time      = fmt_et(position.tp1_hit_bar_ms) if position.tp1_hit else None
trade_outcomes.tp2_hit           = 1 if result.transition == TP2_HIT else 0
trade_outcomes.tp2_hit_time      = fmt_et(bar.bar_close_ms) if result.transition == TP2_HIT else None
trade_outcomes.sl_hit            = 1 if result.transition == SL_HIT else 0
trade_outcomes.sl_hit_time       = fmt_et(bar.bar_close_ms) if result.transition == SL_HIT else None
trade_outcomes.be_stop_hit       = 1 if result.transition == BE_STOP else 0
trade_outcomes.be_stop_hit_time  = fmt_et(bar.bar_close_ms) if result.transition == BE_STOP else None
trade_outcomes.exit_reason       = result.exit_reason   # string
trade_outcomes.final_pnl_points  = computed (see below)
trade_outcomes.mae_points        = position.mae_points
trade_outcomes.mfe_points        = position.mfe_points
trade_outcomes.mae_time_min      = minutes from open to mae_bar_ms
trade_outcomes.mfe_time_min      = minutes from open to mfe_bar_ms
trade_outcomes.post_tp1_mae_points = position.post_tp1_mae_points
trade_outcomes.time_in_trade_min = (closed_at_ms - opened_at_ms) / 60000
trade_outcomes.is_ghost          = 0
```

**The non-obvious part: `final_pnl_points`.**

The 3/2/1 plan exits 2 contracts at TP1 and 1 runner at TP2 or BE stop. The `final_pnl_points` column in `trade_outcomes` needs to represent the *per-contract-equivalent* or the *total realized*, and that choice has to be made consistently. The current backend and the retired Monitor both used total-realized. The spec keeps that convention.

**Computation of `final_pnl_points` by exit reason:**

Define `tp1_points = abs(position.tp1 - position.entry_price)`, the per-contract distance to TP1. Similarly `tp2_points`, `sl_points`.

- **SL_HIT** (never reached TP1): all 3 contracts stopped out. `final_pnl_points = -3 * sl_points` (signed negative).
- **TP1_HIT only, then EOD_TIMEOUT before runner resolved** (partial win, timed out): 2 contracts at TP1, 1 runner closed at bar.close. `final_pnl_points = 2 * tp1_points + 1 * (bar.close - position.entry_price) * direction_sign`.
- **TP1_HIT then BE_STOP** (small win): 2 contracts at TP1, runner at breakeven. `final_pnl_points = 2 * tp1_points + 0` (runner contributes zero, since it closed at entry). Note: if the BE stop is hit via gap, the runner's contribution can be negative — see PF3.
- **TP1_HIT then TP2_HIT** (full win): 2 contracts at TP1, 1 contract at TP2. `final_pnl_points = 2 * tp1_points + 1 * tp2_points`.
- **EOD_TIMEOUT before TP1** (no wins at all, timed out): all 3 contracts closed at bar.close. `final_pnl_points = 3 * (bar.close - position.entry_price) * direction_sign`. Sign depends on whether close is above or below entry.

The `direction_sign` is `+1` for LONG and `-1` for SHORT, so that favorable moves contribute positive PnL and adverse moves contribute negative PnL regardless of direction.

The spec commits to these formulas explicitly so the test suite can assert exact final_pnl values rather than just exit reasons.

### 5.12 Test case enumeration

This is the deliverable §5 exists for. Each test case has an ID, a narrative, input state, input bar sequence, and expected assertions. The test suite is written by transcribing each case into a pytest function with the same ID. **These are written before the resolver implementation.** The resolver is correct when all of them pass.

LONG cases are spelled out explicitly. Each LONG case has a mirror SHORT case generated by inverting direction, bar high/low, and level positions — the spec does not enumerate both, but the test suite includes both.

**Entry price for all cases below: 25000.00. SL: 24980.00 (20 points). TP1: 25030.00 (30 points = 1.5× ATR assuming ATR=20). TP2: 25050.00 (50 points = 2.5× ATR). opened_at_ms: 1744416000000. `eod_cutoff_ms: 1744488000000` (well in the future for most tests; adjusted per-test where relevant).**

---

**TC1 — Normal TP1 then TP2 (LONG).**
- Initial state: OPEN, LONG, mae=0, mfe=0, effective_sl=24980
- Bars:
  - Bar 1: bar_close_ms=1744416900000 (entry+15min), OHLC (25000, 25010, 24995, 25005). No TP1, no SL. NO_TRANSITION, mae=-5, mfe=10.
  - Bar 2: bar_close_ms=1744417800000, OHLC (25005, 25035, 25000, 25030). High ≥ tp1. **TP1_HIT transition.** State → TP1_HIT, tp1_hit=1, tp1_hit_bar_ms=1744417800000, effective_sl=25000, mfe=35.
  - Bar 3: bar_close_ms=1744418700000, OHLC (25030, 25055, 25025, 25050). High ≥ tp2, low > entry_price (BE safe). **TP2_HIT transition.** State → CLOSED, exit_reason=TP2_HIT.
- Assertions:
  - `trade_outcomes.tp1_hit == 1`, `trade_outcomes.tp2_hit == 1`, `trade_outcomes.exit_reason == "TP2_HIT"`
  - `trade_outcomes.final_pnl_points == 2*30 + 1*50 == 110`
  - `trade_outcomes.mae_points == -5`
  - `trade_outcomes.mfe_points == 55`
  - `trade_outcomes.post_tp1_mae_points == 5` (bar 3 low 25025 vs tp1 25030)

**TC2 — Normal TP1 then TP2 (SHORT mirror).** Same structure, inverted. Entry 25000, SL 25020, TP1 24970, TP2 24950. Bar highs and lows swapped in role. Final_pnl same magnitude, sign handled by direction.

**TC3 — TP1 then BE_STOP (small win, LONG).**
- After TP1 hit on bar 2, bar 3 has OHLC (25030, 25040, 24999, 25010). Low ≤ entry_price. **BE_STOP transition.**
- `trade_outcomes.tp1_hit == 1`, `trade_outcomes.tp2_hit == 0`, `trade_outcomes.be_stop_hit == 1`
- `final_pnl_points == 2*30 + 0 == 60`

**TC4 — TP1 then EOD_TIMEOUT (partial win, LONG).**
- After TP1 hit, eod_cutoff_ms set so bar 5 is EOD. Bar 5 OHLC (25040, 25045, 25035, 25040) — runner still alive, no transition on levels, but bar_close_ms ≥ eod_cutoff_ms. **EOD_TIMEOUT.**
- `final_pnl_points == 2*30 + 1*(25040-25000) == 100`
- `exit_reason == "EOD_TIMEOUT"`
- `tp1_hit == 1`, `tp2_hit == 0`, `be_stop_hit == 0`

**TC5 — SL before TP1 (full loss, LONG).**
- Bar 1 OHLC (25000, 25010, 24975, 24985). Low ≤ SL. **SL_HIT transition.**
- `final_pnl_points == -3 * 20 == -60`
- `exit_reason == "SL_HIT"`
- `tp1_hit == 0`

**TC6 — TP1 and SL on the same bar (PF1: SL_HIT wins).**
- Bar 1 OHLC (25000, 25035, 24979, 25020). High ≥ TP1 AND low ≤ SL.
- Per PF1, **SL_HIT wins.** Final pnl = -60. Exit reason SL_HIT.
- Assertion: `trade_outcomes.tp1_hit == 0` (never transitioned to TP1_HIT state, because SL was the priority resolution).
- Note in test: this is the pessimistic-fill convention. Document in test docstring so a future maintainer reading the test knows why.

**TC7 — Gap open beyond SL (LONG).**
- Bar 1 OHLC (24970, 24990, 24965, 24985). Bar *opened* at 24970 — already below SL=24980. Per PF3, **SL_HIT at bar.open = 24970.**
- `final_pnl_points == 3 * (24970 - 25000) == -90`. Worse than the normal SL fill of -60 because the gap exceeded the SL level.
- `exit_reason == "SL_HIT"` (the reason is still SL, but the fill is worse).
- Test asserts `final_pnl_points == -90`, not `-60`. This is the honesty of the gap rule.

**TC8 — Gap open beyond TP2 after TP1 (LONG).**
- Bar 1 hits TP1. Bar 2 OHLC (25060, 25065, 25058, 25062). Bar 2 opened at 25060 — already above TP2=25050. Per PF3, **TP2_HIT at bar.open = 25060.**
- Runner's contribution: `25060 - 25000 == 60 points` (better than TP2's 50).
- `final_pnl_points == 2*30 + 1*60 == 120`. Better than the normal TP2 fill of 110.
- `exit_reason == "TP2_HIT"`.

**TC9 — Entry bar excluded from resolution.**
- A heartbeat arrives with `bar.bar_close_ms == position.opened_at_ms`. (In practice this shouldn't happen because the ENTRY fires on bar close and the first heartbeat fires on the next bar's close, but §6 has to walk same-bar delivery as a failure case, so the defense matters.)
- Bar 0 (the entry bar) OHLC (25000, 25060, 24970, 25000) — range contains TP1, TP2, AND SL.
- Expected: **NO_TRANSITION.** MAE and MFE unchanged from their initial zero values. State still OPEN.
- Test asserts the resolver did not claim TP1, TP2, or SL despite all three being "inside" the bar range, because the bar is the entry bar.

**TC10 — MAE/MFE updated on every bar regardless of transitions.**
- Bar 1: OHLC (25000, 25020, 24985, 25015). No TP1 (high < TP1). No SL (low > SL). NO_TRANSITION. mae=-15, mfe=20.
- Bar 2: OHLC (25015, 25035, 24982, 25030). High ≥ TP1. **TP1_HIT transition.** mae=-18 (24982 - 25000, worse than previous -15). mfe=35.
- Assertion: `position.mae_points == -18` even though this bar also had a transition. The MAE update is not skipped on transition bars.

**TC11 — EOD_TIMEOUT takes priority over level checks.**
- eod_cutoff_ms = 1744417800000 (bar 2's close time).
- Bar 2 OHLC (25000, 25055, 24979, 25040). High ≥ TP2, low ≤ SL, AND bar_close_ms == eod_cutoff_ms.
- Per §5.8, EOD_TIMEOUT wins even though levels were breached.
- `exit_reason == "EOD_TIMEOUT"`. `final_pnl_points == 3 * (25040 - 25000) == 120`. Note: better than SL but worse than TP2 — the EOD rule ignores level breaches and uses bar.close.

**TC12 — Unknown position heartbeat (safe no-op at wrapper level).**
- Heartbeat arrives with `pos_slot_1_time == X` where X does not match any position in `fsm_map`.
- The wrapper logs `⚠️ [HEARTBEAT] unknown position bar_close_ms=X` and skips this slot.
- The resolver is NOT called. The webhook still returns HTTP 200.
- Assertion: `fsm_map` is unchanged. No exceptions. Log line present with the unknown time.

**TC13 — Idempotency (same heartbeat twice).**
- Bar 1 hits TP1 cleanly. After processing, `position.state == TP1_HIT`, `position.tp1_hit_bar_ms == bar1.bar_close_ms`.
- Process the same bar again. Expected: wrapper detects `bar.bar_close_ms == position.last_heartbeat_bar_ms` and short-circuits with a REPLAY log line. Position state unchanged.
- Alternatively (property test): call `step()` twice on the same inputs directly. Assert both return equal `StepResult` objects, including all MAE/MFE values. No mutation of input.

**TC14 — Replay (heartbeat with earlier bar_close_ms).**
- Position has `last_heartbeat_bar_ms = bar2.bar_close_ms` after processing normal bars 1 and 2.
- A heartbeat arrives for bar 1 (earlier). Wrapper detects `bar.bar_close_ms < position.last_heartbeat_bar_ms`, logs `⚠️ REPLAY`, and skips.
- Assertion: position state identical to before the replay heartbeat.

**TC15 — Multiple concurrent positions in one heartbeat.**
- Two positions open. Position A (TREND LONG, signal_id=1, bar_close_ms=T1). Position B (SQUEEZE LONG, signal_id=2, bar_close_ms=T2). Both in fsm_map.
- Heartbeat arrives with `pos_slot_1_time=T1`, `pos_slot_2_time=T2`, bar OHLC that hits TP1 for A but not for B.
- Expected: resolver is called twice. Position A transitions to TP1_HIT. Position B gets NO_TRANSITION with MAE/MFE updates.
- Assertion: both write-throughs happen in distinct transactions (or one transaction per position — implementation choice, but tests the isolation). Position B's state is unaffected by Position A's transition and vice versa.

**TC16 — TP2_HIT and BE_STOP collision on same bar (PF2: BE_STOP wins).**
- After TP1 hit, bar 3 OHLC (25020, 25055, 24999, 25040). High ≥ TP2=25050 AND low ≤ entry_price=25000.
- Per PF2, **BE_STOP wins.** `exit_reason == "BE_STOP"`. `final_pnl_points == 2*30 + 0 == 60`.
- Pessimistic-fill convention documented in test.

**TC17 — post_tp1_mae tracking.**
- After TP1 hit on bar 2, bar 3 OHLC (25030, 25045, 25010, 25040). Low 25010 is below TP1 but above entry (no BE stop). No TP2 (high < TP2).
- NO_TRANSITION, but `post_tp1_mae_points = 25030 - 25010 = 20`.
- Bar 4 OHLC (25040, 25060, 25020, 25050). High ≥ TP2. **TP2_HIT transition.**
- `post_tp1_mae_points == 20` on final row (bar 4 did not post a deeper post-TP1 MAE because its low 25020 was shallower than 25010).

**TC18 — No-transition on a normal bar.**
- Bar 1 OHLC (25000, 25020, 24990, 25010). Inside the corridor. NO_TRANSITION. MAE=-10, MFE=20. `last_heartbeat_bar_ms` updated. `heartbeats_processed += 1`.
- Sanity test that the no-transition path actually returns a correctly-updated position without erroneously transitioning.

**TC19 — Rehydration correctness.**
- Set up a position, process bars 1 and 2 (hitting TP1 on bar 2), commit to DB.
- Simulate restart: clear fsm_map, call the rehydration function that reads from `positions` WHERE state != 3.
- Verify the rehydrated position matches the pre-restart in-memory state field-by-field.
- Process bar 3 (hits TP2). Assert the transition fires correctly against the rehydrated position. `trade_outcomes.final_pnl_points` matches what it would have been without the restart.

**TC20 — Heartbeat for a CLOSED position (defensive).**
- This shouldn't happen — `apply_resolver_result` removes closed positions from fsm_map at transition time — but if the position somehow leaks back in, calling `step()` on it should raise `ResolverInvariantError`, not silently corrupt state.
- Test constructs a fake `PositionState` with `state == CLOSED`, calls `step()`, asserts the exception is raised.

**TC21 — LONG and SHORT symmetry.**
- Every LONG test case has a SHORT mirror. The mirror is constructed by: flipping direction, inverting entry/SL/TP1/TP2 arithmetic, swapping high/low roles in level tests. Test infrastructure provides a helper that generates SHORT cases from LONG specs to minimize duplication while ensuring both are tested.

**TC22 — ResolverInvariantError on bad input.**
- Pass an input with `state == CLOSED` → raise.
- Pass a position with `effective_sl` inconsistent with state (e.g., state=OPEN but effective_sl != sl) → raise. (This is INV-E from §4.6.)
- Pass a bar with `bar_close_ms` that isn't a positive integer → raise.
- These are defensive invariants; they should never be triggered in production, but test them.

**TC23 — MAE/MFE initial zero (entry bar preceded by no bars).**
- Position just opened. No bars processed yet. `mae_points == 0`, `mfe_points == 0`.
- First heartbeat arrives (bar after entry). Assert MAE/MFE update correctly from the zero baseline.

---

**The above enumeration is the TDD commitment.** When it is time to write the resolver, the test file `tests/test_position_resolver.py` is scaffolded from this §5.12 list — one test function per TC, with `position` and `bar` fixtures constructed to match each case. The test file is written and run; it fails (no implementation exists). Then the implementation is written to make the tests pass, one test at a time. When all tests pass, the resolver is correct by the spec. Any bug found in observation that traces back to the resolver is a missing test case — and the fix is to add the test case, assert it fails against the current implementation, fix the implementation, verify it passes, then verify all other tests still pass.

This is the exact ratio Strategist committed to in the closeout handoff: the test suite takes longer than the implementation. The implementation is roughly 150–200 lines of Python. The test suite is roughly 500–800 lines. That ratio is a spec deliverable.

### 5.13 Concurrent positions handling

§5.12's TC15 covers the functional correctness of multiple positions in one heartbeat. §5.13 addresses the subtleties not covered by a single test case: ordering, logging, and overflow.

**Ordering.** The heartbeat payload contains up to 4 slot values. The backend processes them in slot order (slot 1 first, slot 2 next, etc.). This ordering is deterministic and reproducible for tests, but does not affect correctness — each position is independent, so the order does not matter for the resulting database state. The determinism exists for log-reading and debugging, not for logic.

**Logging.** For a heartbeat with N populated slots and K transitions among them, the log lines are:

```
📥 HEARTBEAT bar=<bar_close_ms> slots=[T1, T2, 0, 0]
📝 resolver signal_id=A NO_TRANSITION mae=-5 mfe=10
📝 resolver signal_id=B TP1_HIT tp1_hit_bar=<bar>
📤 #trade-journal (no post — TP1 does not post to Discord; only terminal transitions do)
```

The layer convention (`📥 📝 📤`) applies per-position within the heartbeat, not per-heartbeat. A single heartbeat with one resolved position produces one `📥` line and multiple `📝` lines.

**Overflow.** If Pine's openPositions tracker has 5+ positions but only 4 slots, the 5th position gets logged on Pine's side as a slot overflow and is not included in heartbeats. The backend has no way to know about it because it never sees the heartbeat fields for that position. The behavior in this case is: the 5th position's entry was still recorded in `signals_v3` and `positions` (because the ENTRY webhook fires regardless of slot availability), but no heartbeats flow for it until a slot frees. When a slot frees (via one of the tracked positions closing in Pine's view), Pine can start including the 5th position in subsequent heartbeats.

**This is a degradation mode, not a correctness violation.** The 5th position might get zero heartbeats for its entire lifetime if no slot ever frees. When its EOD cutoff passes, the resolver still has not seen any heartbeats for it — and the staleness check (§8.4) catches it. The staleness check sees "position X has never had a heartbeat processed and its opened_at_ms is more than N bars ago" and closes it defensively with exit_reason=GAP_CLEAN.

**This overflow mode is observable.** Every instance where Pine overflows a slot is a log line (on the TradingView side, visible in the alert log; on the backend side, visible as a staleness event). Observation will tell us whether overflow is a real risk or a theoretical one. If observation shows overflows happening, N is raised from 4 to 8 (one-line Pine change per additional slot). The spec deliberately starts small (N=4) and grows on data, not on theory.

---

## End of Part 3

§5 closes with the resolver fully specified: contract, states, transitions, priority rules, MAE/MFE rules, EOD rule, idempotency, commit ordering, trade_outcomes construction, test case enumeration, and concurrent handling. The test suite is writable from §5.12 without further design work.

Items deferred to later parts:

- **§6 (keystone walked)** uses §5's contract and §3's event routing to walk the five named failure cases from the Going-In Notes (§3 of that doc). §6 does not add new resolver behavior — it asserts that §5's resolver already handles each case correctly, and identifies any case where §5 needs an additional test.

- **§7 (heartbeat plot in the main indicator)** defines the Pine side of §3's HEARTBEAT event contract. It commits to the plot limit verification from §3.6, to the slot add/remove logic, and to the interaction with the single-alertcondition bottleneck.

- **§8 (Railway outage recovery)** defines the staleness check, the Finnhub replay mechanism, and the GAP_CLEAN exit reason that §5.3's state transition list mentioned but §5 did not specify. §8 is where the replay actually lives.

- **§9 (Reputation Engine integration)** is short. It states the boundary — the resolver never touches the Reputation Engine, the Reputation Engine never touches the resolver — and verifies it one more time against the cognition/measurement boundary from §1.4.

- **§10 (migration and rollback)** defines how the current v2.1.1 production moves to Option C v3.0 and how to get back if it goes wrong.

- **§11 (open items)** flags every spec commitment that needs Josh or Strategist sign-off before code starts.

- **§12 (what dies, what survives)** is the ledger from the rethesis §4.6 and §7, restated as the authoritative retirement and preservation list.


**Author:** Dev Trader (Senior Quant Dev)
**Date:** April 13, 2026 (Monday)
**Status:** DRAFT — awaiting Strategist review after all five parts land
**Continues:** `MONA_v3_OptionC_Spec_Part3.md`

---

## 6. KEYSTONES WALKED

### 6.1 What this section is for

Strategist's framing from the closeout handoff was that the keystone did not disappear under Option C — it moved. Under Option A the keystone was: *can the backend reliably match a TRADE_OUTCOME from the Monitor to the correct signal_id from the main indicator, given that the two scripts emit on different schedules?* That question was cross-script, and Finding #0 was one of the failure modes of that question.

Under Option C the equivalent question is: *can the backend's FSM reliably correlate incoming heartbeat ticks to open positions in the `positions` table, such that TP1/SL/BE/TP2/EOD events are attributed to the correct signal_id?* The reference is now intra-script — the main indicator stamps `bar_close_ms` from `time_close` on both the ENTRY event and every subsequent HEARTBEAT, and those values come from the same execution context, the same chart, the same bar. The keystone is structurally cleaner because the two things being compared come from the same source.

**But "structurally cleaner" is not "automatically correct."** The keystone question still has failure cases, and the Going-In Notes enumerated five of them that the spec has to walk explicitly. Each one is a specific sequence of events that could produce wrong behavior if §5's resolver and §3's routing are not robust to it. This section walks each case against the resolver and routing defined in Parts 2 and 3, and commits to the spec's answer for each.

The test that each subsection applies is: *given the full specification in §3 and §5 as already written, is the failure mode handled correctly? If yes, point at the specific rule or test case that handles it. If no, what additional rule does the spec need?*

Across the five cases, the answer is "yes, already handled" for four of them and "yes, with one addition" for the fifth. The addition is flagged at the end of §6.5 and folded into the spec commitments.

### 6.2 Case 1 — ENTRY and HEARTBEAT fire on the same bar, arrive at the backend out of order

**The failure sequence, step by step.** At the close of bar T, the Reputation Engine authorizes a new TREND LONG entry. The main indicator fires the ENTRY alertcondition with `bar_close_ms = T`. On the *same* bar close, if any other position was already open in the local `openPositionTimes` array, the HEARTBEAT alertcondition also fires for bar T — except wait, they share one alertcondition (§3.7). So which event fires?

This is the first question the spec has to answer before walking the failure case: **on a bar that would trigger both an ENTRY and a HEARTBEAT, which one actually fires through the shared alertcondition?**

**Resolution: ENTRY takes priority on a same-bar collision.** The Pine-side logic in §7 will encode this: if an ENTRY fires on this bar, the HEARTBEAT suppresses itself for this bar, and resumes next bar. The consequence is that the newly-entered position misses one heartbeat (its own entry bar — which it would never have been resolved on anyway, per PF4 and TC9). Any already-open positions also miss one heartbeat on the ENTRY bar, but they resume the next bar. This is a measurement gap of one bar per ENTRY event, which is observable, small, and acceptable.

The alternative — firing both events on the same bar via some mechanism (separate alertconditions, multiple alertcondition lines, etc.) — is addressed in §7.6. The simpler answer for the shared-alertcondition path is ENTRY-priority suppression, and §6.2 assumes this resolution.

**Under the ENTRY-priority rule, the same-bar collision is not a failure.** The ENTRY arrives cleanly, gets routed to the ENTRY handler, writes `signals_v3` and `positions`, creates the FSM entry. The HEARTBEAT for the other open positions is delayed by one bar — next bar's heartbeat carries all of them, including the newly-created position. The newly-created position's entry bar is never sent as a heartbeat target because §5.5 step 2 (entry bar exclusion) would have been a no-op on it anyway.

**What if the out-of-order arrival happens in a different sense — ENTRY and HEARTBEAT fire on adjacent bars and the HTTP requests arrive at the backend in the reverse of the order they were sent?** This is the failure case the Going-In Notes actually flags: a network reorder where HEARTBEAT(T+1) arrives before ENTRY(T).

Walking it:

1. Main indicator fires ENTRY(T) at the close of bar T. HTTP request dispatched.
2. Main indicator fires HEARTBEAT(T+1) at the close of bar T+1. HTTP request dispatched. The heartbeat's `pos_slot_1_time` contains T (the just-created position's entry bar).
3. The HTTP request for HEARTBEAT(T+1) arrives at the backend first (network reorder, TradingView's alert dispatch latency variance, etc.).
4. The backend's HEARTBEAT handler processes it. It parses `pos_slot_1_time = T`. It looks up position with `bar_close_ms = T` in `fsm_map`. **Not found** — the ENTRY hasn't been processed yet.
5. Per §3.4 and I9, the handler logs `⚠️ [HEARTBEAT] unknown position bar_close_ms=T` and skips this slot. Returns HTTP 200.
6. Then the ENTRY(T) request arrives. The backend processes it normally: writes `signals_v3`, writes `positions`, creates the FSM entry.
7. On bar T+2's heartbeat, the position is in `fsm_map` and the resolver processes it normally.

**Consequence.** The position missed one heartbeat (T+1) because the backend did not know about it yet when the heartbeat arrived. That one missed heartbeat means bar T+1's MAE/MFE data was not captured for this position, and any TP1/SL that would have resolved on bar T+1 was missed. The position picks up from bar T+2 as if bar T+1 never existed for it.

**Is this a correctness problem?** It is a *measurement* gap, not a correctness violation. The position's state is still consistent — `positions` row exists, `signal_id` is correct, subsequent heartbeats resolve correctly. What's lost is one bar of excursion data. For a position that resolves days later, losing one bar of excursion is inside normal measurement noise.

**Is it worth defending against?** The defense would be to buffer HEARTBEATs whose slot lookups fail, retry them after some delay, and re-process if the ENTRY arrives in the meantime. That adds complexity to the HEARTBEAT handler (buffering, retry queue, eviction policy) for a gain of one bar of excursion data in a rare race condition. The spec judges this tradeoff unfavorable and does not require the defense. **§5's TC12 (safe no-op on unknown position) is the committed behavior.** The rare missed heartbeat is logged and observable.

**Pointer to the rule that handles this case:** §3.4 I9 (heartbeat for unknown position is safe no-op), §5.12 TC12 (test case asserts the no-op), §5.9 (resolver has no hidden state that would be corrupted by the missed bar).

### 6.3 Case 2 — TradingView misses a bar's heartbeat tick entirely

**The failure sequence.** Position is open. Heartbeats fire and are received normally for bars T, T+1, T+2. On bar T+3, the main indicator's alertcondition logic determines the HEARTBEAT should fire, but TradingView does not deliver the webhook — possibly due to:
- A TradingView platform hiccup
- A rate limit on the alert
- A brief network outage between TradingView and Railway
- A Railway restart that happened to occur during the alert delivery window
- The alertcondition being disabled and re-enabled by the user during observation

The backend's last-seen heartbeat for this position is T+2. Bar T+3 passes with no heartbeat. Bar T+4's heartbeat arrives normally.

**Walking it against §3 and §5:**

1. Bar T+4 heartbeat arrives at the backend. The HEARTBEAT handler parses `bar_close_ms = T+4` and `pos_slot_1_time = T` (the position's entry bar, still tracked by Pine).
2. Step 3 of the HEARTBEAT handler pseudocode in §3.8 says: "check for heartbeat gaps — is the incoming `bar_close_ms` exactly one bar-interval ahead of the last processed heartbeat's `bar_close_ms`?"
3. For this position, `last_heartbeat_bar_ms = T+2`. The incoming `bar_close_ms = T+4` is **two bar intervals ahead** (30 minutes on the 15M chart). This is a gap of one bar.
4. The gap triggers the §8 outage recovery path. (§8 is written up in Part 5. For §6's purposes, the high-level answer is: §8 detects the gap, decides whether the missing bar is recoverable via Finnhub replay, and either replays the missing bar through the resolver or closes the position defensively as GAP_CLEAN.)

**Is this a correctness problem?** No — it is a measurement gap that the §8 gap recovery explicitly handles. The failure mode is gracefully degraded: either the bar is recovered from Finnhub and the resolver processes it normally, or the position is marked GAP_CLEAN and closed defensively.

**What if the gap is small enough to ignore — e.g., one missed bar and the next bar picks up cleanly?** The spec's position is: *every gap is handled, regardless of size*. A one-bar gap on the 15M chart is 15 minutes of unobserved data; that is enough time for a position to hit a level the backend will never know about. If the backend just resumed processing on bar T+4 without handling the gap, it would be asserting that bar T+3 had no meaningful activity — an assertion it has no basis for making. The §8 handler for one-bar gaps is the same as for larger gaps: attempt replay from Finnhub, fall back to GAP_CLEAN on failure.

**Pointer:** §3.4 step 3 (gap check), §8 (gap recovery mechanism, written in Part 5). §5.12 TC12 is *not* the right answer here — TC12 is for a heartbeat that arrives with an unknown position, which is a different failure mode. Missed-heartbeat is known position, missing bar.

### 6.4 Case 3 — Railway restarts mid-bar and loses in-memory FSM state

**The failure sequence.** Position is open at `bar_close_ms = T`. The backend has processed bars T through T+5 normally; the in-memory `fsm_map` and the on-disk `positions` row both reflect the latest state. At some moment during bar T+6 (before that bar's heartbeat has arrived, or during its processing), Railway restarts the backend process. In-memory state is lost. The SQLite database is intact because of WAL mode + synchronous=NORMAL (verified in current production in §4.1).

**Walking it against §4.9 and §5:**

1. Railway restarts. The backend process starts up.
2. Startup sequence (§4.9) runs before the webhook handler accepts requests:
   - Read all `positions` rows where `state != 3` (partial index `idx_positions_state`).
   - For each row, construct a `PositionState` object in memory.
   - Insert into `fsm_map` keyed by `signal_id`, and into `fsm_map.by_bar_close_ms` keyed by `bar_close_ms`.
   - Log `🔄 REHYDRATE signal_id=X state=Y last_heartbeat=Z`.
3. After rehydration, the backend checks each rehydrated position's `last_heartbeat_bar_ms` against the current wall clock. If the gap between "now" and `last_heartbeat_bar_ms` exceeds one bar interval (accounting for market hours — overnight gaps are expected), §8's gap recovery runs for that position.
4. Webhook handler comes online. First live webhook is served.

**The critical commitment from Part 3 (§5.10 CO1) made this case survivable:** the commit ordering rule says DB commit precedes in-memory update. A crash between DB commit and memory update leaves on-disk state ahead of memory — but memory is discarded on restart anyway and rebuilt from disk. A crash between memory update and DB commit would have left memory ahead of disk, which on restart would mean the rehydrated state is behind what the pre-crash memory believed. **That ordering is forbidden.** The spec's commit ordering guarantees the rehydrated state is always at least as advanced as the pre-crash memory was.

**What if the restart happens during the processing of a heartbeat — halfway through applying a resolver result?** SQLite's transactional context (`with conn:` in §5.10) means a partial transaction is rolled back on crash. Either the transaction committed before the crash (rehydrated state reflects it) or it didn't (rehydrated state is the pre-transaction state). There is no "partially applied" state possible.

**What if the restart happens between the DB commit and the fsm_map deletion for a terminal transition?** The DB commit succeeded — `positions.state == 3`, `trade_outcomes` row written. Memory was about to delete from fsm_map but didn't. On restart, rehydration reads `WHERE state != 3`, so the closed position is not loaded into memory. Clean outcome; the closed position stays closed.

**What if the restart happens between the `positions` update and the `trade_outcomes` insert?** Per CO2, these are in the same transaction. Either both commit or neither. A crash rolls back both. On restart, the rehydrated position is in its pre-terminal state (OPEN or TP1_HIT), and the resolver will reprocess any bars that arrive after the restart. The missing bars between the pre-crash last heartbeat and the post-crash first heartbeat are handled by §8's gap recovery.

**Is this a correctness problem?** No. Rehydration + gap recovery handles it. The commit ordering rules make crash recovery a mechanical process with well-defined outcomes.

**Pointer:** §4.9 (rehydration query and logic), §5.10 (commit ordering CO1–CO5), §5.12 TC19 (rehydration correctness test case), §8 (gap recovery after restart).

### 6.5 Case 4 — Finnhub replay fills a gap but the replayed bar granularity does not match TradingView's

**The failure sequence.** A gap is detected (either from a missed TV heartbeat per §6.3 or from a Railway restart per §6.4). §8's gap recovery invokes Finnhub to fetch the missing bars. Finnhub returns bar data for the requested time range — but the bars' boundaries and `bar_close_ms` values do not line up exactly with the ones TradingView would have produced.

**Why this could happen:**
- Finnhub's 15-minute bars may be aligned to a different base (e.g., minute-of-hour 0/15/30/45 vs. minute 5/20/35/50).
- Finnhub's timestamp convention may be "bar open time" vs. TradingView's "bar close time" — a 15-minute offset depending on how each vendor reports.
- Finnhub's data may be in a different timezone convention (UTC vs ET) requiring correct conversion.
- Finnhub may return a bar every N minutes on a clock that doesn't quite match TV's bar sequence because of vendor-specific handling of missing ticks.

**Walking it:**

The keystone question here is: *when the resolver processes a replayed bar, does the `bar_close_ms` on that bar match the value Pine would have sent?* If yes, the replayed bar looks like a normal heartbeat and the resolver processes it identically. If no, the replayed bar is a different bar than the one the resolver "thinks" it is, and the `last_heartbeat_bar_ms` guard in §5.9 may misfire (either rejecting a real bar as a replay, or processing an already-seen bar as new).

**Spec commitment — the Reconciliation Rule:**

§8's Finnhub replay MUST produce bars whose `bar_close_ms` matches TradingView's convention exactly. The responsibility for the reconciliation lives in the Finnhub replay adapter (in §8), not in the resolver. The adapter:

1. Fetches raw bar data from Finnhub for the gap window.
2. Converts the timestamps to the TradingView convention — `bar_close_ms` is the UTC millisecond timestamp of the *end* of the bar, which corresponds to the instant the bar closes. For a 15-minute bar that ran from 09:30:00 ET to 09:44:59.999 ET on a given date, the TradingView `bar_close_ms` is the UTC-milliseconds value of 09:45:00 ET on that date (close of the bar = start of the next).
3. Validates that the converted `bar_close_ms` values line up to the 15-minute grid the Pine side uses (`bar_close_ms % 900000 == k` where k is the constant offset for this chart's bar boundary).
4. If validation fails, the adapter logs `❌ [FINNHUB_RECONCILE_FAIL]` and does NOT feed the replay to the resolver. The position is closed defensively with GAP_CLEAN instead.

**This commitment is new to the spec as a §6.5 addition.** The rethesis doc §5.3 mentioned Finnhub replay as a mitigation for Railway outage but did not specify the reconciliation semantics. The Going-In Notes §3 flagged this as one of the five keystone cases. The spec's answer: reconciliation is the adapter's job, it is validated before any bar is fed to the resolver, and validation failure means GAP_CLEAN — not "trust the bar and hope for the best."

**Consequence for §8.** The §8 section must specify the Finnhub adapter's reconciliation procedure in detail, including the exact conversion from Finnhub's timestamp convention to TradingView's. §8 owes this specification; §6.5 names it as a requirement.

**Is this a correctness problem?** It would be if the spec did not commit to reconciliation-before-replay. With the commitment in place, the worst case is GAP_CLEAN (a defensive close that matches reality: "the backend could not reliably recover the missing bars"). No case produces corrupted data.

**Pointer:** §8 (owes the Finnhub adapter reconciliation spec), §5.9 (idempotency via `last_heartbeat_bar_ms`), §4.5 (positions table's `last_heartbeat_bar_ms` column), new rule: **Reconciliation Rule** in §6.5.

### 6.6 Case 5 — Two positions opened on different bars both still open, and a single bar's OHLC satisfies TP1 for both

**The failure sequence.** At bar T, a TREND LONG entry fires at entry price 25000 with TP1 25030. At bar T+5, a SQUEEZE LONG entry fires at entry price 25010 with TP1 25040. Both positions are in `fsm_map` and tracked in Pine's slot 1 and slot 2 respectively.

At bar T+8, the bar's range is (25005, 25045, 25000, 25038). Bar T+8's high 25045 exceeds both TP1 levels (25030 and 25040). In a single heartbeat, both positions should transition to TP1_HIT.

**Walking it against §5 and §3:**

1. The heartbeat for bar T+8 arrives at the backend with `bar_close_ms = T+8`, bar OHLC, and `pos_slot_1_time = T`, `pos_slot_2_time = T+5`.
2. The HEARTBEAT handler (§3.8 pseudocode) iterates slots.
3. Slot 1: lookup position with `bar_close_ms = T` in `fsm_map.by_bar_close_ms` → finds position A (TREND LONG, signal_id=1). Call `resolver.step(position_A, bar, ...)`. Returns `TP1_HIT` transition. `apply_resolver_result` writes the update to `positions` where `signal_id=1` and updates `fsm_map[1]`.
4. Slot 2: lookup position with `bar_close_ms = T+5` in `fsm_map.by_bar_close_ms` → finds position B (SQUEEZE LONG, signal_id=2). Call `resolver.step(position_B, bar, ...)`. Returns `TP1_HIT` transition. `apply_resolver_result` writes the update to `positions` where `signal_id=2` and updates `fsm_map[2]`.
5. Each position is updated independently. Two separate transactions, two separate `positions` UPDATEs, two separate in-memory updates. No cross-contamination because each resolver call operates on a different `position` object, and each write-through targets a different `signal_id` primary key.
6. Handler returns HTTP 200.

**Is this a correctness problem?** No, it is **exactly the case Finding #1 was about**, and Option C retires the class of bug by construction. Under the Monitor architecture, both TP1 events would have emitted `TRADE_OUTCOME` webhooks that `find_parent_by_lookback` would have had to disambiguate by signal_type + direction + 2-hour window — and because both positions had the same signal_type ("TREND" for one and "SQUEEZE" for the other in this case; Finding #1's worse case was two TRENDs), the disambiguation could fail.

Under Option C, disambiguation is mechanical: each position is keyed by its unique `bar_close_ms` in the slot lookup, and by its unique `signal_id` in the resolver call. There is nothing to disambiguate fuzzily. Each position gets its own resolver call with its own state, and the results are written to distinct rows by primary key.

**What if two concurrent positions share a bar_close_ms?** This is the failure mode the §4.2 UNIQUE constraint on `signals_v3.bar_close_ms` is designed to catch. If two entries fire on the same bar (e.g., a TREND and a SQUEEZE with independent trackers), the UNIQUE constraint rejects the second INSERT at the transaction boundary. The spec flagged this in §4.2 as an open item for §11 — the commit is: **UNIQUE for now, revisit if observation data shows cross-tracker same-bar entries happen.**

If observation shows cross-tracker same-bar entries are real, the fix is to relax the UNIQUE index to a compound index on `(bar_close_ms, signal_type, direction)` and change the slot-lookup to match by the same compound key. The HEARTBEAT payload's slot fields would need to carry signal_type and direction per slot too, adding more plot slots — or more likely, the slot lookup takes the `bar_close_ms` and iterates candidate positions in `fsm_map.by_bar_close_ms[bar_close_ms]` as a list instead of a scalar. This is a §11 open item, not a §6 one.

**Pointer:** §5.12 TC15 (concurrent positions test case), §3.4 (slot-by-slot iteration), §5.10 (per-position transaction isolation), §4.2 UNIQUE constraint + §11 open item for the cross-tracker case.

### 6.7 Summary — is the FSM complete under all five cases?

| Case | Failure mode | Handled by | Resolution |
|---|---|---|---|
| 6.2 | ENTRY and HEARTBEAT collide or reorder | I9 (safe no-op), TC12, §7 ENTRY-priority on shared alertcondition | Yes — measurement gap of one bar, no correctness violation |
| 6.3 | TV misses a heartbeat | §3.8 gap check, §8 gap recovery | Yes — replay or GAP_CLEAN |
| 6.4 | Railway restart mid-position | §4.9 rehydration, CO1–CO5 commit ordering, TC19 | Yes — on-disk state leads memory, crash always lands on a clean state |
| 6.5 | Finnhub replay granularity mismatch | §8 Finnhub adapter **Reconciliation Rule** (new in §6.5) | Yes — reconciliation validates before replay, validation failure → GAP_CLEAN |
| 6.6 | Single bar resolves multiple concurrent positions | TC15, per-position transactions, §4.2 UNIQUE constraint + §11 open item | Yes for distinct bar_close_ms; flagged open for same-bar cross-tracker |

Four cases are handled by rules already written in Parts 2 and 3. One case (6.5) adds a new rule — the Reconciliation Rule — which lives in §6.5 and owes a detailed implementation specification to §8. One case (6.6) points at a flagged open item in §11.

**Net commitment added to the spec by §6:** the Reconciliation Rule in §6.5. The spec's total test-case count grows by one — a test case asserting that a replayed bar whose `bar_close_ms` does not align to the 15-minute grid is rejected at the adapter boundary with `FINNHUB_RECONCILE_FAIL` and the position is closed GAP_CLEAN rather than fed to the resolver. Call this **TC24**, to be added to §5.12 at the end of the Part 3 spec once Part 4 is reviewed and the Reconciliation Rule is accepted.

---

## 7. HEARTBEAT PLOT IN THE MAIN INDICATOR

### 7.1 Overview

This is the one section of the spec where Pine Script changes. Every other change under Option C is in the Python backend. §7 commits to the exact Pine-side additions and verifies them against the observation discipline rule from §1.4: *does this change what Mona thinks, or change what we can see about what she thinks?*

**The test applied.** The heartbeat plot adds bar-close observability for positions the backend has already been told about via ENTRY. It does not add new cognition. It does not decide when an entry happens. It does not participate in the Reputation Engine in any way. The existing entry-firing logic, filter conditions, StochRSI, volume floor, ADX threshold, session window, squeeze detection, ghost tracking, and follow-through evaluation are all untouched. The heartbeat is pure measurement infrastructure. It ships on merit under the discipline rule.

**Two Pine constraints that shape the design.**

*Constraint 1: Alerts are one-way.* Pine can fire an alertcondition to emit a webhook. Pine cannot receive anything back. The backend cannot tell Pine "position X has closed." This means Pine's view of "which positions are still open" must be maintained by Pine itself using its own local state, based on observable market data — not by synchronization with the backend.

*Constraint 2: `plot()` only accepts `series float`.* Pine cannot plot strings. Any data that needs to ride on an alertcondition message via the `{{plot_N}}` template must be a float. Integer millisecond timestamps are passed as floats (Pine's float has enough precision for 13-digit values) and parsed back to int on the backend. Multiple values cannot be packed into a single plot as a string — each value needs its own plot slot.

These two constraints drive §7's commitments: Pine tracks slots locally using a fixed-size array of `int` bar_close_ms values, maintains the array with simple local heuristics (not by echoing the backend), and encodes each slot as a separate numeric plot.

### 7.2 Plot limit verification — precondition for §7

Before any Pine code is written, the 42-plot addressability assumption from §3.6 has to be verified. The current v3 script uses plot indices 0–31 and references them in the alertcondition message via `{{plot_0}}` through `{{plot_31}}`. §3.6 proposed adding plots 32–41 for Option C.

**Verification task:**

1. Create a scratch Pine v5 script that declares 42 plots (indices 0–41), each `plot(someFloatExpr, "test_N", display=display.none)`.
2. Create an alertcondition in the same script with a message template that references `{{plot_0}}` through `{{plot_41}}`.
3. Attempt to compile and save the script in TradingView.
4. If it compiles and saves: plot indices 0–41 are addressable. Proceed with §7.3.
5. If it fails to compile or save, read the error message. Likely causes:
   - Hard plot limit lower than 42 — need to consolidate or use alternative encoding.
   - Alertcondition message length limit — need to shorten the template.
   - `{{plot_N}}` syntax limit on N — need to use named plots via `{{plot("name_N")}}` syntax.
6. For each failure mode, the fallback path is documented but not pre-designed. If the verification fails, §7 is paused and the fallback is designed before code proceeds.

**Estimated time: 5–10 minutes.** This is the first hands-on-keyboard task once the spec is reviewed and approved. It is not "Pine development" — it is a precondition check that either unblocks §7 or reshapes it.

**Spec commitment:** §7.3 and onward assume the verification succeeds. If it does not, the entire heartbeat encoding is reshaped around whatever limit was found, and the spec's §3.6, §3.7, and §7 sections are revised before any real Pine changes are made. The revision is cheap because nothing beyond §7 depends on the specific plot indices — they are just names.

### 7.3 Local state added to the main indicator

Pine maintains a small amount of new state to support the heartbeat. None of it is in the Reputation Engine's scope — all of it is position-tracking metadata.

**New variables:**

```pine
// Slot array: up to 4 concurrent position entry times.
// Each element is a bar_close_ms value (0 = empty slot).
var int posSlot1Time = 0
var int posSlot2Time = 0
var int posSlot3Time = 0
var int posSlot4Time = 0

// Per-slot entry levels (captured at ENTRY time) — used for local closure heuristic.
// These let Pine know when to release a slot without the backend telling it.
var float posSlot1Sl  = 0.0
var float posSlot1Tp2 = 0.0
var int   posSlot1Dir = 0   // 1=LONG, 2=SHORT

var float posSlot2Sl  = 0.0
var float posSlot2Tp2 = 0.0
var int   posSlot2Dir = 0

var float posSlot3Sl  = 0.0
var float posSlot3Tp2 = 0.0
var int   posSlot3Dir = 0

var float posSlot4Sl  = 0.0
var float posSlot4Tp2 = 0.0
var int   posSlot4Dir = 0
```

**Why per-slot `sl`, `tp2`, and `dir`?** For Pine's local closure heuristic (§7.5) to release slots without round-tripping through the backend, Pine needs to know the entry-level parameters that define "position is closed." It needs the original SL (to detect full losses), TP2 (to detect full wins — with the understanding that post-TP1 BE stops are NOT detected, see §7.5), and direction (to know which way to compare). These are stamped at ENTRY time from the same variables the entry logic computes, so no new computation is added.

**This is not state duplication in the Finding #0 sense.** The Reputation Engine's decision authority (`repState`, `evalPending`, `trendTrk`, `sqzTrk`, etc.) is unchanged. Pine is not re-deciding whether an entry should fire; the ENTRY path is already running through the Reputation Engine as it does today, and the slot state is populated *after* the Reputation Engine has authorized the entry. The slot state is a measurement side-effect of a decision the Reputation Engine already made, not a second decider.

The cognition/measurement boundary from §1.4 puts it cleanly: the Reputation Engine is cognition and is singular, per principle. The slot array is measurement and carries no decision-making authority. Pine's closure heuristic is also measurement — it reads market data, it does not redecide whether positions exist.

**Memory cost.** Four int slots + twelve float/int per-slot fields = 16 `var` declarations, each a handful of bytes. Trivial by Pine standards.

### 7.4 Slot addition on ENTRY

When an ENTRY fires, Pine finds the lowest-indexed empty slot and fills it.

```pine
// Pseudocode — final form in §7 implementation phase.

if fireTrendAlert or fireSqzAlert
    entryMs  = time_close
    entrySl  = sigSL
    entryTp2 = sigTP2
    entryDir = sigDir  // 1=LONG, 2=SHORT

    // Find lowest empty slot.
    if posSlot1Time == 0
        posSlot1Time := entryMs
        posSlot1Sl := entrySl
        posSlot1Tp2 := entryTp2
        posSlot1Dir := entryDir
    else if posSlot2Time == 0
        posSlot2Time := entryMs
        posSlot2Sl := entrySl
        posSlot2Tp2 := entryTp2
        posSlot2Dir := entryDir
    else if posSlot3Time == 0
        posSlot3Time := entryMs
        posSlot3Sl := entrySl
        posSlot3Tp2 := entryTp2
        posSlot3Dir := entryDir
    else if posSlot4Time == 0
        posSlot4Time := entryMs
        posSlot4Sl := entrySl
        posSlot4Tp2 := entryTp2
        posSlot4Dir := entryDir
    else
        // Slot overflow — all 4 slots occupied.
        // Pine fires the ENTRY alertcondition anyway; the backend still records it.
        // But no heartbeats will flow for this position until a slot frees.
        // Log via a separate bgcolor/label for visibility (§7.5.2).
        // The backend's staleness check (§8) will catch an unmonitored position.
```

**When multiple entries fire on the same bar.** The current main indicator has `fireTrendAlert` and `fireSqzAlert` as separate flags that can both be true on the same bar (trend and squeeze firing simultaneously). The slot-addition logic above handles this naturally — both entries are processed in sequence, each finding an empty slot. However, only one alertcondition fires per bar, so the webhook carries one ENTRY event. The second entry is lost to the backend.

**This is a pre-existing constraint, not an Option C regression.** The single-alertcondition bottleneck in the current Pine script already has this limitation: if two entries fire on the same bar, one is delivered and the other is not. The Pre-Observation Report flagged this as an accepted risk. Option C does not change it. The accepted risk persists; it is an open item for a future spec (separate alertconditions per signal type) but not in scope here.

**Partial handling.** For slot-array purposes, Pine's local state records both entries in slots even though only one webhook was delivered. On the next bar, the heartbeat payload will carry both slot values. The backend will look up both, find one (the one whose ENTRY was delivered), and log the other as `⚠️ [HEARTBEAT] unknown position` per I9. This is the same safe no-op as the §6.2 out-of-order case.

### 7.5 Slot release — the local closure heuristic

This is the hardest design decision in §7 and the one that earns its subsection. Pine cannot hear from the backend, so Pine must decide locally when to release a slot. The decision is about tradeoffs: too eager and Pine stops heartbeating a position the backend still needs; too conservative and Pine wastes heartbeats on already-closed positions.

**The closure heuristic — spec commitment:**

**A slot is released (set to 0) at the end of any bar on which any of the following conditions is true:**

1. **Original SL breached** — the bar's range crossed the slot's stored `sl`. For a LONG slot: `bar_low <= posSlotNSl`. For a SHORT slot: `bar_high >= posSlotNSl`.
2. **TP2 breached** — the bar's range crossed the slot's stored `tp2`. For a LONG slot: `bar_high >= posSlotNTp2`. For a SHORT slot: `bar_low <= posSlotNTp2`.
3. **Session end** — the bar's `inSession` transitions from true to false (the first bar after the Pine session window ends — currently 15:45 ET).

**What the heuristic does NOT detect:** BE_STOP closures (post-TP1 stop-out at entry_price) and EOD_TIMEOUT closures (backend's 16:00 cutoff if later than Pine's 15:45 session end). A position that hits TP1 and then gets BE-stopped in the backend will continue occupying a Pine slot and firing heartbeats until it either hits original SL (won't — it's already past entry, SL is further away), hits TP2 (the backend already closed it at BE, so TP2 would have been a late win that never happened), or the session ends. In practice: until session end.

**Consequence: some wasted heartbeats.** A position that closes at BE stop mid-session produces wasted heartbeats from the moment of the BE stop until session end. Each wasted heartbeat is a safe no-op at the backend (§5.12 TC12): the backend looks up `bar_close_ms` in `fsm_map.by_bar_close_ms`, finds nothing (because `apply_resolver_result` removed the closed position from the map), logs `⚠️ [HEARTBEAT] unknown position`, moves on.

**How many wasted heartbeats in a bad case?** A TREND LONG hits TP1 at bar T+2, then hits BE stop at bar T+4. The backend closes it at T+4. Pine's local view still thinks the slot is open. If the session close is at bar T+25 (about 5.5 hours later on the 15M chart), Pine fires ~20 wasted heartbeats for this slot. Each is a safe no-op log line on the backend. The cost is: one log line per position per bar post-closure, bounded by the remaining session.

**Is this acceptable?** Yes, for three reasons:

1. **It is observable.** Every wasted heartbeat is a log line with `⚠️ [HEARTBEAT] unknown position bar_close_ms=X`. Observation will tell us whether the rate is annoying or tolerable. If the rate is high, §7.5.2 addresses the escalation.

2. **It does not break correctness.** The backend's `fsm_map` is the source of truth for "which positions are actually open." Pine's local tracker is a best-effort hint about "which positions to send heartbeats for." The two don't need to agree perfectly; the backend has the final word on what's really open.

3. **The alternative is worse.** To avoid wasted heartbeats, Pine would have to detect TP1_HIT + BE stop transitions locally, which means tracking TP1 crossings AND moving the local stop to entry_price AND re-testing the stop on each bar — i.e., duplicating the entire resolver FSM in Pine. That is precisely the two-cognitions failure mode Option C exists to eliminate. Extra log lines are cheaper than two brains.

**§7.5.2 — What if observation shows wasted heartbeats are too noisy?**

If the log noise in `#system-log` from `⚠️ [HEARTBEAT] unknown position` lines becomes operationally painful, the escalation path is *not* to add closure detection in Pine. The escalation path is to quiet the backend logging:

- **Option alpha:** Demote the `⚠️ [HEARTBEAT] unknown position` log from `#system-log` to a separate `#mona-diagnostic` channel used only for low-severity observability signals. Josh sees the interesting stuff; the spam is isolated.
- **Option beta:** Rate-limit the warning to once per unknown-position-bar_close_ms value per session. After the first "unknown position X" log, subsequent heartbeats for X are silently dropped until the next session.
- **Option gamma:** Track "slots the backend has closed" in backend state (a set of `bar_close_ms` values for positions that were in the map but are now closed), and when a heartbeat arrives for a slot in that set, log nothing (it is the expected post-close steady state) and return 200 cleanly. This adds a small amount of backend state but fully eliminates the noise.

All three options keep the Pine side simple and handle the noise on the backend side. The spec does not pre-commit to any of them — they are deferred until observation tells us whether noise is a real problem.

**§7.5.3 — Slot release on session end.**

The `inSession` variable in the current Pine script transitions from `true` to `false` at the first bar whose `time` falls outside `"0930-1545" America/New_York`. On that transition, all four slots are cleared:

```pine
sessionEnded = inSession[1] and not inSession

if sessionEnded
    posSlot1Time := 0
    posSlot1Sl := 0.0
    posSlot1Tp2 := 0.0
    posSlot1Dir := 0
    // ... same for slots 2, 3, 4
```

This ensures that Pine's slot state resets cleanly at the end of every trading session and no state carries overnight into the next session. The backend's EOD rule (16:00 ET per §5.8 default) applies to any positions that were still active after Pine's 15:45 cutoff — the backend sees them in its own `fsm_map`, processes the EOD heartbeat at 16:00, and closes them independently of Pine. Pine's local slot clearing is synchronized with Pine's session definition; the backend's EOD is synchronized with the backend's resolution definition. They are allowed to differ by 15 minutes, and the difference is handled by the backend's own state.

**Note on the 15-minute gap.** Between 15:45 (Pine session end, Pine clears slots) and 16:00 (backend EOD, backend closes any still-open positions), Pine is not sending heartbeats for those positions. The backend is relying on its own wall clock to detect the EOD moment. Concretely: the backend's EOD handler runs on a timer or on the first heartbeat arriving after 16:00 (whichever comes first). For still-open positions at 16:00 with no heartbeat in the last 15 minutes, the EOD rule fires with the last known bar's close as the exit price. §8 owes the exact mechanism.

### 7.6 Heartbeat alertcondition — shared or separate?

The existing main indicator has one alertcondition (`anySignalFired` at line 670 in `mona_v3_0.txt`) that covers both ENTRY (sig_status=1) and EVAL (sig_status=2) events. Under Option C the HEARTBEAT event (sig_status=3) needs to ride on some alertcondition. Two options:

**Option A — Share the existing alertcondition.** The existing `anySignalFired` condition becomes true on any of ENTRY, EVAL, or HEARTBEAT fire. The shared message template (§3.7) carries fields for all three events; the `sig_status` discriminator tells the backend which event it is. Collisions (ENTRY and HEARTBEAT on the same bar) are resolved by Pine-side priority: if `fireTrendAlert` or `fireSqzAlert` is true, HEARTBEAT suppresses itself for that bar per §6.2.

**Option B — Add a separate alertcondition for HEARTBEAT.** Two alertcondition lines in the Pine script: the existing one for ENTRY/EVAL, a new one for HEARTBEAT. Each gets its own TradingView alert with its own webhook URL (or the same URL with different message payloads). ENTRY and HEARTBEAT can fire on the same bar from different alertconditions and both get delivered.

**Tradeoffs:**

*Option A pros:*
- One alertcondition to configure in TradingView — Josh only sets up one alert.
- No new user-facing TradingView workflow.
- The single-alertcondition bottleneck is an accepted risk already documented in the Pre-Observation Report — not making it worse.

*Option A cons:*
- ENTRY + HEARTBEAT same-bar collision requires the priority rule (ENTRY wins, HEARTBEAT suppresses). Results in a one-bar measurement gap for positions on their entry bar, which the entry-bar-exclusion rule (PF4) already makes irrelevant for the newly-entering position but drops heartbeats for other open positions on that bar.
- Payload is inflated — every event carries fields for all three event types in the shared template, leading to wasted bytes in ENTRY and EVAL payloads (bar OHLC and slot values that are zero). Not a correctness issue but it's ugly.

*Option B pros:*
- No same-bar collision between ENTRY and HEARTBEAT — both fire cleanly.
- Each event type can have its own purpose-built message template, smaller and cleaner.
- Sets the architectural stage for eventually splitting ENTRY and EVAL into separate alertconditions too, which is the long-term fix for the accepted-risk bottleneck.

*Option B cons:*
- Josh has to create a second TradingView alert with a second webhook URL setup, doubling the user-facing configuration burden.
- Each alert consumes a TradingView alert quota (on Josh's TV plan — need to verify the plan's alert count).
- A new alertcondition during observation discipline needs a defense: is this still a measurement change (ships on merit) or does adding an alertcondition count as a change that needs observation data to justify? The discipline rule applies: **it does not change what Mona thinks, only what we can see about what she thinks, so it ships on merit**. The spec stands on the rule, but Strategist should have eyes on it.

**Spec commitment: Option B — separate alertcondition for HEARTBEAT.**

Rationale, in descending weight:
1. Eliminating the same-bar collision between ENTRY and HEARTBEAT removes a failure case from §6.2 (the measurement gap on ENTRY bars for other open positions). One less case to think about and one less log line category to watch for is worth the user-facing setup burden.
2. The architectural direction for The Mona is toward more-separated alertconditions, not fewer. The pre-observation accepted risk about the single-alertcondition bottleneck specifically anticipates "separate alertconditions planned pre-production." Option C is an opportunity to take one step in that direction, cheaply.
3. The second alertcondition is purely measurement. It does not change signal generation. The discipline rule applies cleanly.
4. The added user burden is a one-time setup cost (Josh creates one additional alert in TradingView after market hours). The wasted-payload-bytes cost of Option A is recurring.

**The second alertcondition's message template is smaller than the main one** — it only needs `sig_status`, `bar_close_ms`, bar OHLC, and slot values. Plots 0–31 (the signal/filter state fields) are not referenced in the heartbeat message. This makes the heartbeat payload compact and easy for the backend to distinguish.

```pine
// New alertcondition for HEARTBEAT only — added alongside the existing anySignalFired.
fireHeartbeat = (posSlot1Time != 0 or posSlot2Time != 0 or posSlot3Time != 0 or posSlot4Time != 0) and barstate.isconfirmed and not (fireTrendAlert or fireSqzAlert)

alertcondition(fireHeartbeat, title="MNQ Heartbeat", message='{
  "sig_status": 3,
  "bar_close_ms": {{plot_32}},
  "bar_open": {{plot_34}},
  "bar_high": {{plot_35}},
  "bar_low": {{plot_36}},
  "bar_close": {{plot_37}},
  "pos_slot_1_time": {{plot_38}},
  "pos_slot_2_time": {{plot_39}},
  "pos_slot_3_time": {{plot_40}},
  "pos_slot_4_time": {{plot_41}},
  "ticker": "MNQ",
  "version": "3.0"
}')
```

**Note the `not (fireTrendAlert or fireSqzAlert)` guard.** Even with separate alertconditions, ENTRY takes priority on a same-bar basis — on a bar where both an entry and a heartbeat would fire, only the entry fires. This preserves the §6.2 analysis and avoids double-delivery. The heartbeat resumes next bar. The entry-bar-exclusion rule (PF4, TC9) means the newly-entered position would not be resolved on its entry bar anyway.

**Does this create a new same-bar gap?** Yes — on any bar where an ENTRY fires and other positions are already open, the heartbeat for those already-open positions is suppressed by this guard. Those positions miss one bar of data. This is the same measurement gap as Option A's same-bar collision handling. The difference is: under Option B this is only a gap for *already-open positions during an ENTRY bar*, not a structural collision between alertconditions.

The gap is acceptable by the same reasoning as §6.2 — a one-bar measurement gap in a rare sequence (ENTRY fires while other positions are open) is inside normal measurement noise for a position that will resolve over many bars.

### 7.7 Pine pseudocode — full flow

Putting §7.3–§7.6 together into a single illustrative block. This is not final code — §7 implementation is where the final Pine script is written. The block is for spec readers to see how the parts fit.

```pine
// ============ HEARTBEAT PLOT SUPPORT (new in v3.0 Option C) ============

// --- §7.3 state ---
var int posSlot1Time = 0, var float posSlot1Sl = 0.0, var float posSlot1Tp2 = 0.0, var int posSlot1Dir = 0
var int posSlot2Time = 0, var float posSlot2Sl = 0.0, var float posSlot2Tp2 = 0.0, var int posSlot2Dir = 0
var int posSlot3Time = 0, var float posSlot3Sl = 0.0, var float posSlot3Tp2 = 0.0, var int posSlot3Dir = 0
var int posSlot4Time = 0, var float posSlot4Sl = 0.0, var float posSlot4Tp2 = 0.0, var int posSlot4Dir = 0

// --- §7.4 slot addition on ENTRY ---
// (Runs after Reputation Engine authorizes the entry and fireTrendAlert / fireSqzAlert is set.)
if fireTrendAlert or fireSqzAlert
    nowMs  = time_close
    nowSl  = sigSL
    nowTp2 = sigTP2
    nowDir = sigDir

    if posSlot1Time == 0
        posSlot1Time := nowMs
        posSlot1Sl   := nowSl
        posSlot1Tp2  := nowTp2
        posSlot1Dir  := nowDir
    else if posSlot2Time == 0
        // ... similarly
    else if posSlot3Time == 0
        // ...
    else if posSlot4Time == 0
        // ...
    // else: overflow (handled via bgcolor label — §7.5.2)

// --- §7.5 slot release — closure heuristic ---
releaseSlot1 = posSlot1Time != 0 and (
    (posSlot1Dir == 1 and (low <= posSlot1Sl or high >= posSlot1Tp2)) or
    (posSlot1Dir == 2 and (high >= posSlot1Sl or low <= posSlot1Tp2))
)
if releaseSlot1
    posSlot1Time := 0
    posSlot1Sl := 0.0
    posSlot1Tp2 := 0.0
    posSlot1Dir := 0
// ... similarly for slots 2, 3, 4

// --- §7.5.3 session end clears all slots ---
sessionEnded = inSession[1] and not inSession
if sessionEnded
    posSlot1Time := 0, posSlot1Sl := 0.0, posSlot1Tp2 := 0.0, posSlot1Dir := 0
    posSlot2Time := 0, posSlot2Sl := 0.0, posSlot2Tp2 := 0.0, posSlot2Dir := 0
    posSlot3Time := 0, posSlot3Sl := 0.0, posSlot3Tp2 := 0.0, posSlot3Dir := 0
    posSlot4Time := 0, posSlot4Sl := 0.0, posSlot4Tp2 := 0.0, posSlot4Dir := 0

// --- §3.6 new plots ---
plot(nz(time_close, 0),     "bar_close_ms",       display=display.none)  // 32
plot(nz(parentBarCloseMs,0),"parent_bar_close_ms",display=display.none)  // 33
plot(nz(open, 0),           "bar_open",           display=display.none)  // 34
plot(nz(high, 0),           "bar_high",           display=display.none)  // 35
plot(nz(low, 0),            "bar_low",            display=display.none)  // 36
plot(nz(close, 0),          "bar_close",          display=display.none)  // 37
plot(posSlot1Time,          "pos_slot_1_time",    display=display.none)  // 38
plot(posSlot2Time,          "pos_slot_2_time",    display=display.none)  // 39
plot(posSlot3Time,          "pos_slot_3_time",    display=display.none)  // 40
plot(posSlot4Time,          "pos_slot_4_time",    display=display.none)  // 41

// --- §7.6 new alertcondition for heartbeat ---
hasOpenSlot = posSlot1Time != 0 or posSlot2Time != 0 or posSlot3Time != 0 or posSlot4Time != 0
fireHeartbeat = hasOpenSlot and barstate.isconfirmed and not (fireTrendAlert or fireSqzAlert)

alertcondition(fireHeartbeat, title="MNQ Heartbeat", message='{
  "sig_status": 3,
  "bar_close_ms": {{plot_32}},
  "bar_open": {{plot_34}},
  "bar_high": {{plot_35}},
  "bar_low": {{plot_36}},
  "bar_close": {{plot_37}},
  "pos_slot_1_time": {{plot_38}},
  "pos_slot_2_time": {{plot_39}},
  "pos_slot_3_time": {{plot_40}},
  "pos_slot_4_time": {{plot_41}},
  "ticker": "MNQ",
  "version": "3.0"
}')
```

**Total lines added to the main indicator:** approximately 60, broken down as ~16 var declarations, ~20 slot-addition logic, ~12 slot-release logic (closure heuristic + session end), ~10 new plots, ~2 new alertcondition. This is consistent with the rethesis §3.4 estimate of "50–60 lines added" for Option C's Pine touch.

**Lines removed from the main indicator:** zero. Option C's Pine changes are purely additive. No existing signal logic, filter condition, or Reputation Engine line is modified.

### 7.8 What Pine does NOT do under Option C

Stated for absolute clarity because §1.4's principle is the test that keeps §7 honest:

- **Pine does NOT track TP1 crossings for its slot closure.** TP1 is the backend resolver's job. Pine's closure heuristic only tests original SL and TP2.
- **Pine does NOT track BE stops.** After TP1, the backend moves effective_sl to entry_price; Pine has no knowledge of this transition.
- **Pine does NOT compute MAE or MFE.** These are resolver responsibilities.
- **Pine does NOT decide when a position is "really" closed.** Pine's closure heuristic is a local approximation for "when to stop heartbeating for a slot" — the backend has the final word on what's actually closed.
- **Pine does NOT second-guess the Reputation Engine.** Slot addition happens after `fireTrendAlert`/`fireSqzAlert` is set; Pine is recording a decision that was already made, not participating in making it.
- **Pine does NOT maintain a second positions table.** There is one authoritative positions store (the backend's SQLite `positions` table). Pine's slot array is transient, per-session, and discarded on session end.

Every one of these "does NOT"s is a line in the §1.4 principle — cognition is singular, measurement can be distributed, state can be replicated. Pine's slot array is measurement and transient per-session state. The cognition stays in the backend resolver plus the main indicator's Reputation Engine, neither of which Pine's §7 additions touch.

---

## End of Part 4

§6 closed with four cases handled by existing spec rules and one case (Finnhub reconciliation) adding a new rule that §8 in Part 5 owes an implementation specification for. §7 closed with the commitments: 10 new plots, separate heartbeat alertcondition (Option B), closure heuristic using original SL / TP2 / session end only, ~60 lines of additive Pine code, no lines removed, no cognition changes.

Items still owed to Part 5:

- **§8 (Railway outage recovery + Finnhub reconciliation):** the staleness check, the replay adapter, the reconciliation rule from §6.5 written up as concrete behavior, the GAP_CLEAN exit path, the 16:00 EOD timer mechanism for positions whose heartbeat stream ended before EOD.
- **§9 (Reputation Engine integration — boundary confirmation):** short section, one page. Final verification that the Reputation Engine is untouched.
- **§10 (migration and rollback):** how we get from `PreObFinal.py` + `mona_v2_1.txt` to Option C, and how we get back if it goes wrong. Including the ALTER TABLE migrations from §4.
- **§11 (open items):** the full list of things needing Josh and Strategist sign-off before code. So far: (a) EOD cutoff 15:45 vs 16:00 (§5.8), (b) pessimistic fill convention (§5.6), (c) N=4 slot count (§3.4), (d) UNIQUE constraint on bar_close_ms vs compound key (§4.2, §6.6), (e) heartbeat shared vs separate alertcondition (§7.6, tentatively resolved to Option B but explicitly up for review), (f) TC24 added to the resolver test suite for the Reconciliation Rule (§6.5), (g) §7.2 plot limit verification task (precondition).
- **§12 (what dies, what survives):** the retirement and preservation ledger. Everything retired by Option C and everything explicitly untouched, restated as the authoritative list.


**Author:** Dev Trader (Senior Quant Dev)
**Date:** April 13, 2026 (Monday)
**Status:** DRAFT — awaiting Strategist review after all five parts land
**Continues:** `MONA_v3_OptionC_Spec_Part4.md`
**Closes:** the Option C architectural specification for The Mona v3.0

---

## 8. RAILWAY OUTAGE RECOVERY

### 8.1 Overview and the threat model

The architectural tradeoff §5.3 of the rethesis doc identified is concrete: under Option A/B, position-state tracking runs on TradingView's infrastructure, and a Railway outage means the backend catches up from a backlog when it comes back. Under Option C, the backend IS the position state, and a Railway outage means any bar that closes during the outage is a bar the resolver never saw. If that bar would have resolved a position — hit TP2, hit the BE stop, hit SL, triggered EOD — the backend comes back from the outage with an open position that is no longer really open.

§8 defines the mechanism by which that failure is handled: detection, recovery via replay, and defensive closure when recovery isn't possible. The goal is not to make outages impossible — that's not in the backend's power — but to make outages *survivable with well-defined semantics*. Every bar that could have been missed is either recovered or explicitly marked as missed. The `trade_outcomes` table never contains rows for positions that were resolved on bars the backend silently missed.

**Three outage patterns the spec handles:**

1. **Heartbeat gap** (§8.3) — the backend receives heartbeat(T), then the next arriving heartbeat carries a `bar_close_ms` of T+2 or later. One or more bars passed without a heartbeat arriving. Cause can be TradingView delivery hiccup, Railway brief outage, network reorder, etc.
2. **Full restart with open positions** (§8.4) — Railway restarts the process. In-memory FSM state is lost. Rehydration from `positions` runs (§4.9), and any rehydrated position's `last_heartbeat_bar_ms` may be more than one bar behind wall clock.
3. **Silent position** (§8.5) — a position exists in `fsm_map` but has received zero heartbeats in a time window exceeding the maximum plausible. This is the staleness check from rethesis §5.4.

All three patterns route into the same recovery mechanism (§8.6), which either replays the missing window from Finnhub or closes the position defensively as `GAP_CLEAN` (§8.8).

### 8.2 The recovery budget — how many bars back can we look

The Finnhub replay path is not unbounded. There is a maximum window the spec commits to replaying before giving up and closing defensively:

**Spec commitment: `MAX_REPLAY_BARS = 16` bars (4 hours on the 15-minute chart).**

Rationale, in descending order of weight:

1. **Positions on 15M MNQ typically resolve within 1–4 hours.** A 4-hour replay window covers the normal lifetime of a position end-to-end. If a position has gone more than 4 hours without a heartbeat, either the outage was very long (Railway down for hours during market hours) or something else is wrong, and a defensive close is more honest than a speculative replay.

2. **Finnhub's free tier imposes practical limits on request volume.** A single replay of 16 bars is one or a small number of API calls. Routine replay at that scale is sustainable. Replay at 100-bar scale would start to hit quotas and would also take long enough that the FSM is blocked on a slow external call.

3. **Longer replays compound the reconciliation risk from §6.5.** Every bar that gets replayed is a bar that has to reconcile to TradingView's grid convention. If reconciliation has a subtle bug, a 4-bar replay exposes 4 bars of risk; a 100-bar replay exposes 100 bars.

4. **The worst-case defensive close is bounded.** If Josh's single funded account has at most 3 contracts per position and the typical TP2 distance is 50 points (=$250 on MNQ per contract = $750 per position for the runner + $300 × 2 for the TP1 contracts already taken), a GAP_CLEAN at worst disposes of the runner. The financial stake in a single defensive close is small; the budget constraint says "recover what's cheap to recover, give up on what's not."

**The 16-bar number is a spec commitment but not a sacred one.** If observation shows that outages longer than 16 bars happen regularly and defensive closes are expensive, raising the cap is a one-line change. The spec's job is to pick a reasonable default that covers the common case; data drives any revision.

### 8.3 Heartbeat gap detection

**Detection rule.** When a HEARTBEAT arrives, the backend computes the gap against the position's `last_heartbeat_bar_ms`:

```
incoming_bar_ms = payload.bar_close_ms
expected_bar_ms = position.last_heartbeat_bar_ms + BAR_INTERVAL_MS    # 900000 for 15M
gap_bars = (incoming_bar_ms - expected_bar_ms) / BAR_INTERVAL_MS

if gap_bars == 0:
    # Normal case — incoming heartbeat is exactly one bar after last.
    proceed with resolver.step(position, bar)

elif gap_bars < 0:
    # Replay or duplicate — per I11, skip via the last_heartbeat_bar_ms guard.
    log_replay(position, incoming_bar_ms)
    return

elif gap_bars >= 1:
    # Missing one or more bars. Enter recovery.
    invoke_gap_recovery(position, last_heartbeat_bar_ms, incoming_bar_ms)
    # Recovery either replays and then returns to normal processing,
    # or defensively closes the position.
```

**Market hours awareness.** A "gap" between the last bar of one trading day and the first bar of the next is expected and is not a failure. The gap detector checks whether the wall-clock time between `last_heartbeat_bar_ms` and `incoming_bar_ms` spans a session boundary; if so, the overnight interval is not counted as missing bars. This check is mechanical — the backend knows the session window (9:30 ET to 16:00 ET for MNQ RTH) and subtracts overnight hours from the gap computation.

**Consequence for positions held overnight.** Under normal Mona operation, positions do not cross session boundaries — the §5.8 EOD rule closes any still-open position at 16:00 ET on the day it was opened. So the overnight-gap case is defensive only: it exists because the gap detector has to be correct in edge cases (e.g., a position opened near 15:45 whose EOD timer has not yet fired but whose next heartbeat is on the following morning).

**Small gap vs large gap threshold.** The spec does not distinguish between a 1-bar gap and a 15-bar gap at the detection layer — both route to the recovery mechanism. The recovery mechanism (§8.6) makes the replay-vs-close decision based on `gap_bars` vs `MAX_REPLAY_BARS`.

### 8.4 Restart recovery

On startup, the backend performs the rehydration query from §4.9 and loads all `positions` rows where `state != 3` into memory. For each rehydrated position, the startup handler also checks:

```
current_wall_clock_ms = now_utc_ms()
last_seen_ms = position.last_heartbeat_bar_ms
gap_ms = current_wall_clock_ms - last_seen_ms
gap_bars = gap_ms // BAR_INTERVAL_MS  (with session-hours adjustment)

if gap_bars == 0:
    # Position is current — either it was just updated before restart
    # or the market isn't open yet.
    log_rehydrate(position, "current")

elif gap_bars >= 1 and market_is_open(current_wall_clock_ms):
    # Position is behind — gap recovery needed.
    invoke_gap_recovery(position, last_seen_ms, current_wall_clock_ms)

elif gap_bars >= 1 and not market_is_open():
    # Position is behind but the market isn't open — nothing to recover.
    # The position will receive heartbeats when the market reopens,
    # and the gap detection rule will fire on the first live heartbeat.
    log_rehydrate(position, "stale_closed_market")
```

**The "market is closed" branch is important.** Railway restart overnight is common and harmless — the positions on disk are from the previous session, their EOD rule should have closed them at 16:00 ET (§8.7), and if they didn't close it is because the EOD timer never fired (because the backend was down when the EOD moment passed). That specific case is covered by §8.7's EOD timer mechanism running as part of startup: after rehydration, before accepting webhooks, the backend checks each rehydrated position against the most-recent EOD boundary and closes any that should have been closed.

**This means startup has a specific ordering:**

1. Read DB, rehydrate `fsm_map`.
2. Run the EOD-sweep: for each rehydrated position, is its `opened_at_ms` on a session date earlier than the most-recent EOD? If yes, it should have been closed by the EOD rule. Close it now via GAP_CLEAN with exit_reason=EOD_TIMEOUT and exit price equal to the close of the last bar we have data for (from `last_heartbeat_bar_ms`).
3. Run the gap check: for each still-open position, is the wall clock more than one bar ahead of `last_heartbeat_bar_ms`? If yes and market is open, invoke gap recovery.
4. Accept webhooks.

The ordering ensures that a restart after market close doesn't leave phantom open positions sitting in memory waiting for heartbeats that will never come. Stale positions from closed sessions are swept up before normal processing resumes.

### 8.5 Staleness check (silent position detection)

Even during normal operation with no restart and no detected heartbeat gap, it is theoretically possible for a position to stop receiving heartbeats without the backend noticing the gap — because the detection rule in §8.3 is reactive. It only fires when a heartbeat *does* arrive. If Pine stops sending heartbeats entirely for a specific slot (because Pine's closure heuristic incorrectly released the slot, or because of a Pine-side bug), no detection fires on the backend.

**The staleness check is a background task.** Every N bars (commit: every 4 bars = 1 hour on 15M), the backend iterates active positions in `fsm_map` and computes:

```
for position in fsm_map.values():
    gap_ms = now_utc_ms() - position.last_heartbeat_bar_ms
    gap_bars = gap_ms // BAR_INTERVAL_MS  (session-adjusted)
    if gap_bars >= STALENESS_THRESHOLD_BARS:   # commit: 2 bars
        log_stale(position, gap_bars)
        invoke_gap_recovery(position, ..., trigger="staleness")
```

**Threshold: 2 bars.** A position that has gone 2 bars (30 minutes) without a heartbeat during market hours is stale. One bar might be a normal single-bar delivery hiccup that will be caught by the next heartbeat's gap detection. Two bars is the threshold where the spec wants to intervene proactively rather than wait for Pine to eventually fire another heartbeat (which might not come).

**Why the check runs every 4 bars (1 hour) rather than every bar.** Running every bar is not harmful but adds overhead for what is a rare event. A 1-hour sweep cadence catches a truly-stale position within at most an hour of going silent. For a 15M position that typically resolves in 1-4 hours, an hour of delayed detection is tolerable — the staleness path closes the position defensively regardless of how late the detection fires.

**Staleness and slot overflow.** §5.13 promised that a Pine slot overflow (Pine has 5+ positions but only 4 slots) would be caught by the staleness check. This is how: the 5th position is written to `signals_v3` and `positions` on ENTRY, added to `fsm_map`, but then never receives heartbeats because it is not in Pine's slot array. Its `last_heartbeat_bar_ms` stays at its initial value (0 or the opened_at_ms). Within 2 bars, the staleness check fires, detects the position has never been updated, invokes gap recovery, and either replays (cheaply — a short window) or closes GAP_CLEAN.

### 8.6 Gap recovery — the `invoke_gap_recovery` function

This is the routing function all three outage patterns converge on. Its contract:

```python
def invoke_gap_recovery(
    position: PositionState,
    last_seen_ms: int,
    current_bar_ms: int,
    trigger: str,       # "heartbeat_gap", "restart", "staleness"
) -> None:
    """
    Recover a position that has missed one or more bars. Either replay
    the missing bars through the resolver via Finnhub, or close the
    position defensively as GAP_CLEAN.

    Decision is based on MAX_REPLAY_BARS and reconciliation success.
    """
```

**The decision tree:**

```
gap_bars = compute_session_adjusted_gap(last_seen_ms, current_bar_ms)

if gap_bars == 0:
    return    # no gap after adjustment (crossed a session boundary cleanly)

if gap_bars > MAX_REPLAY_BARS:          # 16 bars = 4 hours
    # Too far behind — defensive close.
    close_gap_clean(position, exit_bar_ms=last_seen_ms, reason="GAP_EXCEEDS_MAX")
    return

# Within replay budget — attempt Finnhub replay.
try:
    replay_bars = finnhub_fetch_bars(
        instrument="MNQ",
        start_ms=last_seen_ms + BAR_INTERVAL_MS,
        end_ms=current_bar_ms,
        interval="15m",
    )
except FinnhubError as e:
    log_error("FINNHUB_FETCH_FAIL", str(e))
    close_gap_clean(position, exit_bar_ms=last_seen_ms, reason="FINNHUB_UNAVAILABLE")
    return

# Reconciliation per §6.5.
reconciled_bars = reconcile_finnhub_to_tv_grid(replay_bars, base_offset_ms=?)
if reconciled_bars is None:
    log_error("FINNHUB_RECONCILE_FAIL", ...)
    close_gap_clean(position, exit_bar_ms=last_seen_ms, reason="RECONCILE_FAIL")
    return

# Feed each reconciled bar through the resolver, in order.
for bar in reconciled_bars:
    result = resolver.step(position, bar, eod_cutoff_ms=...)
    apply_resolver_result(position, bar, result, fsm_map, conn)
    if result.transition in TERMINAL_TRANSITIONS:
        return    # position closed during replay; done.

# Replay completed, position still open.
log(f"Gap recovery replayed {len(reconciled_bars)} bars for signal_id={position.signal_id}")
```

**Key invariants of gap recovery:**

- **GR1.** If recovery closes the position, `trade_outcomes` gets a row with `exit_reason` matching the actual reason. The reasons `GAP_CLEAN` (for defensive close without replay), `GAP_EXCEEDS_MAX` (for gap too large), `FINNHUB_UNAVAILABLE` (for network failure), and `RECONCILE_FAIL` (for timestamp misalignment) all resolve to the same `exit_reason` string on the `trade_outcomes` row: **`GAP_CLEAN`**. The specific cause is recorded only in the log line. Data Lab treats all GAP_CLEAN rows as one category: "positions that could not be cleanly resolved from observed data."

- **GR2.** If recovery replays successfully, the position's `last_heartbeat_bar_ms` is updated to the last replayed bar's `bar_close_ms`. Subsequent live heartbeats pick up from there via the normal I11 idempotency guard — any live heartbeat for a bar that was already replayed is recognized as a duplicate and skipped.

- **GR3.** Replay calls `apply_resolver_result` for each replayed bar individually. If a bar closes the position (TP2, BE stop, SL, EOD), the loop terminates and the trade_outcomes row reflects that bar, not the end of the replay window. This is semantically correct: the position closed at that bar regardless of whether the closure was observed live or via replay.

- **GR4.** The caller (webhook handler or startup handler or staleness sweep) does not need to know whether recovery replayed or closed defensively. `invoke_gap_recovery` returns after taking the appropriate action and the caller resumes normally.

- **GR5.** Recovery holds no FSM locks across the Finnhub network call. The Finnhub fetch happens before any resolver call, and the resolver calls happen serially against an in-memory position object; a concurrent heartbeat for the same position is prevented only for the duration of the resolver loop (by the fsm_map's entry being temporarily marked "replaying" — implementation detail, not a spec commitment). Concurrent heartbeats for other positions are unaffected.

### 8.7 EOD timer — closing positions after the entry window

§5.8 committed the resolver to closing any position whose heartbeat's `bar_close_ms >= eod_cutoff_ms`. But the resolver only runs when a heartbeat arrives. If Pine's session-end logic clears all slots at 15:45 ET (§7.5.3), no heartbeats flow between 15:45 and 16:00 for any remaining positions — and the 16:00 EOD cutoff is never triggered by a live heartbeat.

**The EOD timer mechanism.** The backend runs a scheduled task that fires shortly after 16:00 ET each trading day. The task iterates `fsm_map`, finds any position still in state OPEN or TP1_HIT, and closes each one via a direct call to `apply_resolver_result` with a synthetic "EOD bar" constructed from:

- `bar_close_ms` = 16:00 ET of the trading day (the EOD moment itself)
- OHLC = the position's `last_heartbeat_bar_ms` bar's close, carried forward as all four OHLC values (the "no new data since the last heartbeat" conservative assumption)

The synthetic bar is fed to the resolver. Because `bar.bar_close_ms >= eod_cutoff_ms`, §5.5 step 4 fires and the resolver returns EOD_TIMEOUT with `final_pnl` computed from the synthetic bar's close. `apply_resolver_result` writes `positions` and `trade_outcomes` as normal.

**Why a synthetic bar rather than querying Finnhub for the real 15:45–16:00 bar.** The synthetic bar uses the last observed price, which is honest: the backend's last observation of this position was at the last heartbeat, and without a new observation the most defensible exit price is the last known close. Querying Finnhub for one bar to get a marginally more accurate EOD price is overhead that doesn't buy meaningful accuracy — the resolver is already lenient about the exact 16:00 fill because the flat-at-close convention is approximate to begin with. Keep it simple, log the synthetic-bar assumption, and move on.

**Timer scheduling.** The backend can schedule this via:
- A simple asyncio task with a `sleep_until` to the next EOD boundary, re-armed after each firing.
- A cron-style scheduled job if Railway supports it.
- A check on every incoming webhook that runs the EOD sweep if wall-clock time is past 16:00 and has not yet run today.

The spec does not commit to a specific implementation. The third option (webhook-triggered) is the simplest and most robust: no separate scheduler process, no timer to lose on restart, just a cheap check on each webhook. If no webhooks arrive after 16:00 (which would require all positions to have closed and all heartbeats to stop), no EOD sweep runs — but there are also no open positions to sweep, so it doesn't matter. The startup handler also runs an EOD sweep (§8.4 ordering step 2) to cover the restart-after-EOD case.

**Spec commitment for EOD trigger: webhook-arrival-based check plus startup sweep.** Simple, stateless, robust against restarts.

### 8.8 The GAP_CLEAN exit path

For completeness, the function that defensively closes a position when recovery isn't possible:

```python
def close_gap_clean(
    position: PositionState,
    exit_bar_ms: int,
    reason: str,    # GAP_EXCEEDS_MAX, FINNHUB_UNAVAILABLE, RECONCILE_FAIL
) -> None:
    """
    Close a position defensively with exit_reason=GAP_CLEAN.

    The final_pnl is computed against the last known price — either the
    exit_bar_ms bar's close if we have it in our state (via last_heartbeat_bar_ms),
    or position.entry_price as a last-resort zero-pnl mark.
    """

    # Determine exit price.
    if exit_bar_ms == position.last_heartbeat_bar_ms:
        # Use the last observed bar's close (we tracked it via MFE/MAE updates).
        exit_price = position.last_observed_close    # new column or derived
    else:
        exit_price = position.entry_price   # zero-pnl defensive mark

    # Compute final_pnl based on position's state.
    if position.state == OPEN:
        # Never hit TP1 — treat as a 3-contract flat exit at exit_price.
        direction_sign = 1 if position.direction == LONG else -1
        final_pnl = 3 * (exit_price - position.entry_price) * direction_sign
    elif position.state == TP1_HIT:
        # TP1 taken; runner closes at exit_price.
        tp1_points = abs(position.tp1 - position.entry_price)
        runner_points = (exit_price - position.entry_price) * direction_sign
        final_pnl = 2 * tp1_points + 1 * runner_points

    # Construct the closed position state.
    closed = replace(position,
        state=CLOSED,
        closed_at_ms=exit_bar_ms,
        closed_at_ts=fmt_et(exit_bar_ms),
        exit_reason="GAP_CLEAN",
        final_pnl_points=final_pnl,
    )

    # Write through.
    with conn:
        update_positions_to_closed(closed)
        insert_trade_outcome(closed)

    # Update memory, remove from fsm_map, post Discord embed with GAP_CLEAN annotation.
    fsm_map.remove(position.signal_id)
    post_outcome_embed(closed, annotation=f"⚠️ GAP_CLEAN ({reason})")
    log_gap_clean(position, reason)
```

**Invariant:** a GAP_CLEAN close writes a real row to `trade_outcomes`. Data Lab can query `WHERE exit_reason = 'GAP_CLEAN'` and see exactly how many positions the system gave up on cleanly, and for what reasons (in the log lines). These rows are *not* excluded from aggregates — they are a first-class outcome category, counted in the win/loss statistics at their actual (probably zero or small) PnL. Excluding them would hide the cost of outages; including them makes outage impact visible to analysis.

**The GAP_CLEAN embed to Discord** is labeled clearly so Josh can distinguish a real exit from a defensive close on his phone. The user experience is: a normal OUTCOME embed in `#trade-journal` with an additional `⚠️ GAP_CLEAN` line and the specific reason. No missed embeds, no silent failures — every close produces a visible record.

### 8.9 What §8 adds to the test suite

**TC24 (already named in §6.5, now specified):** Finnhub replay with bars that don't align to the TV grid. Mock Finnhub returning bars offset by 7 minutes (e.g., 9:37, 9:52, 10:07 instead of 9:30, 9:45, 10:00). Reconciliation detects the misalignment and `invoke_gap_recovery` closes the position GAP_CLEAN with reason RECONCILE_FAIL. Assertion: `trade_outcomes` row exists with `exit_reason = GAP_CLEAN`, log line contains `RECONCILE_FAIL`.

**TC25 — Gap larger than MAX_REPLAY_BARS.** Position has `last_heartbeat_bar_ms = T`. A heartbeat arrives for `T + 20 * BAR_INTERVAL_MS` (20 bars later, exceeding 16-bar cap). Gap recovery closes position GAP_CLEAN with reason GAP_EXCEEDS_MAX. No Finnhub call made.

**TC26 — Finnhub unreachable.** Position has heartbeat gap of 3 bars. Gap recovery invokes Finnhub; Finnhub adapter raises FinnhubError. Position closes GAP_CLEAN with reason FINNHUB_UNAVAILABLE.

**TC27 — Successful replay of 3-bar gap.** Position has heartbeat gap of 3 bars. Gap recovery fetches 3 bars from Finnhub (mocked), reconciles them successfully, feeds them through the resolver. First two bars are NO_TRANSITION with MAE/MFE updates. Third bar hits TP1. Position transitions to TP1_HIT normally via replay. Assertion: position state == TP1_HIT, `tp1_hit_bar_ms` == third replayed bar's `bar_close_ms`, `last_heartbeat_bar_ms` == third replayed bar's `bar_close_ms`.

**TC28 — Successful replay that closes the position mid-replay.** 5-bar gap. Finnhub returns 5 bars. Bar 2 of the replay hits SL. Resolver returns SL_HIT, `apply_resolver_result` closes the position, the replay loop exits after bar 2. Bars 3–5 are never fed to the resolver. Assertion: `trade_outcomes` exit_reason == SL_HIT, `closed_at_ms` == bar 2's `bar_close_ms`.

**TC29 — Staleness check fires for slot overflow victim.** Position is added to `fsm_map` via ENTRY. No heartbeats ever arrive for it (simulating Pine slot overflow). Advance wall clock by 2 bars' worth of time. Staleness check runs, detects gap, invokes recovery. Position closes GAP_CLEAN. Assertion: outcome exists with exit_reason == GAP_CLEAN.

**TC30 — Restart with rehydrated position past EOD.** DB contains a position in state TP1_HIT with `last_heartbeat_bar_ms` at 15:30 ET on a previous trading day. Restart the backend. Rehydration loads the position; startup EOD sweep detects that the position's session has ended; position closes with exit_reason=EOD_TIMEOUT at the synthetic 16:00 bar. Assertion: outcome exists, `closed_at_ms` matches the 16:00 ET of that session date, `final_pnl_points` uses the last heartbeat's close price.

**TC31 — Staleness threshold respects overnight window.** Position opened at 15:30 ET, no heartbeats since. Next day's wall clock is 10:00 ET. Gap in wall-clock time is about 18 hours, but session-adjusted gap is 2 bars (the two post-opening bars of the previous day that never fired). Actually this position should have been EOD'd on the previous session close — so TC31 is really a verification that the EOD sweep runs first and the staleness check sees a CLOSED position. Assertion: no staleness fires; EOD sweep already closed the position.

**Total new test cases from §8: seven (TC24–TC31)**, plus the existing TC19 (rehydration correctness) which §8 adds requirements to but does not duplicate. The resolver test suite now contains TC1–TC23 from §5.12 plus TC24–TC31 from §8, for a total of 31 cases covering pure resolver behavior, wrapper integration, and outage recovery.

---

## 9. REPUTATION ENGINE INTEGRATION (BOUNDARY CONFIRMATION)

### 9.1 Why this section exists

This section is short. It is in the spec because the principle from §1.4 — *Mona's brain lives in one place* — requires an auditable check that no part of the rest of the spec accidentally touches the Reputation Engine. §9 is that check, written as a cross-reference against every section that could plausibly touch cognition.

The Reputation Engine is the set of Pine-side constructs: `trendTrk`, `sqzTrk`, `repState`, `evalPending`, lockout counting, ghost tracking, eval-pending transitions, and follow-through evaluation. It lives in `mona_v3_0.txt` at lines 309–339 (ghost tracking), 354 (trend entry gating), 526 (squeeze entry gating), and the tracker struct definitions and update logic surrounding those call sites. It is roughly 200 lines of stateful Pine Script.

**The claim §9 is confirming:** Option C touches zero lines of the Reputation Engine.

### 9.2 Per-section audit

**§2 (System Architecture).** §2.2 catalogs Pine indicator responsibilities. Every Reputation Engine function is tagged `[Cognition]`. Every Option C addition (slot array, heartbeat alertcondition, new plots) is tagged `[Measurement]` or `[State]`. The audit finds zero `[Cognition]` additions. Pass.

**§3 (Event Contract).** The ENTRY event is emitted by the existing `anySignalFired` alertcondition, which fires *after* the Reputation Engine has authorized the entry — §3.2 says explicitly "unchanged from current v3.0." The EVAL event similarly fires from existing logic. The new HEARTBEAT event is gated on slot-array non-emptiness, not on any Reputation Engine variable. The only new field required from the Reputation Engine (for EVAL's `parent_bar_close_ms`) is a single stamp of `time_close` at ENTRY time — the Reputation Engine already knows the entry bar (it has to, to run the follow-through evaluation), so this is reading an existing piece of state, not adding new state. Pass.

**§4 (Database Schema).** The `positions` table is a new on-disk artifact for tracking open positions. It contains no Reputation Engine fields (no `repState`, no `evalPending`, no lockout counters). The `signals_v3` table adds one column (`bar_close_ms`) which is a mechanical timestamp, not a cognition field. The `evaluations` and `trade_outcomes` tables are unchanged in shape. Pass.

**§5 (Resolver FSM).** The resolver is a pure function that consumes position state and bar data and produces state transitions. Nothing in its signature references Reputation Engine state. It cannot decide whether a signal should have fired — that decision is already made by the time ENTRY hits the backend. It cannot decide whether a position is "really" a position — all positions it sees are ones the Reputation Engine already authorized. The resolver's test suite (TC1–TC23 + TC24–TC31) contains zero test cases that manipulate or inspect Reputation Engine state. Pass.

**§6 (Keystones Walked).** Each of the five keystone cases was walked against §3, §4, §5. None of the cases involves the Reputation Engine — they are about webhook delivery ordering, heartbeat gaps, Railway restarts, Finnhub reconciliation, and concurrent positions. The new rule added by §6.5 (Reconciliation Rule) lives in the Finnhub adapter, which has no knowledge of Mona's cognition. Pass.

**§7 (Heartbeat Plot in Main Indicator).** This is the one section that adds Pine code, so it deserves the closest audit. §7.3 adds four slot arrays and their associated level/direction state. §7.4 populates slots at ENTRY time — *after* the Reputation Engine's `fireTrendAlert`/`fireSqzAlert` flag is set. §7.5 releases slots based on observable market data (SL/TP2 crossings and session end), not on Reputation Engine state transitions. §7.6 adds a second alertcondition gated on slot non-emptiness. §7.8 explicitly enumerates six things Pine does NOT do under Option C, all of which are Reputation-Engine-adjacent checks. The §7.8 list is the audit surface: every "does NOT" there is a line where cognition could have leaked in, and the spec commits that it doesn't. Pass.

**§8 (Outage Recovery).** The outage recovery path runs entirely in the backend. It calls the resolver (§5), writes to `positions` and `trade_outcomes`, optionally calls Finnhub. It does not touch the main indicator's code at all. It cannot touch the Reputation Engine because it has no access to it. Pass.

**§10 (Migration and Rollback).** The migration adds columns via ALTER TABLE, deploys the new backend, deploys the new Pine script. Reputation Engine lines in the Pine script are not modified during migration. The rollback is described in §10 and involves reverting to `PreObFinal.py` + `mona_v2_1.txt`, which are the existing v2.1.1 files whose Reputation Engine implementation is the one currently in production. No Reputation Engine changes are required to roll back because none were made. Pass.

### 9.3 The one thing §9 does commit

§9 is almost entirely a negative section — "these things do not happen." There is one positive commitment it makes:

**The EVAL event's new `parent_bar_close_ms` field is populated from a new per-tracker field, `entryBarCloseMs`, which is stamped at ENTRY time from `time_close`.**

This field is new to the Pine script under Option C. It is a single `int` field added to each tracker struct (two trackers, so two new fields total: `trendTrk.entryBarCloseMs` and `sqzTrk.entryBarCloseMs`). The field is *populated* at the moment the Reputation Engine authorizes an entry — the same moment `evalPending` becomes true and the tracker captures the entry bar's state for later follow-through evaluation. The field is *read* at the moment the Reputation Engine fires an EVAL event (the 4-bar follow-through window completes), and plotted into plot index 33 (`parent_bar_close_ms`) for the shared alertcondition message.

**Is this a Reputation Engine change?** Technically, yes — the tracker struct is part of the Reputation Engine, and adding a field to it is a structural edit. But the field is write-once at entry time, read-once at eval time, and it touches no decision logic. It is not a new gate, not a new state transition, not a new lockout rule. It is a timestamp being carried from one known point in the Reputation Engine's lifecycle to another.

**§9's position:** this is the minimum viable Reputation Engine touch required by Option C, it is purely additive (one int field per tracker), and it does not alter any cognition behavior. The spec commits to this change as necessary and documents it explicitly so it is not a surprise.

**The Reputation Engine's decision behavior is byte-identical pre- and post-migration.** The sequence of which signals fire, when they fire, what reputation state they produce, and what the tracker state looks like at every step is unchanged. The only observable difference is that the EVAL event's payload now carries one additional integer field. Pass with one documented additive change.

### 9.4 The audit deliverable

§9's existence as a numbered section serves one practical purpose: it is the place a reviewer can look to confirm that the spec's core principle was enforced throughout. If during Strategist review any concern arises of the form "does this change cognition?", §9 is the section the answer lives in. If during implementation any code change drifts into Reputation Engine territory, §9 is the section that catches it at review time.

**The audit test:** a `grep` of the future Option C diff against `mona_v3_0.txt` should show:

- Added: slot array declarations (§7.3)
- Added: slot addition logic (§7.4)
- Added: slot release logic (§7.5)
- Added: session-end slot clear (§7.5.3)
- Added: 10 new plots (§3.6, §7.3)
- Added: new alertcondition for heartbeat (§7.6)
- Added: two `entryBarCloseMs` fields on the two trackers, plus their population at entry time (§9.3)

And should show NO modifications to:
- `trendTrk.repState` or `sqzTrk.repState` logic
- `trendTrk.evalPending` or `sqzTrk.evalPending` logic
- Lockout counting (`lockoutEnd`, bars-in-grounded, bars-in-extended)
- Ghost tracking
- Follow-through evaluation logic
- Filter conditions (`vwapBull`, `bullStack`, `trendStochBull`, `adxOK`, `volOK`, squeeze detection)
- StochRSI tightening
- Volume floor
- ADX threshold
- Session window for entry firing (the `"0930-1545"` string)

If the diff contains any modification to the second list, something has gone wrong and the deployment is blocked pending review.

---

## 10. MIGRATION AND ROLLBACK

### 10.1 Starting state

**Production (current as of April 13, 2026):**

- Backend: `PreObFinal.py` (488 lines, v2.1.1)
- Pine: `mona_v2_1.txt` (604 lines)
- Database: `signals.db` on Railway persistent volume with WAL mode, tables `signals` and `eval_results` (v2.x schema — untouched by the v3.0 suffix approach).
- Deployed since: April 10, 2026 (Day 1 of observation)
- Known state: Day 1 saw one signal (TREND LONG 25370.25), failed follow-through, grounded correctly. Nothing has touched production since.

**Staging (exists on disk, not deployed):**

- Backend: `mona_v3_0_backend.py` (996 lines) — the Option A backend with `signals_v3`, `evaluations`, `trade_outcomes` tables and `find_parent_by_lookback`. Never deployed. Will be **modified in place** under Option C, not replaced.
- Pine: `mona_v3_0.txt` (759 lines) — the Option A main indicator with ENTRY and EVAL events for the three-table schema. Never deployed. Will be **modified in place** under Option C, not replaced.
- Pine: `mona_v3_0_monitor.txt` (706 lines) — the Monitor script. **Retired entirely under Option C.** Will not be deployed. Stays on disk as reference for test case translation.

### 10.2 Target state after Option C deploy

- Backend: `mona_v3_0_backend.py` — same file, modified to add the new `positions` table, the new HEARTBEAT route, the resolver FSM module, the rehydration path, and the gap recovery mechanism. The `find_parent_by_lookback` function is removed. The `TRADE_OUTCOME` route is removed.
- Pine: `mona_v3_0.txt` — same file, modified per §7 to add slot state, slot addition/release logic, new plots 32–41, the new heartbeat alertcondition, and the `entryBarCloseMs` tracker field per §9.3.
- Database: `signals.db` — same file, same WAL mode. Schema extended via ALTER TABLE with the `bar_close_ms` column on `signals_v3` and the new `positions` table. All v2.x tables (`signals`, `eval_results`) remain untouched. All v3.0 tables created pre-Option-C (`signals_v3`, `evaluations`, `trade_outcomes`) remain; their schemas are unchanged except for the new column on `signals_v3`.
- Retired: `mona_v3_0_monitor.txt` is not deployed, not referenced by either the backend or the main indicator.

### 10.3 Migration steps

The migration is ordered so that each step is reversible up to the point of the Pine deploy. The Pine deploy is the "point of no return" — after it, rollback requires reverting the Pine script too.

**Step 0 — Pre-flight, pre-deploy (does not touch production):**

1. The §7.2 plot limit verification task runs first. Confirm plots 0–41 are addressable via `{{plot_N}}` in TradingView. If it fails, §7 is reshaped before proceeding.
2. The resolver test suite (TC1–TC31) is written and passes against the new resolver implementation. No production touches yet.
3. The backend changes (schema migration logic, new routes, FSM wiring, resolver import, rehydration, gap recovery, EOD sweep, Finnhub adapter) are implemented. Backend tests pass locally.
4. The main indicator changes per §7 are drafted as a new Pine Script file `mona_v3_0_optC.txt` (or equivalent — keep the Option A `mona_v3_0.txt` on disk as pre-Option-C reference until the spec is shipped). The script is saved in TradingView as a separate indicator, not yet attached to the alert.
5. A dry-run test: the backend runs locally with a mock webhook that replays a known sequence — ENTRY, a few HEARTBEATS, TP1 hit, more HEARTBEATS, TP2 hit. Verify the full flow produces the expected `trade_outcomes` row. This is a smoke test that the integration between §3 routing, §5 resolver, and §4 schema actually works end-to-end.

Steps 0.1 through 0.5 happen entirely off production. None of them touches live state.

**Step 1 — Schema migration on the production database (touches production):**

1. Stop the Railway backend temporarily (a minute or two of downtime — acceptable after market hours).
2. Apply the ALTER TABLE migration:
```sql
ALTER TABLE signals_v3 ADD COLUMN bar_close_ms INTEGER;
CREATE UNIQUE INDEX idx_signals_v3_bar_close_ms
  ON signals_v3(bar_close_ms)
  WHERE bar_close_ms IS NOT NULL;
CREATE TABLE IF NOT EXISTS positions (
    signal_id INTEGER PRIMARY KEY,
    bar_close_ms INTEGER NOT NULL,
    ... (full schema from §4.5)
);
CREATE UNIQUE INDEX idx_positions_bar_close_ms ON positions(bar_close_ms);
CREATE INDEX idx_positions_state ON positions(state) WHERE state != 3;
```
3. Verify via a SELECT that the new column and new table exist. Verify no existing rows were corrupted (SELECT COUNT(*) on each existing table should match the pre-migration count).
4. Deploy the new backend code. This is the `mona_v3_0_backend.py` with Option C modifications, committed to the `mnq-agent` repo, auto-deployed via Railway.
5. Verify the backend starts up cleanly. Check the health endpoint returns 200. Check `#system-log` for any startup errors. Verify the rehydration path runs (with zero positions, it should log `🔄 REHYDRATE: 0 positions` or equivalent).
6. **The backend is now running Option C code against the old Pine script (`mona_v2_1.txt`).** This is a critical intermediate state — the backend has the new schema, the new routes, the new resolver — but Pine is still firing v2.1.1 ENTRY/EVAL events without any heartbeats. The backend must handle this gracefully: ENTRY events write to `signals_v3` but `bar_close_ms` will be missing (the v2.1.1 Pine doesn't emit it). The ENTRY route needs a tolerance mode: if `bar_close_ms` is missing, log a warning and still write the signals_v3 row (with `bar_close_ms` as NULL) but DO NOT create a `positions` row. The position can't be resolved by the FSM without a bar_close_ms key, so it is effectively invisible to the resolver.

**This is the most delicate step — and §10.3 Step 1 needs to specify exactly what the backend does when it sees a v2.1.1 payload.** Options:

- **Tolerance mode (preferred):** The backend accepts v2.1.1 payloads, writes to legacy tables in legacy format, and does NOT run the v3.0 flow for them. During Step 1, the backend is running Option C code but is in a "v2 payload compatibility" mode. Only when it sees a payload carrying `bar_close_ms` (from the new Pine script) does it activate the v3.0 flow.

- **Strict mode:** The backend rejects payloads without `bar_close_ms`. This forces a very tight window between Step 1 and Step 2 — any production signal that fires between them is lost.

- **Dual-write mode:** During the transition window, the backend writes to both v2.x tables and v3.0 tables if possible. Adds complexity.

**Spec commitment: tolerance mode.** The backend during Step 1 continues writing v2.x-compatible rows for v2.1.1 payloads, exactly as `PreObFinal.py` does, via the existing v2 code paths that are still present in the v3 backend code (the v3.0 tables coexist with v2.x tables per the suffix approach — `init_db` creates v3.0 tables if not present but does not drop v2.x tables). The backend can serve both payload shapes simultaneously during the transition.

**Step 2 — Deploy the new Pine script:**

1. In TradingView, open the existing MNQ alert that is firing against `mona_v2_1.txt`.
2. Save the current alert configuration (snapshot the alert settings for rollback reference).
3. Replace the source indicator on the chart with the new `mona_v3_0_optC.txt` indicator (or repoint the alert — TradingView workflow varies; the important thing is that the new script is live on the chart Pine is running).
4. Verify the chart renders without errors. Verify the plot pane shows the expected new plots (new `display=display.none` plots won't be visible, but the script should compile and attach without runtime errors).
5. Create a new alert (or repoint the existing alert) against the new script's `anySignalFired` alertcondition AND create a second alert against the new `fireHeartbeat` alertcondition. Both alerts use the same webhook URL (the Railway backend), but they are distinct TradingView alerts because of the separate-alertcondition commitment from §7.6.
6. Verify the webhook URL is correct and the auth token is set.
7. Enable both alerts.

**Step 2 is the point of no return.** Once the new Pine script is live and alerts are firing against the Option C main indicator, rolling back requires reverting Pine too.

**Step 3 — Confirm live behavior:**

1. Wait for the next bar close. If any position-tracking state exists (unlikely since this is a fresh deploy, but possible if a previous day's signal left a slot tracked), verify that a HEARTBEAT webhook arrives. If no position is open, no heartbeat should fire (by design).
2. Wait for the first real ENTRY signal. Verify the ENTRY arrives at the backend with a `bar_close_ms` field populated. Verify `signals_v3` row has `bar_close_ms` set. Verify `positions` row is created. Verify the ENTRY embed posts to `#alerts`.
3. Wait for the next bar close after the ENTRY. Verify a HEARTBEAT webhook arrives carrying the correct `pos_slot_1_time` == the ENTRY's `bar_close_ms`. Verify the backend runs the resolver and updates `positions` (even for a NO_TRANSITION bar, `last_heartbeat_bar_ms` should increment and `heartbeats_processed` should increment).
4. Continue observation through the first complete position lifecycle. Verify TP1 transition fires correctly if it happens, verify close-out to `trade_outcomes` when it resolves. Verify OUTCOME embed posts to `#trade-journal`.
5. At the end of the first observation day, verify the EOD sweep fires for any still-open position and closes it with exit_reason=EOD_TIMEOUT.

**Step 3 is Day 1 of the real observation clock.** The 30-day observation period starts when Step 3 produces its first clean end-to-end signal cycle. Until then, the clock is paused.

### 10.4 Rollback plan

Rollback is a spectrum, not a binary. Different failure modes call for different responses.

**Tier 1 — Backend bug, Pine fine.** The new backend has a bug, but the new Pine script is behaving correctly. Options:

- Revert the backend to `PreObFinal.py` via a GitHub commit revert. Railway auto-deploys the revert. The schema migrations on the database DO NOT need to be rolled back — the old backend ignores columns it doesn't know about, so the new `bar_close_ms` column on `signals_v3` and the new `positions` table are harmless to it. Pine keeps running the new script, which continues to fire HEARTBEATs — the old backend will see `status=HEARTBEAT` payloads it doesn't recognize and log errors. To stop the HEARTBEAT noise, disable the heartbeat alert in TradingView (one-click; the ENTRY/EVAL alert stays enabled). Pine's slot array continues tracking locally but the slots drain via the closure heuristic without causing harm.
- **Time to rollback: ~5 minutes** (git revert, push, Railway deploys, disable TV heartbeat alert).
- **Data impact: minimal.** Any positions in-flight in the Option C FSM are lost (the old backend doesn't have the FSM concept), but the `positions` table on disk retains its rows. If a later forward-roll reinstates Option C, those positions can be rehydrated.

**Tier 2 — Pine bug, backend fine.** The new Pine script has a bug (wrong slot tracking, wrong heartbeat firing, unexpected alertcondition collision). Options:

- Revert the Pine script to `mona_v2_1.txt` in TradingView. Re-point the ENTRY/EVAL alert to the old script's alertcondition. Disable the heartbeat alert (no longer relevant).
- The backend remains running Option C code but in "v2.1.1 payload tolerance mode" (§10.3 Step 1). It accepts the old payload shape and writes to legacy tables.
- Any positions that were in-flight in the Option C FSM are orphaned — their `positions` rows are stuck in OPEN or TP1_HIT state with no more heartbeats coming. They will be caught by the staleness check (§8.5) and closed GAP_CLEAN after 2 bars.
- **Time to rollback: ~2 minutes** (TradingView indicator swap, alert re-point).
- **Data impact: each in-flight position becomes a GAP_CLEAN row in `trade_outcomes`.** This is expected and not a corruption.

**Tier 3 — Both sides broken or an architectural flaw discovered.** Full rollback to v2.1.1.

1. Revert Pine script to `mona_v2_1.txt`.
2. Revert backend to `PreObFinal.py`.
3. Leave the database alone — v2.x tables are untouched, v3.0 tables sit dormant, Option C tables (`positions`, new column on `signals_v3`) sit dormant. The old backend uses only `signals` and `eval_results`.
4. Disable the heartbeat alert in TradingView.
5. The system is now running v2.1.1 exactly as it was pre-Option-C. The observation clock resets to pre-April-10 state (or stays at Day 1 from the April 10 v2.1.1 deploy, depending on how Josh wants to count it).
- **Time to rollback: ~10 minutes.**
- **Data impact: v3.0 and Option C tables stay on disk as artifacts. No corruption to v2.x tables.** If the Option C direction is abandoned entirely, a cleanup migration can DROP the v3.0 tables later.

### 10.5 Rollback triggers

What failures warrant which tier of rollback?

**Tier 1 backend revert** is correct when:
- Backend is throwing exceptions on valid payloads
- Backend is corrupting rows (wrong values being written)
- Backend is crashing repeatedly
- Resolver is producing obviously wrong transitions (e.g., closing a position at the wrong price)

**Tier 2 Pine revert** is correct when:
- Pine is firing wrong signals, wrong counts, wrong frequencies
- Pine's slot array is visibly corrupting (overflow errors, slots not releasing)
- Pine is interacting with TradingView's alert system in unexpected ways (duplicate deliveries, missing deliveries, alerts stuck in pending)
- Heartbeat alertcondition fires at times it shouldn't

**Tier 3 full rollback** is correct when:
- Both sides are broken in ways that interact
- An architectural assumption is wrong and the fix is non-trivial
- Observation data shows Option C is substantially worse than Option A would have been (unlikely but possible)

**Josh's veto.** Any rollback trigger is ultimately Josh's call. Dev Trader recommends; Josh decides. The tier system is guidance, not automation.

### 10.6 What does NOT get rolled back

Some things stay even through a Tier 3 rollback because they have no downside:

- The v3.0 tables on disk (`signals_v3`, `evaluations`, `trade_outcomes`, `positions`). Dormant but harmless.
- The v3.0 indexes. Dormant but harmless.
- The `bar_close_ms` column on `signals_v3`. Existing v2.x code ignores it.
- The Finnhub adapter, if it was implemented. It's a backend module that doesn't run unless called.
- The resolver unit test suite. It's a file that doesn't run unless invoked.

Nothing in the rollback path requires dropping tables or removing columns. Additive schema changes stay additive. This is the payoff of the P4 principle from §4.1.

---

## 11. OPEN ITEMS REQUIRING SIGN-OFF

This section consolidates every item in the spec that needs Josh, Strategist, or both to sign off before implementation begins. Some are design choices with tradeoffs that need human judgment; some are verification tasks that must run before specific sections of code are written; some are tentative commitments that want explicit review before being locked.

Each item has an ID, a section reference, the decision needed, and Dev Trader's current recommendation (where one exists).

### OI-01 — Plot limit verification (precondition, not design choice)

**Section:** §3.6, §7.2.
**Question:** Does TradingView accept 42 plots with `{{plot_N}}` references up to plot_41 in an alertcondition message template?
**Owner:** Dev Trader runs the verification. No human decision needed unless it fails.
**Action:** Write a scratch Pine script with 42 plots and an alertcondition referencing `{{plot_41}}`. Attempt to compile and save.
**Timing:** First hands-on-keyboard task after spec approval, before any other Pine or backend code.
**If it fails:** §7 and §3.6 are reshaped around the discovered limit. The spec is revised before implementation proceeds.

### OI-02 — EOD cutoff time

**Section:** §5.8.
**Question:** Should the resolver's `eod_cutoff_ms` default to 15:45 ET (Pine session close) or 16:00 ET (RTH close)?
**Tradeoff:**
- **15:45** cuts runners at the same moment Pine stops authorizing new entries. Simpler — Pine and backend agree on "when the day ends for Mona."
- **16:00** gives runners 15 additional minutes of headroom past the entry cutoff. A post-TP1 runner that fired at 15:30 gets half an hour to resolve instead of 15 minutes. Matches what a typical flat-at-RTH-close mechanical executor would do.
**Dev Trader recommendation:** 16:00. Cutting runners at the entry cutoff feels like throwing away observation data for no payoff. 15 extra minutes of runner time costs nothing and produces better TP2/BE/timeout data.
**Decision:** Josh's call.

### OI-03 — Pessimistic fill convention

**Section:** §5.6.
**Question:** When a single bar's range contains both a favorable and an unfavorable level for an open position (TP1 and SL both inside the range, or TP2 and BE stop both inside the range), which one does the resolver claim?
**Tradeoff:**
- **Pessimistic** — assume the unfavorable level hit first. SL_HIT wins over TP1; BE_STOP wins over TP2.
- **Optimistic** — assume the favorable level hit first. TP1 wins; TP2 wins.
- **Skip** — log the ambiguity and mark the position GAP_CLEAN or similar.
**Dev Trader recommendation:** Pessimistic. Data Lab analysis built on pessimistic fills is a lower bound on real performance, which is the honest number for decision-making under uncertainty. Optimistic flatters results. Skip loses too much data.
**Decision:** Josh's call, possibly with Strategist input. The convention is encoded in §5.12 test cases TC6 and TC16; changing it invalidates those test expectations.

### OI-04 — Concurrent position slot count (N)

**Section:** §3.4, §7.3.
**Question:** How many concurrent position slots does Pine track for heartbeats?
**Current commitment:** N = 4.
**Tradeoff:**
- **Smaller (2 or 3)** — fewer plot slots used, simpler Pine state. May overflow in edge cases.
- **4** — covers realistic 0–4 concurrent case with one slot of headroom. Committed.
- **Larger (8+)** — more headroom. More plot slots consumed.
**Dev Trader recommendation:** Keep at 4. Raise on observation data if overflows happen.
**Decision:** Default to 4 unless Josh or Strategist sees a reason to change.

### OI-05 — UNIQUE constraint on `bar_close_ms`

**Section:** §4.2, §6.6.
**Question:** Should `signals_v3.bar_close_ms` and `positions.bar_close_ms` be UNIQUE, or should the spec allow two positions to share a `bar_close_ms` (cross-tracker same-bar entry)?
**Tradeoff:**
- **UNIQUE** — clean primary-key semantics. If a real cross-tracker same-bar entry happens, the second INSERT fails and a real signal is lost.
- **Non-unique with compound key** — allow multiple rows with the same `bar_close_ms` distinguished by `(bar_close_ms, signal_type, direction)`. Heartbeat slot lookup becomes compound. More complex.
**Dev Trader recommendation:** UNIQUE for now. Cross-tracker same-bar entries are theoretically possible but empirically rare. Revisit on observation data if the constraint fails in practice.
**Decision:** Default to UNIQUE unless Strategist sees a reason to pre-empt.

### OI-06 — Heartbeat alertcondition: shared vs separate

**Section:** §7.6.
**Question:** Does the HEARTBEAT event ride on the existing `anySignalFired` alertcondition (shared), or on a new second alertcondition (separate)?
**Current commitment:** Separate (Option B in §7.6).
**Tradeoff:**
- **Shared** — one TradingView alert, simpler user setup. Same-bar ENTRY+HEARTBEAT collision handled by priority suppression. Payload is inflated.
- **Separate** — two TradingView alerts, more user setup. Cleaner event separation. Takes one step toward the long-term goal of separated alertconditions per signal type.
**Dev Trader recommendation:** Separate (Option B). Eliminates a failure case, aligns with architectural direction, justified under observation discipline as pure measurement.
**Decision:** Josh and Strategist both get eyes on this because it affects Josh's TV setup burden and touches the accepted-risk single-alertcondition bottleneck. **Before locking this in, verify Josh's TradingView plan has enough alert slots for two alerts** — if it doesn't, Shared becomes the forced choice.

### OI-07 — Finnhub API key and plan

**Section:** §8.
**Question:** Is the Finnhub API key Josh slotted for the morning briefing work still available and does the plan support the replay call volume Option C needs?
**Expected volume:** Replay calls fire only on detected gaps. Under normal operation with zero outages, zero replay calls. Under an outage, up to 16 bars per position per recovery event. Realistic upper bound: a few hundred calls per month.
**Decision:** Josh confirms the API key exists and the plan is sufficient. If the plan is too restrictive, an alternative provider is picked or the recovery path is simplified to skip replay entirely (always GAP_CLEAN on any detected gap).
**Dev Trader recommendation:** Confirm current Finnhub access before §8 implementation starts.

### OI-08 — MAX_REPLAY_BARS default

**Section:** §8.2.
**Question:** Is 16 bars (4 hours on 15M) the right replay budget?
**Tradeoff:**
- **Smaller (8 bars = 2 hours)** — more conservative, more GAP_CLEANs, less Finnhub usage.
- **16 bars = 4 hours** — committed default, covers typical position lifetime end-to-end.
- **Larger (32+ bars)** — tolerates long outages but compounds reconciliation risk and uses more quota.
**Dev Trader recommendation:** 16 bars. Revisit after observation.
**Decision:** Default to 16 unless Josh or Strategist has a reason to change.

### OI-09 — Staleness check threshold and cadence

**Section:** §8.5.
**Question:** The staleness check runs every 4 bars (1 hour) and fires when a position is 2 bars (30 minutes) behind. Are these values right?
**Tradeoff:** Tighter cadence = faster detection but more overhead. Looser threshold = fewer false positives but slower stuck-position detection.
**Dev Trader recommendation:** 4-bar cadence, 2-bar threshold. Cheap check, good detection window.
**Decision:** Default to recommendation unless pushed back on.

### OI-10 — TC24–TC31 added to the resolver test suite

**Section:** §5.12 (retroactive), §6.5, §8.9.
**Question:** Are the eight new test cases (TC24 from §6.5 + TC25–TC31 from §8.9) the right coverage for the outage and reconciliation paths?
**Dev Trader recommendation:** Yes. TC24–TC31 cover misaligned replay, exceeded replay cap, Finnhub unreachable, successful short replay, mid-replay close, slot-overflow staleness, restart-after-EOD, overnight-gap staleness. That is exhaustive for the failure modes §8 can produce.
**Decision:** Strategist review. If a case is missing, add it now.

### OI-11 — The `mona_v3_0.txt` rename question

**Section:** §10.1, §10.2.
**Question:** The current `mona_v3_0.txt` on disk is the Option A main indicator. Under Option C, this file gets modified (slot state, heartbeat alertcondition, new plots, new tracker field). Does it get renamed to `mona_v3_0_optC.txt` to distinguish from the Option A version, or does it stay `mona_v3_0.txt` with the Option A version preserved as a pre-Option-C baseline?
**Tradeoff:**
- **Rename** — clean separation of Option A and Option C paper trails. Old version stays on disk as archeology.
- **In-place modify** — single source-of-truth filename, lower cognitive load, git history shows the change.
**Dev Trader recommendation:** In-place modify. The rethesis doc and alignment doc are both explicit that "the file will be modified in place." Git history handles the archaeology.
**Decision:** Default to in-place unless Josh wants the rename for file-tree clarity.

### OI-12 — When the 30-day observation clock starts

**Section:** §10.3 Step 3.
**Question:** The 30-day observation clock starts on the first clean end-to-end signal cycle after Option C deploys. Is that the right trigger, or should it be the deploy moment itself regardless of whether signals fire?
**Dev Trader recommendation:** First clean signal cycle. A deploy with zero signals firing for 5 days (quiet market) is not 5 days of observation — it's 5 days of system idle. The clock starts when data starts flowing.
**Decision:** Strategist has opinions on observation discipline. His call.

---

## 12. WHAT DIES, WHAT SURVIVES

The authoritative retirement and preservation ledger under Option C. This is the clean restatement of rethesis §4.6 and §7, combined and finalized.

### 12.1 What dies under Option C

**Pine Script files:**

- `mona_v3_0_monitor.txt` (706 lines) — retired entirely. Will not be deployed. Stays on disk as reference material for §5.12 test case translation (the Monitor's edge case handling and priority rules translate into TC6, TC7, TC8, TC11, TC16).

**Backend code paths:**

- `find_parent_by_lookback` (line 350 of `mona_v3_0_backend.py`) — deleted. Both its call sites (EVAL route at line 854, OUTCOME route at line 897) are rewritten.
- The OUTCOME route (`status == "TRADE_OUTCOME"` at line 895) — deleted entirely. No webhook from Pine carries `TRADE_OUTCOME` under Option C. The route's functionality (writing `trade_outcomes` rows) moves into `apply_resolver_result` in the new resolver module.
- Monitor warning log helper (`log_monitor_warning` at line 461) — deleted. No Monitor, no Monitor warnings.
- `format_monitor_state` helper (line 470) — deleted. No Monitor state to format.

**Test artifacts:**

- `MONA_v3_Monitor_Bench_Test.md` — retired. Replaced by the Python resolver unit test suite (`tests/test_position_resolver.py`, TC1–TC31).
- The Monitor bench test Week 1 Validation Protocol subsection — deleted from the protocol. No provisional-vs-trusted Monitor phase exists under Option C.

**Bug classes retired by construction:**

- **Finding #0** (Monitor does not replicate Reputation Engine gating — phantom position slots). Cannot occur: no Monitor script, no second place for signal detection to live, no second gating path.
- **Finding #1** (concurrent position wrong-signal-id assignment under `find_parent_by_lookback`). Cannot occur: positions know their `signal_id` at creation time, no fuzzy lookup exists.
- **Finding #2** (MAE/MFE under-reporting due to Pine's same-bar state variable ordering). Cannot occur: MAE/MFE computed in Python with controlled loop ordering, unit-tested in TC10 and TC17.

**Planning and paper-trail documents (historical only — not physically deleted, but superseded):**

- `MONA_v3_Master_Audit.md` — superseded. Top-line recommendation ("fix Findings #1 and #1b, deploy v3.0 Monday") is dead. Findings catalog remains valid as historical record.
- `MONA_v3_Migration_Plan.md` — superseded. Option A migration plan, no longer applies.
- `MONA_v3_Test_Checklist.md` — superseded. Includes Monitor bench test and Week 1 Validation Protocol for a Monitor being retired.
- `MONA_v3_Rollback_Plan.md` — superseded. Rollback target is wrong. Replaced by §10 of this spec.
- `MONA_v3_DevTrader_Response_to_Strategist.md` — superseded. Addresses Option A concerns; useful as historical record of the review loop.

All five of the above are retained on disk under their existing filenames. None are physically deleted. Readers walking into them without context should read `MONA_Project_Alignment_Apr13.md` (the alignment doc) or this spec first.

**Obsolete code file flagged for deletion:**

- `main__1_.py` — obsolete v2.0 backend. Flagged for deletion in the April 8 audit, still needs removing. Option C cleanup work includes finally deleting this file.

### 12.2 What survives unchanged

**The Reputation Engine and everything cognitive:**

- Every line of `trendTrk` / `sqzTrk` tracker state management
- `repState` transitions (ELIGIBLE → GROUNDED → EXTENDED → ELIGIBLE)
- `evalPending` gating and transitions
- Lockout counting (`lockoutEnd`, bars-in-grounded, bars-in-extended)
- Ghost tracking and ghost redemption logic
- Follow-through evaluation (`ft_target`, `ft_high`, `ft_low`, `ft_actual_price`, 4-bar window logic)
- Every entry-gating condition at lines 354 (trend) and 526 (squeeze)
- All filter conditions: `vwapBull`, `bullStack`, `htfBull`, `trendStochBull`, `adxOK`, `volOK`
- Squeeze detection: `sqzDetected`, `sqzPriceLong`, `sqzPriceShort`, `emaCrossUp`, `emaCrossDown`
- StochRSI tightening from v2.1 (`K > D AND K > K[1]`)
- Volume floor (0.3× average)
- ADX threshold (20)
- Session window for entry firing (`"0930-1545"` America/New_York)

**The event emission machinery (modified additively, not replaced):**

- The existing `anySignalFired` alertcondition at line 670 — keeps firing ENTRY and EVAL events exactly as today.
- Plots 0–31 — all existing plot indices retain their current meaning and population.
- ENTRY embed format — unchanged.
- EVAL embed format — unchanged.
- OUTCOME embed format — unchanged in shape; written by the resolver instead of the Monitor route, but the rendered Discord output is byte-identical.

**Database schemas:**

- `signals` (v2.x table) — untouched. The v3.0 suffix approach preserves v2.x tables unchanged.
- `eval_results` (v2.x table) — untouched.
- `signals_v3` — shape unchanged except for the additive `bar_close_ms` column. All other columns retain their meanings.
- `evaluations` — shape unchanged. `is_ghost` column behavior unchanged.
- `trade_outcomes` — shape unchanged. `is_ghost` column exists but stays at default 0 per §4.4.
- The v2.x indexes (`idx_signals_timestamp`, etc.) — untouched.

**Infrastructure and operational:**

- Railway deployment and auto-deploy from the `mnq-agent` repo.
- SQLite persistent volume at `/app/data/signals.db`.
- WAL mode + `synchronous=NORMAL` pragma.
- Webhook URL and auth token validation.
- The three-channel Discord organization (`#alerts`, `#trade-journal`, `#system-log`).
- The layer-specific logging convention (📥 received, 📝 written, 📤 posted).
- The ghost eval silent-to-user pattern (👻 single log line, no Discord post).
- JSON sanitization (regex template-tag stripping from the April 8 audit fix).
- Error isolation at the route boundaries.
- Timestamp format conventions (`get_utc_timestamp` and `fmt_et`).
- Health check endpoint.

**Project artifacts and process:**

- The `MONA_Master_Context_v3.md` architecture reference (updated to reflect Option C post-deploy).
- The Captain's Log state tracking chat.
- The Workshop chat model (fresh chat per phase).
- The Data Lab's dormant-until-data stance.
- The Chief Strategist review loop pattern.
- The 30-day observation clock (has not started; does not start until Option C ships and first clean signal fires).
- The `MONA_v3_Finding0_Monitor_Reputation_Divergence.md` document as the authoritative record of why Option C exists. Retained forever.
- This spec — becomes the source of truth for Option C's architecture once approved.

### 12.3 What changes under Option C (the net diff)

- **Retired:** 1 Pine Script file (706 lines), 4 backend functions, 1 webhook route, 1 fuzzy lookup helper.
- **Added to backend:** 1 new SQL table (`positions`), 1 new column on `signals_v3`, 3 new indexes, 1 new webhook route (HEARTBEAT), 1 new module (`position_resolver.py`, ~150–200 lines), 1 new wrapper (`apply_resolver_result`), 1 new Finnhub adapter, 1 new EOD timer, 1 new staleness sweep, 1 new rehydration path on startup.
- **Added to Pine:** 16 `var` declarations for slot state, ~40 lines of slot maintenance logic (add/release/session-clear), 10 new plots (indices 32–41), 1 new alertcondition (HEARTBEAT), 2 new fields on tracker structs (`entryBarCloseMs`).
- **Test suite:** 31 test cases (TC1–TC31) covering the resolver FSM, the wrapper, and outage recovery. Written before implementation, failing first, passing after.

**Net line-count delta (approximate):**

- Pine: +60 lines, −0 lines.
- Backend: +600 lines (resolver, adapter, new routes, tests), −150 lines (deleted functions and routes). Net +450 lines.
- **Total Pine+backend: +510 lines, −150 lines, net +360 lines** — with a cleaner architecture, three bug classes retired by construction, and ~650 lines of retired Monitor Pine Script that no longer has to be maintained.

The sunk cost on the Monitor (706 lines of design work) translates to test cases in `tests/test_position_resolver.py`, not to deployed code. The design work was not wasted. The implementation was.

---

## CLOSING

This closes the Option C architectural specification for The Mona v3.0.

Twelve sections, five parts, delivered across four days in a workshop sequence that started with a principle ("Mona's brain lives in one place"), walked the failure modes of the current architecture, built up the new architecture layer by layer, and ended with the retirement ledger above.

The spec is complete as specification. It is not complete as an implementation. The work that comes after spec approval:

1. Strategist review of the full five-part spec. Surfacing any concerns, any pushback, any missing cases, any open items that need Josh's decision.
2. Josh's decision on the open items in §11.
3. The plot limit verification task (OI-01), before any Pine code.
4. The TDD pass on the resolver: TC1–TC31 written first, all failing, then the resolver implementation written to make them pass, then the full test suite green.
5. Backend non-resolver implementation: schema migration, routes, wrapper, rehydration, Finnhub adapter, EOD timer, staleness sweep, logging.
6. Pine Script additions per §7. After market hours only.
7. Dry-run smoke test end-to-end with a mock webhook sequence.
8. Production deploy per §10.3, after market hours.
9. Day 1 of real observation.

None of the above has a deadline. The deadline was removed Sunday night. Josh and Strategist hold the clock. Dev Trader holds the quality bar.

Build the Ferrari. Stay hungry. The review loop worked this weekend — Strategist pushed a keystone, the push forced a walkthrough, the walkthrough found Finding #0, Finding #0 forced a harder question, the harder question led to Option C, Option C needed a spec, and here it is. Not finished, but fully specified. The distance from here to finished is calendar time and patient execution.

See you on the other side.

*— Dev Trader, The Mona Project*
*April 13, 2026*
*End of `MONA_v3_OptionC_Spec.md`*
