"""
The Mona v2.1 — Backend Server
Receives TradingView webhook alerts, logs to SQLite, posts to Discord.
Supports both alert() and alertcondition() webhook formats.

Fixes applied (April 8, 2026 audit):
  - JSON sanitization before parsing (BLOCKER 1 fix)
  - Raw payload logging on parse failure (diagnostic)
  - Error isolation: DB commit before Discord post (no duplicate rows)
  - Timestamp format normalized for SQLite compatibility
  - Timezone via zoneinfo (handles EST/EDT automatically)
  - Health check endpoint

Updates applied (April 9, 2026 — Workshop #1):
  - Template tag sanitization (catches unresolved {{plot_NN}} from TradingView)

Pre-observation hardening (April 9, 2026 — Workshop #2):
  - Webhook auth token validation via URL query param (?token=xxx)
  - SQLite WAL mode enabled in init_db() for durability + concurrent reads
"""

import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import aiohttp

# =============================================================
# CONFIG
# =============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Webhook auth token — shared secret with TradingView alert URL
# If unset, validation is skipped (backwards-compatible deployment)
# To enable: set env var in Railway, then update TradingView alert URL to ?token=xxx
WEBHOOK_AUTH_TOKEN = os.getenv("WEBHOOK_AUTH_TOKEN")

# Channel IDs — match env vars in Railway
CHANNELS = {
    "alerts-high":   os.getenv("CH_ALERTS_HIGH"),
    "trade-journal": os.getenv("CH_TRADE_JOURNAL"),
    "system-log":    os.getenv("CH_SYSTEM_LOG"),
}

DISCORD_API = "https://discord.com/api/v10"

# Persistent volume on Railway, local fallback for dev
DB_DIR = "/app/data" if os.path.exists("/app") else "."
DB_PATH = os.path.join(DB_DIR, "signals.db")

# Eastern timezone — handles EST/EDT automatically
ET = ZoneInfo("America/New_York")


# =============================================================
# JSON SANITIZATION — BLOCKER 1 FIX
# =============================================================

def sanitize_json(raw: str) -> str:
    """
    Clean TradingView alertcondition payload before JSON parsing.
    TradingView's template engine can insert NaN, Infinity, empty values,
    or fail to resolve {{plot()}} tags entirely. This catches all known cases.
    """
    cleaned = raw
    # Catch unresolved TradingView template tags that leak through as literal strings
    # (e.g. {{plot_20}} or {{plot("name")}}) — replace with 0
    # This is the BLOCKER 1 root cause from April 9, 2026
    cleaned = re.sub(r'\{\{[^}]*\}\}', '0', cleaned)
    # Replace JavaScript-style invalid values with valid JSON
    cleaned = re.sub(r'\bNaN\b', '0', cleaned)
    cleaned = re.sub(r'-Infinity\b', '0', cleaned)
    cleaned = re.sub(r'\bInfinity\b', '0', cleaned)
    # Fix empty values: "key":, or "key":}
    cleaned = re.sub(r':\s*,', ':0,', cleaned)
    cleaned = re.sub(r':\s*}', ':0}', cleaned)
    # Fix trailing commas before closing brace: ,}
    cleaned = re.sub(r',\s*}', '}', cleaned)
    return cleaned


# =============================================================
# HELPERS
# =============================================================

def safe_float(val, default=0.0):
    """Safely convert TradingView plot values to float. Handles NaN, strings, None."""
    if val is None:
        return default
    try:
        f = float(val)
        return default if f != f else f  # NaN check
    except (TypeError, ValueError):
        return default


def get_et_now():
    """Current time in Eastern. Handles EST/EDT automatically."""
    return datetime.now(ET)


