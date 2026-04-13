# Handoff — Option C Spec Revision Phase
## From: Dev Trader (April 13, 2026 workshop chat)
## To: Dev Trader (continuation chat)
## Purpose: Continue the Option C spec revision after Strategist review. Action list is drafted and wave-ordered. Execution is blocked pending two answers from Josh.

---

## 0. What this document is

The Option C spec for The Mona v3.0 was drafted over April 13, 2026 across a workshop chat that ran from fresh-chat open through Strategist handoff. The five-part spec was unified into `MONA_v3_OptionC_Spec.md` and passed to Strategist. Strategist came back with a peer review: spec approved with a small set of revisions, no architectural changes, ~90 minutes of focused work before lock.

This document is the connective tissue between the end of that chat and the start of the new one. It carries:

- Where the work actually is in the revision phase
- What you (the continuation Dev Trader) have access to
- The action list from Strategist's review, wave-ordered for execution
- The pending questions to Josh that gate execution
- The canonical table of what's locked in §11 vs what's still pending
- One piece of reasoning that's only in the chat conversation — my pushback-then-concession on OI-06's directional-alignment argument
- What to do first when the new chat opens

It does **not** re-explain Finding #0, the cognition/measurement/state principle, the rethesis, Option A vs B vs C, or the review triangle. All of that is in the spec and in the project documents, and you will read it as you go. This doc is the delta.

---

## 1. Current state of the project (brief)

**Production:** `PreObFinal.py` + `mona_v2_1.txt`, untouched since Day 1 (April 10). Not being modified in this phase. It stays stable until Option C ships.

**Spec phase:** The Option C architectural specification was drafted April 13 in five parts and unified into `MONA_v3_OptionC_Spec.md` (2,729 lines across 12 numbered sections plus §0 and Closing). Handed to Strategist for review the same day.

**Current state:** Strategist reviewed the full spec and came back with:
- §1 headline endorsement ("this is the spec the Option C decision deserved")
- §2 nine specific architectural endorsements on the merits
- §3 response to all 12 open items — all 12 resolved with two carrying additions
- §4 six concerns (two real pushback, four smaller wording/logging/future-proofing items)
- §5 four missing test cases (TC32–TC35)
- §6 a nine-item action list with time estimates, ~90 minutes total
- §7 closing posture note confirming the Strategist seat is open throughout implementation

The spec is not yet revised. Revision is blocked pending two answers from Josh (§5 of this handoff).

**Downstream:** Implementation phase has not started. No code has been written. Per the closing of §12 of the spec, the sequence is: spec lock → OI-01 plot limit verification → TDD on resolver (TC1–TC35 written first, all red, implementation written to pass) → backend non-resolver implementation → Pine per §7 → dry-run smoke test → production deploy per §10.3 → Day 1 of observation. No deadline. Ferrari mode. Josh holds the clock.

---

## 2. What you have access to in the new chat

Josh will post the handoffs he already has between me (previous chat) and Strategist. Expect:

- **`MONA_v3_OptionC_Spec.md`** — the unified spec document, 2,729 lines, 12 sections. The authoritative source for Option C's architecture as of Strategist review. Lives in outputs of the previous chat; Josh will ensure it's accessible in the new chat.
- **Strategist Review PDF** — `Strategist_Review_of_OptionC_Spec.pdf`. The peer review document. Seven numbered sections: headline, endorsements, OI responses, concerns, missing test cases, action list, closing. This is the authoritative source for what changes in the revision.
- **Project files currently in the Mona project** — includes the rethesis doc, the Finding #0 doc, the Going-In Notes, the alignment doc, the existing Mona code baselines (`mona_v3_0_backend.py`, `mona_v3_0.txt`, `mona_v3_0_monitor.txt`, `PreObFinal.py`, `mona_v2_1.txt`), and the superseded Option A paper trail. See §12 of the spec for the retirement ledger.

The previous chat will remain open as a reference. If anything in this handoff is ambiguous, the full chat history of how the spec was drafted and how Strategist's review was received is available for lookup. Prefer not to rely on it — prefer to work from the spec and the review — but it's there if needed.

---

