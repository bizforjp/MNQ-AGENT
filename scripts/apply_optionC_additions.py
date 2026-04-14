"""
Apply Option C additive edits to mona_v3_0.txt.txt, producing
mona_v3_0_optionC.txt. No existing signal/filter/Reputation Engine logic is
modified — every edit either inserts a new block, adds a new field, or
appends a new plot/alertcondition.

See §7 of MONA_v3_OptionC_Spec.md and Wave 3 of the revision package.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "mona_v3_0.txt.txt")
DST = os.path.join(ROOT, "mona_v3_0_optionC.txt")


def read_src() -> str:
    with open(SRC, "r", encoding="utf-8") as f:
        return f.read()


def write_dst(text: str) -> None:
    with open(DST, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def must_replace(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(
            f"[FAIL {label}] expected exactly one match; got {text.count(old)}"
        )
    return text.replace(old, new)


# ----------------------------------------------------------------------
# 1. Add entryBarCloseMs field to the SignalTracker type (§9.3)
# ----------------------------------------------------------------------
def add_entry_bar_close_ms_field(text: str) -> str:
    old = (
        "    float  lastSignalPrice = na\n"
        "    float  lastFollowTarget = na\n"
        "    int    lastSignalDir   = 0\n"
    )
    new = (
        "    float  lastSignalPrice = na\n"
        "    float  lastFollowTarget = na\n"
        "    int    lastSignalDir   = 0\n"
        "    int    entryBarCloseMs = 0       // NEW v3.0 Option C: §9.3\n"
        "                                      // write-once at ENTRY auth from time_close,\n"
        "                                      // read-once at EVAL firing into plot_33.\n"
    )
    return must_replace(text, old, new, label="SignalTracker field")


# ----------------------------------------------------------------------
# 2. Add sigParentBarCloseMs (plot 33 source) to webhook data variables
# ----------------------------------------------------------------------
def add_sig_parent_variable(text: str) -> str:
    old = "float sigHtfBull    = 0.0          // NEW v3.0\n"
    new = (
        "float sigHtfBull    = 0.0          // NEW v3.0\n"
        "float sigBarCloseMs       = 0.0    // NEW v3.0 Option C: plot 32 (time_close)\n"
        "float sigParentBarCloseMs = 0.0    // NEW v3.0 Option C: plot 33 (EVAL parent)\n"
    )
    return must_replace(text, old, new, label="sig webhook vars")


# ----------------------------------------------------------------------
# 3. Populate entryBarCloseMs at each of the four ENTRY authorization sites
#    per tracker. Uses a surgical anchor per site so we do not touch the
#    Reputation Engine's decision logic — only append one assignment.
# ----------------------------------------------------------------------
ENTRY_ANCHORS = [
    # (anchor, label)
    ("        trendTrk.lastSignalDir    := 1\n\n    else if trendHasShort",
     "trend real LONG"),
    ("        trendTrk.lastSignalDir    := 2\n\n// ---- 5. Ghost signal detection ----",
     "trend real SHORT"),
    ("        trendTrk.lastSignalDir    := 1\n    else if trendHasShort\n",
     "trend ghost LONG"),
    ("        trendTrk.lastSignalDir    := 2\n\n\n// ============",
     "trend ghost SHORT"),
    ("        sqzTrk.lastSignalDir    := 1\n\n    else if sqzHasShort",
     "sqz real LONG"),
    ("        sqzTrk.lastSignalDir    := 2\n\n// ---- 5. Ghost signal detection ----",
     "sqz real SHORT"),
    ("        sqzTrk.lastSignalDir    := 1\n    else if sqzHasShort\n",
     "sqz ghost LONG"),
    ("        sqzTrk.lastSignalDir    := 2\n\n\n// ============",
     "sqz ghost SHORT"),
]


def stamp_entry_bar_close_ms_per_site(text: str) -> str:
    """
    The anchors above are intentionally overlapping with each other in ways
    that are safe because we insert-after rather than replace. For each
    anchor we insert one line right after the last `.lastSignalDir := N`
    assignment that identifies the site.
    """
    # Walk the file and insert after each lastSignalDir assignment in
    # deterministic textual order (top-to-bottom).
    pattern = re.compile(
        r"(        (?P<tracker>trendTrk|sqzTrk)\.lastSignalDir\s*:=\s*[12]\n)"
    )

    def repl(m):
        tracker = m.group("tracker")
        return m.group(1) + f"        {tracker}.entryBarCloseMs  := time_close\n"

    new_text, n = pattern.subn(repl, text)
    if n != 8:
        raise SystemExit(
            f"[FAIL entryBarCloseMs stamp] expected 8 sites; got {n}"
        )
    return new_text


# ----------------------------------------------------------------------
# 4. Read entryBarCloseMs into sigParentBarCloseMs at each EVAL firing
# ----------------------------------------------------------------------
def publish_parent_bar_close_ms_at_eval(text: str) -> str:
    """
    Four EVAL fire sites, each terminated by `fireTrendAlert := true` or
    `fireSqzAlert := true`. We insert `sigParentBarCloseMs := trk.entryBarCloseMs`
    BEFORE that line so the value is set when the alert carries it out.
    """
    # Trend real eval: the `fireTrendAlert := true` at ~line 303 is the
    # first occurrence in the source. To disambiguate from the entry-site
    # `fireTrendAlert := true`, we anchor on the preceding `sigIsGhost := 0.0`.
    patches = [
        (
            "    sigIsGhost     := 0.0\n    fireTrendAlert := true\n    trendEvalDone  := true\n",
            "    sigIsGhost     := 0.0\n"
            "    sigParentBarCloseMs := trendTrk.entryBarCloseMs\n"
            "    fireTrendAlert := true\n    trendEvalDone  := true\n",
            "trend real eval fire",
        ),
        (
            "    sigIsGhost     := 1.0\n    fireTrendAlert := true\n\n    trendTrk.ghostPending := false\n",
            "    sigIsGhost     := 1.0\n"
            "    sigParentBarCloseMs := trendTrk.entryBarCloseMs\n"
            "    fireTrendAlert := true\n\n    trendTrk.ghostPending := false\n",
            "trend ghost eval fire",
        ),
        (
            "    sigIsGhost     := 0.0\n    fireSqzAlert := true\n    sqzEvalDone  := true\n",
            "    sigIsGhost     := 0.0\n"
            "    sigParentBarCloseMs := sqzTrk.entryBarCloseMs\n"
            "    fireSqzAlert := true\n    sqzEvalDone  := true\n",
            "sqz real eval fire",
        ),
        (
            "    sigIsGhost     := 1.0\n    fireSqzAlert := true\n\n    sqzTrk.ghostPending := false\n",
            "    sigIsGhost     := 1.0\n"
            "    sigParentBarCloseMs := sqzTrk.entryBarCloseMs\n"
            "    fireSqzAlert := true\n\n    sqzTrk.ghostPending := false\n",
            "sqz ghost eval fire",
        ),
    ]
    for old, new, label in patches:
        text = must_replace(text, old, new, label=label)
    return text


# ----------------------------------------------------------------------
# 5. Insert heartbeat support block (§7.3-§7.5) + new plots + new
#    alertcondition. Placed right before the existing plots section so
#    the Pine-script reading order remains logical.
# ----------------------------------------------------------------------
HEARTBEAT_BLOCK = """
// ============================================================================
// HEARTBEAT PLOT SUPPORT — v3.0 Option C (§7)
// ============================================================================
// Additive only. Does not modify the Reputation Engine, signal logic, filters,
// session window, or any existing alertcondition. Slot state is measurement
// infrastructure (per §1.4 discipline rule) — Pine records positions the
// Reputation Engine already authorized, but does not re-decide.
//
// Four slots max (§3.4 / OI-04: N=4, revisit on observation data). Slot
// release uses the local closure heuristic (§7.5): original SL breach,
// TP2 breach, or session end. BE_STOP and EOD_TIMEOUT closures are handled
// by the backend resolver — Pine's wasted heartbeats are a safe no-op at
// the backend per §5.12 TC12.

