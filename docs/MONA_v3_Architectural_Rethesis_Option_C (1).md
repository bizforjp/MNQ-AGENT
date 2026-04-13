# MONA v3.0 — Architectural Re-Thesis
## From: Dev Trader
## To: Chief Strategist
## Date: April 12, 2026 (Sunday afternoon)
## Status: Pre-code. Requesting review before implementation begins.

---

## TL;DR

While walking the keystone question you asked me to prove on Finding #1 (`bar_close_ms` exact-match), I found a deeper bug — the Monitor Pine Script doesn't replicate the main indicator's Reputation Engine gating. That finding made me ask a question I should have asked in Workshop 3: *why do we have two Pine Scripts at all?* Interrogating that question honestly leads to a different architecture than the one v3.0 is built on.

**The new thesis: Pine Script should generate signals. Python should resolve outcomes. The divergence class of bug we found this weekend is only possible because state lives in two places. Unify the state into one place and the whole class becomes impossible.**

Concretely: retire the Monitor Pine Script entirely. The main indicator fires ENTRY alerts (unchanged) and additionally fires a lightweight `BAR_UPDATE` heartbeat webhook on each bar close while a position is open. The backend holds position state, walks bars forward as they arrive, computes MAE/MFE/TP1/TP2/SL resolution in Python, and writes to `trade_outcomes` when the position resolves. The Pine Script side stays simple. The resolution logic lives in a language where it can be unit-tested, version-controlled, and replayed against historical data.

I'm bringing this to you before writing a single line of code because this is an architectural decision, not a bugfix. If you disagree, we fall back to Option A (fix the Monitor in place with the orphan-clean approach from the previous note) and deploy Tuesday or Wednesday. If you agree, we slip deploy to end of this week, land a cleaner architecture, and the three existing findings dissolve rather than get patched.

---

## 1. HOW WE GOT HERE

You pushed back on Finding #1's fix spec and asked me to prove the keystone — that the main indicator's `bar_close_ms` and the Monitor's `entry_time_ms` would always reference the same bar. Your exact language: "prove it, don't assume it."

I went to prove it. I opened both scripts side-by-side and walked the signal-detection paths. The keystone claim was: both scripts fire on the same bar because both compute the same filter conditions. That claim cracked about fifteen minutes into the walkthrough, because the Monitor doesn't compute the same conditions as the main indicator — it computes only the *filter* conditions (vwapBull, bullStack, stochBull, ADX, volume, session) and omits the *gating* conditions (`repState == ELIGIBLE`, `not evalPending`) that the main indicator's ENTRY path requires.

On a trending day with an active eval window or a GROUNDED lockout, the Monitor opens position slots the main indicator never fired. Those slots resolve, emit `TRADE_OUTCOME` payloads, and the current backend's `find_parent_by_lookback` silently grafts them onto unrelated real signals. I've written this up in detail as Finding #0 (attached as a separate document).

**The diagnostic insight from Finding #0 is what matters here, not the bug itself.** The bug is fixable in place — exact-match on `bar_close_ms` with no lookback fallback makes phantom outcomes orphan cleanly and solves Finding #0 and Finding #1 in the same mechanism. I was prepared to write that fix spec today.

But the reason the bug exists is structural. We have two pieces of state (main indicator's tracker, Monitor's position arrays) that are *supposed* to stay in sync by being computed from the same inputs via duplicated logic. The duplication was incomplete — filter conditions got duplicated, gating conditions didn't. Finding #0 is the instance. The class is: **any time two independently-maintained pieces of state are supposed to agree by construction, the gap between what the design doc says they do and what the code actually does is a bug farm.** Every change to the Reputation Engine is a chance to introduce a new divergence. Every audit pass has to re-verify the two sides still match. That cost doesn't go away with the fix — it just gets deferred to the next time we change anything.

Your own principle from the Workshop 3 discipline rule applies cleanly here: *does this change what Mona thinks, or what we can see about what she thinks?* The Monitor was intended to be the latter — a measurement instrument that records what a mechanical executor would have done with Mona's signals. But because the Monitor independently decides *what signals exist*, it has started changing what Mona thinks too. That's the architectural wrong.