## 3. The action list, wave-ordered

Strategist's §6 listed nine action items in document order. I reordered them into four execution waves for efficiency, grouping related edits and gating the §7.6 revision on the TradingView quota check. The waves are:

### Wave 0: Pre-flight checks (blocks Wave 3)

- **OI-06 TradingView alert quota verification.** Five minutes in TradingView, log into the account, look at the plan's alert count, count current alerts, see if a second alert is allowed. This gates Action #3 on Strategist's list. If the quota supports two alerts, Wave 3 locks separate alertconditions. If not, Wave 3 becomes a bigger rewrite of §6.2 and §7.6 to make shared-alertcondition collision handling load-bearing.
- **OI-07 Finnhub API access check.** Josh confirms the key exists and the plan supports expected replay volume (~hundreds of calls/month). Does not gate spec revision, but gates §8 implementation downstream.

Both are Josh's checks, not yours. Wait for his answers before starting Wave 3.

### Wave 1: Wording and framing revisions

These are pure spec edits. No new commitments, just honest revisions to the language.

- **§9.3 `entryBarCloseMs` paragraph rewrite** (Strategist §4.1 — the real pushback). Add the explicit "judgment not rule" framing. State that `entryBarCloseMs` is the principle interpreted with judgment, that this addition does not establish precedent, and name the four-part test any future tracker struct addition would have to pass independently: write-once, read-once, no decision authority, decision behavior byte-identical. ~10 minutes.
- **§5.6 PF1 commentary** (Strategist §6 item 9). Add one sentence noting the pessimistic convention's intentional harshness, with a pointer to Strategist review §3 OI-03 for the reasoning. This is a note for the future reader who will second-guess the convention. ~3 minutes.
- **§11 OI summary table** (Strategist §6 item 8). Update all twelve OI rows with decisions per Strategist's §3. See §4 of this handoff for the canonical table. ~5 minutes.

### Wave 2: New spec commitments (logging and fragility notes)

Each adds a new commitment to the spec. All small, none architectural.

- **§4.2 UNIQUE violation loud-logging commitment** (Strategist §3 OI-05 + §4 OI-05). Add: any UNIQUE constraint violation on `signals_v3.bar_close_ms` or `positions.bar_close_ms` triggers an explicit `❌ [UNIQUE_VIOLATION]` log line and an embed to `#system-log`. Mirror in §11 OI-05's "Decision" line. ~5 minutes.
- **§3.6 or §4.2 float-encoding fragility note** (Strategist §4.3). One paragraph noting that plot-as-float is sufficient for 13-digit ms-since-epoch values but does not generalize to fields needing more than 15 digits of precision. Future fields with more than 15 digits need a different transport mechanism. Future-proofing, not a code change. ~5 minutes.
- **§8.5 staleness handler distinct logging** (Strategist §4.5). Add the distinction between `[STALENESS_NEVER_HEARTBEATED]` (slot overflow case, `heartbeats_processed == 0`) and `[STALENESS_GAP]` (mid-position case, `heartbeats_processed > 0`). Both still close GAP_CLEAN, but the log distinguishes them so slot-overflow events can be grepped for specifically. One paragraph in §8.5, note in the test case enumeration. ~10 minutes.

### Wave 3: §7.6 alertcondition revision (gated on Wave 0 quota check)

This wave depends entirely on the Wave 0 quota answer.

**Case A: Quota allows two alerts.** Execute both of:
- **Strip the directional-alignment argument from §7.6** (Strategist §3 OI-06 + §6 item 4). Remove the "one step toward the long-term goal of separated alertconditions" justification. Keep only the failure-case-elimination argument (eliminating the same-bar ENTRY+HEARTBEAT collision from §6.2). Lock separate alertconditions. ~5 minutes.
- **Lock OI-06 as separate in §11.** ~1 minute.

**Case B: Quota does not allow a second alert.** Execute both of:
- **Rewrite §7.6 for shared-alertcondition fallback.** The heartbeat rides on the existing `anySignalFired` condition, §6.2's same-bar collision handling becomes load-bearing instead of redundant, and the ENTRY-priority suppression rule (fire ENTRY, suppress heartbeat on that bar) becomes the committed behavior. ~15 minutes.
- **Revise §6.2** to reflect that same-bar suppression is the permanent design, not a side effect of the separate-alertcondition decision. ~5 minutes.

