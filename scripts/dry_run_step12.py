"""
Step 12 dry-run — fire a synthetic webhook sequence end-to-end.

Sequence (Option C §2.3 happy path, LONG TREND):
  ENTRY  @ T    entry_price=25000, sl=24980, tp1=25030, tp2=25050
  HB     @ T+1  bar stays below TP1  (no transition)
  HB     @ T+2  bar hits TP1         (OPEN -> TP1_HIT)
  HB     @ T+3  bar inside the range (no transition)
  HB     @ T+4  bar hits TP2         (TP1_HIT -> CLOSED, trade_outcomes row)

Asserts:
  - signals_v3 has one row, with bar_close_ms populated.
  - positions went through state 1 (OPEN) -> 2 (TP1_HIT) -> 3 (CLOSED).
  - trade_outcomes has exactly one row, exit_reason=TP2_HIT, tp1_hit=1,
    tp2_hit=1, final_pnl_points = 2*30 + 1*50 = 110.
  - Discord embeds: one ENTRY embed + one OUTCOME embed captured.

Run:
  python scripts/dry_run_step12.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB_PATH = os.path.join(_ROOT, "dry_run.db")
os.environ["DB_PATH"] = DB_PATH

# Stub channel IDs so the webhook handler actually invokes the embed path.
os.environ.setdefault("CH_MONA_ALERTS",  "DRYRUN_ALERTS_CHANNEL")
os.environ.setdefault("CH_MONA_JOURNAL", "DRYRUN_JOURNAL_CHANNEL")
os.environ.setdefault("CH_MONA_LOG",     "DRYRUN_LOG_CHANNEL")

# Reset DB before the module imports so startup init_db works on a clean file.
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Point the backend at our local DB before import.
import mona_v3_0_backend as backend
backend.DB_PATH = DB_PATH
backend.DB_DIR = _ROOT

# Stub out Discord — capture sends instead of HTTP-ing Discord.
_sent_discord = []


async def _capture_discord(channel_id, content=None, embed=None):
    _sent_discord.append({"channel_id": channel_id, "content": content, "embed": embed})
    return {"id": "fake"}


backend.send_discord_message = _capture_discord

from fastapi.testclient import TestClient  # noqa: E402
from backend.schema import BAR_INTERVAL_MS  # noqa: E402


# ---- Canonical moments for the dry-run ----
def _et_ms(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


T   = _et_ms(2026, 4, 13, 10, 0)       # entry bar
T_1 = T + 1 * BAR_INTERVAL_MS          # 10:15
T_2 = T + 2 * BAR_INTERVAL_MS          # 10:30
T_3 = T + 3 * BAR_INTERVAL_MS          # 10:45
T_4 = T + 4 * BAR_INTERVAL_MS          # 11:00


ENTRY_PAYLOAD = {
    "status": "ENTRY",
    "sig_dir": 1,        # LONG
    "sig_type": 1,       # TREND
    "sig_status": 1,
    "entry_price": 25000.0,
    "sl":          24980.0,
    "tp1":         25030.0,
    "tp2":         25050.0,
    "stop_pts":    20.0,
    "rr1":         1.5,
    "rr2":         2.5,
    "atr":         20.0,
    "vwap":        24995.0,
    "adx":         27.5,
    "volume_ratio": 1.4,
    "reputation":  "ELIGIBLE",
    "consecutive_stops": 0,
    "bar_close_ms": T,
    "timestamp":    "2026-04-13 10:00:00",
    "opened_at_ts": "2026-04-13 10:00:00",
    "ticker":       "MNQ",
    "version":      "3.0",
}


def _hb(bar_close_ms, o, h, l, c, slot_time):
    return {
        "sig_status":     3,
        "bar_close_ms":   bar_close_ms,
        "bar_open":       o,
        "bar_high":       h,
        "bar_low":        l,
        "bar_close":      c,
        "pos_slot_1_time": slot_time,
        "pos_slot_2_time": 0,
        "pos_slot_3_time": 0,
        "pos_slot_4_time": 0,
        "ticker": "MNQ",
        "version": "3.0",
    }


def _post(client, payload):
    resp = client.post("/webhook", data=json.dumps(payload).encode())
    print(f"  HTTP {resp.status_code}  body={resp.json()}")
    return resp


def _dump_state(label):
    print(f"\n---- {label} ----")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT signal_id, bar_close_ms FROM signals_v3"):
            print(f"  signals_v3: {dict(row)}")
        for row in conn.execute(
            "SELECT signal_id, state, tp1_hit, mae_points, mfe_points, "
            "last_heartbeat_bar_ms, heartbeats_processed, exit_reason, "
            "final_pnl_points FROM positions"
        ):
            print(f"  positions:  {dict(row)}")
        for row in conn.execute(
            "SELECT signal_id, tp1_hit, tp2_hit, sl_hit, be_stop_hit, "
            "exit_reason, final_pnl_points, mae_points, mfe_points, "
            "time_in_trade_min FROM trade_outcomes"
        ):
            print(f"  outcomes:   {dict(row)}")


def main():
    print(f"[dry-run] DB = {DB_PATH}")
    with TestClient(backend.app) as client:  # triggers startup hook
        print("\n[1] ENTRY")
        _post(client, ENTRY_PAYLOAD)
        _dump_state("after ENTRY")

        print("\n[2] HEARTBEAT T+1  (normal bar, no transition)")
        _post(client, _hb(T_1, 25000, 25010, 24995, 25005, T))
        _dump_state("after HB T+1")

        print("\n[3] HEARTBEAT T+2  (TP1 hit — high crosses 25030)")
        _post(client, _hb(T_2, 25005, 25032, 25000, 25025, T))
        _dump_state("after HB T+2 (TP1)")

        print("\n[4] HEARTBEAT T+3  (runner holds above BE, no transition)")
        _post(client, _hb(T_3, 25025, 25040, 25010, 25035, T))
        _dump_state("after HB T+3")

        print("\n[5] HEARTBEAT T+4  (TP2 hit — high crosses 25050)")
        _post(client, _hb(T_4, 25035, 25052, 25030, 25048, T))
        _dump_state("after HB T+4 (TP2 close)")

    _assert()

    def _ascii(s):
        return (s or "").encode("ascii", "replace").decode("ascii")

    print("\n---- captured Discord embeds ----")
    for sent in _sent_discord:
        chan = sent["channel_id"]
        if sent["embed"]:
            title = _ascii(sent["embed"].get("title") or "")
            print(f"  channel={chan or 'none'}  EMBED  title={title!r}")
        elif sent["content"]:
            preview = _ascii(sent["content"].splitlines()[0][:120])
            print(f"  channel={chan or 'none'}  TEXT   {preview!r}")

    print("\n[dry-run] OK — all assertions passed.")


def _assert():
    errors = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        n_signals = conn.execute(
            "SELECT COUNT(*) FROM signals_v3"
        ).fetchone()[0]
        if n_signals != 1:
            errors.append(f"signals_v3 count != 1 (got {n_signals})")

        pos = conn.execute("SELECT * FROM positions").fetchone()
        if pos is None:
            errors.append("no positions row")
        else:
            if pos["state"] != 3:
                errors.append(f"positions.state != 3 (got {pos['state']})")
            if pos["tp1_hit"] != 1:
                errors.append(f"positions.tp1_hit != 1 (got {pos['tp1_hit']})")
            if pos["exit_reason"] != "TP2_HIT":
                errors.append(
                    f"positions.exit_reason != TP2_HIT (got {pos['exit_reason']})"
                )

        out = conn.execute("SELECT * FROM trade_outcomes").fetchall()
        if len(out) != 1:
            errors.append(f"trade_outcomes count != 1 (got {len(out)})")
        else:
            r = out[0]
            # final_pnl = 2*30 + 1*(25050-25000) = 60 + 50 = 110
            if abs(r["final_pnl_points"] - 110.0) > 1e-6:
                errors.append(
                    f"final_pnl_points != 110.0 (got {r['final_pnl_points']})"
                )
            if r["tp1_hit"] != 1 or r["tp2_hit"] != 1:
                errors.append(
                    f"tp1_hit/tp2_hit != (1,1) (got "
                    f"{r['tp1_hit']}/{r['tp2_hit']})"
                )
            if r["sl_hit"] != 0 or r["be_stop_hit"] != 0:
                errors.append("sl_hit or be_stop_hit set on TP2 outcome")

    embed_titles = [
        e["embed"]["title"] for e in _sent_discord if e["embed"]
    ]
    has_entry = any("TREND CONTINUATION" in t for t in embed_titles)
    has_outcome = any("TP1" in t and "TP2" in t for t in embed_titles)
    if not has_entry:
        errors.append(f"no ENTRY embed posted (titles={embed_titles})")
    if not has_outcome:
        errors.append(f"no OUTCOME embed posted (titles={embed_titles})")

    if errors:
        print("\n[dry-run] FAIL")
        for e in errors:
            safe = e.encode("ascii", "replace").decode("ascii")
            print(f"  FAIL: {safe}")
        print("\n  captured embeds:")
        for sent in _sent_discord:
            if sent["embed"]:
                t = sent["embed"].get("title", "") or ""
                print("    EMBED " + t.encode("ascii", "replace").decode("ascii"))
        sys.exit(1)


if __name__ == "__main__":
    main()
