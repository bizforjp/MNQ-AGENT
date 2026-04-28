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

# ---- Option C backend modules (§2.2, §4, §5, §8) ----
from backend.schema import (
    init_db as schema_init_db,
    migrate_add_bar_close_ms_to_signals_v3,
    migrate_add_payload_json_to_eval_results,
    BAR_INTERVAL_MS,
)
from backend.webhook_router import (
    route_entry, route_eval, route_heartbeat_for_position,
)
from backend.rehydrate import (
    rehydrate_positions, eod_sweep, eod_cutoff_for_session_of,
)
from backend.position_resolver import Bar, PositionFSMState, Transition
from backend.finnhub_adapter import FinnhubAdapter, FinnhubError

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
    status_map = {1: "ENTRY",   2: "EVAL_RESULT", 3: "HEARTBEAT"}
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
    Initialize SQLite with the Option C schema (§4).

    Delegates table/index creation to `backend.schema.init_db`. That module
    owns the authoritative shape for signals_v3, evaluations, trade_outcomes,
    positions, and the legacy v2 coexistence tables. Runs the additive
    `bar_close_ms` migration for DBs that predate the column.
    """
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        schema_init_db(conn)
        migrate_add_bar_close_ms_to_signals_v3(conn)
        migrate_add_payload_json_to_eval_results(conn)


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


# =============================================================
# OPTION C IN-MEMORY STATE
# =============================================================
#
# _fsm_map is the authoritative in-memory active-position map (§5.2).
# Keyed by signal_id; each value is a PositionState. Rehydrated on
# startup from the `positions` table (§4.9 / §8.4).
#
# _captured_outcomes is the in-process sink for terminal resolver
# transitions. post_embed threads it through route_heartbeat_for_position
# so TP2/SL/BE/EOD closes produce user-visible OUTCOME embeds. In dry-run
# a driver can read from _captured_outcomes; in production the sink
# triggers the async Discord post.

_fsm_map: dict = {}
_captured_outcomes: list = []
_finnhub = None


def _get_finnhub():
    """Lazy adapter. Falls back to a stub that always raises FinnhubError
    so gap recovery takes the GAP_CLEAN branch when no key is configured."""
    global _finnhub
    if _finnhub is not None:
        return _finnhub
    api_key = os.getenv("FINNHUB_API_KEY", "")
    if api_key:
        _finnhub = FinnhubAdapter(api_key=api_key)
    else:
        class _NoFinnhub:
            def fetch_bars(self, **kw):
                raise FinnhubError("FINNHUB_API_KEY not configured")
        _finnhub = _NoFinnhub()
    return _finnhub


def _outcome_sink(payload: dict) -> None:
    """post_embed callback — captures terminal transitions for Discord."""
    _captured_outcomes.append(payload)


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
        vol = safe_float(data.get('volume_ratio'))
        vol_str = f"{vol:.2f}x" if vol >= 0.01 else "N/A"
        fields.append({
            "name": "\U0001f4ca Context",
            "value": f"ATR: {atr:.2f} pts  \u2022  ADX: {safe_float(data.get('adx')):.1f}  \u2022  Vol: {vol_str}",
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
    resolver_closures: list = []

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = ON")

            # ============ ROUTE 1: ENTRY (Option C §3.2) ============
            if status == "ENTRY":
                signal_id = route_entry(
                    data, fsm_map=_fsm_map, conn=conn, log=None,
                )
                if signal_id is not None:
                    # v3 path: backfill the rich metadata columns that the
                    # minimal router INSERT left null. Discord embed relies
                    # on the incoming payload directly so this UPDATE is
                    # for Data Lab / future analytics, not this request.
                    conn.execute(
                        '''UPDATE signals_v3 SET
                            rr1=?, rr2=?, atr=?, vwap=?,
                            ema9=?, ema21=?, ema50=?, adx=?,
                            stoch_k=?, stoch_d=?, htf_bull=?,
                            near_sr=?, volume_ratio=?, session_minute=?,
                            reputation=?, consecutive_stops=?, conditions=?
                           WHERE signal_id=?''',
                        (safe_float(data.get("rr1")),
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
                         data.get("conditions", ""),
                         signal_id),
                    )
                    conn.commit()
                    table_written = "signals_v3+positions"
                else:
                    # Legacy v2 payload written to `signals` by the tolerance
                    # dispatcher. No positions row, no entry embed for dry-run.
                    table_written = "signals"
                embed = build_entry_embed(data, signal_id or 0)
                target_channel = CHANNELS.get("alerts")

            # ============ ROUTE 2: EVAL (Option C §3.3) ============
            elif status == "EVAL_RESULT":
                is_ghost = safe_int(data.get("is_ghost")) == 1
                signal_id = route_eval(data, conn=conn, log=None)
                table_written = "evaluations" if signal_id else None

                if signal_id is None:
                    await log_error(
                        "ORPHAN",
                        f"EVAL {signal_type} {direction} rejected — exact-match "
                        "lookup on parent_bar_close_ms returned no row",
                        raw_snippet=raw_message,
                    )
                    return {"status": "rejected", "reason": "no_parent"}

                if is_ghost:
                    embed = None
                    target_channel = None
                else:
                    parent = get_parent_signal(signal_id)
                    embed = build_eval_embed(data, signal_id, parent)
                    target_channel = CHANNELS.get("trade-journal")

            # ============ ROUTE 3: HEARTBEAT (Option C §3.4) ============
            elif status == "HEARTBEAT":
                bar_close_ms = safe_int(data.get("bar_close_ms"))
                if bar_close_ms <= 0:
                    await log_error(
                        "SCHEMA", "HEARTBEAT missing bar_close_ms",
                        raw_snippet=raw_message,
                    )
                    raise HTTPException(status_code=400,
                                        detail="HEARTBEAT missing bar_close_ms")

                bar = Bar(
                    bar_close_ms=bar_close_ms,
                    open=safe_float(data.get("bar_open", data.get("open"))),
                    high=safe_float(data.get("bar_high", data.get("high"))),
                    low=safe_float(data.get("bar_low", data.get("low"))),
                    close=safe_float(data.get("bar_close", data.get("close"))),
                )
                eod_cutoff = eod_cutoff_for_session_of(bar_close_ms)

                slot_times = [
                    safe_int(data.get(f"pos_slot_{i}_time", 0))
                    for i in range(1, 5)
                ]
                active_slots = [ms for ms in slot_times if ms > 0]

                for slot_ms in active_slots:
                    row = conn.execute(
                        "SELECT signal_id FROM positions WHERE bar_close_ms=?",
                        (slot_ms,),
                    ).fetchone()
                    if row is None:
                        await log_monitor_warning(
                            f"HEARTBEAT unknown position bar_close_ms={slot_ms}"
                        )
                        continue
                    sid = row[0]
                    before = _fsm_map.get(sid)
                    route_heartbeat_for_position(
                        signal_id=sid, bar=bar,
                        fsm_map=_fsm_map, conn=conn,
                        finnhub=_get_finnhub(),
                        eod_cutoff_ms=eod_cutoff,
                        post_embed=_outcome_sink,
                        log=None,
                    )
                    # Detect terminal close: position was in fsm_map before and
                    # is gone now. Collect for Discord outcome post.
                    after = _fsm_map.get(sid)
                    if before is not None and after is None:
                        resolver_closures.append(sid)

                signal_id = None  # heartbeat has no single signal_id
                table_written = "positions" + (
                    "+trade_outcomes" if resolver_closures else ""
                )

            # ============ RETIRED: TRADE_OUTCOME (Option C §3.5) ============
            elif status == "TRADE_OUTCOME":
                await log_error(
                    "TRADE_OUTCOME_RETIRED",
                    "TRADE_OUTCOME webhook received under Option C — retired "
                    "(positions resolve via backend FSM, not Pine)",
                    raw_snippet=raw_message,
                )
                return {"status": "rejected", "reason": "trade_outcome_retired"}

    except HTTPException:
        raise
    except sqlite3.IntegrityError as e:
        # §4.2 UNIQUE_VIOLATION loud-logging: post to #system-log.
        violation = data.get("_unique_violation")
        if violation:
            detail_msg = (
                f"bar_close_ms={violation['bar_close_ms']} "
                f"attempted={violation['attempted_type']} {violation['attempted_dir']} "
                f"existing=#{violation['existing_id']} {violation['existing_type']} "
                f"{violation['existing_dir']}"
            )
            await log_error("UNIQUE_VIOLATION", detail_msg, raw_snippet=raw_message)
        else:
            await log_error("DB", f"{status}: {str(e)}")
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        print(f"Database error: {e}")
        await log_error("DB", f"{status}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    # LOG LINE 2 of 3: database write committed (healthy path)
    if not is_ghost and table_written and (signal_id or resolver_closures):
        await log_written(status, signal_type, direction,
                          signal_id or 0, table_written)

    # ---- PHASE 3: Discord posting (non-critical) ----
    if embed and target_channel:
        try:
            await send_discord_message(target_channel, embed=embed)
            await log_posted(status, signal_type, direction, signal_id or 0)
        except Exception as e:
            print(f"Discord embed post failed: {e}")
            await log_error("DISCORD", f"{status} #{signal_id} embed post failed: {str(e)}")

    # HEARTBEAT closures — build + post one OUTCOME embed per closed position.
    for closed_sid in resolver_closures:
        try:
            outcome_payload = next(
                (p for p in _captured_outcomes if p.get("signal_id") == closed_sid),
                None,
            )
            parent_row = get_parent_signal(closed_sid)
            closed_row = _fetch_closed_outcome(closed_sid)
            if closed_row:
                outcome_embed = build_outcome_embed(
                    closed_row, closed_sid, parent_row,
                )
                await send_discord_message(
                    CHANNELS.get("trade-journal"), embed=outcome_embed,
                )
                await log_posted(
                    "OUTCOME",
                    parent_row.get("signal_type") if parent_row else "",
                    parent_row.get("signal") if parent_row else "",
                    closed_sid,
                )
        except Exception as e:
            print(f"Outcome embed post failed for {closed_sid}: {e}")
            await log_error("DISCORD",
                            f"OUTCOME #{closed_sid} post failed: {e}")

    # Ghost events: single 👻 line in place of the three healthy prefixes
    if is_ghost and signal_id:
        await log_ghost(signal_type, direction, signal_id, data.get("result", "UNKNOWN"))

    return {
        "status": "ok",
        "action": status,
        "signal_id": signal_id,
        "closures": resolver_closures,
        "ghost": is_ghost,
    }


_RESOLVER_EXIT_TO_EMBED_LABEL = {
    "TP2_HIT":     "TP1_TP2",
    "BE_STOP":     "TP1_BE",
    "SL_HIT":      "SL_FULL",
    "EOD_TIMEOUT": "EOD_TIMEOUT",
    "GAP_CLEAN":   "GAP_CLEAN",
}


def _fetch_closed_outcome(signal_id: int) -> dict:
    """Load the trade_outcomes row written by the resolver, translated into
    the dict shape `build_outcome_embed` expects."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                '''SELECT * FROM trade_outcomes
                   WHERE signal_id=?
                   ORDER BY outcome_id DESC LIMIT 1''',
                (signal_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            resolver_reason = d.get("exit_reason")
            d["exit_reason"] = _RESOLVER_EXIT_TO_EMBED_LABEL.get(
                resolver_reason, resolver_reason,
            )
            return d
    except Exception as e:
        print(f"_fetch_closed_outcome error: {e}")
        return None


# =============================================================
# STARTUP
# =============================================================

@app.on_event("startup")
async def startup():
    """Option C startup ordering (§8.4):
      1. init_db (schema)
      2. rehydrate fsm_map from positions table
      3. EOD sweep — close any position whose session already ended
      4. accept webhooks (FastAPI is already bound; we just populate state)
    """
    init_db()
    print(f"Database initialized at {DB_PATH}")
    print(f"Webhook auth: {'ENABLED' if WEBHOOK_AUTH_TOKEN else 'DISABLED'}")
    print(f"Channel mapping: {[k for k,v in CHANNELS.items() if v]}")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        rehydrated = rehydrate_positions(conn)
        _fsm_map.update(rehydrated)
        if rehydrated:
            current_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            eod_sweep(
                _fsm_map, conn, current_ms,
                post_embed=_outcome_sink, log=None,
            )
    print(f"Rehydrated {len(_fsm_map)} open position(s)")

    et_now = get_et_now()
    auth_status = "\U0001f512 AUTH" if WEBHOOK_AUTH_TOKEN else "\U0001f513 OPEN"
    await send_discord_message(
        CHANNELS.get("mona-log"),
        content=(
            f"\U0001f7e2 **The Mona v{VERSION} DB Connected** \u2014 "
            f"{et_now.strftime('%I:%M %p ET')} \u2022 {auth_status} \u2022 "
            f"WAL ON \u2022 OPTION C SCHEMA \u2022 "
            f"{len(_fsm_map)} open position(s)"
        )
    )