var int   posSlot1Time = 0
var float posSlot1Sl   = 0.0
var float posSlot1Tp2  = 0.0
var int   posSlot1Dir  = 0

var int   posSlot2Time = 0
var float posSlot2Sl   = 0.0
var float posSlot2Tp2  = 0.0
var int   posSlot2Dir  = 0

var int   posSlot3Time = 0
var float posSlot3Sl   = 0.0
var float posSlot3Tp2  = 0.0
var int   posSlot3Dir  = 0

var int   posSlot4Time = 0
var float posSlot4Sl   = 0.0
var float posSlot4Tp2  = 0.0
var int   posSlot4Dir  = 0

// ---- §7.4 Slot addition on ENTRY ----
// Runs AFTER the Reputation Engine has set fireTrendAlert / fireSqzAlert and
// populated sigDir / sigSL / sigTP2. Lowest-indexed empty slot wins.
if (fireTrendAlert or fireSqzAlert) and sigStatus == 1.0
    nowMs  = time_close
    nowSl  = sigSL
    nowTp2 = sigTP2
    nowDir = int(sigDir)
    if posSlot1Time == 0
        posSlot1Time := nowMs
        posSlot1Sl   := nowSl
        posSlot1Tp2  := nowTp2
        posSlot1Dir  := nowDir
    else if posSlot2Time == 0
        posSlot2Time := nowMs
        posSlot2Sl   := nowSl
        posSlot2Tp2  := nowTp2
        posSlot2Dir  := nowDir
    else if posSlot3Time == 0
        posSlot3Time := nowMs
        posSlot3Sl   := nowSl
        posSlot3Tp2  := nowTp2
        posSlot3Dir  := nowDir
    else if posSlot4Time == 0
        posSlot4Time := nowMs
        posSlot4Sl   := nowSl
        posSlot4Tp2  := nowTp2
        posSlot4Dir  := nowDir
    // else: overflow (§7.5.2) — backend staleness check will catch it.

