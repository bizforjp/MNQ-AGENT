"""
MNQ Trading Agent -- Backend Server
Receives TradingView webhook alerts and posts to Discord.
"""

import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import aiohttp

# =============================================================
# CONFIG
# =============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mnq-agent-2024")

# Channel IDs
CHANNELS = {
    "alerts-high":        os.getenv("CH_ALERTS_HIGH"),
    "alerts-moderate":    os.getenv("CH_ALERTS_MODERATE"),
    "morning-briefing":   os.getenv("CH_MORNING_BRIEFING"),
    "daily-bias":         os.getenv("CH_DAILY_BIAS"),
    "economic-calendar":  os.getenv("CH_ECONOMIC_CALENDAR"),
    "trade-journal":      os.getenv("CH_TRADE_JOURNAL"),
    "performance":        os.getenv("CH_PERFORMANCE"),
    "bot-commands":       os.getenv("CH_BOT_COMMANDS"),
    "system-log":         os.getenv("CH_SYSTEM_LOG"),
}

DISCORD_API = "https://discord.com/api/v10"

# =============================================================
# APP
# =============================================================

app = FastAPI(title="MNQ Trading Agent")


# =============================================================
# DISCORD HELPERS
# =============================================================

async def send_discord_message(channel_id: str, content: str = None, embed: dict = None):
    """Send a message to a Discord channel via bot token."""
    if not channel_id or not DISCORD_BOT_TOKEN:
        print(f"Missing channel_id ({channel_id}) or bot token")
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

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 200 or resp.status == 201:
                return await resp.json()
            else:
                error = await resp.text()
                print(f"Discord error {resp.status}: {error}")
                return None


def get_et_now():
    """Get current Eastern Time."""
    utc_now = datetime.now(timezone.utc)
    et = utc_now - timedelta(hours=4)  # EDT
    return et


# =============================================================
# JSON SIGNAL HANDLERS (v2.0)
# =============================================================

def build_json_embed(data: dict) -> dict:
    """Build a clean Discord embed from v2.0 JSON signal data (ACTIONABLE ENTRIES)."""
    direction = data.get("signal", "UNKNOWN")
    signal_type = data.get("signal_type", "TREND")
    is_long = direction == "LONG"
    is_squeeze = signal_type == "SQUEEZE"

    # Colors
    if is_squeeze:
        color = 0x00CED1 if is_long else 0x9B59B6  # Teal or Purple
    else:
        color = 0x00FF00 if is_long else 0xFF0000  # Green or Red

    # Title
    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    type_label = "TREND CONTINUATION" if signal_type == "TREND" else "SQUEEZE BREAKOUT"
    title = f"{emoji} MNQ {type_label} -- {direction}"

    # Price levels
    price = data.get("price", 0)
    sl = data.get("sl", 0)
    tp1 = data.get("tp1", 0)
    tp2 = data.get("tp2", 0)
    atr = data.get("atr", 0)
    stop_pts = data.get("stop_pts", 0)

    # Reputation
    reputation = data.get("reputation", "")
    consec_stops = data.get("consecutive_stops", 0)

    # Conditions string
    conditions = data.get("conditions", "")
    condition_items = [c.strip() for c in conditions.split("|") if c.strip()]
    conditions_display = " \u00b7 ".join(condition_items)

    # Nearest S/R
    near_sr = data.get("near_sr", "")

    # Build fields
    fields = []

    if conditions_display:
        fields.append({
            "name": "\u2705 Conditions",
            "value": conditions_display,
            "inline": False,
        })

    if price:
        levels_text = (
            f"\U0001f4cd **Entry:** {price:.2f}\n"
            f"\U0001f6d1 **Stop:** {sl:.2f} ({stop_pts:.1f} pts)\n"
            f"\U0001f3af **TP1:** {tp1:.2f} ({data.get('atr', 0) * 1.5:.1f} pts)\n"
            f"\U0001f680 **TP2:** {tp2:.2f} ({data.get('atr', 0) * 2.5:.1f} pts)"
        )
        fields.append({
            "name": "\U0001f4b0 Trade Levels",
            "value": levels_text,
            "inline": False,
        })

    if atr:
        fields.append({
            "name": "\U0001f4ca ATR",
            "value": f"{atr:.2f} pts",
            "inline": True,
        })

    if near_sr:
        fields.append({
            "name": "\U0001f4cd Nearest S/R",
            "value": near_sr,
            "inline": True,
        })

    if reputation:
        fields.append({
            "name": "\U0001f9e0 Mona's Conviction",
            "value": f"**{reputation}** (Consecutive Stops: {consec_stops})",
            "inline": False,
        })

    et_now = get_et_now()
    timestamp_str = et_now.strftime("%I:%M %p ET")
    version = data.get("version", "2.0")

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"MNQ Agent v{version} \u2022 {timestamp_str}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return embed