def get_utc_timestamp():
    """UTC timestamp formatted for SQLite compatibility."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# =============================================================
# TRANSLATOR — alertcondition numeric codes → string labels
# =============================================================

def translate_signal_codes(data):
    """
    Convert alertcondition numeric codes to string labels.
    If data already has string labels (from alert()), passes through unchanged.
    """
    if "sig_dir" not in data:
        return data  # Already in string format, no translation needed

    dir_map    = {1: "LONG", 2: "SHORT"}
    type_map   = {1: "TREND", 2: "SQUEEZE"}
    status_map = {1: "ENTRY", 2: "EVAL_RESULT"}
    rep_map    = {0: "ELIGIBLE", 1: "GROUNDED", 2: "EXTENDED"}
    result_map = {1: "PASS", 2: "FAIL"}

    data["signal"]      = dir_map.get(int(safe_float(data.get("sig_dir"))), "UNKNOWN")
    data["signal_type"] = type_map.get(int(safe_float(data.get("sig_type"))), "UNKNOWN")
    data["status"]      = status_map.get(int(safe_float(data.get("sig_status"))), "UNKNOWN")
    data["reputation"]  = rep_map.get(int(safe_float(data.get("sig_rep"))), "ELIGIBLE")
    data["consecutive_stops"] = int(safe_float(data.get("consecutive_stops", data.get("sig_stops", 0))))
    data["follow_thru_target"] = safe_float(data.get("ft_target"))

    if safe_float(data.get("sig_result")) > 0:
        data["result"] = result_map.get(int(safe_float(data["sig_result"])), "UNKNOWN")

    # Derive conditions string from signal type
    data["conditions"] = "VWAP|Stack|HTF|Stoch|ADX" if data["signal_type"] == "TREND" else "SQZ|Price|Cross|Stoch"

    # Normalize all numeric fields
    for key in ["price", "sl", "tp1", "tp2", "stop_pts", "atr", "vwap",
                "ema9", "ema21", "adx", "stoch_k", "near_sr", "rr1", "rr2", "volume_ratio"]:
        if key in data:
            data[key] = safe_float(data[key])

    return data


# =============================================================
# DATABASE
# =============================================================

def init_db():
    """Initialize SQLite database with full schema."""
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        # Enable Write-Ahead Logging — better durability + concurrent reads.
        # WAL is persistent on the file, so this only needs to run once,
        # but running on every startup is harmless and safer.
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')  # Balanced durability/performance for WAL

        # Full signal context — every indicator value at time of signal
        c.execute('''CREATE TABLE IF NOT EXISTS signals (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT NOT NULL,
            signal            TEXT NOT NULL,
            signal_type       TEXT NOT NULL,
            price             REAL,
            sl                REAL,
            tp1               REAL,
            tp2               REAL,
            stop_pts          REAL,
            atr               REAL,
            vwap              REAL,
            ema9              REAL,
            ema21             REAL,
            adx               REAL,
            stoch_k           REAL,
            near_sr           REAL,
            rr1               REAL,
            rr2               REAL,
            conditions        TEXT,
            volume_ratio      REAL,
            reputation        TEXT,
            consecutive_stops INTEGER,
            follow_thru       TEXT DEFAULT 'PENDING',
            outcome           TEXT DEFAULT 'PENDING',
            exit_price        REAL,
            pnl_points        REAL,
            notes             TEXT
        )''')

        # Evaluation history linked to parent signals
        c.execute('''CREATE TABLE IF NOT EXISTS eval_results (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id         INTEGER,
            timestamp         TEXT NOT NULL,
            signal            TEXT NOT NULL,
            signal_type       TEXT NOT NULL,
            price             REAL,
            follow_thru_target REAL,
            result            TEXT NOT NULL,
            reputation        TEXT,
            consecutive_stops INTEGER,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        )''')

        # --- Column migration ---
        # If the signals table was created by an older main.py version,
        # it may be missing columns added later (e.g. rr1, rr2).
        # ALTER TABLE adds them without touching existing data.
        c.execute("PRAGMA table_info(signals)")
        existing_cols = {row[1] for row in c.fetchall()}

        migrations = {
            "rr1":          "REAL",
            "rr2":          "REAL",
            "volume_ratio": "REAL",
            "near_sr":      "REAL",
            "conditions":   "TEXT",
            "outcome":      "TEXT DEFAULT 'PENDING'",
            "exit_price":   "REAL",
            "pnl_points":   "REAL",
            "notes":        "TEXT",
        }

        for col, col_type in migrations.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_type}")
                print(f"Migration: added column '{col}' to signals table")

        conn.commit()


# =============================================================
# DISCORD HELPERS
# =============================================================

async def send_discord_message(channel_id, content=None, embed=None):
    """Send message to Discord via Bot Token + REST API."""
    if not channel_id or not DISCORD_BOT_TOKEN:
        return None

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                else:
                    print(f"Discord error {resp.status}: {await resp.text()}")
                    return None
    except Exception as e:
        print(f"Discord send failed: {e}")
        return None


# =============================================================
# EMBED BUILDERS
# =============================================================

def build_entry_embed(data, signal_id):
    """Build actionable entry embed with Signal ID in footer."""
    direction   = data.get("signal", "UNKNOWN")
    signal_type = data.get("signal_type", "TREND")
    is_long     = direction == "LONG"
    is_squeeze  = signal_type == "SQUEEZE"

    color = (0x00CED1 if is_squeeze and is_long else
             0x9B59B6 if is_squeeze else
             0x00FF00 if is_long else 0xFF0000)

    emoji      = "\U0001f7e2" if is_long else "\U0001f534"
    type_label = "SQUEEZE BREAKOUT" if is_squeeze else "TREND CONTINUATION"

    fields = []

    if data.get("price"):
        levels_text = (
            f"\U0001f4cd **Entry:** {data['price']:.2f}\n"
            f"\U0001f6d1 **Stop:** {safe_float(data.get('sl')):.2f} ({safe_float(data.get('stop_pts')):.1f} pts)\n"
            f"\U0001f3af **TP1:** {safe_float(data.get('tp1')):.2f} (R:R {safe_float(data.get('rr1')):.2f})\n"
            f"\U0001f680 **TP2:** {safe_float(data.get('tp2')):.2f} (R:R {safe_float(data.get('rr2')):.2f})"
        )
        fields.append({"name": "\U0001f4b0 Trade Levels", "value": levels_text, "inline": False})

    fields.append({
        "name": "\U0001f9e0 Mona's Conviction",
        "value": f"**{data.get('reputation', 'ELIGIBLE')}** (Stops: {data.get('consecutive_stops', 0)})",
        "inline": False
    })

    et_now = get_et_now()
    return {
        "title": f"{emoji} MNQ {type_label} — {direction}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"The Mona v2.1 \u2022 Signal ID: #{signal_id} \u2022 {et_now.strftime('%I:%M %p ET')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_eval_embed(data, signal_id):
    """Build evaluation result embed with parent Signal ID."""
    is_pass = data.get("result") == "PASS"
    color   = 0x00FF00 if is_pass else 0xFF0000
    icon    = "\u2705" if is_pass else "\u274c"

    fields = [
        {"name": "Signal Price", "value": f"{safe_float(data.get('price')):.2f}", "inline": True},
        {"name": "FT Target",   "value": f"{safe_float(data.get('follow_thru_target')):.2f}", "inline": True},
        {"name": "\U0001f9e0 Reputation", "value": f"**{data.get('reputation', 'ELIGIBLE')}** (Stops: {data.get('consecutive_stops', 0)})", "inline": False},
    ]

    return {
        "title": f"{icon} EVAL {data.get('result', 'UNKNOWN')} — {data.get('signal_type', '')} {data.get('signal', '')}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"The Mona v2.1 \u2022 Parent ID: #{signal_id if signal_id else 'N/A'}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# =============================================================
# WEBHOOK HANDLER
# =============================================================

app = FastAPI(title="The Mona v2.1")


@app.get("/")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "version": "2.1", "auth_enabled": bool(WEBHOOK_AUTH_TOKEN)}


@app.post("/webhook")
async def receive_webhook(request: Request, token: str = ""):
    # ---- PHASE 0: Auth (rejects before any parsing or DB work) ----
    # If WEBHOOK_AUTH_TOKEN env var is unset, skip validation entirely (backwards-compat).
    # If set, the request must include ?token=xxx matching the env var or get a 401.
    if WEBHOOK_AUTH_TOKEN:
        if not token or token != WEBHOOK_AUTH_TOKEN:
            print(f"Auth failed: missing or invalid token from {request.client.host if request.client else 'unknown'}")
            raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    raw_message = body.decode("utf-8").strip()

    if not raw_message:
        raise HTTPException(status_code=400, detail="Empty message")

    # ---- PHASE 1: Parse (can fail — log raw payload if it does) ----

    try:
        cleaned = sanitize_json(raw_message)
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Log the raw payload so we can see exactly what broke
        error_msg = f"\u274c **JSON Parse Error** at char {e.pos}:\n```\n{raw_message[:800]}\n```"
        print(f"JSON parse error: {e}")
        print(f"Raw payload: {raw_message}")
        await send_discord_message(CHANNELS.get("system-log"), content=error_msg)
        raise HTTPException(status_code=400, detail=f"JSON parse error at char {e.pos}: {str(e)}")

    # ---- PHASE 2: Translate + DB Write (must succeed before Discord) ----

    data = translate_signal_codes(data)

    status      = data.get("status", "ENTRY")
    signal_type = data.get("signal_type", "UNKNOWN")
    direction   = data.get("signal", "UNKNOWN")
    timestamp   = get_utc_timestamp()

    signal_id = None
    embed = None

    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()

            # ============ ROUTE 1: ENTRY — actionable alert ============
            if status == "ENTRY":
                c.execute('''INSERT INTO signals
                    (timestamp, signal, signal_type, price, sl, tp1, tp2, stop_pts,
                     atr, vwap, ema9, ema21, adx, stoch_k, near_sr,
                     rr1, rr2, conditions, volume_ratio,
                     reputation, consecutive_stops)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (timestamp, direction, signal_type,
                     safe_float(data.get("price")),
                     safe_float(data.get("sl")),
                     safe_float(data.get("tp1")),
                     safe_float(data.get("tp2")),
                     safe_float(data.get("stop_pts")),
                     safe_float(data.get("atr")),
                     safe_float(data.get("vwap")),
                     safe_float(data.get("ema9")),
                     safe_float(data.get("ema21")),
                     safe_float(data.get("adx")),
                     safe_float(data.get("stoch_k")),
                     safe_float(data.get("near_sr")),
                     safe_float(data.get("rr1")),
                     safe_float(data.get("rr2")),
                     data.get("conditions", ""),
                     safe_float(data.get("volume_ratio")),
                     data.get("reputation", "ELIGIBLE"),
                     int(safe_float(data.get("consecutive_stops")))))

                signal_id = c.lastrowid
                conn.commit()

                embed = build_entry_embed(data, signal_id)

            # ============ ROUTE 2: EVAL_RESULT — trade journal ============
            elif status == "EVAL_RESULT":
                # Lookback Matcher: find parent signal by type + direction + 2hr window
                c.execute('''SELECT id FROM signals
                             WHERE signal_type = ? AND signal = ?
                               AND timestamp >= strftime('%Y-%m-%d %H:%M:%S', 'now', '-2 hours')
                             ORDER BY id DESC LIMIT 1''',
                          (signal_type, direction))
                row = c.fetchone()
                signal_id = row[0] if row else None

                # Update parent signal's follow_thru field
                if signal_id:
                    c.execute('''UPDATE signals SET follow_thru = ? WHERE id = ?''',
                              (data.get("result", "UNKNOWN"), signal_id))

                # Write eval_results row
                c.execute('''INSERT INTO eval_results
                    (signal_id, timestamp, signal, signal_type, price,
                     follow_thru_target, result, reputation, consecutive_stops)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (signal_id, timestamp, direction, signal_type,
                     safe_float(data.get("price")),
                     safe_float(data.get("follow_thru_target")),
                     data.get("result", "UNKNOWN"),
                     data.get("reputation", "ELIGIBLE"),
                     int(safe_float(data.get("consecutive_stops")))))

                conn.commit()

                embed = build_eval_embed(data, signal_id)

    except Exception as e:
        # Database error — log it, don't proceed to Discord
        error_msg = f"\u274c **DB Error:** {str(e)}"
        print(f"Database error: {e}")
        await send_discord_message(CHANNELS.get("system-log"), content=error_msg)
        raise HTTPException(status_code=500, detail=str(e))

    # ---- PHASE 3: Discord posting (non-critical — failures logged, not raised) ----

    if embed:
        target_channel = CHANNELS.get("alerts-high") if status == "ENTRY" else CHANNELS.get("trade-journal")
        try:
            await send_discord_message(target_channel, embed=embed)
        except Exception as e:
            print(f"Discord embed post failed: {e}")
            await send_discord_message(
                CHANNELS.get("system-log"),
                content=f"\u26a0\ufe0f **Discord post failed** for {status} #{signal_id}: {str(e)}"
            )

    # System log — always fires, failure is silent
    try:
        await send_discord_message(
            CHANNELS.get("system-log"),
            content=f"\U0001f4e5 `{get_et_now().strftime('%I:%M %p ET')}` {status}: {signal_type} {direction}"
                    + (f" | Signal #{signal_id}" if signal_id else "")
        )
    except Exception:
        pass  # System log failure is truly non-critical

    return {"status": "ok", "action": status, "signal_id": signal_id}


# =============================================================
# STARTUP
# =============================================================

@app.on_event("startup")
async def startup():
    init_db()
    print(f"Database initialized at {DB_PATH}")
    print(f"Webhook auth: {'ENABLED' if WEBHOOK_AUTH_TOKEN else 'DISABLED (set WEBHOOK_AUTH_TOKEN env var to enable)'}")
    et_now = get_et_now()
    auth_status = "🔒 AUTH" if WEBHOOK_AUTH_TOKEN else "🔓 OPEN"
    await send_discord_message(
        CHANNELS.get("system-log"),
        content=f"\U0001f7e2 **The Mona v2.1 DB Connected** — {et_now.strftime('%I:%M %p ET')} • {auth_status} • WAL ON"
    )