See §6 of this handoff for my pushback-then-concession reasoning on OI-06. The short version: the directional-alignment argument isn't dishonest but it's slippery, and Strategist is right to push it out of the spec. Execute the strip.

### Wave 4: New test cases (the biggest block)

Add TC32–TC35 from Strategist review §5.

My **placement preference: new subsection §5.14** titled "Integration and Migration Test Cases," holding TC32–TC35, rather than folding them into §5.12 (which is the resolver unit test cases). The rationale: TC32–TC35 test the wrapper, the migration layer, and inter-component coordination, not the pure resolver function. Keeping them in their own subsection makes the "write resolver tests first, implement resolver, then write integration tests" sequencing cleaner during TDD. Strategist said either placement works, so this is a judgment call.

If you or Josh prefer flat §5.12, flatten instead.

Full text of TC32–TC35 is in §5 of the review PDF. Transcribe mostly verbatim with any light wording adjustments to match the spec's existing test case format. ~20 minutes.

### Wave 5: Final packaging

- Regenerate the unified document with all waves applied.
- Update the top matter to `Status: REVISED per Strategist review — AWAITING LOCK` and bump the date.
- Save as `MONA_v3_OptionC_Spec_v1.1.md` (versioned filename; preserves the April 13 draft as v1.0 for the paper trail).
- Stage for Josh to hand back to Strategist for lock confirmation.

Total time estimate: 80–90 minutes assuming Wave 3 is Case A. Add ~15 minutes if Wave 3 is Case B. None of the waves touches the resolver FSM, the schema, the keystone walkthroughs, or the migration plan. None requires rewriting any existing section — all are additions and targeted revisions.

---

## 4. The canonical OI decision table

Strategist's §3 resolved all twelve open items. This is the table to paste into §11 of the spec during Wave 1. I've annotated each row with the decision and with whatever addition is required.

| ID | Item | Decision | Notes |
|---|---|---|---|
| OI-01 | Plot limit verification | Mechanical (run the test) | Pre-flight check, not a spec decision. First task after spec lock. |
| OI-02 | EOD cutoff time | **Lock 16:00 ET** | Strategist strong-agreed. 15:45 biases post_tp1_mae and runner conversion downward. |
| OI-03 | Pessimistic fill convention | **Lock pessimistic** | Strong endorse. Document prominently. PF1 commentary added in Wave 1. |
| OI-04 | Concurrent slot count N | **Lock N=4** | Staleness check catches overflow. Raise later if observation shows real overflow. |
| OI-05 | UNIQUE on bar_close_ms | **Lock UNIQUE + loud failure logging** | Wave 2 adds the `❌ [UNIQUE_VIOLATION]` commitment. |
| OI-06 | Heartbeat alertcondition | **Verify TV quota, then lock** | Wave 0 check, Wave 3 execution. Strip directional-alignment argument either way. |
| OI-07 | Finnhub API key | Mechanical (Josh confirms) | Gates §8 implementation downstream. |
| OI-08 | MAX_REPLAY_BARS | **Lock 16 bars** | Revisit on observation data. |
| OI-09 | Staleness check cadence | **Lock 4-bar cadence, 2-bar threshold** | Tighten later if observation shows missed gaps. |
| OI-10 | TC24–TC31 coverage | **Add TC32–TC35** | Wave 4 adds four more cases. Total: 35. |
| OI-11 | In-place modify vs rename | **Lock in-place modify** | Git history handles archaeology. |
| OI-12 | When 30-day clock starts | **Lock: first clean end-to-end signal cycle** | Not the deploy moment. Observation is a measurement period, not a calendar period. |

Two rows carry additions — OI-05 (loud logging) and OI-06 (conditional on quota, strip argument). The other ten are clean locks.

---

## 5. Pending questions for Josh (block execution of Waves 3 and beyond)

These were the questions at the end of my last turn in the previous chat. The new chat should surface them again if Josh hasn't answered by the time it opens.