// ---- §7.5 Slot release — local closure heuristic ----
// A slot is released when the bar's range crossed original SL or TP2.
// BE stops post-TP1 and EOD timeouts are NOT detected here by design.
releaseSlot1 = posSlot1Time != 0 and ((posSlot1Dir == 1 and (low <= posSlot1Sl or high >= posSlot1Tp2)) or (posSlot1Dir == 2 and (high >= posSlot1Sl or low <= posSlot1Tp2)))
releaseSlot2 = posSlot2Time != 0 and ((posSlot2Dir == 1 and (low <= posSlot2Sl or high >= posSlot2Tp2)) or (posSlot2Dir == 2 and (high >= posSlot2Sl or low <= posSlot2Tp2)))
releaseSlot3 = posSlot3Time != 0 and ((posSlot3Dir == 1 and (low <= posSlot3Sl or high >= posSlot3Tp2)) or (posSlot3Dir == 2 and (high >= posSlot3Sl or low <= posSlot3Tp2)))
releaseSlot4 = posSlot4Time != 0 and ((posSlot4Dir == 1 and (low <= posSlot4Sl or high >= posSlot4Tp2)) or (posSlot4Dir == 2 and (high >= posSlot4Sl or low <= posSlot4Tp2)))
if releaseSlot1
    posSlot1Time := 0
    posSlot1Sl   := 0.0
    posSlot1Tp2  := 0.0
    posSlot1Dir  := 0
if releaseSlot2
    posSlot2Time := 0
    posSlot2Sl   := 0.0
    posSlot2Tp2  := 0.0
    posSlot2Dir  := 0
if releaseSlot3
    posSlot3Time := 0
    posSlot3Sl   := 0.0
    posSlot3Tp2  := 0.0
    posSlot3Dir  := 0
if releaseSlot4
    posSlot4Time := 0
    posSlot4Sl   := 0.0
    posSlot4Tp2  := 0.0
    posSlot4Dir  := 0

// ---- §7.5.3 Session end clears all slots ----
sessionEnded = inSession[1] and not inSession
if sessionEnded
    posSlot1Time := 0
    posSlot1Sl   := 0.0
    posSlot1Tp2  := 0.0
    posSlot1Dir  := 0
    posSlot2Time := 0
    posSlot2Sl   := 0.0
    posSlot2Tp2  := 0.0
    posSlot2Dir  := 0
    posSlot3Time := 0
    posSlot3Sl   := 0.0
    posSlot3Tp2  := 0.0
    posSlot3Dir  := 0
    posSlot4Time := 0
    posSlot4Sl   := 0.0
    posSlot4Tp2  := 0.0
    posSlot4Dir  := 0

// ---- Populate sigBarCloseMs on every bar (feeds plot 32) ----
sigBarCloseMs := time_close


