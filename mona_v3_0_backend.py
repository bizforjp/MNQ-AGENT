"""
The Mona v3.0 — Backend Server
Three-table schema | Hypothetical execution tracking | Rich EVAL embeds

Architecture philosophy:
  - signals_v3       → The Mona's opinion (immutable, frozen at signal time)
  - evaluations      → The Reputation Engine's grade (includes ghost evals)
  - trade_outcomes   → Reality's verdict under mechanical 3/2/1 execution plan
                       (populated by TP/SL Monitor Pine Script, never by a human)

Inherits everything that worked in v2.1:
  - JSON sanitization with template tag catcher
  - Error isolation: parse → DB commit → Discord post
  - Timestamp format compatible with SQLite datetime()
  - zoneinfo for EST/EDT
  - Health check endpoint
  - Webhook auth token (optional via env var)
  - SQLite WAL mode

New in v3.0:
  - Three-table schema with suffixed primary table (signals_v3) for safe rollback
  - v2.x tables left untouched — automatic rollback safety net
  - New route: TRADE_OUTCOME → trade_outcomes (from TP/SL Monitor)
  - Ghost evals stored silently (is_ghost=1), never posted to Discord
  - Orphan evals rejected + logged (no more Parent ID #N/A rows)
  - Rich EVAL embed: pulls parent signal data for full lifecycle context
  - Rich OUTCOME embed: hypothetical P&L breakdown under 3/2/1 plan
  - New env var scheme: CH_MONA_ALERTS / CH_MONA_JOURNAL / CH_MONA_LOG
    (with backwards-compat fallback to v2.x env var names)
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

VERSION = "3.0"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WEBHOOK_AUTH_TOKEN = os.getenv("WEBHOOK_AUTH_TOKEN")

# ---- Channel resolution with v2.x fallback ----
# Primary: new CH_MONA_* env vars
# Fallback: old CH_* env vars (so a missed Railway update doesn't break deploy)
def _resolve_channel(new_var: str, old_var: str) -> str:
    return os.getenv(new_var) or os.getenv(old_var) or ""

CHANNELS = {
    "alerts":        _resolve_channel("CH_MONA_ALERTS",      "CH_ALERTS_HIGH"),
    "trade-journal": _resolve_channel("CH_MONA_JOURNAL",     "CH_TRADE_JOURNAL"),
    "performance":   _resolve_channel("CH_MONA_PERFORMANCE", "CH_PERFORMANCE"),
    "mona-log":      _resolve_channel("CH_MONA_LOG",         "CH_SYSTEM_LOG"),
}

DISCORD_API = "https://discord.com/api/v10"

# Persistent volume on Railway, local fallback for dev
DB_DIR = "/app/data" if os.path.exists("/app") else "."
DB_PATH = os.path.join(DB_DIR, "signals.db")

# Eastern timezone — handles EST/EDT automatically
ET = ZoneInfo("America/New_York")


# =============================================================
# JSON SANITIZATION
# =============================================================

def sanitize_json(raw: str) -> str:
    """
    Clean TradingView webhook payload before JSON parsing.
    TradingView's template engine can leak NaN, Infinity, empty values,
    or fail to resolve {{plot()}} tags. This catches all known cases.
    """
    cleaned = raw
    # Unresolved TradingView template tags (e.g. {{plot_20}}) → 0
    cleaned = re.sub(r'\{\{[^}]*\}\}', '0', cleaned)
    # JavaScript-style invalid numbers → 0
    cleaned = re.sub(r'\bNaN\b', '0', cleaned)
    cleaned = re.sub(r'-Infinity\b', '0', cleaned)
    cleaned = re.sub(r'\bInfinity\b', '0', cleaned)
    # Empty values: "key":, or "key":}
    cleaned = re.sub(r':\s*,', ':0,', cleaned)
    cleaned = re.sub(r':\s*}', ':0}', cleaned)
    # Trailing commas before closing brace
    cleaned = re.sub(r',\s*}', '}', cleaned)
    return cleaned


# =============================================================
# HELPERS
# =============================================================

def safe_float(val, default=0.0):
    """Safely convert values to float. Handles NaN, strings, None."""
    if val is None:
        return default
    try:
        f = float(val)
        return default if f != f else f  # NaN check
    except (TypeError, ValueError):
        return default


def safe_int(val, default=0):
    """Safely convert values to int."""
    return int(safe_float(val, default))


def get_et_now():
    """Current time in Eastern. Handles EST/EDT automatically."""
    return datetime.now(ET)


def get_utc_timestamp():
    """UTC timestamp formatted for SQLite datetime() compatibility."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def fmt_et(ts_str: str) -> str:
    """Convert a SQLite-format UTC timestamp to a readable ET string."""
    try:
        dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime('%I:%M %p ET')
    except Exception:
        return ts_str