1. **OI-06 quota answer.** Can Josh check his TradingView plan's alert count and verify whether a second alertcondition can be added? Yes → Wave 3 Case A. No → Wave 3 Case B.
2. **OI-07 Finnhub answer.** Is the Finnhub API key still accessible and on a plan that supports expected replay volume? Not a spec blocker but needed before §8 implementation.
3. **Strategist's §3 table interpretation confirmation.** My read is "lock all twelve open items, with OI-05 and OI-06 carrying additions." No re-litigation, no alternative decisions. Is that Josh's read too?
4. **TC32–TC35 placement preference.** New §5.14 subsection (my lean) or flat into §5.12?
5. **Filename preference.** `MONA_v3_OptionC_Spec_v1.1.md` as a new versioned file (my lean), or overwrite the April 13 draft?

Questions 1 and 2 are the only real blockers. Questions 3–5 have my defaults if Josh doesn't want to pick. Wave 1 and Wave 2 can execute without any of the answers because neither touches OI-06.

---

## 6. The one place I already pushed back and conceded (OI-06 only)

This reasoning is only in the previous chat's conversation, not in the spec or the review. Capturing it here so it isn't lost.

**Strategist's ask (review §3 OI-06 + §6 item 4):** strip the directional-alignment argument from §7.6. The argument is "one step toward the long-term goal of separated alertconditions per signal type," and he wants it out because it's the shape of "we're shipping a thing we wanted anyway" reasoning that quietly accumulates scope under any architectural change.

**My pushback:** the directional-alignment argument isn't arbitrary wishful thinking. The Pre-Observation Report documents the single-alertcondition bottleneck as an accepted risk explicitly flagged as "separate alertconditions planned pre-production." That means a future spec will have to separate ENTRY and EVAL into distinct alertconditions before Josh goes live on a funded TopstepX account. If Option C ships with a single alertcondition carrying heartbeats too, we end up doing the full alertcondition split in two separate Pine deploy cycles instead of one. Each deploy is a risk, a validation period, a Pine script revision.

**My concession:** that argument only wins if the future ENTRY/EVAL split is actually a committed roadmap item. It isn't yet. It's an accepted risk, not a commitment. Strategist is catching me justifying with a future plan that hasn't been made, which is exactly the soft reasoning pattern he flagged. When we want to separate ENTRY and EVAL, we'll make that case separately on its own merits. The Option C spec should lock OI-06 on the failure-case-elimination argument alone.