"""


def insert_heartbeat_block(text: str) -> str:
    """Insert the block immediately before the HIDDEN DATA PLOTS header."""
    anchor = "// ========================== HIDDEN DATA PLOTS ===============================\n"
    if text.count(anchor) != 1:
        raise SystemExit("[FAIL heartbeat block] anchor mismatch")
    return text.replace(anchor, HEARTBEAT_BLOCK + anchor, 1)


# ----------------------------------------------------------------------
# 6. Append 10 new plots (32-41) after existing plot 31
# ----------------------------------------------------------------------
NEW_PLOTS = """
// ---- v3.0 Option C plots (32-41) — §7 + §3.6 ----
plot(sigBarCloseMs,              "bar_close_ms",        display=display.none)  // 32
plot(nz(sigParentBarCloseMs, 0), "parent_bar_close_ms", display=display.none)  // 33
plot(nz(open,  0),               "bar_open",            display=display.none)  // 34
plot(nz(high,  0),               "bar_high",            display=display.none)  // 35
plot(nz(low,   0),               "bar_low",             display=display.none)  // 36
plot(nz(close, 0),               "bar_close",           display=display.none)  // 37
plot(posSlot1Time,               "pos_slot_1_time",     display=display.none)  // 38
plot(posSlot2Time,               "pos_slot_2_time",     display=display.none)  // 39
plot(posSlot3Time,               "pos_slot_3_time",     display=display.none)  // 40
plot(posSlot4Time,               "pos_slot_4_time",     display=display.none)  // 41
"""


def append_new_plots(text: str) -> str:
    anchor = 'plot(nz(sigIsGhost, 0),    "sig_is_ghost",    display=display.none)     // 31\n'
    if text.count(anchor) != 1:
        raise SystemExit("[FAIL new plots] anchor mismatch")
    return text.replace(anchor, anchor + NEW_PLOTS, 1)


# ----------------------------------------------------------------------
# 7. Extend the existing anySignalFired message template with bar_close_ms
#    and parent_bar_close_ms fields, and add the new fireHeartbeat
#    alertcondition (§7.6 Case A — separate alertconditions, locked per
#    Wave 3 of the revision pass after TV quota verified).
# ----------------------------------------------------------------------
OLD_ALERT_TAIL = '"is_ghost":{{plot_31}},"ticker":"MNQ","version":"3.0"}\')'
NEW_ALERT_TAIL = (
    '"is_ghost":{{plot_31}},'
    '"bar_close_ms":{{plot_32}},'
    '"parent_bar_close_ms":{{plot_33}},'
    '"ticker":"MNQ","version":"3.0"}\')'
)

HEARTBEAT_ALERT_BLOCK = """

// ---- v3.0 Option C HEARTBEAT alertcondition (§7.6 Case A) ----
// Separate alertcondition locked per Wave 3: OI-06 resolved to Essential plan
// (19 alert headroom), decision stands on its own merit from failure-case
// elimination (§6.2 same-bar ENTRY/HEARTBEAT collision removed). The
// `not (fireTrendAlert or fireSqzAlert)` guard keeps ENTRY priority on
// collision bars; the entry-bar-exclusion rule (PF4, TC9) makes the
// newly-entered position unresolvable on its entry bar anyway.
hasOpenSlot  = posSlot1Time != 0 or posSlot2Time != 0 or posSlot3Time != 0 or posSlot4Time != 0
fireHeartbeat = hasOpenSlot and barstate.isconfirmed and not (fireTrendAlert or fireSqzAlert)

alertcondition(fireHeartbeat, title="MNQ Heartbeat", message='{"sig_status":3,"bar_close_ms":{{plot_32}},"bar_open":{{plot_34}},"bar_high":{{plot_35}},"bar_low":{{plot_36}},"bar_close":{{plot_37}},"pos_slot_1_time":{{plot_38}},"pos_slot_2_time":{{plot_39}},"pos_slot_3_time":{{plot_40}},"pos_slot_4_time":{{plot_41}},"ticker":"MNQ","version":"3.0"}')
"""


def extend_alertcondition(text: str) -> str:
    text = must_replace(
        text, OLD_ALERT_TAIL, NEW_ALERT_TAIL, label="signal alertcondition tail"
    )
    # Insert the heartbeat alertcondition right after the existing one.
    anchor = NEW_ALERT_TAIL + "\n"
    if text.count(anchor) != 1:
        raise SystemExit("[FAIL heartbeat alert] anchor mismatch")
    return text.replace(anchor, anchor + HEARTBEAT_ALERT_BLOCK, 1)


# ----------------------------------------------------------------------
def main():
    text = read_src()
    steps = [
        ("1. add entryBarCloseMs field to SignalTracker", add_entry_bar_close_ms_field),
        ("2. add sig* webhook variables (plot 32/33 sources)", add_sig_parent_variable),
        ("3. stamp entryBarCloseMs at each ENTRY site (8x)", stamp_entry_bar_close_ms_per_site),
        ("4. publish sigParentBarCloseMs at each EVAL fire (4x)", publish_parent_bar_close_ms_at_eval),
        ("5. insert slot state + closure heuristic + session clear", insert_heartbeat_block),
        ("6. append plots 32-41", append_new_plots),
        ("7. extend signal alertcondition + add fireHeartbeat alertcondition", extend_alertcondition),
    ]
    for label, fn in steps:
        text = fn(text)
        print(f"OK {label}")
    write_dst(text)
    new_lines = text.count("\n")
    src_lines = read_src().count("\n")
    print(f"\nWrote {DST}\n  {src_lines} -> {new_lines} lines "
          f"(+{new_lines - src_lines} additive)")


if __name__ == "__main__":
    main()
