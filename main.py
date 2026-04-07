"""
MNQ Trading Agent -- Backend Server
Receives TradingView webhook alerts, logs to SQLite, and posts to Discord.
"""

import os
import json
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import aiohttp

# =============================================================
# CONFIG & DATABASE SETUP
# =============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mnq-agent-2024")

CHANNELS = {
    "alerts-high":        os.getenv("CH_ALERTS_HIGH"),
    "trade-journal":      os.getenv("CH_TRADE_JOURNAL"),
    "system-log":         os.getenv("CH_SYSTEM_LOG"),
}

DISCORD_API = "https://discord.com/api/v10"

# Use persistent volume path if on Railway, otherwise local folder
DB_DIR = "/app/data" if os.path.exists("/app") else "."
DB_PATH = os.path.join(DB_DIR, "signals.db")

def init_db():
    """Initialize SQLite database and schemas."""
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            signal TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            price REAL, sl REAL, tp1 REAL, tp2 REAL, stop_pts REAL,
            atr REAL, vwap REAL, ema9 REAL, ema21 REAL, adx REAL,
            stoch_k REAL, near_sr TEXT, conditions TEXT, volume_ratio REAL,
            reputation TEXT, consecutive_stops INTEGER,
            follow_thru TEXT DEFAULT 'PENDING',
            outcome TEXT DEFAULT 'PENDING',
            exit_price REAL, pnl_points REAL, notes TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS eval_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            timestamp TEXT NOT NULL,
            signal TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            price REAL, follow_thru_target REAL, result TEXT,
            reputation TEXT, consecutive_stops INTEGER,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        )''')
        conn.commit()

# =============================================================
# APP
# =============================================================

app = FastAPI(title="MNQ Trading Agent")

# =============================================================
# DISCORD HELPERS
# =============================================================

async def send_discord_message(channel_id: str, content: str = None, embed: dict = None):
    if not channel_id or not DISCORD_BOT_TOKEN:
        return None

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {}
    if content: payload["content"] = content
    if embed: payload["embeds"] = [embed]

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            else:
                print(f"Discord error {resp.status}: {await resp.text()}")
                return None

def get_et_now():
    return datetime.now(timezone.utc) - timedelta(hours=4)

# =============================================================
# EMBED BUILDERS
# =============================================================

def build_json_embed(data: dict, signal_id: int) -> dict:
    """Build actionable entry embed with injected DB ID."""
    direction = data.get("signal", "UNKNOWN")
    signal_type = data.get("signal_type", "TREND")
    is_long = direction == "LONG"
    is_squeeze = signal_type == "SQUEEZE"

    color = 0x00CED1 if is_squeeze and is_long else 0x9B59B6 if is_squeeze else 0x00FF00 if is_long else 0xFF0000
    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    type_label = "TREND CONTINUATION" if signal_type == "TREND" else "SQUEEZE BREAKOUT"
    
    fields = []
    
    if data.get("price"):
        levels_text = (
            f"\U0001f4cd **Entry:** {data.get('price'):.2f}\n"
            f"\U0001f6d1 **Stop:** {data.get('sl'):.2f} ({data.get('stop_pts'):.1f} pts)\n"
            f"\U0001f3af **TP1:** {data.get('tp1'):.2f} ({data.get('atr', 0) * 1.5:.1f} pts)\n"
            f"\U0001f680 **TP2:** {data.get('tp2'):.2f} ({data.get('atr', 0) * 2.5:.1f} pts)"
        )
        fields.append({"name": "\U0001f4b0 Trade Levels", "value": levels_text, "inline": False})

    fields.append({"name": "\U0001f9e0 Mona's Conviction", "value": f"**{data.get('reputation')}** (Stops: {data.get('consecutive_stops')})", "inline": False})

    et_now = get_et_now()
    embed = {
        "title": f"{emoji} MNQ {type_label} -- {direction}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"MNQ Agent v2.0 • Signal ID: #{signal_id} • {et_now.strftime('%I:%M %p ET')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed

def build_eval_embed(data: dict, signal_id: int) -> dict:
    """Build evaluation result embed."""
    is_pass = data.get("result") == "PASS"
    color = 0x00FF00 if is_pass else 0xFF0000
    icon = "\u2705" if is_pass else "\u274c"

    fields = [
        {"name": "Signal Price", "value": f"{data.get('price', 0):.2f}", "inline": True},
        {"name": "Target Hit?", "value": f"{data.get('follow_thru_target', 0):.2f}", "inline": True},
        {"name": "\U0001f9e0 New Reputation", "value": f"**{data.get('reputation')}** (Stops: {data.get('consecutive_stops')})", "inline": False}
    ]

    embed = {
        "title": f"{icon} EVAL {data.get('result')} -- {data.get('signal_type')} {data.get('signal')}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"MNQ Agent v2.0 • Parent ID: #{signal_id if signal_id else 'N/A'}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed

# =============================================================
# WEBHOOK ROUTE & DB LOGIC
# =============================================================

@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    message = body.decode("utf-8").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    try:
        data = json.loads(message)
        status = data.get("status", "ENTRY")
        signal_type = data.get("signal_type", "UNKNOWN")
        direction = data.get("signal", "UNKNOWN")
        timestamp_iso = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()

            # ROUTE 1: ACTIONABLE ENTRY
            if status == "ENTRY":
                c.execute('''INSERT INTO signals 
                    (timestamp, signal, signal_type, price, sl, tp1, tp2, stop_pts, atr, vwap, ema9, ema21, adx, stoch_k, near_sr, conditions, volume_ratio, reputation, consecutive_stops) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                    (timestamp_iso, direction, signal_type, data.get('price'), data.get('sl'), data.get('tp1'), data.get('tp2'), data.get('stop_pts'), data.get('atr'), data.get('vwap'), data.get('ema9'), data.get('ema21'), data.get('adx'), data.get('stoch_k'), data.get('near_sr'), data.get('conditions'), data.get('volume_ratio'), data.get('reputation'), data.get('consecutive_stops')))
                
                signal_id = c.lastrowid
                embed = build_json_embed(data, signal_id)
                target_channel = CHANNELS.get("alerts-high")

            # ROUTE 2: MONA'S HOMEWORK (EVAL RESULT)
            elif status == "EVAL_RESULT":
                # Lookback Matcher: Find parent signal from the last 2 hours
                c.execute('''SELECT id FROM signals 
                             WHERE signal_type = ? AND signal = ? 
                             AND timestamp >= datetime('now', '-2 hours')
                             ORDER BY id DESC LIMIT 1''', (signal_type, direction))
                row = c.fetchone()
                signal_id = row[0] if row else None

                if signal_id:
                    c.execute('''UPDATE signals SET follow_thru = ? WHERE id = ?''', (data.get("result"), signal_id))
                
                c.execute('''INSERT INTO eval_results 
                    (signal_id, timestamp, signal, signal_type, price, follow_thru_target, result, reputation, consecutive_stops)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (signal_id, timestamp_iso, direction, signal_type, data.get('price'), data.get('follow_thru_target'), data.get("result"), data.get('reputation'), data.get('consecutive_stops')))
                
                embed = build_eval_embed(data, signal_id)
                target_channel = CHANNELS.get("trade-journal")

        # Send to Discord
        if target_channel:
            await send_discord_message(target_channel, embed=embed)

        return {"status": "ok", "action": status, "signal_id": signal_id}

    except Exception as e:
        print(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("startup")
async def startup():
    init_db()
    print(f"Database initialized at {DB_PATH}")
    log_channel = CHANNELS.get("system-log")
    if log_channel:
        await send_discord_message(log_channel, content=f"\U0001f7e2 **MNQ Agent v2.0 (Mona) DB Connected** -- {get_et_now().strftime('%I:%M %p ET')}")