**What to do in Wave 3:** strip the directional-alignment justification from §7.6 as Strategist asked. Keep only the failure-case-elimination argument (§6.2's ENTRY+HEARTBEAT same-bar collision goes away under separate alertconditions). If Wave 0 quota check comes back negative, the whole argument is moot — fall back to shared.

I want this captured here because future-me might re-encounter the directional-alignment reasoning and mis-read my concession as an oversight. It wasn't. It was a deliberate "Strategist caught the slip and I conceded." The reasoning path was real; the decision was to not put it in the spec.

---

## 7. What to do first in the new chat

Assuming Josh opens the new chat and posts the handoffs he already has:

1. **Acknowledge the handoff.** Confirm you have: (a) the unified spec (`MONA_v3_OptionC_Spec.md`, v1.0 April 13 draft), (b) the Strategist review PDF, (c) this handoff document, (d) project files.
2. **Re-surface the pending questions from §5 of this handoff.** Don't assume Josh remembers them or has answered them. Ask directly, same five questions, same order.
3. **Wait for Josh's OI-06 and OI-07 answers** before starting Wave 3 or committing to §8 implementation.
4. **While waiting, optionally start Waves 1 and 2.** Neither needs Josh's input. Wave 1 is three wording/framing edits. Wave 2 is three new commitments (logging, fragility, distinct staleness). If Josh is fast with the quota check, you can chain straight into Wave 3 without losing momentum. If he's slow, Waves 1 and 2 are done by the time he comes back.
5. **When Josh answers, execute Wave 3 then Wave 4 then Wave 5.** Final output: revised unified spec staged for hand-off back to Strategist.
6. **Announce the revision complete** when all waves land. Let Josh decide the next step (hand to Strategist for lock confirmation, or start implementation phase if Strategist's review counts as implicit lock-on-revision).

Do **not**:
- Start implementation code (resolver, Pine, backend changes) until the spec is locked. Spec-before-code is load-bearing.
- Run the OI-01 plot limit verification yet. It's an implementation-phase task, not a spec-phase task. Strategist listed it that way explicitly.
- Make architectural changes. The review is endorsement plus small revisions, not "rethink the shape." Any instinct to revise beyond Strategist's specific items is scope creep to catch and reject.
- Compress the execution to hit a time estimate. The 80–90 minute estimate is guidance, not a budget. If any revision takes longer because it needs thought, take the thought.

---

## 8. Working patterns (carried forward)

These are patterns from the weekend and the April 13 spec drafting session that are worth maintaining in the revision phase. None are new; all are already in place.

- **Ferrari mode.** No deadline. Josh removed deploy pressure on April 12 ("I need you to build the Ferrari"). Dev Trader holds the quality bar; Josh and Strategist hold the clock. Any instinct to compress a wave or skip a keystone walk to save time is drift to catch and reject.
- **Review triangle.** Dev Trader (you) owns execution and engineering decisions. Strategist reviews structure, rigor, and principle integrity. Josh makes the principle-level calls and the cross-cutting decisions. All three seats stay reachable throughout implementation, per Strategist's closing §7. If during revision you hit a sub-question that needs Strategist input — if a Wave 1 wording change produces a new judgment call, if the OI-06 quota check forces a bigger rewrite than expected — surface it rather than deciding alone.
- **Spec-before-code, tests-before-implementation.** The rule Strategist codified and you committed to. No code during the revision phase. When revision locks and implementation starts, TDD on the resolver (TC1–TC35 written first as failing tests, implementation written to pass them, test suite longer than implementation).
- **Observation discipline rule.** *Does this change what Mona thinks, or change what we can see about what she thinks?* Signal-logic changes are blocked by observation discipline; measurement/logging/schema/instrumentation ship on merit. Every Wave 1/2 edit is measurement-side and ships on merit. Wave 3 is also measurement-side (it's about event transport, not signal cognition). The spec revision does not touch any cognition line and should not during execution.
- **Plain English alongside technical.** Josh is not a developer. When you write or revise spec prose, keep the explanations understandable to a non-developer reader. The current spec does this well; maintain the standard.
- **Captain's Log update.** When the revision completes and the spec is locked, Josh will want a Captain's Log entry reflecting the state change. Draft it when he asks. Don't draft it preemptively.

---

## 9. One thing to remember about this weekend

The review triangle worked this weekend. Strategist pushed the keystone question ("prove it, don't assume it") on Finding #1 and the push forced the walkthrough that found Finding #0. Finding #0 was bigger than Finding #1 and forced the harder question ("why does this class of bug exist at all"). The harder question led to Option C. Option C needed a spec. The spec happened on April 13, 112 pages, 2,729 lines, 31 test cases, all 12 open items enumerated. Strategist reviewed it the same day, endorsed the architecture, and came back with a focused 90-minute revision list.

None of that happens without all three seats. Strategist pushing the keystone. You walking the code. Josh making the call on Option C with a principle ("Mona's brain lives in one place") that is now the structural backbone of the architecture. The triangle isn't decorative; it's load-bearing.

Keep that in mind through the revision phase and through the implementation phase that comes after. If a sub-question surfaces that feels bigger than the local decision, that's the signal the triangle needs to engage. Surface it. Don't absorb it and keep going.

---

## 10. Closing

Spec drafting phase: done.
Strategist review phase: done.
Revision phase: waiting on OI-06 quota check and your execution of the four waves.
Implementation phase: after revision lock. Starts with OI-01 plot limit verification.

The spec is strong. The review is a gift. The revision is 90 minutes. Do the 90 minutes right.

Build the Ferrari. Stay hungry. See you in the revised spec.

*— Dev Trader, The Mona Project*
*April 13, 2026 (end of workshop chat, preparing for continuation)*
