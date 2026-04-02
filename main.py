"""
MNQ Trading Agent — Backend Server
Receives TradingView webhook alerts and posts to Discord.
"""

import os
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import aiohttp

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════

app = FastAPI(title="MNQ Trading Agent")


# ═══════════════════════════════════════════════════════════
# DISCORD HELPERS
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
# SIGNAL PARSER
# ═══════════════════════════════════════════════════════════

def parse_signal(message: str) -> dict:
    """Parse TradingView alert message into structured data."""
    signal = {
        "raw": message,
        "type": "UNKNOWN",
        "direction": "UNKNOWN",
        "details": [],
        "nearest_sr": None,
    }

    msg_upper = message.upper()

    # Determine signal type
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

    # Extract details between pipes
    if "—" in message:
        parts = message.split("—", 1)
        if len(parts) > 1:
            detail_str = parts[1]
            signal["details"] = [d.strip() for d in detail_str.split("|")]

    # Extract nearest S/R
    if "Near:" in message:
        near_part = message.split("Near:")[-1].strip()
        signal["nearest_sr"] = near_part

    return signal


def build_signal_embed(signal: dict) -> dict:
    """Build a rich Discord embed from a parsed signal."""
    is_long = signal["direction"] == "LONG"
    is_squeeze = signal["type"] == "SQUEEZE"

    # Colors
    if is_long:
        color = 0x00FF00  # Green
    else:
        color = 0xFF0000  # Red

    if is_squeeze:
        color = 0x00CED1 if is_long else 0x9B59B6  # Teal or Purple

    # Title
    emoji = "🟢" if is_long else "🔴"
    type_label = "TREND CONTINUATION" if signal["type"] == "TREND" else "SQUEEZE BREAKOUT"
    title = f"{emoji} MNQ {type_label} — {signal['direction']}"

    # Build fields
    fields = []

    for detail in signal["details"]:
        if detail and "Near:" not in detail:
            fields.append({
                "name": "Condition",
                "value": detail.strip(),
                "inline": True,
            })

    if signal["nearest_sr"]:
        fields.append({
            "name": "📍 Nearest S/R",
            "value": signal["nearest_sr"],
            "inline": False,
        })

    # Timestamp
    et_now = get_et_now()
    timestamp_str = et_now.strftime("%I:%M %p ET")

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"MNQ Agent v1.4 • {timestamp_str}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return embed


# ═══════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """Health check."""
    return {"status": "MNQ Agent running", "version": "1.4"}


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive TradingView webhook alert.
    
    TradingView sends a POST with the alert message in the body.
    """
    try:
        body = await request.body()
        message = body.decode("utf-8").strip()

        if not message:
            raise HTTPException(status_code=400, detail="Empty message")

        print(f"Received webhook: {message}")

        # Parse the signal
        signal = parse_signal(message)

        # Build the embed
        embed = build_signal_embed(signal)

        # Determine which channel to post to
        # TREND signals → alerts-high (confirmed trend)
        # SQUEEZE signals → alerts-high (breakout confirmed)
        channel_id = CHANNELS.get("alerts-high")

        # Send to Discord
        result = await send_discord_message(channel_id, embed=embed)

        # Log to system-log channel
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            et_now = get_et_now()
            log_msg = f"📋 `{et_now.strftime('%H:%M ET')}` Signal received: **{signal['type']} {signal['direction']}**"
            await send_discord_message(log_channel, content=log_msg)

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "signal_type": signal["type"],
                "direction": signal["direction"],
            },
        )

    except Exception as e:
        print(f"Webhook error: {e}")
        # Log error to system-log
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            await send_discord_message(
                log_channel,
                content=f"❌ Webhook error: `{str(e)}`",
            )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/test")
async def test_alert():
    """Send a test alert to verify Discord connection."""
    test_message = "MNQ TREND LONG — VWAP Bull | EMA Bull Stack | 1H Bullish | StochRSI Curl Up | Near: Pivot R1 24200.00"

    signal = parse_signal(test_message)
    embed = build_signal_embed(signal)

    channel_id = CHANNELS.get("alerts-high")
    result = await send_discord_message(channel_id, embed=embed)

    if result:
        # Also log it
        log_channel = CHANNELS.get("system-log")
        if log_channel:
            await send_discord_message(
                log_channel,
                content="✅ Test alert sent successfully",
            )
        return {"status": "Test alert sent"}
    else:
        return {"status": "Failed to send test alert"}


# ═══════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Log startup to system-log channel."""
    print("MNQ Agent starting up...")

    # Verify config
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
                content=f"🟢 **MNQ Agent v1.4 online** — {et_now.strftime('%B %d, %Y %I:%M %p ET')}",
            )