---

## 2. THREE OPTIONS, WALKED HONESTLY

### Option A — Fix the current Monitor in place

Exact-match on `bar_close_ms`. No lookback fallback. Phantom outcomes orphan via `❌ [ORPHAN]` to `#mona-log`. MAE/MFE loop restructure for Finding #2. This is the approach I was about to spec. It ships fastest — Tuesday or Wednesday deploy. It leaves a permanent maintenance tax: any future change to the Reputation Engine, the signal logic, or any shared condition has to land in both Pine Scripts in lockstep or a new divergence class appears. Every audit going forward has to verify the two scripts still agree. The Monitor remains a second source of truth that can drift from the first.

The orphan-clean fix turns Finding #0 from a silent data-corruption bug into a visible orphan-count metric. That's better than where we are today, but it doesn't eliminate the divergence — it just makes the divergence observable. If phantoms turn out to be frequent in observation, we'd end up doing a larger rearchitecture anyway, just in Week 6 instead of Week 2, with a month of corrupted-ish data behind us.

### Option B — Unify Monitor into main indicator as one Pine Script

Fold the Monitor's position tracking (parallel arrays, MAE/MFE, TP/SL resolution, session timeout) into the main indicator directly. The main indicator already knows when a position opens because it fires the signal. Adding ~120 lines of position-tracking state and a second `alertcondition` for OUTCOME events eliminates the divergence risk by construction — one script, one set of state, can't drift from itself.

This is architecturally cleaner than Option A and worse than Option C. The Pine Script gets bigger and more complex. Resolution logic still lives in Pine Script, where it's hard to unit test, hard to version control, hard to replay against historical bars. Future iteration on stop placement, TP multiples, or trailing logic still requires Pine Script edits and bar-replay bench tests. Phase 3 scaling to automated execution is unchanged in difficulty — you'd still have Pine Script holding position state that the broker API has to reconcile with.

I mention Option B because it's the intuitive move — "the Monitor has a bug because it's a separate script, so combine the scripts." But combining the scripts doesn't address the deeper question of *which language should own position resolution.*

### Option C — Backend-authoritative resolution via bar-close heartbeat

Main indicator keeps doing what it's best at: signal generation with Reputation Engine gating. When a position is open, the main indicator fires a lightweight `BAR_UPDATE` webhook on each bar close, carrying the just-closed bar's OHLC and the signal_id(s) of currently open positions. The backend holds the authoritative position state, walks bars forward as they arrive, computes MAE/MFE/TP1/TP2/SL/EOD resolution in Python, and writes to `trade_outcomes` when the position closes out.

The Pine Script side becomes: "am I tracking any open positions? If yes, fire the heartbeat. When the backend's webhook response (or an ENTRY alert being cleared) tells me a position is closed, stop tracking it." Maybe 40 lines of new state and one new `alertcondition`. No MAE/MFE computation in Pine Script. No TP/SL resolution in Pine Script. No session-close timeout logic in Pine Script. All of that is in Python.

**This is the architecture v3.0 should have had in Workshop 3, and the reason it didn't is mine.** I followed a separation-of-concerns instinct — "the Monitor watches the chart, the backend records the data" — without interrogating whether the separation was buying anything. Finding #0 is the bill. Your keystone push is what made me read the bill.

---

## 3. WHY OPTION C

Five reasons, in descending order of how much I weight them:

**3.1 — The divergence class of bug becomes impossible, not just fixed.** Findings #0, #1, and #2 all exist because position state is maintained in Pine Script and the backend has to reconcile. In Option C, the backend *is* the position state. There is nothing to reconcile. Finding #0 can't happen because there's no second script with independent gating. Finding #1 can't happen because signal_id is assigned at the moment the position is created, not reconstructed via lookup. Finding #2 can't happen because MAE/MFE is computed in Python with clean loops that don't have Pine Script's same-bar state-variable ordering constraints. This is the test I want applied to any rearchitecture proposal: *do the existing bugs become impossible, or do they just get patched?* In Option C they become impossible.