# =============================================================
# TRANSLATOR — webhook payload → normalized data dict
# =============================================================

def translate_payload(data: dict) -> dict:
    """
    Convert Pine Script alertcondition numeric codes to string labels.
    v3.0 handles three payload types: ENTRY, EVAL_RESULT, TRADE_OUTCOME.
    """
    # If already has string labels (from alert() syntax), pass through
    if "signal" in data and "signal_type" in data and "status" in data:
        return _normalize_numeric_fields(data)

    dir_map    = {1: "LONG",    2: "SHORT"}
    type_map   = {1: "TREND",   2: "SQUEEZE"}
    status_map = {1: "ENTRY",   2: "EVAL_RESULT", 3: "TRADE_OUTCOME"}
    rep_map    = {0: "ELIGIBLE", 1: "GROUNDED",   2: "EXTENDED"}
    result_map = {1: "PASS",    2: "FAIL"}
    exit_map   = {1: "SL_FULL", 2: "TP1_BE", 3: "TP1_TP2", 4: "EOD_TIMEOUT"}

    data["signal"]      = dir_map.get(safe_int(data.get("sig_dir")),    "UNKNOWN")
    data["signal_type"] = type_map.get(safe_int(data.get("sig_type")),  "UNKNOWN")
    data["status"]      = status_map.get(safe_int(data.get("sig_status")), "UNKNOWN")
    data["reputation"]  = rep_map.get(safe_int(data.get("sig_rep")),    "ELIGIBLE")

    if safe_int(data.get("sig_result")) > 0:
        data["result"] = result_map.get(safe_int(data["sig_result"]), "UNKNOWN")

    # Eval state transition (new in v3.0)
    if "state_before" in data:
        data["state_before"] = rep_map.get(safe_int(data["state_before"]), "ELIGIBLE")
    if "state_after" in data:
        data["state_after"] = rep_map.get(safe_int(data["state_after"]), "ELIGIBLE")

    # Trade outcome exit reason (new in v3.0)
    if "exit_code" in data:
        data["exit_reason"] = exit_map.get(safe_int(data["exit_code"]), "UNKNOWN")

    return _normalize_numeric_fields(data)


def _normalize_numeric_fields(data: dict) -> dict:
    """Coerce all expected numeric fields to floats/ints safely."""
    float_fields = [
        "entry_price", "price", "sl", "tp1", "tp2", "stop_pts", "rr1", "rr2",
        "atr", "vwap", "ema9", "ema21", "ema50",
        "adx", "stoch_k", "stoch_d", "near_sr", "volume_ratio",
        "ft_target", "ft_high", "ft_low", "ft_actual_price", "move_points",
        "mae_points", "mfe_points", "post_tp1_mae_points", "final_pnl_points",
    ]
    for key in float_fields:
        if key in data:
            data[key] = safe_float(data[key])

    int_fields = [
        "htf_bull", "session_minute", "consecutive_stops", "stops_after",
        "lockout_bars", "is_ghost",
        "tp1_hit", "tp2_hit", "sl_hit", "be_stop_hit", "time_in_trade_min",
        "mae_time_min", "mfe_time_min",
    ]
    for key in int_fields:
        if key in data:
            data[key] = safe_int(data[key])

    return data


# =============================================================
# DATABASE — THREE-TABLE SCHEMA (v3.0)
# =============================================================

