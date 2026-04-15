"""
v3.0 schema + additive migration per §4 of MONA_v3_OptionC_Spec.md.

Principles (§4.1):
  P1 — positions table is persisted from day one.
  P2 — signal_id is the position's PK, assigned at creation, never reconstructed.
  P3 — bar_close_ms is the interoperability key between Pine and backend.
       Exact-match only, no fuzzy lookups.
  P4 — schema changes are additive (ADD COLUMN / CREATE TABLE only; no DROP).

Tolerance mode (§10.3 Step 1):
  Legacy v2.x tables (`signals`, `eval_results`) are left untouched. v3 tables
  coexist with them so the backend can serve both payload shapes during the
  transition window.

Constants consumed by outage-recovery code (§8):
  BAR_INTERVAL_MS           — 15 minutes in ms
  MAX_REPLAY_BARS           — §8.2 recovery budget, 16 bars = 4h on 15m
  STALENESS_THRESHOLD_BARS  — §8.5, 2 bars = 30min
  STALENESS_CADENCE_BARS    — §8.5, every 4 bars = 1h
  SESSION_START_ET / SESSION_END_ET — MNQ RTH window; EOD cutoff at 16:00 ET.
"""
import sqlite3

BAR_INTERVAL_MS = 15 * 60 * 1000  # 900_000

MAX_REPLAY_BARS = 16
STALENESS_THRESHOLD_BARS = 2
STALENESS_CADENCE_BARS = 4

SESSION_START_ET = (9, 30)
SESSION_END_ET = (16, 0)


def init_db(conn: sqlite3.Connection) -> None:
    """
    Create v3 tables + indexes. Idempotent (all CREATE IF NOT EXISTS).
    Does not modify or drop any legacy v2.x table — tolerance mode.
    """
    c = conn.cursor()
    c.execute("PRAGMA foreign_keys = ON")

    # ---- signals_v3 ----
    # The full v3.0 column set lives here; bar_close_ms is added by the
    # migration function below when upgrading an existing DB (§4.2).
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals_v3 (
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
            conditions         TEXT,
            bar_close_ms       INTEGER
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_v3_bar_close_ms
          ON signals_v3(bar_close_ms)
          WHERE bar_close_ms IS NOT NULL
    """)

    # ---- evaluations ---- (unchanged shape from current backend)
    c.execute("""
        CREATE TABLE IF NOT EXISTS evaluations (
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
        )
    """)

    # ---- trade_outcomes ---- (unchanged shape)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
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
            is_ghost            INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (signal_id) REFERENCES signals_v3(signal_id)
        )
    """)

    # ---- Legacy v2.x tables — tolerance mode coexistence (§10.3 Step 1) ----
    # Kept alongside v3 tables so the backend can handle v2.1.1 Pine payloads
    # during the migration window. Shape is minimal — we only need enough
    # columns for the tolerance dispatcher's writes to succeed.
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT NOT NULL,
            signal        TEXT NOT NULL,
            signal_type   TEXT NOT NULL,
            entry_price   REAL,
            sl            REAL,
            tp1           REAL,
            tp2           REAL,
            payload_json  TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            eval_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT NOT NULL,
            signal        TEXT,
            signal_type   TEXT,
            result        TEXT NOT NULL,
            payload_json  TEXT
        )
    """)

    # ---- positions (new in v3.0 Option C, §4.5) ----
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            signal_id              INTEGER PRIMARY KEY,
            bar_close_ms           INTEGER NOT NULL,
            direction              INTEGER NOT NULL,
            signal_type            INTEGER NOT NULL,
            entry_price            REAL NOT NULL,
            sl                     REAL NOT NULL,
            tp1                    REAL NOT NULL,
            tp2                    REAL NOT NULL,
            opened_at_ms           INTEGER NOT NULL,
            opened_at_ts           TEXT NOT NULL,
            state                  INTEGER NOT NULL,
            tp1_hit                INTEGER NOT NULL DEFAULT 0,
            tp1_hit_bar_ms         INTEGER,
            effective_sl           REAL NOT NULL,
            mae_points             REAL NOT NULL DEFAULT 0,
            mae_bar_ms             INTEGER,
            mfe_points             REAL NOT NULL DEFAULT 0,
            mfe_bar_ms             INTEGER,
            post_tp1_mae_points    REAL NOT NULL DEFAULT 0,
            last_heartbeat_bar_ms  INTEGER,
            heartbeats_processed   INTEGER NOT NULL DEFAULT 0,
            last_observed_close    REAL NOT NULL DEFAULT 0,
            closed_at_ms           INTEGER,
            closed_at_ts           TEXT,
            exit_reason            TEXT,
            final_pnl_points       REAL,
            FOREIGN KEY (signal_id) REFERENCES signals_v3(signal_id)
        )
    """)
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_bar_close_ms "
        "ON positions(bar_close_ms)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_positions_state "
        "ON positions(state) WHERE state != 3"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_timestamp "
        "ON signals_v3(timestamp)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_signal ON evaluations(signal_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcome_signal ON trade_outcomes(signal_id)"
    )

    conn.commit()


def migrate_add_bar_close_ms_to_signals_v3(conn: sqlite3.Connection) -> None:
    """
    Additive migration for databases that already have signals_v3 without
    the bar_close_ms column. Safe to run against a fresh DB (no-op).
    """
    c = conn.cursor()
    cols = {row[1] for row in c.execute("PRAGMA table_info(signals_v3)").fetchall()}
    if "bar_close_ms" not in cols:
        c.execute("ALTER TABLE signals_v3 ADD COLUMN bar_close_ms INTEGER")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_v3_bar_close_ms "
        "ON signals_v3(bar_close_ms) WHERE bar_close_ms IS NOT NULL"
    )
    conn.commit()