**3.2 — Resolution logic is a finite state machine and finite state machines belong in Python.** TP/SL resolution is: "given an entry, SL, TP1, TP2, and a stream of bars, walk forward and decide the exit." That is a textbook FSM. Python lets me write an exhaustive unit test suite — concurrent positions, gap opens, TP1-and-SL-same-bar, EOD timeout, BE stop after TP1 hit, every edge case. I can replay it against arbitrary historical data by feeding a bar sequence in and asserting the exit. When Phase 3 comes and you ask "what if we trailed the runner at 0.8× ATR after TP1 instead of a hard BE stop," the answer in Option C is "rerun the resolution function with that parameter and diff the outcomes." That's an afternoon of work. In Option A or B the answer is "rewrite Pine Script, bench test it, re-observe for N days to validate." That future-iteration cost compounds.

**3.3 — Phase 3 alignment is clean.** The endgame is automated execution through TopstepX. When that day comes, the signal generator keeps firing ENTRY alerts (unchanged). The "what happened to the position" question becomes "query the broker API" instead of "ask the Monitor." In Option C, the backend is already structured to hold position state and receive updates about what's happening to open positions — swapping the update source from a Pine Script heartbeat to a broker API webhook is a handler change, not an architecture change. In Options A and B, the Monitor (Pine Script) would have to be deprecated or reconciled with broker state, which is another migration project downstream.

**3.4 — Simpler Pine Script, not more complex.** The Monitor is currently 706 lines. The "position tracking" piece of the main indicator in Option C is maybe 40 lines because all it does is know "I have open positions, fire the heartbeat." Everything else — MAE/MFE, TP/SL resolution, session timeout, priority rules for same-bar TP1+SL, intrabar conventions — all of that Pine Script goes away. The main indicator gains maybe 50-60 lines total (position tracking + heartbeat alertcondition + some state housekeeping) and loses a 706-line sibling script. Net Pine Script surface area drops by ~650 lines. That is a lot of bug-farm acreage we don't have to maintain anymore.

**3.5 — The Workshop 3 discipline rule applies in Mona's favor.** *Does this change what Mona thinks, or what we can see about what she thinks?* Mona's signal generation — the Reputation Engine, the filter conditions, the StochRSI tightening, the volume floor, the squeeze logic — none of that changes in Option C. Not a single line. The main indicator's signal-firing behavior is byte-identical pre- and post-migration. What changes is where position resolution lives: from a second Pine Script to the backend. That's purely a change in *what we can see about what Mona thinks*, not *what Mona thinks*. By the discipline rule you laid down, this is a change that ships on merit — it doesn't need observation data to justify.

---

## 4. WHAT OPTION C LOOKS LIKE CONCRETELY

### 4.1 — Signal firing (unchanged)

Main indicator fires ENTRY alerts exactly as it does today. `sig_status = 1`. All filter conditions, all Reputation Engine gating, all existing plot values. `signals_v3` writes unchanged. Week 1 Validation Protocol for signal-side fields is unchanged.

### 4.2 — Position state (moves to backend)