def build_eval_embed(data: dict) -> dict:
    """Build a Discord embed for Mona's 4-bar evaluation results (JOURNAL)."""
    direction = data.get("signal", "UNKNOWN")
    signal_type = data.get("signal_type", "UNKNOWN")
    result = data.get("result", "FAIL")

    is_pass = (result == "PASS")
    color = 0x00FF00 if is_pass else 0xFF0000
    icon = "\u2705" if is_pass else "\u274c"  # Green check / Red X

    title = f"{icon} EVAL {result} -- {signal_type} {direction}"

    price = data.get("price", 0)
    target = data.get("follow_thru_target", 0)
    rep = data.get("reputation", "UNKNOWN")
    stops = data.get("consecutive_stops", 0)

    fields = [
        {"name": "Signal Price", "value": f"{price:.2f}", "inline": True},
        {"name": "Target Hit?", "value": f"{target:.2f}", "inline": True},
        {"name": "\U0001f9e0 New Reputation", "value": f"**{rep}** (Stops: {stops})", "inline": False}
    ]

    et_now = get_et_now()
    timestamp_str = et_now.strftime("%I:%M %p ET")

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"MNQ Agent v2.0 \u2022 {timestamp_str}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed


# =============================================================
# TEXT SIGNAL HANDLER (v1.4 fallback)
# =============================================================

def parse_signal(message: str) -> dict:
    """Parse TradingView alert message into structured data (v1.4 text format)."""
    signal = {
        "raw": message,
        "type": "UNKNOWN",
        "direction": "UNKNOWN",
        "details": [],
        "nearest_sr": None,
    }

    msg_upper = message.upper()

    if "TREND LONG" in msg_upper:
        signal["type"] = "TREND"
        signal["direction"] = "LONG"
    elif "TREND SHORT" in msg_upper:
        signal["type"] = "TREND"
        signal["direction"] = "SHORT"
    elif "SQUEEZE LONG" in msg_upper:
        signal["type"] = "SQUEEZE"
        signal["direction"] = "LONG"
    elif "SQUEEZE SHORT" in msg_upper:
        signal["type"] = "SQUEEZE"
        signal["direction"] = "SHORT"

    if "\u2014" in message:
        parts = message.split("\u2014", 1)
        if len(parts) > 1:
            detail_str = parts[1]
            signal["details"] = [d.strip() for d in detail_str.split("|")]

    if "Near:" in message:
        near_part = message.split("Near:")[-1].strip()
        signal["nearest_sr"] = near_part

    return signal


def build_text_embed(signal: dict) -> dict:
    """Build a Discord embed from v1.4 text signal (fallback)."""
    is_long = signal["direction"] == "LONG"
    is_squeeze = signal["type"] == "SQUEEZE"

    if is_squeeze:
        color = 0x00CED1 if is_long else 0x9B59B6
    else:
        color = 0x00FF00 if is_long else 0xFF0000

    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    type_label = "TREND CONTINUATION" if signal["type"] == "TREND" else "SQUEEZE BREAKOUT"
    title = f"{emoji} MNQ {type_label} -- {signal['direction']}"

    fields = []

    clean_details = [d.strip() for d in signal["details"] if d.strip() and "Near:" not in d]
    if clean_details:
        fields.append({
            "name": "\u2705 Conditions",
            "value": " \u00b7 ".join(clean_details),
            "inline": False,
        })

    if signal["nearest_sr"]:
        fields.append({
            "name": "\U0001f4cd Nearest S/R",
            "value": signal["nearest_sr"],
            "inline": False,
        })

    et_now = get_et_now()
    timestamp_str = et_now.strftime("%I:%M %p ET")

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"MNQ Agent v1.4 \u2022 {timestamp_str}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return embed