def init_db():
    """
    Initialize SQLite database with v3.0 three-table schema.

    Strategy: suffix approach for rollback safety.
      - signals_v3     → new table, all v3.0 entries go here
      - evaluations    → new table, all v3.0 evals go here
      - trade_outcomes → new table, populated by TP/SL Monitor

    Old v2.x tables (signals, eval_results) are left UNTOUCHED.
    If v3.0 ever needs to be rolled back, reverting the code restores
    v2.1.1 behavior with zero SQL gymnastics — the old tables are still
    there waiting.

    After 1-2 weeks of confirmed stable v3.0 operation, a cleanup
    migration can rename signals_v3 → signals and drop the old ones.
    """
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        # WAL mode for durability + concurrent reads
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')

        # ---- Table 1: signals_v3 (the Mona's opinion) ----
        c.execute('''CREATE TABLE IF NOT EXISTS signals_v3 (
            signal_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp          TEXT NOT NULL,
            signal             TEXT NOT NULL,
            signal_type        TEXT NOT NULL,

            entry_price        REAL NOT NULL,
            sl                 REAL NOT NULL,
            tp1                REAL NOT NULL,
            tp2                REAL NOT NULL,
            stop_pts           REAL NOT NULL,
            rr1                REAL,
            rr2                REAL,

            atr                REAL,
            vwap               REAL,
            ema9               REAL,
            ema21              REAL,
            ema50              REAL,
            adx                REAL,
            stoch_k            REAL,
            stoch_d            REAL,
            htf_bull           INTEGER,
            near_sr            REAL,
            volume_ratio       REAL,

            session_minute     INTEGER,
            reputation         TEXT,
            consecutive_stops  INTEGER,
            conditions         TEXT
        )''')

        # ---- Table 2: evaluations (the Reputation Engine's grade) ----
        c.execute('''CREATE TABLE IF NOT EXISTS evaluations (
            eval_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id          INTEGER NOT NULL,
            timestamp          TEXT NOT NULL,

            ft_target          REAL NOT NULL,
            ft_high            REAL,
            ft_low             REAL,
            ft_actual_price    REAL,
            move_points        REAL,
            result             TEXT NOT NULL,

            state_before       TEXT NOT NULL,
            state_after        TEXT NOT NULL,
            stops_after        INTEGER NOT NULL,
            lockout_bars       INTEGER,

            is_ghost           INTEGER NOT NULL,

            FOREIGN KEY (signal_id) REFERENCES signals_v3(signal_id)
        )''')

        # ---- Table 3: trade_outcomes (hypothetical execution results) ----
        c.execute('''CREATE TABLE IF NOT EXISTS trade_outcomes (
            outcome_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id           INTEGER NOT NULL,
            timestamp_opened    TEXT NOT NULL,
            timestamp_closed    TEXT NOT NULL,

            tp1_hit             INTEGER NOT NULL,
            tp1_hit_time        TEXT,
            tp2_hit             INTEGER NOT NULL,
            tp2_hit_time        TEXT,
            sl_hit              INTEGER NOT NULL,
            sl_hit_time         TEXT,
            be_stop_hit         INTEGER NOT NULL,
            be_stop_hit_time    TEXT,

            exit_reason         TEXT NOT NULL,
            final_pnl_points    REAL NOT NULL,

            mae_points          REAL,
            mfe_points          REAL,
            mae_time_min        INTEGER,
            mfe_time_min        INTEGER,
            post_tp1_mae_points REAL,
            time_in_trade_min   INTEGER,

            -- v3.1 provision: reserved for ghost position tracking.
            -- Default 0 in v3.0. Monitor v1 writes only real positions.
            -- v3.1 Monitor will write 1 for ghost positions (no migration needed).
            is_ghost            INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY (signal_id) REFERENCES signals_v3(signal_id)
        )''')

        # ---- Indexes for query performance ----
        c.execute('CREATE INDEX IF NOT EXISTS idx_eval_signal ON evaluations(signal_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_outcome_signal ON trade_outcomes(signal_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_signal_timestamp ON signals_v3(timestamp)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_eval_timestamp ON evaluations(timestamp)')

        conn.commit()


def get_parent_signal(signal_id: int) -> dict:
    """Fetch parent signal row for EVAL/OUTCOME embed context. Returns dict or None."""
    if not signal_id:
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM signals_v3 WHERE signal_id = ?', (signal_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"get_parent_signal error: {e}")
        return None


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
    except Exception as e:
        print(f"find_parent_by_lookback error: {e}")
        return None


# =============================================================
# STRUCTURED LOGGING TO #mona-log
# =============================================================
#
# Layer-specific log prefixes so diagnosis works from a phone glance.
#
# Healthy-path pattern: every successful webhook produces THREE log lines.
# If a line is missing, that stage failed — and which one is missing tells
# you which layer to investigate without opening Railway logs.
#
#   📥 [stage 1] webhook received, parsed, auth passed
#   📝 [stage 2] database write committed
#   📤 [stage 3] user-visible Discord embed posted
#
# Failure signatures (each carries enough context to diagnose):
#
#   ❌ [PARSE]   JSON parse failure — bad payload from TradingView
#   ❌ [AUTH]    Webhook auth token missing or wrong
#   ❌ [DB]      Database write failed — schema, constraint, disk
#   ❌ [DISCORD] Discord API call failed — rate limit, permission, network
#   ❌ [ORPHAN]  Eval/outcome rejected, no matching parent signal
#   ❌ [LOOKUP]  Lookback Matcher returned null unexpectedly
#   ⚠️ [MONITOR] Monitor-specific warning (tracker overflow, state mismatch)
#
# Ghost events use a distinct 👻 prefix in place of the three healthy prefixes
# and are stored silently — they never trigger a user-visible Discord embed,
# only a mona-log line for scroll-visibility.
#
# All logging is fire-and-forget. Failures to post log lines are swallowed
# so Discord hiccups never break the webhook response.