A new `positions` table (or in-memory dict on the backend if we don't need crash recovery in v3.0 — leaning in-memory for simplicity, persist to disk in v3.1):

```
positions:
  signal_id    INTEGER PRIMARY KEY  (foreign key to signals_v3)
  direction    INTEGER              (1=LONG, 2=SHORT)
  signal_type  INTEGER              (1=TREND, 2=SQUEEZE)
  entry_price  REAL
  sl           REAL
  tp1          REAL
  tp2          REAL
  opened_at_ms INTEGER               (bar_close_ms of entry bar)
  tp1_hit      INTEGER              (0 or 1)
  tp1_hit_bar_ms INTEGER            (nullable)
  mae_points   REAL                 (signed, negative = worse than entry)
  mae_bar_ms   INTEGER
  mfe_points   REAL                 (signed, positive = better than entry)
  mfe_bar_ms   INTEGER
  post_tp1_mae_points REAL          (non-negative, depth below TP1 after TP1 hit)
  state        INTEGER              (1=open, 2=tp1_hit, 3=closed)
```

When an ENTRY webhook arrives, the backend writes to `signals_v3` (as today) and creates a row in `positions` with the entry details. When a `BAR_UPDATE` webhook arrives, the backend iterates open positions and updates MAE/MFE/TP1/SL/TP2/BE-stop state. When a position closes, the backend writes to `trade_outcomes` with the correct `signal_id` and removes the row from `positions`.

### 4.3 — Bar update heartbeat (new alertcondition in main indicator)

```pine
hasOpenPositions = array.size(openSignalIds) > 0
fireBarUpdate = hasOpenPositions and barstate.isconfirmed

alertcondition(fireBarUpdate, title="MNQ Bar Update", message='{
  "event": "bar_update",
  "bar_close_ms": {{plot_N}},
  "open": {{open}},
  "high": {{high}},
  "low": {{low}},
  "close": {{close}},
  "open_signal_ids": "{{plot_M}}",  // comma-separated list as string, or similar encoding
  "ticker": "MNQ",
  "version": "3.0"
}')
```

The Pine Script keeps a small array of `openSignalIds` — signal IDs for positions the backend has told us are still open. When an ENTRY fires, the main indicator adds the (locally-generated, will be reconciled by backend) signal reference. When the backend acknowledges a position is closed via the webhook response or a follow-up heartbeat, the main indicator drops that ID from the array.

There's a bootstrapping question here — the Pine Script doesn't know the real `signal_id` until the backend assigns it, which creates a chicken-and-egg for the heartbeat. Two options:

(a) Main indicator identifies positions locally by `bar_close_ms` of their entry bar, not by the backend's `signal_id`. Heartbeat payload uses `open_position_entry_times: [1744416000000, 1744416900000]`. Backend maps entry times to signal_ids on its side. No chicken-and-egg.

(b) Main indicator receives `signal_id` back from TradingView alert... which it can't, because alerts are one-way.

Option (a) is the clean path. Heartbeat identifies positions by their entry bar's `bar_close_ms`, backend does the mapping. I'd write this into the spec.

### 4.4 — Resolution FSM (new Python module)

`backend/position_resolver.py` — a pure function that takes a position state and a new bar, and returns either an updated state or a close-out. Exhaustively unit-tested in `tests/test_position_resolver.py`. Covers:

- Normal TP1 hit, then TP2 hit
- TP1 hit, then BE stop hit (small win)
- TP1 hit, then EOD timeout (partial win)
- SL hit before TP1 (full loss)
- TP1 and SL same bar (TP1 wins by convention)
- Gap open beyond SL (SL hit at open)
- Gap open beyond TP2 (TP2 hit at open after TP1 hit)
- Entry bar excluded from resolution (resolution starts on bar after entry)
- MAE/MFE tracking on every bar regardless of resolution events
- EOD timeout at session close
- Bar update heartbeat arriving for a position the backend doesn't recognize (safe no-op, log `⚠️ [HEARTBEAT]`)

Every one of these is a unit test in Python. When the test suite is green, the resolver is correct by the terms of the convention document. When Phase 3 asks "what if we tighten stops," we add parameter sweeps to the test suite and rerun.

### 4.5 — `trade_outcomes` schema and Discord embeds (unchanged)

Same table. Same fields. Same embed format. The only difference is who writes the rows — the backend's resolver instead of the Monitor's webhook handler. The `#trade-journal` user experience is byte-identical. Week 1 Validation Protocol for outcome-side fields is unchanged in what it checks, just verifying against backend-generated rows instead of Monitor-generated rows.

### 4.6 — What dies

- `mona_v3_0_monitor.txt` (706 lines) — retired
- `MONA_v3_Monitor_Bench_Test.md` — retired (replaced by Python unit test suite)
- Monitor-related sections of the Week 1 Validation Protocol — simplified (no "provisional vs. trusted" Monitor phase; backend resolver is tested exhaustively before deploy)
- `find_parent_by_lookback` in the backend — retired (positions know their signal_id at creation time)
- Finding #0 — retired by construction
- Finding #1 — retired by construction
- Finding #2 — retired by construction

---

## 5. WHAT OPTION C COSTS

**5.1 — Timeline.** I estimate 5 days of work, starting Monday, deploying Thursday evening or Friday morning:

- Mon: Strategist review of this document. Assuming green light, write the Option C architecture spec document (exhaustive, enumerate-every-touch-point, per your pattern commitment from Finding #1).
- Tue: Implement the Python position resolver with full unit test suite. Implement the backend heartbeat handler and the `positions` table/state.
- Wed: Implement main indicator changes (position tracking array, bar update alertcondition, signal_id-by-entry-time encoding). Schema migration for any new fields on existing tables.
- Thu: Bench test — replay Day 1 Signal #1 through the new architecture end-to-end, verify resolution matches what v2.1.1 produced on April 10. Run the Python resolver against synthetic bar streams covering all the edge cases in §4.4.
- Fri: Deploy after market close Thursday or before market open Friday. Week 1 Validation Protocol begins Friday open.

Slip from the original Monday target is 4 days. Original Monday target was already known to be slipping to Tuesday/Wednesday with the fix-in-place path, so the real slip is 2-3 days for a substantially cleaner architecture.

**5.2 — Sunk cost on the Monitor Pine Script.** The 706 lines of `mona_v3_0_monitor.txt` get retired. That's real work that won't ship. I want to name it rather than hide it — the Monitor bench test, the parallel array design, the intrabar resolution convention, the MAE/MFE tracking logic — all of that goes away. But the work is not wasted, for two reasons. First, writing the Monitor is what taught us the architecture was wrong — if we'd skipped straight to Option C in Workshop 3, we might have gone through a different-shaped mistake. Second, the Monitor's resolution logic, its priority rules, and its edge-case handling all map directly to the Python resolver's test cases. The design work translates; only the Pine Script implementation gets retired.

**5.3 — Railway outage risk.** This is the tradeoff I want to flag explicitly, because it's real. In Option A/B, the Monitor runs on TradingView's infrastructure. If Railway goes down, TradingView keeps firing alerts, the Monitor keeps tracking, and when Railway comes back we catch up on the backlog. In Option C, if Railway goes down for an hour during market hours, the backend misses that hour's BAR_UPDATE heartbeats, and any position that resolved during the outage is a gap in the data.

Two mitigations I'd build in:

(a) **Recovery on reconnect.** When Railway comes back up, the backend pulls the last N bars from a data feed (Finnhub — we have an API key slotted for this in the morning briefing work) and replays them through the resolver for any open positions. This is a standard pattern and well-scoped.

(b) **Conservative no-resolution-during-gaps rule.** If the backend notices a time gap in the heartbeat stream larger than one bar interval, it refuses to resolve any position that would have resolved during the gap and marks the position as `❌ [GAP]` instead. This is the orphan-clean equivalent for the outage case — better to fail cleanly than resolve based on stale assumptions.

Railway has been reliable in observation so far, and the prop firm trading day is relatively short (6.25 hours). Expected downtime cost is very low, and the mitigations cover the worst case. I don't think this is a blocker, but I want you to see it and push back if you do.

**5.4 — A new class of bugs to watch for.** Every architecture has its own bug farm. Option C's is: "what if the heartbeat stops firing while a position is still open?" The main indicator could crash, TradingView could pause, the alert could hit a rate limit. The backend has to notice the absence of a heartbeat and decide what to do. I'd add a staleness check — if the backend hasn't received a heartbeat in more than one bar interval for an open position, it logs `⚠️ [STALE]` and escalates. That's part of the resolver spec.

Option C's bugs are *visible* bugs. Missing heartbeats, stale positions, gap-clean resolutions — all of them produce log lines in `#mona-log`. Option A/B's bugs are *silent* bugs — phantom outcomes grafted onto wrong parents, MAE under-reporting on resolution bars, signal_id corruption on concurrent positions. The silent class is worse because it requires walking the code to find it. Option C trades silent bugs for visible bugs. That trade is worth making.

**5.5 — I'm proposing this under a finding, not in a vacuum.** I want to name this explicitly because it's the first thing I'd push back on if our roles were reversed. "You found a bug on Saturday and by Sunday you want to rearchitect? Is this real architectural reasoning or is it scope creep under deploy pressure?"

My honest answer: if you'd asked me in Workshop 3 "should position resolution live in Pine Script or Python?" I don't know if I would have landed on Option C or on Option A. I remember the Workshop 3 discussion being about "separation of concerns" and "the Monitor as a measurement instrument" and those framings felt right at the time. What I didn't interrogate was whether those framings were buying architectural clarity or just metaphor. Finding #0 forced the interrogation. Walking it carefully today, I think Option C is the right answer on the merits — not because it's new, but because it retires a whole class of failure mode that the other options only patch.

The test I'd apply: if Finding #0 hadn't happened, would Option C still be the right call? I believe yes, because the maintainability tax of two-Pine-Script lockstep was always going to surface — it just would have surfaced the first time we tried to change the Reputation Engine after v3.0 shipped, instead of the weekend before v3.0 shipped. Finding #0 made the cost visible earlier than it would have otherwise, which is the review triangle paying its dividend.

That said — I want your eyes on this framing. If you think I'm rationalizing scope creep under deploy pressure, push back hard. I'd rather fix in place (Option A) with your read than rearchitect (Option C) over your objection.

---

## 6. OPEN QUESTIONS FOR STRATEGIST REVIEW

**6.1 —** Is the Railway outage risk acceptable with the Finnhub recovery and gap-clean mitigations in §5.3, or does it argue for keeping Pine Script as a redundant tracker during a transition period?

**6.2 —** The signal_id-by-entry-time encoding in §4.3 (option a) — is there a cleaner way to identify open positions in the heartbeat that I'm not seeing? Pine Script's constraints on payload format are real, but I don't want "I couldn't think of a better way" to lock us into a suboptimal encoding if one exists.

**6.3 —** The in-memory-vs-persisted question for the `positions` table in §4.2. I'm leaning in-memory for v3.0 because it's simpler and crash recovery can come in v3.1, but in-memory means a Railway restart loses any open positions' state. Counterargument: positions typically resolve within a couple hours on 15M MNQ, so Railway-restart-during-open-position is a narrow window. Your read?

**6.4 —** Does the 5-day timeline feel right for this scope? I might be under-budgeting the bench test phase. If you think Thursday deploy is optimistic, I'd rather hear it now than push hard to hit it and cut corners on Wednesday.

**6.5 —** The meta-question: does this pass your smell test as a real architectural decision, or does it read as rearchitecture-under-pressure? I'm asking directly because I want the pushback if it's there.

---

## 7. WHAT I'M NOT PROPOSING

So there's no ambiguity:

- Not touching the Reputation Engine. Stays in the main indicator, unchanged.
- Not touching signal firing logic, filter conditions, StochRSI, volume floor, ADX threshold, session window, anything in the signal side.
- Not touching the `signals_v3`, `evaluations`, or `trade_outcomes` schemas (except possibly adding fields I find I need during implementation, which would be called out in the spec).
- Not touching the Discord embed format or the `#trade-journal` user experience.
- Not touching the Discord reorganization from Workshop 3.
- Not touching the layer-specific logging convention (📥 📝 📤).
- Not restarting the 30-day observation clock — the clock hasn't started yet because v3.0 hasn't deployed. The clock starts when Option C (or A) ships with data flowing.

The thesis is narrow: **position resolution moves from Pine Script to Python. Everything else stays.**

---

## 8. CLOSING

The review triangle worked. You pushed a keystone question, the push forced a walkthrough, the walkthrough found Finding #0, and Finding #0 forced a harder question — not "how do I fix this bug" but "why does this class of bug exist at all." The answer to the harder question led here.

I'm bringing this to you before code because it's the right sequence under the spec-before-code commitment I made yesterday. This document *is* the pre-spec. If it passes your review, the implementation spec comes next. If it doesn't, we fall back to Option A and deploy Tuesday or Wednesday with the orphan-clean fix, and Option C becomes a Phase 3 conversation.

Your call on pace. I'm ready for either path. I'm writing this one clearly because I think it's the right answer and I want you to see the full reasoning, not because I'm attached to shipping it.

See you on the other side of your review.

*— Dev Trader, The Mona Project*
*April 12, 2026*