# =============================================================
# ROUTES
# =============================================================

@app.get("/")
async def root():
    """Health check."""
    return {"status": "MNQ Agent running", "version": "2.0"}


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive TradingView webhook alert.
    Routes ENTRY to alerts-high, EVAL_RESULT to trade-journal.
    """
    try:
        body = await request.body()
        message = body.decode("utf-8").strip()

        if not message:
            raise HTTPException(status_code=400, detail="Empty message")

        print(f"Received webhook: {message}")

        embed = None
        signal_type = "UNKNOWN"
        direction = "UNKNOWN"
        status = "ENTRY"
        log_action = "Signal"
        target_channel_id = None

        try:
            data = json.loads(message)
            status = data.get("status", "ENTRY")
            signal_type = data.get("signal_type", "UNKNOWN")
            direction = data.get("signal", "UNKNOWN")

            # Route 1: Mona's Homework
            if status == "EVAL_RESULT":
                embed = build_eval_embed(data)
                target_channel_id = CHANNELS.get("trade-journal")
                log_action = "Evaluation"
            
            # Route 2: Actionable Trade
            else:
                embed = build_json_embed(data)
                target_channel_id = CHANNELS.get("alerts-high")
                log_action = "Signal"

            print(f"Parsed as JSON (v2.0): {status} - {signal_type} {direction}")

        except (json.JSONDecodeError, ValueError):
            # v1.4 fallback
            signal = parse_signal(message)
            embed = build_text_embed(signal)
            signal_type = signal["type"]
            direction = signal["direction"]
            target_channel_id = CHANNELS.get("alerts-high")
            print(f"Parsed as text (v1.4): {signal_type} {direction}")

        # Send to the appropriate Discord channel
        if target_channel_id:
            await send_discord_message(target_channel_id, embed=embed)

        # Log to system-log channel
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            et_now = get_et_now()
            log_msg = f"\U0001f4cb `{et_now.strftime('%H:%M ET')}` {log_action} received: **{signal_type} {direction}**"
            await send_discord_message(log_channel, content=log_msg)

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "action_taken": log_action,
                "signal_type": signal_type,
                "direction": direction,
            },
        )

    except Exception as e:
        print(f"Webhook error: {e}")
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            await send_discord_message(
                log_channel,
                content=f"\u274c Webhook error: `{str(e)}`",
            )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/test")
async def test_alert():
    """Send a test alert to verify Discord connection -- uses v2.0 JSON format."""
    test_data = {
        "signal": "LONG",
        "signal_type": "TREND",
        "price": 19250.00,
        "sl": 19181.50,
        "tp1": 19343.25,
        "tp2": 19406.25,
        "stop_pts": 68.50,
        "atr": 62.27,
        "vwap": 19200.00,
        "ema9": 19240.00,
        "ema21": 19220.00,
        "adx": 28.5,
        "stoch_k": 42.3,
        "near_sr": "Pivot PP 19225.50",
        "conditions": "VWAP Bull | EMA Bull Stack | 1H Bullish | StochRSI Curl Up | ADX Trending",
        "volume_ratio": 1.35,
        "reputation": "ELIGIBLE",
        "consecutive_stops": 0,
        "version": "2.0",
        "status": "ENTRY"
    }

    embed = build_json_embed(test_data)

    channel_id = CHANNELS.get("alerts-high")
    result = await send_discord_message(channel_id, embed=embed)

    if result:
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            await send_discord_message(
                log_channel,
                content="\u2705 Test alert sent successfully (v2.0 format)",
            )
        return {"status": "Test alert sent"}
    else:
        return {"status": "Failed to send test alert"}


# =============================================================
# STARTUP
# =============================================================

@app.on_event("startup")
async def startup():
    """Log startup to system-log channel."""
    print("MNQ Agent starting up...")

    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    for name, ch_id in CHANNELS.items():
        if not ch_id:
            missing.append(f"CH_{name.upper().replace('-', '_')}")

    if missing:
        print(f"WARNING: Missing env vars: {', '.join(missing)}")
    else:
        print("All config loaded successfully")
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            et_now = get_et_now()
            await send_discord_message(
                log_channel,
                content=f"\U0001f7e2 **MNQ Agent v2.0 (Mona) online** -- {et_now.strftime('%B %d, %Y %I:%M %p ET')}",
            )