async def log_received(status, signal_type, direction, raw_len):
    """📥 Webhook received and parsed."""
    try:
        line = (
            f"\U0001f4e5 `{get_et_now().strftime('%I:%M %p ET')}` "
            f"RECV {status}: {signal_type} {direction} ({raw_len}b)"
        )
        await send_discord_message(CHANNELS.get("mona-log"), content=line)
    except Exception:
        pass


async def log_written(status, signal_type, direction, signal_id, table):
    """📝 Database write committed."""
    try:
        line = (
            f"\U0001f4dd `{get_et_now().strftime('%I:%M %p ET')}` "
            f"WROTE {status}: {signal_type} {direction} \u2192 {table} #{signal_id}"
        )
        await send_discord_message(CHANNELS.get("mona-log"), content=line)
    except Exception:
        pass


async def log_posted(status, signal_type, direction, signal_id, monitor_state=None):
    """📤 Discord embed posted. Optionally includes Monitor state snapshot."""
    try:
        line = (
            f"\U0001f4e4 `{get_et_now().strftime('%I:%M %p ET')}` "
            f"POSTED {status}: {signal_type} {direction} #{signal_id}"
        )
        if monitor_state:
            line += f" | Monitor: {monitor_state}"
        await send_discord_message(CHANNELS.get("mona-log"), content=line)
    except Exception:
        pass


async def log_ghost(signal_type, direction, signal_id, result):
    """👻 Ghost eval silently recorded."""
    try:
        line = (
            f"\U0001f47b `{get_et_now().strftime('%I:%M %p ET')}` "
            f"GHOST: {signal_type} {direction} #{signal_id} \u2192 {result}"
        )
        await send_discord_message(CHANNELS.get("mona-log"), content=line)
    except Exception:
        pass


async def log_error(layer, detail, raw_snippet=None):
    """❌ Layer-specific error. Possible layers: PARSE, AUTH, DB, DISCORD, ORPHAN, LOOKUP."""
    try:
        msg = f"\u274c **[{layer}]** `{get_et_now().strftime('%I:%M %p ET')}` {detail}"
        if raw_snippet:
            msg += f"\n```\n{raw_snippet[:500]}\n```"
        await send_discord_message(CHANNELS.get("mona-log"), content=msg)
    except Exception:
        pass


async def log_monitor_warning(detail):
    """⚠️ Monitor-specific warning."""
    try:
        msg = f"\u26a0\ufe0f **[MONITOR]** `{get_et_now().strftime('%I:%M %p ET')}` {detail}"
        await send_discord_message(CHANNELS.get("mona-log"), content=msg)
    except Exception:
        pass


def format_monitor_state(data):
    """Build the Monitor state snapshot string from TRADE_OUTCOME payload fields."""
    slots_used = safe_int(data.get("monitor_slots_used"))
    slots_max  = safe_int(data.get("monitor_slots_max"))
    oldest_min = safe_int(data.get("monitor_oldest_position_age_min"))
    if slots_max == 0:
        return None  # Monitor didn't include state info
    return f"{slots_used}/{slots_max} slots, oldest {oldest_min}min"


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
# EMBED BUILDERS (v3.0)
# =============================================================

def build_entry_embed(data, signal_id):
    """Entry alert embed for #alerts channel. Actionable trade levels."""
    direction   = data.get("signal", "UNKNOWN")
    signal_type = data.get("signal_type", "TREND")
    is_long     = direction == "LONG"
    is_squeeze  = signal_type == "SQUEEZE"

    color = (0x00CED1 if is_squeeze and is_long else
             0x9B59B6 if is_squeeze else
             0x00FF00 if is_long else 0xFF0000)

    emoji      = "\U0001f7e2" if is_long else "\U0001f534"
    type_label = "SQUEEZE BREAKOUT" if is_squeeze else "TREND CONTINUATION"

    entry = safe_float(data.get("entry_price", data.get("price")))
    atr   = safe_float(data.get("atr"))

    fields = []

    if entry:
        levels_text = (
            f"\U0001f4cd **Entry:** {entry:.2f}\n"
            f"\U0001f6d1 **Stop:** {safe_float(data.get('sl')):.2f} ({safe_float(data.get('stop_pts')):.1f} pts)\n"
            f"\U0001f3af **TP1:** {safe_float(data.get('tp1')):.2f} (R:R {safe_float(data.get('rr1')):.2f})\n"
            f"\U0001f680 **TP2:** {safe_float(data.get('tp2')):.2f} (R:R {safe_float(data.get('rr2')):.2f})"
        )
        fields.append({"name": "\U0001f4b0 Trade Levels", "value": levels_text, "inline": False})

    if atr:
        fields.append({
            "name": "\U0001f4ca Context",
            "value": f"ATR: {atr:.2f} pts  \u2022  ADX: {safe_float(data.get('adx')):.1f}  \u2022  Vol: {safe_float(data.get('volume_ratio')):.2f}x",
            "inline": False
        })

    fields.append({
        "name": "\U0001f9e0 Mona's Conviction",
        "value": f"**{data.get('reputation', 'ELIGIBLE')}** (Stops: {safe_int(data.get('consecutive_stops'))})",
        "inline": False
    })

    et_now = get_et_now()
    return {
        "title": f"{emoji} MNQ {type_label} \u2014 {direction}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"The Mona v{VERSION} \u2022 Signal ID: #{signal_id} \u2022 {et_now.strftime('%I:%M %p ET')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_eval_embed(data, signal_id, parent):
    """
    Rich EVAL embed for #trade-journal.
    Shows the full signal-to-eval lifecycle in one view.
    Pulls entry price / time / ATR from the parent signal row.
    """
    result    = data.get("result", "UNKNOWN")
    is_pass   = result == "PASS"
    color     = 0x00FF00 if is_pass else 0xFF0000
    icon      = "\u2705" if is_pass else "\u274c"
    direction = data.get("signal", "UNKNOWN")
    sig_type  = data.get("signal_type", "")

    fields = []

    # --- Original Signal block (from parent) ---
    if parent:
        entry_price  = safe_float(parent.get("entry_price"))
        entry_atr    = safe_float(parent.get("atr"))
        entry_time   = fmt_et(parent.get("timestamp", ""))

        orig_text = (
            f"**Entry:** {entry_price:.2f}\n"
            f"**Fired:** {entry_time}\n"
            f"**ATR:**   {entry_atr:.2f} pts"
        )
        fields.append({"name": "\U0001f4cd Original Signal", "value": orig_text, "inline": False})
    else:
        fields.append({
            "name": "\u26a0\ufe0f Parent Signal Not Found",
            "value": "Lookback matcher could not locate the parent. This row was rejected from the database.",
            "inline": False
        })
        entry_price = 0.0

    # --- Follow-Through block ---
    ft_target = safe_float(data.get("ft_target"))
    ft_actual = safe_float(data.get("ft_actual_price"))
    ft_high   = safe_float(data.get("ft_high"))
    ft_low    = safe_float(data.get("ft_low"))
    move_pts  = safe_float(data.get("move_points"))

    move_icon = "\u2705" if is_pass else "\u274c"
    sign      = "+" if move_pts >= 0 else ""

    if entry_price:
        threshold_pts = abs(ft_target - entry_price)
        ft_text = (
            f"**Target:** {ft_target:.2f}  (\u00b10.5 \u00d7 ATR = {threshold_pts:.2f} pts)\n"
            f"**Actual:** {ft_actual:.2f}\n"
            f"**High:**   {ft_high:.2f}\n"
            f"**Low:**    {ft_low:.2f}\n"
            f"**Move:**   {sign}{move_pts:.2f} pts  {move_icon}"
        )
    else:
        ft_text = (
            f"**Target:** {ft_target:.2f}\n"
            f"**Actual:** {ft_actual:.2f}\n"
            f"**Move:**   {sign}{move_pts:.2f} pts  {move_icon}"
        )
    fields.append({"name": "\U0001f3af Follow-Through Check", "value": ft_text, "inline": False})

    # --- Reputation block ---
    state_before = data.get("state_before", "ELIGIBLE")
    state_after  = data.get("state_after",  data.get("reputation", "ELIGIBLE"))
    stops_after  = safe_int(data.get("stops_after", data.get("consecutive_stops")))
    lockout_bars = safe_int(data.get("lockout_bars"))

    state_text = f"**State:**   {state_before} \u2192 {state_after}\n**Stops:**   {stops_after}"
    if lockout_bars > 0:
        state_text += f"\n**Lockout:** {lockout_bars} bars"
    fields.append({"name": "\U0001f9e0 Reputation", "value": state_text, "inline": False})

    parent_display = f"#{signal_id}" if signal_id else "N/A"
    return {
        "title": f"{icon} EVAL {result} \u2014 {sig_type} {direction}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"The Mona v{VERSION} \u2022 Parent {parent_display} \u2022 Eval {get_et_now().strftime('%I:%M %p ET')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_outcome_embed(data, signal_id, parent):
    """
    Rich TRADE_OUTCOME embed for #trade-journal.
    Hypothetical execution result under the mechanical 3/2/1 plan.
    """
    exit_reason = data.get("exit_reason", "UNKNOWN")
    direction   = data.get("signal", "UNKNOWN") if data.get("signal") else (parent.get("signal") if parent else "UNKNOWN")
    sig_type    = data.get("signal_type") if data.get("signal_type") else (parent.get("signal_type") if parent else "")

    # Color + icon by outcome
    outcome_style = {
        "TP1_TP2":     (0x00FF00, "\U0001f4b0", "TP1 \u2192 TP2"),      # green, full win
        "TP1_BE":      (0xFFD700, "\U0001f7e1", "TP1 \u2192 BE"),        # yellow, small win
        "SL_FULL":     (0xFF0000, "\U0001f534", "STOPPED OUT"),           # red, full loss
        "EOD_TIMEOUT": (0xCCCCCC, "\u26aa",     "EOD TIMEOUT"),           # gray
    }
    color, icon, label = outcome_style.get(exit_reason, (0x888888, "\u2753", exit_reason))

    fields = []

    # --- Signal block ---
    if parent:
        entry = safe_float(parent.get("entry_price"))
        sl    = safe_float(parent.get("sl"))
        tp1   = safe_float(parent.get("tp1"))
        tp2   = safe_float(parent.get("tp2"))
        stop_pts = safe_float(parent.get("stop_pts"))

        sig_text = (
            f"**Entry:** {entry:.2f} ({direction})\n"
            f"**SL:**    {sl:.2f} (-{stop_pts:.1f} pts)\n"
            f"**TP1:**   {tp1:.2f} (+{(tp1-entry if direction=='LONG' else entry-tp1):.1f} pts)\n"
            f"**TP2:**   {tp2:.2f} (+{(tp2-entry if direction=='LONG' else entry-tp2):.1f} pts)"
        )
        fields.append({"name": f"\U0001f4cd Signal #{signal_id}", "value": sig_text, "inline": False})

    # --- Timeline block ---
    tp1_time = data.get("tp1_hit_time", "")
    tp2_time = data.get("tp2_hit_time", "")
    sl_time  = data.get("sl_hit_time", "")
    be_time  = data.get("be_stop_hit_time", "")
    duration = safe_int(data.get("time_in_trade_min"))

    timeline_lines = []
    if safe_int(data.get("tp1_hit")) and tp1_time:
        timeline_lines.append(f"**TP1 hit:** {fmt_et(tp1_time)}")
    if safe_int(data.get("tp2_hit")) and tp2_time:
        timeline_lines.append(f"**TP2 hit:** {fmt_et(tp2_time)}")
    if safe_int(data.get("sl_hit")) and sl_time:
        timeline_lines.append(f"**SL hit:**  {fmt_et(sl_time)}")
    if safe_int(data.get("be_stop_hit")) and be_time:
        timeline_lines.append(f"**BE stop:** {fmt_et(be_time)}")
    if duration:
        timeline_lines.append(f"**Duration:** {duration} min")

    if timeline_lines:
        fields.append({"name": "\u23f1\ufe0f Execution Timeline", "value": "\n".join(timeline_lines), "inline": False})

    # --- Extremes block ---
    mae = safe_float(data.get("mae_points"))
    mfe = safe_float(data.get("mfe_points"))
    mae_t = safe_int(data.get("mae_time_min"))
    mfe_t = safe_int(data.get("mfe_time_min"))
    post_tp1_mae = safe_float(data.get("post_tp1_mae_points"))

    if mae or mfe:
        extreme_lines = [
            f"**MAE:** {mae:+.2f} pts" + (f"  _(at +{mae_t} min)_" if mae_t else "  _(worst adverse)_"),
            f"**MFE:** {mfe:+.2f} pts" + (f"  _(at +{mfe_t} min)_" if mfe_t else "  _(peak profit)_"),
        ]
        if safe_int(data.get("tp1_hit")) and post_tp1_mae:
            extreme_lines.append(f"**Post-TP1 MAE:** {post_tp1_mae:+.2f} pts  _(runner drawdown)_")
        fields.append({"name": "\U0001f4ca Extremes", "value": "\n".join(extreme_lines), "inline": False})

    # --- P&L block ---
    pnl = safe_float(data.get("final_pnl_points"))
    pnl_sign = "+" if pnl >= 0 else ""
    fields.append({
        "name": "\U0001f4b5 Hypothetical P&L",
        "value": f"**{pnl_sign}{pnl:.2f} points**  (3/2/1 contract plan)",
        "inline": False
    })

    return {
        "title": f"{icon} OUTCOME: {label} \u2014 {sig_type} {direction}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"The Mona v{VERSION} \u2022 Signal #{signal_id} \u2022 Resolved {get_et_now().strftime('%I:%M %p ET')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# =============================================================
# WEBHOOK HANDLER
# =============================================================

app = FastAPI(title=f"The Mona v{VERSION}")


@app.get("/")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "schema": "v3",
        "auth_enabled": bool(WEBHOOK_AUTH_TOKEN),
    }


@app.post("/webhook")
async def receive_webhook(request: Request, token: str = ""):
    # ---- PHASE 0: Auth ----
    if WEBHOOK_AUTH_TOKEN:
        if not token or token != WEBHOOK_AUTH_TOKEN:
            client_ip = request.client.host if request.client else 'unknown'
            await log_error("AUTH", f"missing or invalid token from {client_ip}")
            raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    raw_message = body.decode("utf-8").strip()
    if not raw_message:
        await log_error("PARSE", "empty message body")
        raise HTTPException(status_code=400, detail="Empty message")

    # ---- PHASE 1: Parse ----
    try:
        cleaned = sanitize_json(raw_message)
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw payload: {raw_message}")
        await log_error("PARSE", f"at char {e.pos}: {str(e)}", raw_snippet=raw_message)
        raise HTTPException(status_code=400, detail=f"JSON parse error at char {e.pos}")

    # ---- PHASE 2: Translate + Route + DB Write ----
    data = translate_payload(data)

    status      = data.get("status", "ENTRY")
    signal_type = data.get("signal_type", "UNKNOWN")
    direction   = data.get("signal", "UNKNOWN")
    timestamp   = get_utc_timestamp()

    # LOG LINE 1 of 3: received + parsed (healthy path)
    # Exception: ghost evals skip this line and emit a single 👻 line instead.
    probable_ghost = safe_int(data.get("is_ghost")) == 1
    if not probable_ghost:
        await log_received(status, signal_type, direction, len(raw_message))

    signal_id = None
    embed = None
    target_channel = None
    parent = None
    is_ghost = False
    table_written = None

    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()

            # ============ ROUTE 1: ENTRY → signals_v3 ============
            if status == "ENTRY":
                c.execute('''INSERT INTO signals_v3
                    (timestamp, signal, signal_type,
                     entry_price, sl, tp1, tp2, stop_pts, rr1, rr2,
                     atr, vwap, ema9, ema21, ema50, adx, stoch_k, stoch_d,
                     htf_bull, near_sr, volume_ratio,
                     session_minute, reputation, consecutive_stops, conditions)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (timestamp, direction, signal_type,
                     safe_float(data.get("entry_price", data.get("price"))),
                     safe_float(data.get("sl")),
                     safe_float(data.get("tp1")),
                     safe_float(data.get("tp2")),
                     safe_float(data.get("stop_pts")),
                     safe_float(data.get("rr1")),
                     safe_float(data.get("rr2")),
                     safe_float(data.get("atr")),
                     safe_float(data.get("vwap")),
                     safe_float(data.get("ema9")),
                     safe_float(data.get("ema21")),
                     safe_float(data.get("ema50")),
                     safe_float(data.get("adx")),
                     safe_float(data.get("stoch_k")),
                     safe_float(data.get("stoch_d")),
                     safe_int(data.get("htf_bull")),
                     safe_float(data.get("near_sr")),
                     safe_float(data.get("volume_ratio")),
                     safe_int(data.get("session_minute")),
                     data.get("reputation", "ELIGIBLE"),
                     safe_int(data.get("consecutive_stops")),
                     data.get("conditions", "")))
                signal_id = c.lastrowid
                conn.commit()
                table_written = "signals_v3"
                embed = build_entry_embed(data, signal_id)
                target_channel = CHANNELS.get("alerts")

            # ============ ROUTE 2: EVAL_RESULT → evaluations ============
            elif status == "EVAL_RESULT":
                is_ghost = safe_int(data.get("is_ghost")) == 1

                # Find parent via Lookback Matcher
                signal_id = find_parent_by_lookback(signal_type, direction)

                # Orphan rejection: no parent = no write, distinct error line
                if not signal_id:
                    await log_error(
                        "ORPHAN",
                        f"EVAL {signal_type} {direction} rejected — no parent in last 2h",
                        raw_snippet=raw_message
                    )
                    return {"status": "rejected", "reason": "no_parent"}

                # Write to evaluations table
                c.execute('''INSERT INTO evaluations
                    (signal_id, timestamp,
                     ft_target, ft_high, ft_low, ft_actual_price, move_points,
                     result, state_before, state_after, stops_after, lockout_bars, is_ghost)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (signal_id, timestamp,
                     safe_float(data.get("ft_target")),
                     safe_float(data.get("ft_high")),
                     safe_float(data.get("ft_low")),
                     safe_float(data.get("ft_actual_price")),
                     safe_float(data.get("move_points")),
                     data.get("result", "UNKNOWN"),
                     data.get("state_before", "ELIGIBLE"),
                     data.get("state_after", data.get("reputation", "ELIGIBLE")),
                     safe_int(data.get("stops_after", data.get("consecutive_stops"))),
                     safe_int(data.get("lockout_bars")),
                     1 if is_ghost else 0))
                conn.commit()
                table_written = "evaluations"

                # Ghost evals: silent, no user-visible Discord post, single 👻 log line
                if is_ghost:
                    embed = None
                    target_channel = None
                else:
                    parent = get_parent_signal(signal_id)
                    embed = build_eval_embed(data, signal_id, parent)
                    target_channel = CHANNELS.get("trade-journal")

            # ============ ROUTE 3: TRADE_OUTCOME → trade_outcomes ============
            elif status == "TRADE_OUTCOME":
                signal_id = find_parent_by_lookback(signal_type, direction)

                if not signal_id:
                    await log_error(
                        "ORPHAN",
                        f"OUTCOME {signal_type} {direction} rejected — no parent in last 2h",
                        raw_snippet=raw_message
                    )
                    return {"status": "rejected", "reason": "no_parent"}

                c.execute('''INSERT INTO trade_outcomes
                    (signal_id, timestamp_opened, timestamp_closed,
                     tp1_hit, tp1_hit_time, tp2_hit, tp2_hit_time,
                     sl_hit, sl_hit_time, be_stop_hit, be_stop_hit_time,
                     exit_reason, final_pnl_points,
                     mae_points, mfe_points, mae_time_min, mfe_time_min,
                     post_tp1_mae_points, time_in_trade_min, is_ghost)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (signal_id,
                     data.get("timestamp_opened", timestamp),
                     timestamp,
                     safe_int(data.get("tp1_hit")),
                     data.get("tp1_hit_time"),
                     safe_int(data.get("tp2_hit")),
                     data.get("tp2_hit_time"),
                     safe_int(data.get("sl_hit")),
                     data.get("sl_hit_time"),
                     safe_int(data.get("be_stop_hit")),
                     data.get("be_stop_hit_time"),
                     data.get("exit_reason", "UNKNOWN"),
                     safe_float(data.get("final_pnl_points")),
                     safe_float(data.get("mae_points")),
                     safe_float(data.get("mfe_points")),
                     safe_int(data.get("mae_time_min")),
                     safe_int(data.get("mfe_time_min")),
                     safe_float(data.get("post_tp1_mae_points")),
                     safe_int(data.get("time_in_trade_min")),
                     safe_int(data.get("is_ghost"))))   # v3.1 provision, v3.0 Monitor always sends 0
                conn.commit()
                table_written = "trade_outcomes"

                # Monitor capacity warning if near overflow
                slots_used = safe_int(data.get("monitor_slots_used"))
                slots_max  = safe_int(data.get("monitor_slots_max"))
                if slots_max > 0 and slots_used >= slots_max - 1:
                    await log_monitor_warning(
                        f"tracker near capacity ({slots_used}/{slots_max} slots)"
                    )

                parent = get_parent_signal(signal_id)
                embed = build_outcome_embed(data, signal_id, parent)
                target_channel = CHANNELS.get("trade-journal")

    except Exception as e:
        print(f"Database error: {e}")
        await log_error("DB", f"{status}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    # LOG LINE 2 of 3: database write committed (healthy path)
    if not is_ghost and signal_id and table_written:
        await log_written(status, signal_type, direction, signal_id, table_written)

    # ---- PHASE 3: Discord posting (non-critical) ----
    if embed and target_channel:
        try:
            await send_discord_message(target_channel, embed=embed)
            # LOG LINE 3 of 3: user-visible embed posted (healthy path)
            monitor_state = format_monitor_state(data) if status == "TRADE_OUTCOME" else None
            await log_posted(status, signal_type, direction, signal_id, monitor_state)
        except Exception as e:
            print(f"Discord embed post failed: {e}")
            await log_error("DISCORD", f"{status} #{signal_id} embed post failed: {str(e)}")

    # Ghost events: single 👻 line in place of the three healthy prefixes
    if is_ghost and signal_id:
        await log_ghost(signal_type, direction, signal_id, data.get("result", "UNKNOWN"))

    return {"status": "ok", "action": status, "signal_id": signal_id, "ghost": is_ghost}


# =============================================================
# STARTUP
# =============================================================

@app.on_event("startup")
async def startup():
    init_db()
    print(f"Database initialized at {DB_PATH}")
    print(f"Webhook auth: {'ENABLED' if WEBHOOK_AUTH_TOKEN else 'DISABLED'}")
    print(f"Channel mapping: {[k for k,v in CHANNELS.items() if v]}")

    et_now = get_et_now()
    auth_status = "\U0001f512 AUTH" if WEBHOOK_AUTH_TOKEN else "\U0001f513 OPEN"
    await send_discord_message(
        CHANNELS.get("mona-log"),
        content=(
            f"\U0001f7e2 **The Mona v{VERSION} DB Connected** \u2014 "
            f"{et_now.strftime('%I:%M %p ET')} \u2022 {auth_status} \u2022 WAL ON \u2022 3-TABLE SCHEMA"
        )
    )
