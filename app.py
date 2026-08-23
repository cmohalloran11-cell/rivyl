import hashlib
import json
import os
import random
import re
import string
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for, flash

# Postgres (Neon / Vercel Postgres) -- this app used to run on a local SQLite
# file, which doesn't work on serverless platforms (read-only filesystem, no
# persistent disk). DATABASE_URL must be set; there's no SQLite fallback --
# maintaining two SQL dialects side by side isn't worth the drift/bug risk.
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")

app = Flask(__name__)
_SECRET_KEY = os.environ.get("SECRET_KEY")
if not _SECRET_KEY:
    if os.environ.get("VERCEL"):
        # Session cookies are signed, not encrypted -- a hardcoded fallback
        # secret in production would let anyone forge a cookie claiming to
        # own any team in any league. Refuse to boot instead of doing that.
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. Generate one "
            "(e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`) "
            "and set it in the Vercel project's Environment Variables."
        )
    _SECRET_KEY = "dev-secret-change-me"  # fine for local dev only
app.secret_key = _SECRET_KEY

_PLACEHOLDER_RE = re.compile(r"\?")


def _translate_sql(sql):
    """Lets the rest of this file keep using SQLite-flavored SQL (`?`
    placeholders, `datetime('now')`, `last_insert_rowid()`) untouched --
    translated to Postgres equivalents at the point of execution."""
    sql = sql.replace("datetime('now')", "NOW()")
    sql = sql.replace("last_insert_rowid()", "lastval()")
    return _PLACEHOLDER_RE.sub("%s", sql)


class PGCursor:
    """Thin wrapper so callers can keep doing `db.execute(...).fetchone()`
    / `.fetchall()` the way they did against sqlite3."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


class PGConnection:
    """sqlite3.Connection-shaped facade over a psycopg2 connection -- so the
    rest of the app's `db.execute(...)` / `db.executemany(...)` /
    `db.commit()` call sites don't need to change one by one."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(_translate_sql(sql), params)
        return PGCursor(cur)

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(_translate_sql(sql), seq_of_params)
        return PGCursor(cur)

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(_translate_sql(sql))
        return PGCursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

TEAM_COUNT_CHOICES = [4, 6, 8, 10, 12, 14, 16, 32]
SCORING_CHOICES = ["Standard", "Half PPR", "Full PPR"]
DRAFT_TYPE_CHOICES = ["Snake", "Auction", "Linear"]

# League format -- Redraft is the classic head-to-head season this app has
# always run. Dynasty keeps the same shape but with a much deeper roster
# (more bench/stash spots); true year-over-year keeper rollover isn't built
# since there's no season-boundary concept yet, so "Dynasty" here means the
# deep-roster, long-term-team-building feel, not persistent keepers across
# drafts. Knockout swaps the whole season structure: no head-to-head
# schedule -- every week the lowest scorer among teams still alive is
# eliminated, until one champion remains.
LEAGUE_FORMAT_CHOICES = ["Redraft", "Dynasty", "Knockout"]
DYNASTY_ROSTER_ROUNDS = 25

AI_PERSONALITIES = [
    "The Analyst", "The Trader", "Zero RB", "The Upside Hunter",
    "The Veteran", "The Contrarian", "The Value Hunter", "Ball Hawk",
    "The Rookie", "Old School", "Moneyball", "The Gambler",
    "Late Round QB", "The Grinder", "Chalk Eater", "The Sleeper Hunter",
    "The Taco",
]

# Curated logo "kits" -- an icon + color pairing a team can pick for its crest.
# No image uploads/hosting; this keeps it fast, safe, and always looks decent.
TEAM_KITS = [
    {"icon": "🦁", "color": "#ff5a36"}, {"icon": "🐺", "color": "#4c8dff"},
    {"icon": "🦅", "color": "#22c55e"}, {"icon": "⚡", "color": "#ffc94d"},
    {"icon": "🔥", "color": "#ff4757"}, {"icon": "💀", "color": "#9b7bff"},
    {"icon": "🐉", "color": "#22c55e"}, {"icon": "🛡️", "color": "#4c8dff"},
    {"icon": "⚔️", "color": "#8993a4"}, {"icon": "🏆", "color": "#ffc94d"},
    {"icon": "🐻", "color": "#ff5a36"}, {"icon": "🦈", "color": "#4c8dff"},
    {"icon": "🐯", "color": "#ff5a36"}, {"icon": "🦂", "color": "#9b7bff"},
    {"icon": "🐍", "color": "#22c55e"}, {"icon": "🌪️", "color": "#8993a4"},
    {"icon": "👑", "color": "#ffc94d"}, {"icon": "🎯", "color": "#ff4757"},
    {"icon": "🚀", "color": "#4c8dff"}, {"icon": "🐐", "color": "#8993a4"},
]
DEFAULT_KIT = {"icon": "🏈", "color": "#8993a4"}

ROSTER_ROUNDS = 16
SEASON_WEEKS = 14

# Free agency -- how much better (in rank points, same 1-420 value curve the
# draft engine uses) a free agent must be than an AI team's worst bench player
# at that position before the AI bothers making a waiver claim.
FA_IMPROVEMENT_THRESHOLD = 55
# AI teams re-check the waiver wire at most this often (seconds) -- keeps
# roster moves feeling like periodic waiver processing, not constant churn.
AI_MOVES_INTERVAL_SECONDS = 6 * 60 * 60

# Trades -- how much value (same curve) an AI team will accept losing in a
# trade before it's no longer a "fair enough" deal for that team to accept.
TRADE_VALUE_TOLERANCE = 0.15
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
RANKINGS_PATH = Path(__file__).parent / "data" / "rankings_top500.json"
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]

# Real NFL schedule (who plays whom each week), pulled from ESPN's public
# scoreboard API -- no key required. League week N is treated as NFL season
# week N of this year's schedule.
NFL_SEASON_YEAR = 2026
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
TEAM_ABBR_TO_ESPN = {"WAS": "WSH"}
TEAM_ABBR_FROM_ESPN = {v: k for k, v in TEAM_ABBR_TO_ESPN.items()}

SLEEPER_HEADSHOT_URL = "https://sleepercdn.com/content/nfl/players/{id}.jpg"
TEAM_LOGO_URL = "https://sleepercdn.com/images/team_logos/nfl/{team}.png"

# Which stored rank column to sort/score by, per league scoring format --
# the bundled rankings carry all three so draft value actually reflects PPR
# vs. Half PPR vs. Standard, the way real fantasy sites do.
RANK_COLUMN_BY_SCORING = {"Standard": "rank_std", "Half PPR": "rank_half", "Full PPR": "rank_ppr"}

AI_SPEED_CHOICES = ["instant", "fast", "realistic", "slow"]
AI_SPEED_LABELS = {
    "instant": "Instant — picks immediately",
    "fast": "Fast — 0.6-1.8s (default)",
    "realistic": "Realistic — 1-4s",
    "slow": "Slow — 3-8s",
}
HUMAN_TIMER_CHOICES = [30, 60, 90, 120]

STARTER_REQUIREMENTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
FLEX_SLOTS = 1
# FLEX is a roster SLOT that an RB or WR can fill -- not a position of its own.
# TE doesn't flex here (some leagues allow it; this one doesn't).
FLEX_ELIGIBLE = {"RB", "WR"}

# Persistent per-pick lineup slot codes (stored on draft_picks.lineup_slot),
# so a user's drag-and-drop lineup changes stick instead of being recomputed.
STARTER_SLOT_ORDER = ["QB", "RB1", "RB2", "WR1", "WR2", "TE", "K", "DEF", "FLEX"]
SLOT_ELIGIBLE_POSITIONS = {
    "QB": {"QB"}, "RB1": {"RB"}, "RB2": {"RB"}, "WR1": {"WR"}, "WR2": {"WR"},
    "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"}, "FLEX": FLEX_ELIGIBLE,
    "BN": set(STARTER_REQUIREMENTS),
}


def slot_display_label(slot_code):
    return slot_code[:-1] if slot_code[-1].isdigit() else slot_code
# Soft target: bonus for adding depth at a position stops once a team hits this
# many (e.g. most single-QB rosters settle on 2 QBs -- a starter + a backup).
BENCH_SOFT_CAP = {"QB": 2, "RB": 6, "WR": 7, "TE": 2, "K": 1, "DEF": 1}
# Hard ceiling: AI teams will never draft past this many at a position, full stop.
HARD_CAP = {"QB": 3, "TE": 3, "K": 1, "DEF": 1, "RB": 8, "WR": 9}

# Deterministic, non-LLM decision profiles. Every AI pick is scored from these
# knobs (see score_players / choose_from_scored) -- no network calls, no waiting.
PERSONALITY_PROFILES = {
    "The Analyst": {},
    "Chalk Eater": {},
    "The Grinder": {"pos_bonus": {"RB": 5}},
    "The Trader": {"pos_bonus": {"RB": 6, "WR": 6, "QB": -5, "TE": -5}},
    "Zero RB": {"pos_bonus": {"WR": 16}, "pos_penalty_before_round": {"RB": (-24, 5)}},
    "The Upside Hunter": {"upside_weight": 16},
    "The Veteran": {"exp_bonus_per_year": 3, "exp_cap": 8, "rookie_penalty": -12},
    "The Rookie": {"exp_bonus_per_year": -3, "exp_cap": 8, "rookie_bonus": 14},
    "Old School": {"pos_bonus": {"RB": 8}, "exp_bonus_per_year": 1.5, "exp_cap": 10},
    "Moneyball": {"need_multiplier": 1.8},
    "The Gambler": {"noise_weight": 22, "reach_chance": 0.35, "reach_pool": 6},
    "The Contrarian": {"reach_chance": 0.45, "reach_pool": 5, "noise_weight": 10},
    "The Value Hunter": {"fall_weight": 0.7},
    "The Sleeper Hunter": {"fall_weight": 0.4, "deep_bonus_after_round": (10, 4)},
    "Late Round QB": {"pos_penalty_before_round": {"QB": (-30, 8)}, "pos_bonus_after_round": {"QB": (8, 8)}},
    "Ball Hawk": {"pos_bonus": {"WR": 6}},
    "The Taco": {"noise_weight": 34, "reach_chance": 0.5, "reach_pool": 9},
}

PERSONALITY_FLAVOR = {
    "The Analyst": "sticking to the numbers",
    "The Trader": "banking trade value",
    "Zero RB": "loading up on receivers early",
    "The Upside Hunter": "chasing ceiling over floor",
    "The Veteran": "leaning on proven production",
    "The Rookie": "betting on a fresh breakout",
    "Old School": "building around the run game",
    "Moneyball": "optimizing roster construction",
    "The Gambler": "taking a swing",
    "The Contrarian": "fading the consensus board",
    "The Value Hunter": "pouncing on a player who fell",
    "The Sleeper Hunter": "digging for a late-round sleeper",
    "Late Round QB": "punting the QB position early",
    "The Grinder": "grinding out a safe, solid pick",
    "Chalk Eater": "just taking the chalk, best player on the board",
    "Ball Hawk": "hunting for receiving upside",
    "The Taco": "going a little off-script here",
}

GRADE_THRESHOLDS = [
    (15, "A+"), (8, "A"), (3, "A-"), (0, "B+"), (-3, "B"),
    (-6, "B-"), (-10, "C+"), (-15, "C"), (-999, "D"),
]

# Pre-game projection only -- a deterministic point estimate from draft rank,
# same idea as every fantasy site's "projected points" column. Kept separate
# from real scoring below: this never claims to be a result, only an estimate.
PROJECTION_MODEL = {
    "QB": {"base": 13.0, "rank_bonus": 7.5},
    "RB": {"base": 7.0, "rank_bonus": 8.5},
    "WR": {"base": 6.5, "rank_bonus": 8.5},
    "TE": {"base": 4.5, "rank_bonus": 5.5},
    "K": {"base": 6.0, "rank_bonus": 2.0},
    "DEF": {"base": 6.0, "rank_bonus": 2.5},
}
PPR_PROJECTION_BONUS = {"Standard": 0.0, "Half PPR": 1.0, "Full PPR": 2.0}
# Pass-catching positions that benefit from PPR scoring -- distinct from
# FLEX_ELIGIBLE (a roster-slot rule); TE catches passes but doesn't flex here.
PPR_BONUS_POSITIONS = {"RB", "WR", "TE"}
INJURY_LABELS = {"Questionable": "Q", "Doubtful": "D", "Out": "O", "IR": "IR", "PUP": "PUP", "Suspended": "SUS"}

# ---------------------------------------------------------------------------
# Real fantasy scoring -- standard-style rules applied to actual box score
# stats pulled from ESPN. This is what makes "score" real instead of 0.
# ---------------------------------------------------------------------------
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"

PASS_YARDS_PER_PT = 25.0
RUSH_REC_YARDS_PER_PT = 10.0
PASS_TD_PTS = 4.0
RUSH_TD_PTS = 6.0
REC_TD_PTS = 6.0
INT_THROWN_PTS = -2.0
FUMBLE_LOST_PTS = -2.0
RECEPTION_PTS = {"Standard": 0.0, "Half PPR": 0.5, "Full PPR": 1.0}

FG_MADE_PTS = 3.0
XP_MADE_PTS = 1.0

DEF_SACK_PTS = 1.0
DEF_INT_PTS = 2.0
DEF_FUMBLE_REC_PTS = 2.0
DEF_TD_PTS = 6.0
POINTS_ALLOWED_TIERS = [
    (0, 10.0), (6, 7.0), (13, 4.0), (20, 1.0), (27, 0.0), (34, -1.0),
]
POINTS_ALLOWED_WORST = -4.0


def points_allowed_score(points_allowed):
    if points_allowed is None:
        return 0.0
    for cap, pts in POINTS_ALLOWED_TIERS:
        if points_allowed <= cap:
            return pts
    return POINTS_ALLOWED_WORST


def get_db():
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL (or POSTGRES_URL) environment variable is not set. "
                "Provision a Postgres database (e.g. Vercel Storage or Neon) and set it."
            )
        raw = psycopg2.connect(DATABASE_URL)
        g.db = PGConnection(raw)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc is not None:
            db.rollback()
        else:
            db.commit()
        db.close()


def init_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL (or POSTGRES_URL) environment variable is not set. "
            "Provision a Postgres database (e.g. Vercel Storage or Neon) and set it."
        )
    raw = psycopg2.connect(DATABASE_URL)
    db = PGConnection(raw)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS leagues (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            num_teams INTEGER NOT NULL,
            num_human_slots INTEGER NOT NULL,
            num_ai_slots INTEGER NOT NULL,
            scoring TEXT NOT NULL,
            roster_settings TEXT NOT NULL,
            draft_type TEXT NOT NULL,
            commissioner_name TEXT NOT NULL,
            invite_code TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            draft_status TEXT NOT NULL DEFAULT 'not_started',
            current_pick_index INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 0,
            ai_speed TEXT NOT NULL DEFAULT 'fast',
            human_timer_seconds INTEGER NOT NULL DEFAULT 90,
            pick_deadline REAL,
            grades_json TEXT
        );

        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            slot_index INTEGER NOT NULL,
            owner_type TEXT NOT NULL CHECK (owner_type IN ('human', 'ai')),
            status TEXT NOT NULL CHECK (status IN ('open', 'filled')),
            team_name TEXT,
            owner_name TEXT,
            ai_personality TEXT,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            is_commissioner INTEGER NOT NULL DEFAULT 0,
            logo_icon TEXT,
            logo_color TEXT
        );

        CREATE TABLE IF NOT EXISTS players (
            id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            position TEXT NOT NULL,
            nfl_team TEXT,
            search_rank INTEGER NOT NULL DEFAULT 999999,
            years_exp INTEGER NOT NULL DEFAULT 0,
            injury_status TEXT,
            rank_ppr INTEGER NOT NULL DEFAULT 999999,
            rank_half INTEGER NOT NULL DEFAULT 999999,
            rank_std INTEGER NOT NULL DEFAULT 999999,
            pos_rank TEXT,
            tier INTEGER
        );

        CREATE TABLE IF NOT EXISTS draft_picks (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            overall_pick INTEGER NOT NULL,
            round INTEGER NOT NULL,
            pick_in_round INTEGER NOT NULL,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_id TEXT REFERENCES players(id),
            player_name TEXT,
            position TEXT,
            nfl_team TEXT,
            player_rank INTEGER,
            reasoning TEXT,
            is_autopick INTEGER NOT NULL DEFAULT 0,
            drafted_at TEXT,
            lineup_slot TEXT,
            UNIQUE(league_id, overall_pick)
        );

        CREATE TABLE IF NOT EXISTS matchups (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            week INTEGER NOT NULL,
            team_a_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            team_b_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
            team_a_score REAL NOT NULL DEFAULT 0,
            team_b_score REAL NOT NULL DEFAULT 0,
            played INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS nfl_schedule (
            week INTEGER NOT NULL,
            team TEXT NOT NULL,
            opponent TEXT,
            is_home INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (week, team)
        );

        CREATE TABLE IF NOT EXISTS nfl_games (
            week INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT NOT NULL DEFAULT 'pre',
            updated_at REAL,
            stats_synced INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (week, event_id)
        );

        CREATE TABLE IF NOT EXISTS player_week_stats (
            week INTEGER NOT NULL,
            player_id TEXT NOT NULL,
            pts_ppr REAL NOT NULL DEFAULT 0,
            pts_half REAL NOT NULL DEFAULT 0,
            pts_std REAL NOT NULL DEFAULT 0,
            stat_line TEXT,
            game_status TEXT NOT NULL DEFAULT 'pre',
            updated_at REAL,
            PRIMARY KEY (week, player_id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            from_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            to_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            ai_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS trade_items (
            id SERIAL PRIMARY KEY,
            trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
            pick_id INTEGER NOT NULL REFERENCES draft_picks(id) ON DELETE CASCADE,
            from_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_name TEXT,
            position TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    table_migrations = {
        "leagues": {
            "draft_status": "ALTER TABLE leagues ADD COLUMN draft_status TEXT NOT NULL DEFAULT 'not_started'",
            "current_pick_index": "ALTER TABLE leagues ADD COLUMN current_pick_index INTEGER NOT NULL DEFAULT 0",
            "rounds": "ALTER TABLE leagues ADD COLUMN rounds INTEGER NOT NULL DEFAULT 0",
            "ai_speed": "ALTER TABLE leagues ADD COLUMN ai_speed TEXT NOT NULL DEFAULT 'fast'",
            "human_timer_seconds": "ALTER TABLE leagues ADD COLUMN human_timer_seconds INTEGER NOT NULL DEFAULT 90",
            "pick_deadline": "ALTER TABLE leagues ADD COLUMN pick_deadline REAL",
            "grades_json": "ALTER TABLE leagues ADD COLUMN grades_json TEXT",
            "current_week": "ALTER TABLE leagues ADD COLUMN current_week INTEGER NOT NULL DEFAULT 1",
            "ai_moves_at": "ALTER TABLE leagues ADD COLUMN ai_moves_at REAL",
            "ai_trade_offers_at": "ALTER TABLE leagues ADD COLUMN ai_trade_offers_at REAL",
            "league_format": "ALTER TABLE leagues ADD COLUMN league_format TEXT NOT NULL DEFAULT 'Redraft'",
        },
        "players": {
            "years_exp": "ALTER TABLE players ADD COLUMN years_exp INTEGER NOT NULL DEFAULT 0",
            "injury_status": "ALTER TABLE players ADD COLUMN injury_status TEXT",
            "rank_ppr": "ALTER TABLE players ADD COLUMN rank_ppr INTEGER NOT NULL DEFAULT 999999",
            "rank_half": "ALTER TABLE players ADD COLUMN rank_half INTEGER NOT NULL DEFAULT 999999",
            "rank_std": "ALTER TABLE players ADD COLUMN rank_std INTEGER NOT NULL DEFAULT 999999",
            "pos_rank": "ALTER TABLE players ADD COLUMN pos_rank TEXT",
            "tier": "ALTER TABLE players ADD COLUMN tier INTEGER",
        },
        "draft_picks": {
            "player_rank": "ALTER TABLE draft_picks ADD COLUMN player_rank INTEGER",
            "reasoning": "ALTER TABLE draft_picks ADD COLUMN reasoning TEXT",
            "is_autopick": "ALTER TABLE draft_picks ADD COLUMN is_autopick INTEGER NOT NULL DEFAULT 0",
            "lineup_slot": "ALTER TABLE draft_picks ADD COLUMN lineup_slot TEXT",
        },
        "matchups": {
            "played": "ALTER TABLE matchups ADD COLUMN played INTEGER NOT NULL DEFAULT 0",
        },
        "teams": {
            "logo_icon": "ALTER TABLE teams ADD COLUMN logo_icon TEXT",
            "logo_color": "ALTER TABLE teams ADD COLUMN logo_color TEXT",
            "eliminated_week": "ALTER TABLE teams ADD COLUMN eliminated_week INTEGER",
        },
        "nfl_games": {
            "stats_synced": "ALTER TABLE nfl_games ADD COLUMN stats_synced INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in table_migrations.items():
        existing_columns = {
            row["column_name"] for row in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            ).fetchall()
        }
        for column, ddl in columns.items():
            if column not in existing_columns:
                db.execute(ddl)

    db.commit()
    db.close()


def normalize_player_name(name):
    name = (name or "").lower()
    name = name.replace(".", "").replace("'", "").replace("-", " ")
    for suffix in (" jr", " sr", " ii", " iii", " iv", " v"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return " ".join(name.split())


def load_bundled_rankings():
    try:
        with open(RANKINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def sync_players(db, force=False):
    count = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    if count > 0 and not force:
        return True

    rankings = load_bundled_rankings()
    if not rankings:
        return count > 0

    # Sleeper is used only to enrich the real consensus board below with live
    # team assignment, experience, and current injury status -- it does not
    # define the player pool or the ranking order anymore.
    sleeper_by_key = {}
    try:
        resp = requests.get(SLEEPER_PLAYERS_URL, timeout=25)
        resp.raise_for_status()
        raw = resp.json()
        for pid, p in raw.items():
            if not isinstance(p, dict):
                continue
            pos = p.get("position")
            if pos not in POSITION_ORDER or pos == "DEF":
                continue
            full_name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if not full_name:
                continue
            key = (normalize_player_name(full_name), pos)
            years_exp = p.get("years_exp")
            entry = {
                "id": pid,
                "team": p.get("team"),
                "years_exp": years_exp if isinstance(years_exp, int) else 0,
                "injury_status": p.get("injury_status") or None,
            }
            # Prefer an active (rostered-to-a-team) match if several share a name.
            existing = sleeper_by_key.get(key)
            if existing is None or (existing["team"] is None and entry["team"] is not None):
                sleeper_by_key[key] = entry
    except Exception:
        pass

    rows = []
    for r in rankings:
        pos = r["pos"]
        if pos not in POSITION_ORDER:
            continue
        name = r["player"]
        team = r.get("team")

        if pos == "DEF":
            player_id = f"DEF-{team or name}"
            years_exp, injury_status = 0, None
        else:
            match = sleeper_by_key.get((normalize_player_name(name), pos))
            if match:
                player_id = match["id"]
                team = match["team"] or team
                years_exp, injury_status = match["years_exp"], match["injury_status"]
            else:
                player_id = f"GEN-{normalize_player_name(name)}-{pos}"
                years_exp, injury_status = 0, None

        rows.append((
            player_id, name, pos, team,
            r["rank_half"], years_exp, injury_status,
            r["rank_ppr"], r["rank_half"], r["rank_std"],
            r.get("pos_rank"), r.get("tier"),
        ))

    if not rows:
        return count > 0

    if force:
        db.execute("DELETE FROM players")
    db.executemany(
        """
        INSERT INTO players
            (id, full_name, position, nfl_team, search_rank, years_exp, injury_status,
             rank_ppr, rank_half, rank_std, pos_rank, tier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            full_name = EXCLUDED.full_name, position = EXCLUDED.position,
            nfl_team = EXCLUDED.nfl_team, search_rank = EXCLUDED.search_rank,
            years_exp = EXCLUDED.years_exp, injury_status = EXCLUDED.injury_status,
            rank_ppr = EXCLUDED.rank_ppr, rank_half = EXCLUDED.rank_half,
            rank_std = EXCLUDED.rank_std, pos_rank = EXCLUDED.pos_rank, tier = EXCLUDED.tier
        """,
        rows,
    )
    db.commit()
    return True


def sync_nfl_schedule(db, week, force=False):
    if week < 1:
        return False
    count = db.execute(
        "SELECT COUNT(*) FROM nfl_schedule WHERE week = ?", (week,)
    ).fetchone()[0]
    if count > 0 and not force:
        return True

    try:
        resp = requests.get(
            ESPN_SCOREBOARD_URL,
            params={"week": week, "seasontype": 2, "year": NFL_SEASON_YEAR},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return count > 0

    rows = []
    game_rows = []
    for event in data.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        if len(competitors) != 2:
            continue
        by_side = {c.get("homeAway"): c for c in competitors}
        home, away = by_side.get("home"), by_side.get("away")
        if not home or not away:
            continue
        home_abbr = TEAM_ABBR_FROM_ESPN.get(
            home["team"]["abbreviation"], home["team"]["abbreviation"]
        )
        away_abbr = TEAM_ABBR_FROM_ESPN.get(
            away["team"]["abbreviation"], away["team"]["abbreviation"]
        )
        rows.append((week, home_abbr, away_abbr, 1))
        rows.append((week, away_abbr, home_abbr, 0))

        status_state = competition.get("status", {}).get("type", {}).get("state", "pre")
        try:
            home_score = int(home.get("score")) if home.get("score") not in (None, "") else None
        except (TypeError, ValueError):
            home_score = None
        try:
            away_score = int(away.get("score")) if away.get("score") not in (None, "") else None
        except (TypeError, ValueError):
            away_score = None
        game_rows.append((
            week, event.get("id"), home_abbr, away_abbr, home_score, away_score, status_state, time.time(),
        ))

    if not rows:
        return count > 0

    if force:
        db.execute("DELETE FROM nfl_schedule WHERE week = ?", (week,))
        db.execute("DELETE FROM nfl_games WHERE week = ?", (week,))
    db.executemany(
        """
        INSERT INTO nfl_schedule (week, team, opponent, is_home) VALUES (?, ?, ?, ?)
        ON CONFLICT (week, team) DO UPDATE SET
            opponent = EXCLUDED.opponent, is_home = EXCLUDED.is_home
        """,
        rows,
    )
    db.executemany(
        """
        INSERT INTO nfl_games
            (week, event_id, home_team, away_team, home_score, away_score, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (week, event_id) DO UPDATE SET
            home_team = EXCLUDED.home_team, away_team = EXCLUDED.away_team,
            home_score = EXCLUDED.home_score, away_score = EXCLUDED.away_score,
            status = EXCLUDED.status, updated_at = EXCLUDED.updated_at, stats_synced = 0
        """,
        game_rows,
    )
    db.commit()
    return True


def get_schedule_map(db, week):
    sync_nfl_schedule(db, week)
    rows = db.execute("SELECT * FROM nfl_schedule WHERE week = ?", (week,)).fetchall()
    return {r["team"]: {"opponent": r["opponent"], "is_home": bool(r["is_home"])} for r in rows}


GAME_SCORE_REFRESH_SECONDS = 60


def refresh_game_scores(db, week):
    """Re-poll just scores/status for a week's games (cheaper than a full
    schedule resync) -- used to keep live scores current during game days."""
    try:
        resp = requests.get(
            ESPN_SCOREBOARD_URL,
            params={"week": week, "seasontype": 2, "year": NFL_SEASON_YEAR},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return False

    now = time.time()
    for event in data.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        by_side = {c.get("homeAway"): c for c in competitors}
        home, away = by_side.get("home"), by_side.get("away")
        if not home or not away:
            continue
        status_state = competition.get("status", {}).get("type", {}).get("state", "pre")
        try:
            home_score = int(home.get("score")) if home.get("score") not in (None, "") else None
        except (TypeError, ValueError):
            home_score = None
        try:
            away_score = int(away.get("score")) if away.get("score") not in (None, "") else None
        except (TypeError, ValueError):
            away_score = None
        db.execute(
            """
            UPDATE nfl_games SET home_score = ?, away_score = ?, status = ?, updated_at = ?
            WHERE week = ? AND event_id = ?
            """,
            (home_score, away_score, status_state, now, week, event.get("id")),
        )
    db.commit()
    return True


def ensure_live_games(db, week):
    games = db.execute("SELECT * FROM nfl_games WHERE week = ?", (week,)).fetchall()
    if not games:
        # force=True: nfl_schedule may already be cached from before nfl_games
        # existed, which would otherwise short-circuit this fetch and leave
        # nfl_games permanently empty for that week.
        sync_nfl_schedule(db, week, force=True)
        games = db.execute("SELECT * FROM nfl_games WHERE week = ?", (week,)).fetchall()
        return games

    if all(g["status"] == "post" for g in games):
        return games

    stalest = min((g["updated_at"] or 0) for g in games)
    if time.time() - stalest > GAME_SCORE_REFRESH_SECONDS:
        if refresh_game_scores(db, week):
            games = db.execute("SELECT * FROM nfl_games WHERE week = ?", (week,)).fetchall()
    return games


def to_float(s):
    if s is None:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def made_count(fraction_str):
    if not fraction_str or "/" not in fraction_str:
        return 0.0
    return to_float(fraction_str.split("/")[0])


def default_offense_stat():
    return {
        "pass_yards": 0.0, "pass_td": 0.0, "int": 0.0,
        "rush_yards": 0.0, "rush_td": 0.0,
        "rec": 0.0, "rec_yards": 0.0, "rec_td": 0.0,
        "fumbles_lost": 0.0, "fg_made": 0.0, "xp_made": 0.0,
    }


def compute_offense_points(o, scoring):
    pts = (
        o["pass_yards"] / PASS_YARDS_PER_PT
        + o["pass_td"] * PASS_TD_PTS
        + o["int"] * INT_THROWN_PTS
        + o["rush_yards"] / RUSH_REC_YARDS_PER_PT
        + o["rush_td"] * RUSH_TD_PTS
        + o["rec_yards"] / RUSH_REC_YARDS_PER_PT
        + o["rec_td"] * REC_TD_PTS
        + o["rec"] * RECEPTION_PTS.get(scoring, 0.0)
        + o["fumbles_lost"] * FUMBLE_LOST_PTS
        + o["fg_made"] * FG_MADE_PTS
        + o["xp_made"] * XP_MADE_PTS
    )
    return round(pts, 2)


def compute_def_points(d, points_allowed):
    pts = (
        d["sacks"] * DEF_SACK_PTS
        + d["def_int"] * DEF_INT_PTS
        + d.get("fumble_rec", 0.0) * DEF_FUMBLE_REC_PTS
        + d["def_td"] * DEF_TD_PTS
        + points_allowed_score(points_allowed)
    )
    return round(pts, 2)


def build_player_name_index(db):
    idx = {}
    for r in db.execute("SELECT id, full_name FROM players WHERE position != 'DEF'"):
        idx[normalize_player_name(r["full_name"])] = r["id"]
    return idx


def sync_week_stats(db, week, force=False):
    games = ensure_live_games(db, week)
    if not games:
        return False

    name_index = build_player_name_index(db)
    now = time.time()

    for game in games:
        if game["status"] == "pre":
            continue
        if game["status"] == "post" and game["stats_synced"] and not force:
            continue

        try:
            resp = requests.get(
                ESPN_SUMMARY_URL, params={"event": game["event_id"]}, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue

        team_players = (data.get("boxscore") or {}).get("players") or []
        if not team_players:
            continue

        offense = {}
        team_def_raw = {}
        team_fumbles_lost = {}

        for team_entry in team_players:
            espn_abbr = (team_entry.get("team") or {}).get("abbreviation", "")
            team_abbr = TEAM_ABBR_FROM_ESPN.get(espn_abbr, espn_abbr)
            team_def_raw.setdefault(team_abbr, {"sacks": 0.0, "def_int": 0.0, "def_td": 0.0})
            team_fumbles_lost.setdefault(team_abbr, 0.0)
            groups = {g.get("name"): g for g in team_entry.get("statistics", [])}

            def rows(group_name):
                grp = groups.get(group_name)
                if not grp:
                    return
                keys = grp.get("keys") or []
                for a in grp.get("athletes", []):
                    stat_values = a.get("stats") or []
                    yield a["athlete"]["displayName"], dict(zip(keys, stat_values))

            for name, d in rows("passing"):
                pid = name_index.get(normalize_player_name(name))
                if not pid:
                    continue
                o = offense.setdefault(pid, default_offense_stat())
                o["pass_yards"] += to_float(d.get("passingYards"))
                o["pass_td"] += to_float(d.get("passingTouchdowns"))
                o["int"] += to_float(d.get("interceptions"))

            for name, d in rows("rushing"):
                pid = name_index.get(normalize_player_name(name))
                if not pid:
                    continue
                o = offense.setdefault(pid, default_offense_stat())
                o["rush_yards"] += to_float(d.get("rushingYards"))
                o["rush_td"] += to_float(d.get("rushingTouchdowns"))

            for name, d in rows("receiving"):
                pid = name_index.get(normalize_player_name(name))
                if not pid:
                    continue
                o = offense.setdefault(pid, default_offense_stat())
                o["rec"] += to_float(d.get("receptions"))
                o["rec_yards"] += to_float(d.get("receivingYards"))
                o["rec_td"] += to_float(d.get("receivingTouchdowns"))

            for name, d in rows("fumbles"):
                lost = to_float(d.get("fumblesLost"))
                team_fumbles_lost[team_abbr] += lost
                pid = name_index.get(normalize_player_name(name))
                if pid:
                    o = offense.setdefault(pid, default_offense_stat())
                    o["fumbles_lost"] += lost

            for name, d in rows("kicking"):
                pid = name_index.get(normalize_player_name(name))
                if not pid:
                    continue
                o = offense.setdefault(pid, default_offense_stat())
                o["fg_made"] += made_count(d.get("fieldGoalsMade/fieldGoalAttempts"))
                o["xp_made"] += made_count(d.get("extraPointsMade/extraPointAttempts"))

            for _name, d in rows("defensive"):
                team_def_raw[team_abbr]["sacks"] += to_float(d.get("sacks"))
                team_def_raw[team_abbr]["def_td"] += to_float(d.get("defensiveTouchdowns"))

            for _name, d in rows("interceptions"):
                team_def_raw[team_abbr]["def_int"] += to_float(d.get("interceptions"))
                team_def_raw[team_abbr]["def_td"] += to_float(d.get("interceptionTouchdowns"))

            for _name, d in rows("kickReturns"):
                team_def_raw[team_abbr]["def_td"] += to_float(d.get("kickReturnTouchdowns"))

            for _name, d in rows("puntReturns"):
                team_def_raw[team_abbr]["def_td"] += to_float(d.get("puntReturnTouchdowns"))

        team_abbrs = list(team_def_raw.keys())
        if len(team_abbrs) == 2:
            a, b = team_abbrs
            team_def_raw[a]["fumble_rec"] = team_fumbles_lost.get(b, 0.0)
            team_def_raw[b]["fumble_rec"] = team_fumbles_lost.get(a, 0.0)

        points_allowed = {
            game["home_team"]: game["away_score"],
            game["away_team"]: game["home_score"],
        }

        stat_rows = []
        for pid, o in offense.items():
            stat_rows.append((
                week, pid,
                compute_offense_points(o, "Full PPR"),
                compute_offense_points(o, "Half PPR"),
                compute_offense_points(o, "Standard"),
                json.dumps(o), game["status"], now,
            ))
        for team_abbr, d in team_def_raw.items():
            def_id = f"DEF-{team_abbr}"
            pts = compute_def_points(d, points_allowed.get(team_abbr))
            stat_rows.append((
                week, def_id, pts, pts, pts, json.dumps(d), game["status"], now,
            ))

        if stat_rows:
            db.executemany(
                """
                INSERT INTO player_week_stats
                    (week, player_id, pts_ppr, pts_half, pts_std, stat_line, game_status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (week, player_id) DO UPDATE SET
                    pts_ppr = EXCLUDED.pts_ppr, pts_half = EXCLUDED.pts_half, pts_std = EXCLUDED.pts_std,
                    stat_line = EXCLUDED.stat_line, game_status = EXCLUDED.game_status, updated_at = EXCLUDED.updated_at
                """,
                stat_rows,
            )

        if game["status"] == "post":
            db.execute(
                "UPDATE nfl_games SET stats_synced = 1 WHERE week = ? AND event_id = ?",
                (week, game["event_id"]),
            )
        db.commit()

    return True


def get_player_week_points(db, week, player_id, scoring):
    col = {"Standard": "pts_std", "Half PPR": "pts_half", "Full PPR": "pts_ppr"}.get(scoring, "pts_half")
    row = db.execute(
        f"SELECT {col} AS pts, game_status FROM player_week_stats WHERE week = ? AND player_id = ?",
        (week, player_id),
    ).fetchone()
    if row is None:
        return 0.0, None
    return row["pts"], row["game_status"]


def get_team_live_score(db, league_id, team_id, week, scoring):
    starters, _ = build_lineup(db, league_id, team_id)
    total = 0.0
    for p in starters:
        if not p["player_id"]:
            continue
        pts, _status = get_player_week_points(db, week, p["player_id"], scoring)
        total += pts
    return round(total, 1)


def player_face_url(player_id, position, nfl_team):
    if position == "DEF":
        team = (nfl_team or "").lower()
        return TEAM_LOGO_URL.format(team=team) if team else None
    if player_id and player_id[:1].isdigit():
        return SLEEPER_HEADSHOT_URL.format(id=player_id)
    return None


def player_initials(name):
    parts = [p for p in (name or "").replace(".", "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def generate_invite_code():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choices(alphabet, k=6))


def get_my_team_id(league_id, teams):
    """No accounts/auth here -- 'my team' is whichever team this browser session
    created or joined as. Falls back to the first human team, then any team."""
    session_key = f"team_{league_id}"
    team_id = session.get(session_key)
    if team_id and any(t["id"] == team_id for t in teams):
        return team_id
    human = next((t for t in teams if t["owner_type"] == "human" and t["status"] == "filled"), None)
    if human:
        return human["id"]
    return teams[0]["id"] if teams else None


def is_league_creator(db, league_id):
    """True if this browser session created the league (i.e. holds the
    commissioner's team slot). Our only proxy for 'ownership' -- no accounts."""
    commish = db.execute(
        "SELECT id FROM teams WHERE league_id = ? AND is_commissioner = 1", (league_id,)
    ).fetchone()
    return commish is not None and session.get(f"team_{league_id}") == commish["id"]


@app.route("/")
def index():
    db = get_db()
    leagues = db.execute(
        """
        SELECT l.*,
               (SELECT COUNT(*) FROM teams t WHERE t.league_id = l.id AND t.status = 'open') AS open_slots,
               (SELECT id FROM teams t WHERE t.league_id = l.id AND t.is_commissioner = 1) AS commissioner_team_id
        FROM leagues l
        ORDER BY l.created_at DESC
        """
    ).fetchall()
    deletable_ids = {
        l["id"] for l in leagues
        if l["commissioner_team_id"] is not None
        and session.get(f"team_{l['id']}") == l["commissioner_team_id"]
    }
    return render_template("index.html", leagues=leagues, deletable_ids=deletable_ids)


@app.route("/leagues/new", methods=["GET", "POST"])
def new_league():
    if request.method == "GET":
        return render_template(
            "create_league.html",
            team_count_choices=TEAM_COUNT_CHOICES,
            scoring_choices=SCORING_CHOICES,
            draft_type_choices=DRAFT_TYPE_CHOICES,
            league_format_choices=LEAGUE_FORMAT_CHOICES,
        )

    name = request.form.get("league_name", "").strip()
    commissioner_name = request.form.get("commissioner_name", "").strip()
    commissioner_team_name = request.form.get("commissioner_team_name", "").strip()
    scoring = request.form.get("scoring", SCORING_CHOICES[0])
    roster_settings = request.form.get("roster_settings", "").strip() or "Standard (1QB/2RB/2WR/1TE/1FLEX/1DST/1K)"
    draft_type = request.form.get("draft_type", DRAFT_TYPE_CHOICES[0])
    league_format = request.form.get("league_format", LEAGUE_FORMAT_CHOICES[0])
    if league_format not in LEAGUE_FORMAT_CHOICES:
        league_format = LEAGUE_FORMAT_CHOICES[0]

    try:
        num_teams = int(request.form.get("num_teams", 0))
        num_human_slots = int(request.form.get("num_human_slots", 0))
        num_ai_slots = int(request.form.get("num_ai_slots", 0))
    except ValueError:
        flash("Team counts must be numbers.")
        return redirect(url_for("new_league"))

    errors = []
    if not name:
        errors.append("League name is required.")
    if not commissioner_name:
        errors.append("Your name is required.")
    if num_teams not in TEAM_COUNT_CHOICES:
        errors.append("Choose a valid number of teams.")
    if num_human_slots < 1:
        errors.append("At least one human slot is required (that's you).")
    if num_human_slots + num_ai_slots != num_teams:
        errors.append(
            f"Human slots ({num_human_slots}) + AI slots ({num_ai_slots}) must add up to "
            f"the number of teams ({num_teams})."
        )

    if errors:
        for err in errors:
            flash(err)
        return redirect(url_for("new_league"))

    db = get_db()
    invite_code = generate_invite_code()
    while db.execute("SELECT 1 FROM leagues WHERE invite_code = ?", (invite_code,)).fetchone():
        invite_code = generate_invite_code()

    cur = db.execute(
        """
        INSERT INTO leagues (name, num_teams, num_human_slots, num_ai_slots, scoring,
                              roster_settings, draft_type, commissioner_name, invite_code, league_format)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (name, num_teams, num_human_slots, num_ai_slots, scoring, roster_settings,
         draft_type, commissioner_name, invite_code, league_format),
    )
    league_id = cur.fetchone()["id"]

    slot_index = 1

    commish_cur = db.execute(
        """
        INSERT INTO teams (league_id, slot_index, owner_type, status, team_name,
                            owner_name, is_commissioner)
        VALUES (?, ?, 'human', 'filled', ?, ?, 1)
        RETURNING id
        """,
        (league_id, slot_index, commissioner_team_name or f"{commissioner_name}'s Team", commissioner_name),
    )
    session[f"team_{league_id}"] = commish_cur.fetchone()["id"]
    slot_index += 1

    for _ in range(num_human_slots - 1):
        db.execute(
            """
            INSERT INTO teams (league_id, slot_index, owner_type, status)
            VALUES (?, ?, 'human', 'open')
            """,
            (league_id, slot_index),
        )
        slot_index += 1

    personalities = AI_PERSONALITIES.copy()
    random.shuffle(personalities)
    for i in range(num_ai_slots):
        personality = personalities[i % len(personalities)]
        suffix = "" if i < len(personalities) else f" {i // len(personalities) + 1}"
        db.execute(
            """
            INSERT INTO teams (league_id, slot_index, owner_type, status, team_name, ai_personality)
            VALUES (?, ?, 'ai', 'filled', ?, ?)
            """,
            (league_id, slot_index, f"{personality}{suffix}", f"{personality}{suffix}"),
        )
        slot_index += 1

    db.commit()
    return redirect(url_for("league_home", league_id=league_id))


@app.route("/leagues/<int:league_id>/home")
def home_page(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    my_team_id = get_my_team_id(league_id, teams)
    my_team = next((t for t in teams if t["id"] == my_team_id), None)

    opponent = None
    top_players = []
    my_live_score = opp_live_score = None
    knockout_status = None
    is_knockout = league["league_format"] == "Knockout"

    if my_team is not None and league["draft_status"] in ("in_progress", "complete"):
        starters, _ = build_lineup(db, league_id, my_team_id)
        starters = with_projections(starters, league["scoring"], get_schedule_map(db, league["current_week"]))
        top_players = sorted(
            [p for p in starters if p["player_id"]], key=lambda p: p["proj"] or 0, reverse=True
        )[:5]

        if league["draft_status"] == "complete":
            ensure_schedule(db, league_id)
            sync_week_scoring(db, league_id, league["current_week"])
            maybe_ai_roster_moves(db, league_id)
            maybe_ai_trade_offers(db, league_id)

            if is_knockout:
                alive, _ = get_knockout_standings(db, league_id)
                my_row = next((t for t in alive if t["id"] == my_team_id), None) or my_team
                my_score = get_team_live_score(db, league_id, my_team_id, league["current_week"], league["scoring"])
                scores = {t["id"]: get_team_live_score(db, league_id, t["id"], league["current_week"], league["scoring"]) for t in alive}
                knockout_status = {
                    "alive": my_row["eliminated_week"] is None,
                    "eliminated_week": my_row["eliminated_week"],
                    "score": my_score,
                    "champion": len(alive) == 1 and my_row["eliminated_week"] is None,
                    "at_risk": bool(scores) and my_row["eliminated_week"] is None and my_score == min(scores.values()),
                }
            else:
                m = db.execute(
                    """
                    SELECT * FROM matchups
                    WHERE league_id = ? AND week = ? AND (team_a_id = ? OR team_b_id = ?)
                    """,
                    (league_id, league["current_week"], my_team_id, my_team_id),
                ).fetchone()
                if m is not None:
                    opponent_id = m["team_b_id"] if m["team_a_id"] == my_team_id else m["team_a_id"]
                    opponent = (
                        db.execute("SELECT * FROM teams WHERE id = ?", (opponent_id,)).fetchone()
                        if opponent_id else None
                    )
                    if m["played"]:
                        my_live_score = m["team_a_score"] if m["team_a_id"] == my_team_id else m["team_b_score"]
                        opp_live_score = m["team_b_score"] if m["team_a_id"] == my_team_id else m["team_a_score"]
                    else:
                        my_live_score = get_team_live_score(db, league_id, my_team_id, league["current_week"], league["scoring"])
                        opp_live_score = (
                            get_team_live_score(db, league_id, opponent_id, league["current_week"], league["scoring"])
                            if opponent_id else 0.0
                        )

    return render_template(
        "home.html",
        league=league,
        my_team=my_team,
        opponent=opponent,
        top_players=top_players,
        my_live_score=my_live_score,
        opp_live_score=opp_live_score,
        my_team_id=my_team_id,
        is_knockout=is_knockout,
        knockout_status=knockout_status,
    )


@app.route("/leagues/<int:league_id>")
def league_home(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()

    open_slots = sum(1 for t in teams if t["status"] == "open")
    invite_url = url_for("join_league", code=league["invite_code"], _external=True)

    week_matchups = []
    week_status = "scheduled"
    recent_moves = []
    knockout_alive = []
    knockout_eliminated = []
    knockout_scores = {}
    knockout_champion = None
    standings_teams = teams
    is_knockout = league["league_format"] == "Knockout"

    if league["draft_status"] == "complete":
        ensure_schedule(db, league_id)
        sync_week_scoring(db, league_id, league["current_week"])
        maybe_ai_roster_moves(db, league_id)
        maybe_ai_trade_offers(db, league_id)
        recent_moves = db.execute(
            """
            SELECT tx.*, t.team_name FROM transactions tx
            JOIN teams t ON t.id = tx.team_id
            WHERE tx.league_id = ?
            ORDER BY tx.id DESC LIMIT 12
            """,
            (league_id,),
        ).fetchall()
        week_status = get_week_status(db, league["current_week"])

        if is_knockout:
            knockout_alive, knockout_eliminated = get_knockout_standings(db, league_id)
            for t in knockout_alive:
                knockout_scores[t["id"]] = get_team_live_score(
                    db, league_id, t["id"], league["current_week"], league["scoring"]
                )
            if len(knockout_alive) == 1:
                knockout_champion = knockout_alive[0]
            standings_teams = knockout_alive + knockout_eliminated
        else:
            teams_by_id = {t["id"]: t for t in teams}
            rows = db.execute(
                "SELECT * FROM matchups WHERE league_id = ? AND week = ? ORDER BY id",
                (league_id, league["current_week"]),
            ).fetchall()
            for m in rows:
                if m["played"]:
                    score_a, score_b = m["team_a_score"], m["team_b_score"]
                else:
                    score_a = get_team_live_score(db, league_id, m["team_a_id"], league["current_week"], league["scoring"])
                    score_b = (
                        get_team_live_score(db, league_id, m["team_b_id"], league["current_week"], league["scoring"])
                        if m["team_b_id"] else 0.0
                    )
                week_matchups.append({
                    "team_a": teams_by_id.get(m["team_a_id"]),
                    "team_b": teams_by_id.get(m["team_b_id"]) if m["team_b_id"] else None,
                    "score_a": score_a,
                    "score_b": score_b,
                    "played": bool(m["played"]),
                })

    pf_pa = get_points_for_against(db, league_id) if league["draft_status"] == "complete" and not is_knockout else {}
    can_delete = is_league_creator(db, league_id)
    can_advance_week = (
        can_delete and league["draft_status"] == "complete"
        and week_status == "final" and league["current_week"] < SEASON_WEEKS
    )

    return render_template(
        "league_home.html",
        league=league,
        teams=teams,
        open_slots=open_slots,
        invite_url=invite_url,
        ai_speed_choices=AI_SPEED_CHOICES,
        ai_speed_labels=AI_SPEED_LABELS,
        human_timer_choices=HUMAN_TIMER_CHOICES,
        week_status=week_status,
        week_matchups=week_matchups,
        recent_moves=recent_moves,
        pf_pa=pf_pa,
        my_team_id=get_my_team_id(league_id, teams),
        can_delete=can_delete,
        can_advance_week=can_advance_week,
        is_knockout=is_knockout,
        knockout_alive=knockout_alive,
        knockout_eliminated=knockout_eliminated,
        knockout_scores=knockout_scores,
        knockout_champion=knockout_champion,
        standings_teams=standings_teams,
    )


@app.route("/leagues/<int:league_id>/delete", methods=["POST"])
def delete_league(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    if not is_league_creator(db, league_id):
        flash("Only the commissioner's browser session can delete this league.")
        return redirect(url_for("league_home", league_id=league_id))

    db.execute("DELETE FROM leagues WHERE id = ?", (league_id,))
    db.commit()
    flash(f'Deleted "{league["name"]}".')
    return redirect(url_for("index"))


@app.route("/leagues/<int:league_id>/advance-week", methods=["POST"])
def advance_week(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))
    if not is_league_creator(db, league_id):
        flash("Only the commissioner can advance the week.")
        return redirect(url_for("league_home", league_id=league_id))
    if league["draft_status"] != "complete":
        flash("The draft needs to finish first.")
        return redirect(url_for("league_home", league_id=league_id))

    sync_week_scoring(db, league_id, league["current_week"])
    if get_week_status(db, league["current_week"]) != "final":
        flash("This week's games haven't all finished yet.")
        return redirect(url_for("league_home", league_id=league_id))
    if league["current_week"] >= SEASON_WEEKS:
        flash("That's the final week of the season.")
        return redirect(url_for("league_home", league_id=league_id))

    db.execute("UPDATE leagues SET current_week = current_week + 1 WHERE id = ?", (league_id,))
    db.commit()
    flash(f"Advanced to Week {league['current_week'] + 1}.")
    return redirect(url_for("league_home", league_id=league_id))


@app.route("/leagues/<int:league_id>/team/<int:team_id>")
def team_detail(league_id, team_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    team = db.execute(
        "SELECT * FROM teams WHERE id = ? AND league_id = ?", (team_id, league_id)
    ).fetchone()
    if league is None or team is None:
        flash("Team not found.")
        return redirect(url_for("index"))

    starters, bench = ([], [])
    if league["draft_status"] in ("in_progress", "complete"):
        starters, bench = build_lineup(db, league_id, team_id)
        schedule_map = get_schedule_map(db, league["current_week"])
        starters = with_projections(starters, league["scoring"], schedule_map)
        bench = with_projections(bench, league["scoring"], schedule_map)

    matchup = None
    live_score = None
    if league["draft_status"] == "complete":
        ensure_schedule(db, league_id)
        sync_week_scoring(db, league_id, league["current_week"])
        m = db.execute(
            """
            SELECT * FROM matchups
            WHERE league_id = ? AND week = ? AND (team_a_id = ? OR team_b_id = ?)
            """,
            (league_id, league["current_week"], team_id, team_id),
        ).fetchone()
        if m is not None:
            opponent_id = m["team_b_id"] if m["team_a_id"] == team_id else m["team_a_id"]
            opponent = (
                db.execute("SELECT * FROM teams WHERE id = ?", (opponent_id,)).fetchone()
                if opponent_id else None
            )
            matchup = {"opponent": opponent}
        live_score = (
            (m["team_a_score"] if m["team_a_id"] == team_id else m["team_b_score"])
            if (m is not None and m["played"])
            else get_team_live_score(db, league_id, team_id, league["current_week"], league["scoring"])
        )

    all_teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    my_team_id = get_my_team_id(league_id, all_teams)

    return render_template(
        "team.html",
        league=league,
        team=team,
        team_projection=total_projection(starters),
        live_score=live_score,
        starters=starters,
        bench=bench,
        matchup=matchup,
        my_team_id=my_team_id,
        is_my_team=(my_team_id == team_id),
        team_kits=TEAM_KITS,
    )


@app.route("/leagues/<int:league_id>/team/<int:team_id>/customize", methods=["POST"])
def customize_team(league_id, team_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        return redirect(url_for("index"))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    if get_my_team_id(league_id, teams) != team_id:
        flash("You can only customize your own team.")
        return redirect(url_for("team_detail", league_id=league_id, team_id=team_id))

    team_name = request.form.get("team_name", "").strip()
    if not team_name:
        flash("Team name can't be empty.")
        return redirect(url_for("team_detail", league_id=league_id, team_id=team_id))
    team_name = team_name[:40]

    try:
        kit_index = int(request.form.get("kit_index", ""))
        kit = TEAM_KITS[kit_index]
    except (ValueError, IndexError, TypeError):
        kit = None

    if kit is not None:
        db.execute(
            "UPDATE teams SET team_name = ?, logo_icon = ?, logo_color = ? WHERE id = ?",
            (team_name, kit["icon"], kit["color"], team_id),
        )
    else:
        db.execute("UPDATE teams SET team_name = ? WHERE id = ?", (team_name, team_id))
    db.commit()
    flash("Team updated.")
    return redirect(url_for("team_detail", league_id=league_id, team_id=team_id))


@app.route("/leagues/<int:league_id>/team/<int:team_id>/lineup-swap", methods=["POST"])
def lineup_swap(league_id, team_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        return jsonify({"error": "not_found"}), 404
    if league["draft_status"] not in ("in_progress", "complete"):
        return jsonify({"error": "no_roster_yet"}), 400

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    if get_my_team_id(league_id, teams) != team_id:
        return jsonify({"error": "forbidden"}), 403

    ensure_lineup_slots(db, league_id, team_id)

    try:
        pick_a_id = int(request.form.get("pick_a", ""))
    except ValueError:
        return jsonify({"error": "invalid_request"}), 400

    pick_a = db.execute(
        "SELECT * FROM draft_picks WHERE id = ? AND league_id = ? AND team_id = ?",
        (pick_a_id, league_id, team_id),
    ).fetchone()
    if pick_a is None:
        return jsonify({"error": "invalid_picks"}), 400

    # Dropping onto an empty slot (e.g. an unfilled FLEX) is a move, not a
    # swap -- there's no second player to trade places with.
    target_slot = request.form.get("target_slot")
    if target_slot:
        if target_slot not in SLOT_ELIGIBLE_POSITIONS:
            return jsonify({"error": "invalid_request"}), 400
        # Bench has no fixed capacity -- only real starter slots are exclusive.
        occupied = None if target_slot == "BN" else db.execute(
            """
            SELECT 1 FROM draft_picks
            WHERE league_id = ? AND team_id = ? AND lineup_slot = ? AND player_id IS NOT NULL
            """,
            (league_id, team_id, target_slot),
        ).fetchone()
        if occupied is not None:
            return jsonify({"error": "occupied", "message": "That slot is already filled."}), 400
        if pick_a["position"] not in SLOT_ELIGIBLE_POSITIONS.get(target_slot, set()):
            return jsonify({
                "error": "ineligible",
                "message": f"{pick_a['position']} can't fill {slot_display_label(target_slot)}.",
            }), 400
        db.execute("UPDATE draft_picks SET lineup_slot = ? WHERE id = ?", (target_slot, pick_a["id"]))
        db.commit()
    else:
        try:
            pick_b_id = int(request.form.get("pick_b", ""))
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        pick_b = db.execute(
            "SELECT * FROM draft_picks WHERE id = ? AND league_id = ? AND team_id = ?",
            (pick_b_id, league_id, team_id),
        ).fetchone()
        if pick_b is None:
            return jsonify({"error": "invalid_picks"}), 400

        slot_a, slot_b = pick_a["lineup_slot"], pick_b["lineup_slot"]
        a_fits_b = pick_a["position"] in SLOT_ELIGIBLE_POSITIONS.get(slot_b, set())
        b_fits_a = pick_b["position"] in SLOT_ELIGIBLE_POSITIONS.get(slot_a, set())
        if not (a_fits_b and b_fits_a):
            if not a_fits_b:
                blocker, blocked_slot = pick_a, slot_b
            else:
                blocker, blocked_slot = pick_b, slot_a
            return jsonify({
                "error": "ineligible",
                "message": f"{blocker['position']} can't fill {slot_display_label(blocked_slot)}.",
            }), 400

        db.execute("UPDATE draft_picks SET lineup_slot = ? WHERE id = ?", (slot_b, pick_a["id"]))
        db.execute("UPDATE draft_picks SET lineup_slot = ? WHERE id = ?", (slot_a, pick_b["id"]))
        db.commit()

    starters, bench = build_lineup(db, league_id, team_id)
    schedule_map = get_schedule_map(db, league["current_week"])
    starters = with_projections(starters, league["scoring"], schedule_map)
    bench = with_projections(bench, league["scoring"], schedule_map)
    return jsonify({"ok": True, "starters": starters, "bench": bench})


@app.route("/leagues/<int:league_id>/team/<int:team_id>/roster-move", methods=["POST"])
def roster_move(league_id, team_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))
    if league["draft_status"] != "complete":
        flash("Free agency opens once the draft is complete.")
        return redirect(url_for("team_detail", league_id=league_id, team_id=team_id))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    if get_my_team_id(league_id, teams) != team_id:
        flash("You can only manage your own roster.")
        return redirect(url_for("team_detail", league_id=league_id, team_id=team_id))

    team_row = next(t for t in teams if t["id"] == team_id)
    add_player_id = request.form.get("add_player_id") or None
    drop_pick_id = request.form.get("drop_pick_id", type=int)

    ok, message = execute_roster_move(db, league, team_row, add_player_id=add_player_id, drop_pick_id=drop_pick_id)
    if ok:
        db.commit()
    flash(message)
    return redirect(url_for("team_detail", league_id=league_id, team_id=team_id))


@app.route("/leagues/<int:league_id>/team/<int:team_id>/add/<player_id>")
def add_free_agent(league_id, team_id, player_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    team = db.execute("SELECT * FROM teams WHERE id = ? AND league_id = ?", (team_id, league_id)).fetchone()
    player = db.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if league is None or team is None or player is None:
        flash("Not found.")
        return redirect(url_for("players_list", league_id=league_id))
    if league["draft_status"] != "complete":
        flash("Free agency opens once the draft is complete.")
        return redirect(url_for("players_list", league_id=league_id))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    if get_my_team_id(league_id, teams) != team_id:
        flash("You can only manage your own roster.")
        return redirect(url_for("players_list", league_id=league_id))

    owned = db.execute(
        "SELECT 1 FROM draft_picks WHERE league_id = ? AND player_id = ?", (league_id, player_id)
    ).fetchone()
    if owned is not None:
        flash(f"{player['full_name']} is already rostered.")
        return redirect(url_for("players_list", league_id=league_id))

    picks = get_roster_picks(db, league_id, team_id)
    roster_full = len(picks) >= league["rounds"]

    return render_template(
        "add_player.html", league=league, team=team, player=player,
        picks=picks, roster_full=roster_full, my_team_id=team_id,
    )


@app.route("/leagues/<int:league_id>/trades")
def trades_page(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    my_team_id = get_my_team_id(league_id, teams)
    my_team = next((t for t in teams if t["id"] == my_team_id), None)

    my_picks = get_roster_picks(db, league_id, my_team_id) if my_team_id else []
    other_teams = [t for t in teams if t["id"] != my_team_id and t["status"] == "filled"]

    partner_id = request.args.get("partner_id", type=int)
    partner = next((t for t in other_teams if t["id"] == partner_id), None) if partner_id else None
    partner_picks = get_roster_picks(db, league_id, partner["id"]) if partner is not None else []

    pending_rows = []
    if league["draft_status"] == "complete":
        maybe_ai_trade_offers(db, league_id)
        pending_rows = db.execute(
            """
            SELECT tr.*, ft.team_name AS from_name, ft.owner_type AS from_owner_type,
                   tt.team_name AS to_name, tt.owner_type AS to_owner_type
            FROM trades tr
            JOIN teams ft ON ft.id = tr.from_team_id
            JOIN teams tt ON tt.id = tr.to_team_id
            WHERE tr.league_id = ? AND (tr.from_team_id = ? OR tr.to_team_id = ?)
            ORDER BY tr.created_at DESC, tr.id DESC
            """,
            (league_id, my_team_id, my_team_id),
        ).fetchall()

    incoming, outgoing, history = [], [], []
    for t in pending_rows:
        items = db.execute("SELECT * FROM trade_items WHERE trade_id = ?", (t["id"],)).fetchall()
        give = [i for i in items if i["from_team_id"] == t["from_team_id"]]
        receive = [i for i in items if i["from_team_id"] == t["to_team_id"]]
        entry = dict(t, give=give, receive=receive)
        if t["status"] == "pending":
            (incoming if t["to_team_id"] == my_team_id else outgoing).append(entry)
        else:
            history.append(entry)

    return render_template(
        "trades.html", league=league, my_team=my_team, my_team_id=my_team_id,
        my_picks=my_picks, other_teams=other_teams, partner=partner, partner_picks=partner_picks,
        incoming=incoming, outgoing=outgoing, history=history[:15],
    )


@app.route("/leagues/<int:league_id>/trades/propose", methods=["POST"])
def propose_trade(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None or league["draft_status"] != "complete":
        flash("Trades open once the draft is complete.")
        return redirect(url_for("trades_page", league_id=league_id))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    my_team_id = get_my_team_id(league_id, teams)
    to_team_id = request.form.get("to_team_id", type=int)
    to_team = next((t for t in teams if t["id"] == to_team_id), None)
    if to_team is None or to_team_id == my_team_id or to_team["status"] != "filled":
        flash("Pick a valid trade partner.")
        return redirect(url_for("trades_page", league_id=league_id))

    try:
        give_ids = [int(x) for x in request.form.getlist("give_pick_ids")]
        receive_ids = [int(x) for x in request.form.getlist("receive_pick_ids")]
    except ValueError:
        flash("Invalid player selection.")
        return redirect(url_for("trades_page", league_id=league_id, partner_id=to_team_id))

    ok, message, give_picks, receive_picks = validate_trade_legality(
        db, league, my_team_id, to_team_id, give_ids, receive_ids
    )
    if not ok:
        flash(message)
        return redirect(url_for("trades_page", league_id=league_id, partner_id=to_team_id))

    trade_id = db.execute(
        "INSERT INTO trades (league_id, from_team_id, to_team_id, status) VALUES (?, ?, ?, 'pending') RETURNING id",
        (league_id, my_team_id, to_team_id),
    ).fetchone()["id"]
    for p in give_picks:
        db.execute(
            "INSERT INTO trade_items (trade_id, pick_id, from_team_id, player_name, position) VALUES (?, ?, ?, ?, ?)",
            (trade_id, p["id"], my_team_id, p["player_name"], p["position"]),
        )
    for p in receive_picks:
        db.execute(
            "INSERT INTO trade_items (trade_id, pick_id, from_team_id, player_name, position) VALUES (?, ?, ?, ?, ?)",
            (trade_id, p["id"], to_team_id, p["player_name"], p["position"]),
        )
    db.commit()

    if to_team["owner_type"] == "ai":
        _, message = resolve_trade(db, league_id, trade_id, resolver="ai")
        flash(message)
    else:
        flash("Trade offer sent.")

    return redirect(url_for("trades_page", league_id=league_id))


@app.route("/leagues/<int:league_id>/trades/<int:trade_id>/respond", methods=["POST"])
def respond_trade(league_id, trade_id):
    db = get_db()
    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    my_team_id = get_my_team_id(league_id, teams)
    trade = db.execute("SELECT * FROM trades WHERE id = ? AND league_id = ?", (trade_id, league_id)).fetchone()
    if trade is None or trade["to_team_id"] != my_team_id:
        flash("You can't respond to that trade.")
        return redirect(url_for("trades_page", league_id=league_id))

    action = request.form.get("action")
    if action not in ("accept", "reject"):
        flash("Invalid action.")
        return redirect(url_for("trades_page", league_id=league_id))

    _, message = resolve_trade(db, league_id, trade_id, action=action, resolver="human")
    flash(message)
    return redirect(url_for("trades_page", league_id=league_id))


@app.route("/leagues/<int:league_id>/trades/<int:trade_id>/cancel", methods=["POST"])
def cancel_trade(league_id, trade_id):
    db = get_db()
    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    my_team_id = get_my_team_id(league_id, teams)
    trade = db.execute("SELECT * FROM trades WHERE id = ? AND league_id = ?", (trade_id, league_id)).fetchone()
    if trade is None or trade["from_team_id"] != my_team_id or trade["status"] != "pending":
        flash("Can't cancel that trade.")
        return redirect(url_for("trades_page", league_id=league_id))

    db.execute("UPDATE trades SET status = 'cancelled', resolved_at = datetime('now') WHERE id = ?", (trade_id,))
    db.commit()
    flash("Trade offer cancelled.")
    return redirect(url_for("trades_page", league_id=league_id))


@app.route("/leagues/<int:league_id>/matchup")
def matchup_detail(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    teams_by_id = {t["id"]: t for t in teams}
    my_team_id = get_my_team_id(league_id, teams)
    is_knockout = league["league_format"] == "Knockout"

    if is_knockout:
        return _knockout_matchup_view(db, league, teams, my_team_id)

    week_matchups = []
    current = None
    week_status = "scheduled"
    if league["draft_status"] == "complete":
        ensure_schedule(db, league_id)
        sync_week_scoring(db, league_id, league["current_week"])
        week_status = get_week_status(db, league["current_week"])
        week_matchups = db.execute(
            "SELECT * FROM matchups WHERE league_id = ? AND week = ? ORDER BY id",
            (league_id, league["current_week"]),
        ).fetchall()
        if week_matchups:
            requested_id = request.args.get("matchup_id", type=int)
            current = next((m for m in week_matchups if m["id"] == requested_id), None)
            if current is None:
                current = next(
                    (m for m in week_matchups if my_team_id in (m["team_a_id"], m["team_b_id"])),
                    week_matchups[0],
                )

    left = right = None
    left_starters, right_starters = [], []
    left_score = right_score = 0.0
    played = False
    prev_matchup_id = next_matchup_id = None
    matchup_index = matchup_count = 0

    if current is not None:
        idx = next(i for i, m in enumerate(week_matchups) if m["id"] == current["id"])
        matchup_count = len(week_matchups)
        matchup_index = idx + 1
        prev_matchup_id = week_matchups[(idx - 1) % matchup_count]["id"]
        next_matchup_id = week_matchups[(idx + 1) % matchup_count]["id"]
        played = bool(current["played"])

        team_a = teams_by_id.get(current["team_a_id"])
        team_b = teams_by_id.get(current["team_b_id"]) if current["team_b_id"] else None
        left, right = team_a, team_b

        if played:
            a_score, b_score = current["team_a_score"], current["team_b_score"]
        else:
            a_score = get_team_live_score(db, league_id, team_a["id"], league["current_week"], league["scoring"])
            b_score = (
                get_team_live_score(db, league_id, team_b["id"], league["current_week"], league["scoring"])
                if team_b is not None else 0.0
            )
        left_score, right_score = a_score, b_score
        if team_b is not None and team_b["id"] == my_team_id:
            left, right = team_b, team_a
            left_score, right_score = b_score, a_score

        schedule_map = get_schedule_map(db, league["current_week"])
        left_starters, _ = build_lineup(db, league_id, left["id"])
        left_starters = with_projections(left_starters, league["scoring"], schedule_map)
        if right is not None:
            right_starters, _ = build_lineup(db, league_id, right["id"])
            right_starters = with_projections(right_starters, league["scoring"], schedule_map)

    return render_template(
        "matchup.html",
        league=league,
        left=left,
        right=right,
        left_starters=left_starters,
        right_starters=right_starters,
        left_score=left_score,
        right_score=right_score,
        left_projection=total_projection(left_starters),
        right_projection=total_projection(right_starters),
        played=played,
        week_status=week_status,
        prev_matchup_id=prev_matchup_id,
        next_matchup_id=next_matchup_id,
        matchup_index=matchup_index,
        matchup_count=matchup_count,
        my_team_id=my_team_id,
        is_knockout=False,
    )


def _knockout_matchup_view(db, league, teams, my_team_id):
    """Knockout has no head-to-head opponent -- this renders 'you vs the
    field' instead: your lineup/score plus the live elimination leaderboard."""
    league_id = league["id"]
    left = next((t for t in teams if t["id"] == my_team_id), None)
    left_starters = []
    left_score = 0.0
    week_status = "scheduled"
    knockout_alive, knockout_eliminated = [], []
    knockout_scores = {}
    champion = None

    if league["draft_status"] == "complete" and left is not None:
        ensure_schedule(db, league_id)
        sync_week_scoring(db, league_id, league["current_week"])
        week_status = get_week_status(db, league["current_week"])

        knockout_alive, knockout_eliminated = get_knockout_standings(db, league_id)
        schedule_map = get_schedule_map(db, league["current_week"])
        left_starters, _ = build_lineup(db, league_id, left["id"])
        left_starters = with_projections(left_starters, league["scoring"], schedule_map)
        left_score = get_team_live_score(db, league_id, left["id"], league["current_week"], league["scoring"])

        for t in knockout_alive:
            knockout_scores[t["id"]] = get_team_live_score(db, league_id, t["id"], league["current_week"], league["scoring"])
        knockout_alive = sorted(knockout_alive, key=lambda t: knockout_scores[t["id"]])
        if len(knockout_alive) == 1:
            champion = knockout_alive[0]

    return render_template(
        "matchup.html",
        league=league,
        left=left,
        right=None,
        left_starters=left_starters,
        right_starters=[],
        left_score=left_score,
        right_score=0.0,
        left_projection=total_projection(left_starters),
        right_projection=0.0,
        played=(week_status == "final"),
        week_status=week_status,
        prev_matchup_id=None,
        next_matchup_id=None,
        matchup_index=1,
        matchup_count=1,
        my_team_id=my_team_id,
        is_knockout=True,
        knockout_alive=knockout_alive,
        knockout_eliminated=knockout_eliminated,
        knockout_scores=knockout_scores,
        knockout_champion=champion,
    )


@app.route("/leagues/<int:league_id>/players")
def players_list(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    teams_by_id = {t["id"]: t for t in teams}

    owned = {
        row["player_id"]: row["team_id"]
        for row in db.execute(
            "SELECT player_id, team_id FROM draft_picks WHERE league_id = ? AND player_id IS NOT NULL",
            (league_id,),
        )
    }

    pos_filter = request.args.get("pos", "").upper()
    search_query = request.args.get("q", "").strip()
    own_filter = request.args.get("own", "available").lower()
    if own_filter not in ("all", "available", "rostered"):
        own_filter = "available"
    rank_col = RANK_COLUMN_BY_SCORING.get(league["scoring"], "rank_half")

    query = f"SELECT *, {rank_col} AS rank FROM players"
    conditions, params = [], []
    if pos_filter in POSITION_ORDER:
        conditions.append("position = ?")
        params.append(pos_filter)
    if search_query:
        conditions.append("full_name ILIKE ?")
        params.append(f"%{search_query}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY {rank_col} ASC"

    schedule_map = get_schedule_map(db, league["current_week"])

    rows = []
    for p in db.execute(query, params).fetchall():
        is_owned = p["id"] in owned
        if own_filter == "available" and is_owned:
            continue
        if own_filter == "rostered" and not is_owned:
            continue
        game = schedule_map.get(p["nfl_team"])
        if game:
            opponent = ("vs " if game["is_home"] else "@ ") + (game["opponent"] or "")
        elif p["nfl_team"]:
            opponent = "BYE"
        else:
            opponent = None
        rows.append({
            "player": p,
            "owner_team": teams_by_id.get(owned.get(p["id"])),
            "proj": player_projection(p["position"], p["rank"], league["scoring"]),
            "injury_label": INJURY_LABELS.get(p["injury_status"]),
            "opponent": opponent,
            "face_url": player_face_url(p["id"], p["position"], p["nfl_team"]),
            "initials": player_initials(p["full_name"]),
        })

    return render_template(
        "players.html",
        league=league,
        rows=rows,
        position_order=POSITION_ORDER,
        pos_filter=pos_filter,
        search_query=search_query,
        own_filter=own_filter,
        my_team_id=get_my_team_id(league_id, teams),
    )


@app.route("/leagues/<int:league_id>/fill-ai", methods=["POST"])
def fill_remaining_with_ai(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    open_teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? AND status = 'open' ORDER BY slot_index",
        (league_id,),
    ).fetchall()

    taken = {
        row["ai_personality"]
        for row in db.execute(
            "SELECT ai_personality FROM teams WHERE league_id = ? AND ai_personality IS NOT NULL",
            (league_id,),
        )
    }
    available = [p for p in AI_PERSONALITIES if p not in taken] or AI_PERSONALITIES.copy()
    random.shuffle(available)

    for i, team in enumerate(open_teams):
        personality = available[i % len(available)]
        db.execute(
            """
            UPDATE teams
            SET owner_type = 'ai', status = 'filled', team_name = ?, ai_personality = ?
            WHERE id = ?
            """,
            (personality, personality, team["id"]),
        )

    db.commit()
    if open_teams:
        flash(f"Filled {len(open_teams)} remaining slot(s) with AI managers.")
    return redirect(url_for("league_home", league_id=league_id))


@app.route("/join/<code>", methods=["GET", "POST"])
def join_league(code):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE invite_code = ?", (code,)).fetchone()
    if league is None:
        flash("Invalid or expired invite link.")
        return redirect(url_for("index"))

    open_teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? AND status = 'open' ORDER BY slot_index",
        (league["id"],),
    ).fetchall()

    if request.method == "GET":
        return render_template("join_league.html", league=league, open_slots=len(open_teams))

    if not open_teams:
        flash("Sorry, this league is already full.")
        return redirect(url_for("join_league", code=code))

    owner_name = request.form.get("owner_name", "").strip()
    team_name = request.form.get("team_name", "").strip()

    if not owner_name or not team_name:
        flash("Please enter your name and a team name.")
        return redirect(url_for("join_league", code=code))

    next_slot = open_teams[0]
    db.execute(
        """
        UPDATE teams
        SET status = 'filled', owner_name = ?, team_name = ?
        WHERE id = ?
        """,
        (owner_name, team_name, next_slot["id"]),
    )
    db.commit()
    session[f"team_{league['id']}"] = next_slot["id"]
    return redirect(url_for("league_home", league_id=league["id"]))


def build_snake_order(team_ids, rounds):
    order = []
    for rnd in range(1, rounds + 1):
        round_teams = team_ids if rnd % 2 == 1 else list(reversed(team_ids))
        for pick_in_round, team_id in enumerate(round_teams, start=1):
            order.append((rnd, pick_in_round, team_id))
    return order


# ---------------------------------------------------------------------------
# AI decision engine (deterministic, no LLM calls -- the pick always happens
# instantly; only the *display* of it is paced client-side).
# ---------------------------------------------------------------------------

def stable_unit(seed_str):
    digest = hashlib.md5(seed_str.encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def get_available_players(db, league_id, scoring="Standard"):
    col = RANK_COLUMN_BY_SCORING.get(scoring, "rank_half")
    return db.execute(
        f"""
        SELECT *, {col} AS rank FROM players
        WHERE id NOT IN (
            SELECT player_id FROM draft_picks WHERE league_id = ? AND player_id IS NOT NULL
        )
        ORDER BY {col} ASC
        """,
        (league_id,),
    ).fetchall()


def get_position_counts(db, league_id, team_id):
    rows = db.execute(
        """
        SELECT position, COUNT(*) AS c FROM draft_picks
        WHERE league_id = ? AND team_id = ? AND player_id IS NOT NULL
        GROUP BY position
        """,
        (league_id, team_id),
    ).fetchall()
    return {r["position"]: r["c"] for r in rows}


def compute_need_bonus(position_counts, position):
    have = position_counts.get(position, 0)
    starters = STARTER_REQUIREMENTS.get(position, 0)
    if have < starters:
        return 45
    if position in FLEX_ELIGIBLE:
        flex_have = sum(position_counts.get(p, 0) for p in FLEX_ELIGIBLE)
        flex_total_slots = sum(STARTER_REQUIREMENTS.get(p, 0) for p in FLEX_ELIGIBLE) + FLEX_SLOTS
        if flex_have < flex_total_slots:
            return 20
    cap = BENCH_SOFT_CAP.get(position, 4)
    if have < cap:
        return 6
    return -18


def compute_scarcity_bonus(available_counts, position):
    count = available_counts.get(position, 0)
    return max(0, (14 - count)) * 0.6


def compute_tier_counts(available_players):
    counts = {}
    for p in available_players:
        if p["tier"] is None:
            continue
        key = (p["position"], p["tier"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def compute_tier_bonus(tier_counts, position, tier):
    """Reward being the last (or second-to-last) player left in their tier at a
    position -- real drafters reach a bit to beat the cliff before the position's
    value drops to the next tier down."""
    if tier is None:
        return 0.0
    remaining = tier_counts.get((position, tier), 0)
    if remaining <= 1:
        return 14.0
    if remaining == 2:
        return 7.0
    return 0.0


def score_players(available_players, position_counts, available_counts, profile, current_round, overall_pick):
    tier_counts = compute_tier_counts(available_players)
    scored = []
    for p in available_players:
        rank = p["rank"]
        pos = p["position"]
        value = max(0.0, 420 - rank * 1.8)
        need = compute_need_bonus(position_counts, pos) * profile.get("need_multiplier", 1.0)
        scarcity = compute_scarcity_bonus(available_counts, pos)
        tier_bonus = compute_tier_bonus(tier_counts, pos, p["tier"])
        bonus = tier_bonus

        pos_bonus = profile.get("pos_bonus", {}).get(pos)
        if pos_bonus:
            bonus += pos_bonus

        pen_before = profile.get("pos_penalty_before_round", {}).get(pos)
        if pen_before and current_round <= pen_before[1]:
            bonus += pen_before[0]

        bonus_after = profile.get("pos_bonus_after_round", {}).get(pos)
        if bonus_after and current_round > bonus_after[1]:
            bonus += bonus_after[0]

        if "upside_weight" in profile:
            uf = stable_unit(f"{p['id']}-upside")
            if rank <= 6:
                uf *= 0.3
            bonus += profile["upside_weight"] * uf

        if "exp_bonus_per_year" in profile:
            exp = p["years_exp"] or 0
            cap = profile.get("exp_cap", 10)
            bonus += min(exp, cap) * profile["exp_bonus_per_year"]
            if exp == 0:
                bonus += profile.get("rookie_penalty", 0) + profile.get("rookie_bonus", 0)

        if "fall_weight" in profile:
            bonus += profile["fall_weight"] * max(0, overall_pick - rank)

        deep = profile.get("deep_bonus_after_round")
        if deep and current_round > deep[1]:
            bonus += deep[0]

        noise = profile.get("noise_weight")
        if noise:
            bonus += random.uniform(-noise, noise)

        total = value + need + scarcity + bonus
        scored.append({"score": total, "need": need, "value": value, "tier_bonus": tier_bonus, "player": p})

    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored


def choose_from_scored(scored, profile):
    reach_chance = profile.get("reach_chance", 0)
    if reach_chance and random.random() < reach_chance and len(scored) > 1:
        pool_n = min(profile.get("reach_pool", 5), len(scored))
        pool = scored[:pool_n]
        weights = [pool_n - i for i in range(pool_n)]
        return random.choices(pool, weights=weights, k=1)[0]
    return scored[0]


def build_reasoning(team, personality, player, need_bonus, current_round, is_autopick, tier_bonus=0.0):
    if is_autopick:
        return (
            f"⏰ Time expired — {team['team_name']} was auto-drafted "
            f"{player['full_name']} ({player['position']}, best available fit)."
        )
    flavor = PERSONALITY_FLAVOR.get(personality, "making the pick")
    if tier_bonus >= 14:
        need_phrase = f"last {player['position']} in this tier before the cliff"
    elif need_bonus >= 40:
        need_phrase = f"fills a starting need at {player['position']}"
    elif need_bonus >= 18:
        need_phrase = f"adds flex depth at {player['position']}"
    elif tier_bonus >= 7:
        need_phrase = f"beats the drop-off to the next {player['position']} tier"
    else:
        need_phrase = "best value remaining on the board"
    label = personality or team["team_name"]
    return f"{label} is {flavor} — {player['full_name']} {need_phrase} (rank #{player['rank']})."


def execute_pick_for_team(db, league_id, pick_row, team_row, scoring, personality=None, is_autopick=False):
    available = get_available_players(db, league_id, scoring)
    if not available:
        return None

    position_counts = get_position_counts(db, league_id, team_row["id"])

    # Never let an AI (or an auto-drafted timeout pick) exceed a sane roster cap
    # at a position -- e.g. no team ends up with five QBs.
    within_cap = [
        p for p in available
        if position_counts.get(p["position"], 0) < HARD_CAP.get(p["position"], 99)
    ]
    pool = within_cap or available

    available_counts = {}
    for p in pool:
        available_counts[p["position"]] = available_counts.get(p["position"], 0) + 1

    profile = PERSONALITY_PROFILES.get(personality, {}) if personality else {}
    scored = score_players(
        pool, position_counts, available_counts, profile,
        pick_row["round"], pick_row["overall_pick"],
    )
    chosen = choose_from_scored(scored, profile)
    player = chosen["player"]

    reasoning = build_reasoning(
        team_row, personality, player, chosen["need"], pick_row["round"], is_autopick, chosen["tier_bonus"]
    )

    db.execute(
        """
        UPDATE draft_picks
        SET player_id = ?, player_name = ?, position = ?, nfl_team = ?, player_rank = ?,
            reasoning = ?, is_autopick = ?, drafted_at = datetime('now')
        WHERE id = ?
        """,
        (
            player["id"], player["full_name"], player["position"], player["nfl_team"],
            player["rank"], reasoning, 1 if is_autopick else 0, pick_row["id"],
        ),
    )
    return player


def get_current_pick_row(db, league_id, overall_pick):
    return db.execute(
        """
        SELECT dp.*, t.team_name, t.owner_type, t.owner_name, t.ai_personality
        FROM draft_picks dp
        JOIN teams t ON t.id = dp.team_id
        WHERE dp.league_id = ? AND dp.overall_pick = ?
        """,
        (league_id, overall_pick),
    ).fetchone()


def advance_after_pick(db, league_id):
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    total_picks = league["num_teams"] * league["rounds"]
    next_index = league["current_pick_index"] + 1

    if next_index > total_picks:
        db.execute(
            "UPDATE leagues SET current_pick_index = ?, draft_status = 'complete', pick_deadline = NULL, current_week = 1 WHERE id = ?",
            (next_index, league_id),
        )
        db.commit()
        compute_and_store_grades(db, league_id)
        generate_schedule(db, league_id)
        return

    next_pick = get_current_pick_row(db, league_id, next_index)
    deadline = time.time() + league["human_timer_seconds"] if next_pick["owner_type"] == "human" else None
    db.execute(
        "UPDATE leagues SET current_pick_index = ?, pick_deadline = ? WHERE id = ?",
        (next_index, deadline, league_id),
    )
    db.commit()


def maybe_auto_draft_timeout(db, league_id):
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None or league["draft_status"] != "in_progress" or league["pick_deadline"] is None:
        return
    if time.time() < league["pick_deadline"]:
        return

    pick_row = get_current_pick_row(db, league_id, league["current_pick_index"])
    if pick_row is None or pick_row["owner_type"] != "human" or pick_row["player_id"] is not None:
        return

    team_row = db.execute("SELECT * FROM teams WHERE id = ?", (pick_row["team_id"],)).fetchone()
    if execute_pick_for_team(db, league_id, pick_row, team_row, league["scoring"], personality=None, is_autopick=True) is None:
        return
    db.commit()
    advance_after_pick(db, league_id)


def compute_and_store_grades(db, league_id):
    picks = db.execute(
        """
        SELECT dp.*, t.team_name, t.owner_type, t.owner_name, t.ai_personality
        FROM draft_picks dp
        JOIN teams t ON t.id = dp.team_id
        WHERE dp.league_id = ? AND dp.player_id IS NOT NULL
        ORDER BY dp.overall_pick ASC
        """,
        (league_id,),
    ).fetchall()

    by_team = {}
    for p in picks:
        entry = by_team.setdefault(p["team_id"], {
            "team_name": p["team_name"], "owner_type": p["owner_type"],
            "owner_name": p["owner_name"], "ai_personality": p["ai_personality"],
            "picks": [],
        })
        value = p["overall_pick"] - (p["player_rank"] or p["overall_pick"])
        entry["picks"].append({
            "overall_pick": p["overall_pick"], "player_name": p["player_name"],
            "position": p["position"], "nfl_team": p["nfl_team"], "value": value,
        })

    def grade_for(avg):
        for threshold, label in GRADE_THRESHOLDS:
            if avg >= threshold:
                return label
        return "F"

    all_picks_flat = []
    team_grades = []
    for team_id, entry in by_team.items():
        picks_list = entry["picks"]
        avg_value = sum(p["value"] for p in picks_list) / len(picks_list) if picks_list else 0
        best_pick = max(picks_list, key=lambda p: p["value"]) if picks_list else None
        worst_pick = min(picks_list, key=lambda p: p["value"]) if picks_list else None

        first_positions = [p["position"] for p in picks_list[:4]]
        rb_count = first_positions.count("RB")
        wr_count = first_positions.count("WR")
        if rb_count == 0:
            strategy = "Zero-RB approach"
        elif rb_count >= 3:
            strategy = "RB-heavy build"
        elif wr_count >= 3:
            strategy = "WR-heavy build"
        else:
            strategy = "Balanced build"

        team_grades.append({
            "team_id": team_id, "team_name": entry["team_name"], "owner_type": entry["owner_type"],
            "owner_name": entry["owner_name"], "ai_personality": entry["ai_personality"],
            "grade": grade_for(avg_value), "avg_value": round(avg_value, 1),
            "best_pick": best_pick, "worst_pick": worst_pick, "strategy": strategy,
        })
        all_picks_flat.extend({**p, "team_name": entry["team_name"]} for p in picks_list)

    team_grades.sort(key=lambda t: t["avg_value"], reverse=True)
    biggest_value = max(all_picks_flat, key=lambda p: p["value"]) if all_picks_flat else None
    biggest_reach = min(all_picks_flat, key=lambda p: p["value"]) if all_picks_flat else None

    grades = {"teams": team_grades, "biggest_value": biggest_value, "biggest_reach": biggest_reach}
    db.execute("UPDATE leagues SET grades_json = ? WHERE id = ?", (json.dumps(grades), league_id))
    db.commit()


# ---------------------------------------------------------------------------
# Free agency (add/drop) and trades -- post-draft roster management. Both
# reuse the same 1-420 rank-based value curve the draft engine scores players
# on, so "is this a fair trade" / "is this free agent an upgrade" stay
# consistent with how the AI drafted in the first place. Deterministic, no
# LLM calls, same as everything else in this file.
# ---------------------------------------------------------------------------

def player_trade_value(rank):
    rank = rank if isinstance(rank, int) and rank < 999999 else 400
    return max(0.0, 420 - rank * 1.8)


def log_transaction(db, league_id, team_id, kind, detail):
    db.execute(
        "INSERT INTO transactions (league_id, team_id, kind, detail) VALUES (?, ?, ?, ?)",
        (league_id, team_id, kind, detail),
    )


def get_roster_picks(db, league_id, team_id):
    return db.execute(
        """
        SELECT * FROM draft_picks
        WHERE league_id = ? AND team_id = ? AND player_id IS NOT NULL
        ORDER BY (player_rank IS NULL), player_rank ASC
        """,
        (league_id, team_id),
    ).fetchall()


def next_overall_pick(db, league_id):
    row = db.execute(
        "SELECT COALESCE(MAX(overall_pick), 0) + 1 AS n FROM draft_picks WHERE league_id = ?",
        (league_id,),
    ).fetchone()
    return row["n"]


def roster_position_counts_after(picks, drop_pick_id=None, add_position=None):
    counts = {}
    for p in picks:
        if drop_pick_id is not None and p["id"] == drop_pick_id:
            continue
        counts[p["position"]] = counts.get(p["position"], 0) + 1
    if add_position:
        counts[add_position] = counts.get(add_position, 0) + 1
    return counts


def execute_roster_move(db, league, team_row, add_player_id=None, drop_pick_id=None, note=None):
    """Add a free agent and/or drop a rostered player for one team. Does not
    commit -- caller commits once, after logging is done. Returns (ok, message)."""
    league_id = league["id"]
    team_id = team_row["id"]
    picks = get_roster_picks(db, league_id, team_id)

    drop_pick = None
    if drop_pick_id is not None:
        drop_pick = next((p for p in picks if p["id"] == drop_pick_id), None)
        if drop_pick is None:
            return False, "That player isn't on this roster."

    add_player = None
    if add_player_id:
        add_player = db.execute("SELECT * FROM players WHERE id = ?", (add_player_id,)).fetchone()
        if add_player is None:
            return False, "Player not found."
        owned = db.execute(
            "SELECT 1 FROM draft_picks WHERE league_id = ? AND player_id = ?",
            (league_id, add_player_id),
        ).fetchone()
        if owned is not None:
            return False, f"{add_player['full_name']} is already rostered."

    if add_player is None and drop_pick is None:
        return False, "Nothing to do."

    new_count = len(picks) - (1 if drop_pick else 0) + (1 if add_player else 0)
    if new_count > league["rounds"]:
        return False, "Roster is full — drop a player to make room."

    if add_player is not None:
        new_counts = roster_position_counts_after(
            picks, drop_pick_id=drop_pick["id"] if drop_pick else None, add_position=add_player["position"]
        )
        cap = HARD_CAP.get(add_player["position"], 99)
        if new_counts.get(add_player["position"], 0) > cap:
            return False, f"That would put you over the {cap}-max roster limit at {add_player['position']}."

    if drop_pick is not None and add_player is None:
        log_transaction(db, league_id, team_id, "drop", f"Dropped {drop_pick['player_name']} ({drop_pick['position']}).")
        db.execute("DELETE FROM draft_picks WHERE id = ?", (drop_pick["id"],))
        return True, f"Dropped {drop_pick['player_name']}."

    if drop_pick is not None:
        db.execute("DELETE FROM draft_picks WHERE id = ?", (drop_pick["id"],))

    rank_col = RANK_COLUMN_BY_SCORING.get(league["scoring"], "rank_half")
    rank_val = add_player[rank_col]
    overall_pick = next_overall_pick(db, league_id)
    db.execute(
        """
        INSERT INTO draft_picks
            (league_id, overall_pick, round, pick_in_round, team_id, player_id, player_name,
             position, nfl_team, player_rank, reasoning, is_autopick, drafted_at, lineup_slot)
        VALUES (?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'), 'BN')
        """,
        (
            league_id, overall_pick, team_id, add_player["id"], add_player["full_name"],
            add_player["position"], add_player["nfl_team"], rank_val, note,
        ),
    )
    detail = f"Added {add_player['full_name']} ({add_player['position']}) off waivers."
    if drop_pick is not None:
        detail = f"Added {add_player['full_name']} ({add_player['position']}), dropped {drop_pick['player_name']}."
    log_transaction(db, league_id, team_id, "add", detail)

    return True, detail


def maybe_ai_roster_moves(db, league_id):
    """Lets AI teams work the waiver wire, same as a human would on the
    Players page -- periodic, capped at one move per team per pass, and only
    when a free agent is a clear upgrade (rank-value gap past the threshold)."""
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None or league["draft_status"] != "complete":
        return
    last_run = league["ai_moves_at"]
    if last_run and time.time() - last_run < AI_MOVES_INTERVAL_SECONDS:
        return

    ai_teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? AND owner_type = 'ai' AND status = 'filled'",
        (league_id,),
    ).fetchall()

    for team in ai_teams:
        available = get_available_players(db, league_id, league["scoring"])
        if not available:
            continue
        picks = get_roster_picks(db, league_id, team["id"])
        position_counts = {}
        for p in picks:
            position_counts[p["position"]] = position_counts.get(p["position"], 0) + 1

        best_fa = next(
            (p for p in available if position_counts.get(p["position"], 0) < HARD_CAP.get(p["position"], 99)),
            None,
        )
        if best_fa is None:
            continue

        if len(picks) < league["rounds"]:
            execute_roster_move(
                db, league, team, add_player_id=best_fa["id"], drop_pick_id=None,
                note=f"Waiver pickup — best player available (rank #{best_fa['rank']}).",
            )
            continue

        bench = [p for p in picks if p["lineup_slot"] in (None, "BN")]
        if not bench:
            continue
        same_pos_bench = [p for p in bench if p["position"] == best_fa["position"]]
        worst_bench = max(
            same_pos_bench or bench, key=lambda p: (p["player_rank"] is None, p["player_rank"] or 0)
        )
        worst_rank = worst_bench["player_rank"] if worst_bench["player_rank"] is not None else 999999
        if worst_rank - best_fa["rank"] < FA_IMPROVEMENT_THRESHOLD:
            continue

        execute_roster_move(
            db, league, team, add_player_id=best_fa["id"], drop_pick_id=worst_bench["id"],
            note=f"Waiver upgrade — swapped in rank #{best_fa['rank']} over a rank #{worst_rank} bench piece.",
        )

    db.execute("UPDATE leagues SET ai_moves_at = ? WHERE id = ?", (time.time(), league_id))
    db.commit()


def validate_trade_legality(db, league, from_team_id, to_team_id, give_pick_ids, receive_pick_ids):
    """Re-checked both at proposal time and right before a trade executes (a
    roster can change in between). Returns (ok, message, give_picks, receive_picks)."""
    if not give_pick_ids or not receive_pick_ids:
        return False, "A trade needs at least one player on each side.", None, None

    give_placeholders = ",".join("?" * len(give_pick_ids))
    receive_placeholders = ",".join("?" * len(receive_pick_ids))
    give_picks = db.execute(
        f"SELECT * FROM draft_picks WHERE team_id = ? AND player_id IS NOT NULL AND id IN ({give_placeholders})",
        (from_team_id, *give_pick_ids),
    ).fetchall()
    receive_picks = db.execute(
        f"SELECT * FROM draft_picks WHERE team_id = ? AND player_id IS NOT NULL AND id IN ({receive_placeholders})",
        (to_team_id, *receive_pick_ids),
    ).fetchall()
    if len(give_picks) != len(set(give_pick_ids)) or len(receive_picks) != len(set(receive_pick_ids)):
        return False, "One of the selected players is no longer on that roster.", None, None

    from_count = len(get_roster_picks(db, league["id"], from_team_id))
    to_count = len(get_roster_picks(db, league["id"], to_team_id))
    from_new = from_count - len(give_picks) + len(receive_picks)
    to_new = to_count - len(receive_picks) + len(give_picks)
    if from_new > league["rounds"] or to_new > league["rounds"]:
        return False, "That trade would leave a roster over the size limit.", None, None

    from_picks_all = get_roster_picks(db, league["id"], from_team_id)
    to_picks_all = get_roster_picks(db, league["id"], to_team_id)
    give_ids = {p["id"] for p in give_picks}
    receive_ids = {p["id"] for p in receive_picks}

    from_counts = roster_position_counts_after([p for p in from_picks_all if p["id"] not in give_ids])
    for p in receive_picks:
        from_counts[p["position"]] = from_counts.get(p["position"], 0) + 1
    to_counts = roster_position_counts_after([p for p in to_picks_all if p["id"] not in receive_ids])
    for p in give_picks:
        to_counts[p["position"]] = to_counts.get(p["position"], 0) + 1

    for pos, count in list(from_counts.items()) + list(to_counts.items()):
        if count > HARD_CAP.get(pos, 99):
            return False, f"That trade would leave too many {pos}s on one roster.", None, None

    return True, "OK", give_picks, receive_picks


def execute_trade_swap(db, league_id, from_team_id, to_team_id, give_picks, receive_picks):
    for p in give_picks:
        db.execute("UPDATE draft_picks SET team_id = ?, lineup_slot = 'BN' WHERE id = ?", (to_team_id, p["id"]))
    for p in receive_picks:
        db.execute("UPDATE draft_picks SET team_id = ?, lineup_slot = 'BN' WHERE id = ?", (from_team_id, p["id"]))


def evaluate_trade_for_ai(ai_team, give_picks, receive_picks):
    """give_picks: players the AI would RECEIVE. receive_picks: players the AI
    would GIVE UP. Pure value-curve comparison -- no LLM, no randomness."""
    gets_value = sum(player_trade_value(p["player_rank"]) for p in give_picks)
    gives_value = sum(player_trade_value(p["player_rank"]) for p in receive_picks)

    tolerance = TRADE_VALUE_TOLERANCE
    personality = ai_team["ai_personality"]
    profile = PERSONALITY_PROFILES.get(personality, {})
    if "fall_weight" in profile or personality == "The Trader":
        tolerance += 0.08
    if personality in ("The Veteran", "Old School"):
        tolerance -= 0.05

    accept = gets_value >= gives_value * (1 - tolerance)
    gets_names = ", ".join(p["player_name"] for p in give_picks)
    gives_names = ", ".join(p["player_name"] for p in receive_picks)
    if accept:
        reason = f"Accepted — getting {gets_names} outweighs giving up {gives_names} (value {gets_value:.0f} vs {gives_value:.0f})."
    else:
        reason = f"Rejected — not enough coming back for {gives_names} (value {gets_value:.0f} vs {gives_value:.0f})."
    return accept, reason


def find_ai_trade_offer(ai_team, ai_picks, target_picks):
    """Looks for a sensible offer an AI team could send another team: give up
    a surplus player to fill the AI's biggest positional need, at a fair-ish
    value (within 35% of the target player's value, either direction)."""
    if not ai_picks or not target_picks:
        return None

    ai_counts = {}
    for p in ai_picks:
        ai_counts[p["position"]] = ai_counts.get(p["position"], 0) + 1

    need_positions = sorted(
        STARTER_REQUIREMENTS.keys(),
        key=lambda pos: ai_counts.get(pos, 0) - STARTER_REQUIREMENTS.get(pos, 0),
    )
    want = None
    for pos in need_positions:
        candidates = sorted(
            [p for p in target_picks if p["position"] == pos],
            key=lambda p: (p["player_rank"] is None, p["player_rank"] or 0),
        )
        if candidates:
            want = candidates[0]
            break
    if want is None:
        return None
    want_value = player_trade_value(want["player_rank"])

    surplus_positions = {pos for pos, c in ai_counts.items() if c > BENCH_SOFT_CAP.get(pos, 4)}
    give_pool = [p for p in ai_picks if p["position"] in surplus_positions] or list(ai_picks)

    best_give, best_gap = None, None
    for p in give_pool:
        gap = abs(player_trade_value(p["player_rank"]) - want_value)
        if gap <= want_value * 0.35 and (best_gap is None or gap < best_gap):
            best_give, best_gap = p, gap
    if best_give is None:
        return None
    return best_give, want


def maybe_ai_trade_offers(db, league_id):
    """Lets AI teams shop trades to human teams too, not just respond to
    them -- throttled the same way as waiver moves, and capped at one
    outstanding offer per AI team at a time so a human's Trades tab never
    gets flooded."""
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None or league["draft_status"] != "complete":
        return
    last_run = league["ai_trade_offers_at"]
    if last_run and time.time() - last_run < AI_MOVES_INTERVAL_SECONDS:
        return

    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? AND status = 'filled'", (league_id,)
    ).fetchall()
    ai_teams = [t for t in teams if t["owner_type"] == "ai"]
    human_teams = [t for t in teams if t["owner_type"] == "human"]

    if human_teams:
        for ai_team in ai_teams:
            pending = db.execute(
                "SELECT 1 FROM trades WHERE from_team_id = ? AND status = 'pending'", (ai_team["id"],)
            ).fetchone()
            if pending:
                continue

            target_idx = int(stable_unit(f"{league_id}-{ai_team['id']}-target") * len(human_teams))
            target = human_teams[min(target_idx, len(human_teams) - 1)]

            ai_picks = get_roster_picks(db, league_id, ai_team["id"])
            target_picks = get_roster_picks(db, league_id, target["id"])
            found = find_ai_trade_offer(ai_team, ai_picks, target_picks)
            if found is None:
                continue
            give, want = found

            ok, _, give_picks, receive_picks = validate_trade_legality(
                db, league, ai_team["id"], target["id"], [give["id"]], [want["id"]]
            )
            if not ok:
                continue

            trade_id = db.execute(
                "INSERT INTO trades (league_id, from_team_id, to_team_id, status) VALUES (?, ?, ?, 'pending') RETURNING id",
                (league_id, ai_team["id"], target["id"]),
            ).fetchone()["id"]
            for p in give_picks:
                db.execute(
                    "INSERT INTO trade_items (trade_id, pick_id, from_team_id, player_name, position) VALUES (?, ?, ?, ?, ?)",
                    (trade_id, p["id"], ai_team["id"], p["player_name"], p["position"]),
                )
            for p in receive_picks:
                db.execute(
                    "INSERT INTO trade_items (trade_id, pick_id, from_team_id, player_name, position) VALUES (?, ?, ?, ?, ?)",
                    (trade_id, p["id"], target["id"], p["player_name"], p["position"]),
                )

    db.execute("UPDATE leagues SET ai_trade_offers_at = ? WHERE id = ?", (time.time(), league_id))
    db.commit()


def resolve_trade(db, league_id, trade_id, action=None, resolver="human"):
    """resolver='human': `action` ('accept'/'reject') decides it.
    resolver='ai': the target AI team evaluates for itself."""
    trade = db.execute("SELECT * FROM trades WHERE id = ? AND league_id = ?", (trade_id, league_id)).fetchone()
    if trade is None or trade["status"] != "pending":
        return False, "That trade is no longer pending."

    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    items = db.execute("SELECT * FROM trade_items WHERE trade_id = ?", (trade_id,)).fetchall()
    give_ids = [i["pick_id"] for i in items if i["from_team_id"] == trade["from_team_id"]]
    receive_ids = [i["pick_id"] for i in items if i["from_team_id"] == trade["to_team_id"]]

    ok, message, give_picks, receive_picks = validate_trade_legality(
        db, league, trade["from_team_id"], trade["to_team_id"], give_ids, receive_ids
    )
    if not ok:
        db.execute(
            "UPDATE trades SET status = 'rejected', ai_reason = ?, resolved_at = datetime('now') WHERE id = ?",
            (message, trade_id),
        )
        db.commit()
        return False, message

    reason = None
    if resolver == "ai":
        to_team = db.execute("SELECT * FROM teams WHERE id = ?", (trade["to_team_id"],)).fetchone()
        accept, reason = evaluate_trade_for_ai(to_team, give_picks, receive_picks)
    else:
        accept = action == "accept"

    if not accept:
        db.execute(
            "UPDATE trades SET status = 'rejected', ai_reason = ?, resolved_at = datetime('now') WHERE id = ?",
            (reason, trade_id),
        )
        db.commit()
        return True, "Trade rejected."

    execute_trade_swap(db, league_id, trade["from_team_id"], trade["to_team_id"], give_picks, receive_picks)
    db.execute(
        "UPDATE trades SET status = 'accepted', ai_reason = ?, resolved_at = datetime('now') WHERE id = ?",
        (reason, trade_id),
    )
    from_team = db.execute("SELECT * FROM teams WHERE id = ?", (trade["from_team_id"],)).fetchone()
    to_team = db.execute("SELECT * FROM teams WHERE id = ?", (trade["to_team_id"],)).fetchone()
    give_names = ", ".join(p["player_name"] for p in give_picks)
    receive_names = ", ".join(p["player_name"] for p in receive_picks)
    log_transaction(db, league_id, trade["from_team_id"], "trade",
                     f"Traded {give_names} to {to_team['team_name']} for {receive_names}.")
    log_transaction(db, league_id, trade["to_team_id"], "trade",
                     f"Traded {receive_names} to {from_team['team_name']} for {give_names}.")
    db.commit()
    return True, "Trade accepted."


def round_robin_rounds(team_ids):
    teams = list(team_ids)
    bye = None
    if len(teams) % 2 == 1:
        teams.append(bye)
    n = len(teams)
    rounds = []
    for _ in range(n - 1):
        pairs = [
            (teams[i], teams[n - 1 - i])
            for i in range(n // 2)
            if teams[i] is not None and teams[n - 1 - i] is not None
        ]
        rounds.append(pairs)
        teams.insert(1, teams.pop())
    return rounds


def generate_schedule(db, league_id):
    league = db.execute("SELECT league_format FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league and league["league_format"] == "Knockout":
        return  # Knockout has no head-to-head schedule -- see process_knockout_week
    db.execute("DELETE FROM matchups WHERE league_id = ?", (league_id,))
    teams = db.execute(
        "SELECT id FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    team_ids = [t["id"] for t in teams]
    if len(team_ids) < 2:
        db.commit()
        return

    rounds = round_robin_rounds(team_ids)
    rows = []
    for week in range(1, SEASON_WEEKS + 1):
        round_pairs = rounds[(week - 1) % len(rounds)]
        flip = ((week - 1) // len(rounds)) % 2 == 1
        for team_a, team_b in round_pairs:
            if flip:
                team_a, team_b = team_b, team_a
            rows.append((league_id, week, team_a, team_b))

    db.executemany(
        "INSERT INTO matchups (league_id, week, team_a_id, team_b_id) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.commit()


def ensure_schedule(db, league_id):
    """Self-heals leagues whose draft completed before schedule generation
    existed (or any other reason the matchups table ended up empty)."""
    count = db.execute(
        "SELECT COUNT(*) FROM matchups WHERE league_id = ?", (league_id,)
    ).fetchone()[0]
    if count == 0:
        generate_schedule(db, league_id)


def finalize_week_if_ready(db, league_id, week):
    """Once every NFL game in a week is final, lock in that week's matchup
    scores and record the win/loss -- before that, scores stay live/computed."""
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        return
    matchups = db.execute(
        "SELECT * FROM matchups WHERE league_id = ? AND week = ?", (league_id, week)
    ).fetchall()
    if not matchups:
        return
    games = db.execute("SELECT * FROM nfl_games WHERE week = ?", (week,)).fetchall()
    if not games or not all(g["status"] == "post" for g in games):
        return

    for m in matchups:
        if m["played"]:
            continue
        if m["team_b_id"] is None:
            db.execute("UPDATE matchups SET played = 1 WHERE id = ?", (m["id"],))
            continue
        score_a = get_team_live_score(db, league_id, m["team_a_id"], week, league["scoring"])
        score_b = get_team_live_score(db, league_id, m["team_b_id"], week, league["scoring"])
        db.execute(
            "UPDATE matchups SET team_a_score = ?, team_b_score = ?, played = 1 WHERE id = ?",
            (score_a, score_b, m["id"]),
        )
        if score_a > score_b:
            db.execute("UPDATE teams SET wins = wins + 1 WHERE id = ?", (m["team_a_id"],))
            db.execute("UPDATE teams SET losses = losses + 1 WHERE id = ?", (m["team_b_id"],))
        elif score_b > score_a:
            db.execute("UPDATE teams SET wins = wins + 1 WHERE id = ?", (m["team_b_id"],))
            db.execute("UPDATE teams SET losses = losses + 1 WHERE id = ?", (m["team_a_id"],))
    db.commit()


def process_knockout_week(db, league_id, week):
    """Once a week's NFL games are all final, the lowest-scoring team still
    alive is eliminated. Idempotent -- safe to call repeatedly."""
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None or league["league_format"] != "Knockout":
        return
    games = db.execute("SELECT * FROM nfl_games WHERE week = ?", (week,)).fetchall()
    if not games or not all(g["status"] == "post" for g in games):
        return
    already = db.execute(
        "SELECT 1 FROM teams WHERE league_id = ? AND eliminated_week = ?", (league_id, week)
    ).fetchone()
    if already is not None:
        return

    alive = db.execute(
        "SELECT * FROM teams WHERE league_id = ? AND status = 'filled' AND eliminated_week IS NULL",
        (league_id,),
    ).fetchall()
    if len(alive) <= 1:
        return  # champion already decided -- nothing left to eliminate

    scored = [(t, get_team_live_score(db, league_id, t["id"], week, league["scoring"])) for t in alive]
    min_score = min(s for _, s in scored)
    for t, s in scored:
        if s == min_score:
            db.execute("UPDATE teams SET eliminated_week = ? WHERE id = ?", (week, t["id"]))
    db.commit()


def get_knockout_standings(db, league_id):
    """(alive, eliminated) team lists -- eliminated sorted most-recent first
    (survived longest ranks highest)."""
    teams = db.execute(
        "SELECT * FROM teams WHERE league_id = ? AND status = 'filled' ORDER BY slot_index", (league_id,)
    ).fetchall()
    alive = [t for t in teams if t["eliminated_week"] is None]
    eliminated = sorted(
        (t for t in teams if t["eliminated_week"] is not None),
        key=lambda t: t["eliminated_week"], reverse=True,
    )
    return alive, eliminated


def sync_week_scoring(db, league_id, week):
    sync_week_stats(db, week)
    league = db.execute("SELECT league_format FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league and league["league_format"] == "Knockout":
        process_knockout_week(db, league_id, week)
    else:
        finalize_week_if_ready(db, league_id, week)


def get_points_for_against(db, league_id):
    pf_pa = {}
    rows = db.execute(
        "SELECT * FROM matchups WHERE league_id = ? AND played = 1 AND team_b_id IS NOT NULL",
        (league_id,),
    ).fetchall()
    for m in rows:
        a = pf_pa.setdefault(m["team_a_id"], [0.0, 0.0])
        a[0] += m["team_a_score"]
        a[1] += m["team_b_score"]
        b = pf_pa.setdefault(m["team_b_id"], [0.0, 0.0])
        b[0] += m["team_b_score"]
        b[1] += m["team_a_score"]
    return pf_pa


def get_week_status(db, week):
    games = db.execute("SELECT status FROM nfl_games WHERE week = ?", (week,)).fetchall()
    if not games:
        return "scheduled"
    if all(g["status"] == "post" for g in games):
        return "final"
    if any(g["status"] != "pre" for g in games):
        return "live"
    return "scheduled"


def ensure_lineup_slots(db, league_id, team_id):
    """First-time roster lineup assignment (best rank fills starters). A no-op
    once slots exist, so it never clobbers a user's manual drag-and-drop swaps."""
    picks = db.execute(
        """
        SELECT * FROM draft_picks
        WHERE league_id = ? AND team_id = ? AND player_id IS NOT NULL
        ORDER BY (player_rank IS NULL), player_rank ASC, overall_pick ASC
        """,
        (league_id, team_id),
    ).fetchall()
    if not picks or not all(p["lineup_slot"] is None for p in picks):
        return

    remaining = list(picks)
    assignments = []

    for pos, count in STARTER_REQUIREMENTS.items():
        for i in range(count):
            idx = next((j for j, p in enumerate(remaining) if p["position"] == pos), None)
            if idx is not None:
                pick = remaining.pop(idx)
                slot_code = pos if count == 1 else f"{pos}{i + 1}"
                assignments.append((pick["id"], slot_code))

    idx = next((j for j, p in enumerate(remaining) if p["position"] in FLEX_ELIGIBLE), None)
    if idx is not None:
        pick = remaining.pop(idx)
        assignments.append((pick["id"], "FLEX"))

    for p in remaining:
        assignments.append((p["id"], "BN"))

    db.executemany(
        "UPDATE draft_picks SET lineup_slot = ? WHERE id = ?",
        [(slot, pid) for pid, slot in assignments],
    )
    db.commit()


def build_lineup(db, league_id, team_id):
    ensure_lineup_slots(db, league_id, team_id)

    picks = db.execute(
        """
        SELECT dp.*, p.injury_status, p.years_exp
        FROM draft_picks dp
        LEFT JOIN players p ON p.id = dp.player_id
        WHERE dp.league_id = ? AND dp.team_id = ? AND dp.player_id IS NOT NULL
        """,
        (league_id, team_id),
    ).fetchall()

    empty_slot = {
        "id": None, "player_id": None, "player_name": None, "position": None, "nfl_team": None,
        "player_rank": None, "injury_status": None,
    }

    by_slot = {}
    bench = []
    for p in picks:
        if p["lineup_slot"] in (None, "BN"):
            bench.append(p)
        else:
            by_slot[p["lineup_slot"]] = p

    starters = []
    for slot_code in STARTER_SLOT_ORDER:
        p = by_slot.get(slot_code)
        label = slot_display_label(slot_code)
        if p is not None:
            starters.append(dict(p, slot=label, slot_code=slot_code))
        else:
            starters.append(dict(empty_slot, slot=label, slot_code=slot_code))

    bench.sort(key=lambda p: (p["player_rank"] is None, p["player_rank"] or 0))
    bench = [dict(p, slot="BN", slot_code="BN") for p in bench]
    return starters, bench


def player_projection(position, rank, scoring):
    cfg = PROJECTION_MODEL.get(position)
    if cfg is None:
        return None
    rank = rank if isinstance(rank, int) and rank < 999999 else 150
    rank_factor = max(0.0, 1 - (rank / 220))
    proj = cfg["base"] + cfg["rank_bonus"] * rank_factor
    if position in PPR_BONUS_POSITIONS:
        proj += PPR_PROJECTION_BONUS.get(scoring, 0.0)
    return round(proj, 1)


def with_projections(rows, scoring, schedule_map=None):
    schedule_map = schedule_map or {}
    out = []
    for p in rows:
        row = dict(p)
        has_player = bool(row.get("player_id"))
        row["proj"] = player_projection(row.get("position"), row.get("player_rank"), scoring) if has_player else None
        row["injury_label"] = INJURY_LABELS.get(row.get("injury_status"))
        if has_player:
            game = schedule_map.get(row.get("nfl_team"))
            if game:
                row["opponent"] = ("vs " if game["is_home"] else "@ ") + (game["opponent"] or "")
            elif row.get("nfl_team"):
                row["opponent"] = "BYE"
            else:
                row["opponent"] = None
            row["face_url"] = player_face_url(row.get("player_id"), row.get("position"), row.get("nfl_team"))
            row["initials"] = player_initials(row.get("player_name"))
        else:
            row["opponent"] = None
            row["face_url"] = None
            row["initials"] = None
        out.append(row)
    return out


def total_projection(rows):
    return round(sum(r["proj"] for r in rows if r.get("proj")), 1)


def serialize_pick(row):
    return {
        "id": row["id"], "overall_pick": row["overall_pick"], "round": row["round"],
        "pick_in_round": row["pick_in_round"], "team_id": row["team_id"],
        "team_name": row["team_name"], "owner_type": row["owner_type"],
        "owner_name": row["owner_name"], "ai_personality": row["ai_personality"],
        "player_id": row["player_id"], "player_name": row["player_name"],
        "position": row["position"], "nfl_team": row["nfl_team"],
        "player_rank": row["player_rank"], "reasoning": row["reasoning"],
        "is_autopick": bool(row["is_autopick"]),
    }


def build_state(league_id):
    db = get_db()
    maybe_auto_draft_timeout(db, league_id)
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        return None

    total_picks = league["num_teams"] * league["rounds"] if league["rounds"] else 0

    picks_rows = db.execute(
        """
        SELECT dp.*, t.team_name, t.owner_type, t.owner_name, t.ai_personality
        FROM draft_picks dp
        JOIN teams t ON t.id = dp.team_id
        WHERE dp.league_id = ? AND dp.round >= 1
        ORDER BY dp.overall_pick ASC
        """,
        (league_id,),
    ).fetchall()
    picks = [serialize_pick(r) for r in picks_rows]

    teams_rows = db.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    picks_by_team = {}
    for p in picks:
        if p["player_id"]:
            picks_by_team.setdefault(p["team_id"], []).append(p)
    teams = [
        {
            "id": t["id"], "slot_index": t["slot_index"], "team_name": t["team_name"],
            "owner_type": t["owner_type"], "owner_name": t["owner_name"],
            "ai_personality": t["ai_personality"], "is_commissioner": bool(t["is_commissioner"]),
            "roster": picks_by_team.get(t["id"], []),
        }
        for t in teams_rows
    ]

    on_the_clock = None
    remaining_seconds = None
    available_players = []
    if league["draft_status"] == "in_progress":
        on_clock_row = get_current_pick_row(db, league_id, league["current_pick_index"])
        if on_clock_row is not None:
            on_the_clock = serialize_pick(on_clock_row)
            if on_clock_row["owner_type"] == "human" and league["pick_deadline"]:
                remaining_seconds = max(0, round(league["pick_deadline"] - time.time()))
        available_players = [
            {
                "id": p["id"], "full_name": p["full_name"], "position": p["position"],
                "nfl_team": p["nfl_team"], "search_rank": p["rank"],
                "years_exp": p["years_exp"],
            }
            for p in get_available_players(db, league_id, league["scoring"])[:200]
        ]

    grades = json.loads(league["grades_json"]) if league["grades_json"] else None

    return {
        "league": {
            "id": league["id"], "name": league["name"], "num_teams": league["num_teams"],
            "draft_status": league["draft_status"], "current_pick_index": league["current_pick_index"],
            "rounds": league["rounds"], "total_picks": total_picks,
            "ai_speed": league["ai_speed"], "human_timer_seconds": league["human_timer_seconds"],
        },
        "my_team_id": get_my_team_id(league_id, teams_rows),
        "on_the_clock": on_the_clock,
        "remaining_seconds": remaining_seconds,
        "picks": picks,
        "teams": teams,
        "available_players": available_players,
        "grades": grades,
        "urls": {
            "state": url_for("draft_state", league_id=league_id),
            "advance": url_for("draft_advance", league_id=league_id),
            "pick": url_for("draft_pick", league_id=league_id),
        },
    }


@app.route("/leagues/<int:league_id>/draft/start", methods=["POST"])
def start_draft(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    if league["draft_status"] != "not_started":
        return redirect(url_for("draft_room", league_id=league_id))

    open_count = db.execute(
        "SELECT COUNT(*) FROM teams WHERE league_id = ? AND status = 'open'", (league_id,)
    ).fetchone()[0]
    if open_count > 0:
        flash("Fill every team slot (human or AI) before starting the draft.")
        return redirect(url_for("league_home", league_id=league_id))

    if not sync_players(db):
        flash("Couldn't load NFL player data (no network?). Try again in a moment.")
        return redirect(url_for("league_home", league_id=league_id))

    ai_speed = request.form.get("ai_speed", "fast")
    if ai_speed not in AI_SPEED_CHOICES:
        ai_speed = "fast"
    try:
        human_timer = int(request.form.get("human_timer_seconds", 90))
    except ValueError:
        human_timer = 90
    if human_timer not in HUMAN_TIMER_CHOICES:
        human_timer = 90

    teams = db.execute(
        "SELECT id, owner_type FROM teams WHERE league_id = ? ORDER BY slot_index", (league_id,)
    ).fetchall()
    draft_order = [(t["id"], t["owner_type"]) for t in teams]

    randomize_order = request.form.get("randomize_order") == "on"
    if randomize_order:
        random.shuffle(draft_order)

    team_ids = [tid for tid, _ in draft_order]

    rounds = DYNASTY_ROSTER_ROUNDS if league["league_format"] == "Dynasty" else ROSTER_ROUNDS
    order = build_snake_order(team_ids, rounds)
    db.executemany(
        """
        INSERT INTO draft_picks (league_id, overall_pick, round, pick_in_round, team_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(league_id, i + 1, rnd, pick_in_round, team_id) for i, (rnd, pick_in_round, team_id) in enumerate(order)],
    )

    first_team_type = draft_order[0][1] if draft_order else "human"
    deadline = time.time() + human_timer if first_team_type == "human" else None

    db.execute(
        """
        UPDATE leagues
        SET draft_status = 'in_progress', current_pick_index = 1, rounds = ?,
            ai_speed = ?, human_timer_seconds = ?, pick_deadline = ?, grades_json = NULL
        WHERE id = ?
        """,
        (rounds, ai_speed, human_timer, deadline, league_id),
    )
    db.commit()

    return redirect(url_for("draft_room", league_id=league_id))


@app.route("/leagues/<int:league_id>/draft")
def draft_room(league_id):
    db = get_db()
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league is None:
        flash("League not found.")
        return redirect(url_for("index"))

    if league["draft_status"] == "not_started":
        return redirect(url_for("league_home", league_id=league_id))

    state = build_state(league_id)
    return render_template("draft.html", league=league, state_json=json.dumps(state))


@app.route("/leagues/<int:league_id>/draft/state")
def draft_state(league_id):
    state = build_state(league_id)
    if state is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(state)


@app.route("/leagues/<int:league_id>/draft/advance", methods=["POST"])
def draft_advance(league_id):
    db = get_db()
    maybe_auto_draft_timeout(db, league_id)
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()

    if league is not None and league["draft_status"] == "in_progress":
        pick_row = get_current_pick_row(db, league_id, league["current_pick_index"])
        if pick_row is not None and pick_row["owner_type"] == "ai" and pick_row["player_id"] is None:
            team_row = db.execute("SELECT * FROM teams WHERE id = ?", (pick_row["team_id"],)).fetchone()
            if execute_pick_for_team(db, league_id, pick_row, team_row, league["scoring"], personality=team_row["ai_personality"]):
                db.commit()
                advance_after_pick(db, league_id)

    state = build_state(league_id)
    if state is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(state)


@app.route("/leagues/<int:league_id>/draft/pick", methods=["POST"])
def draft_pick(league_id):
    db = get_db()
    maybe_auto_draft_timeout(db, league_id)
    league = db.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()

    if league is None:
        return jsonify({"error": "not_found"}), 404
    if league["draft_status"] != "in_progress":
        return jsonify({"error": "not_in_progress", **build_state(league_id)}), 400

    player_id = request.form.get("player_id") or (request.get_json(silent=True) or {}).get("player_id")
    pick_row = get_current_pick_row(db, league_id, league["current_pick_index"])

    if pick_row is None or pick_row["owner_type"] != "human" or pick_row["player_id"] is not None:
        return jsonify({"error": "not_your_turn", **build_state(league_id)}), 409

    # Whoever is on the clock is a *human team*, but that doesn't mean it's
    # THIS browser's team -- without this check, any visitor watching the
    # draft room could submit a pick on behalf of whichever human happens to
    # be up, which is exactly what was happening.
    if session.get(f"team_{league_id}") != pick_row["team_id"]:
        return jsonify({"error": "not_your_turn", **build_state(league_id)}), 403

    player = db.execute(
        """
        SELECT * FROM players
        WHERE id = ? AND id NOT IN (
            SELECT player_id FROM draft_picks WHERE league_id = ? AND player_id IS NOT NULL
        )
        """,
        (player_id, league_id),
    ).fetchone()

    if player is None:
        return jsonify({"error": "unavailable", **build_state(league_id)}), 409

    rank_col = RANK_COLUMN_BY_SCORING.get(league["scoring"], "rank_half")
    reasoning = f"{pick_row['owner_name'] or pick_row['team_name']} selects {player['full_name']} ({player['position']})."
    db.execute(
        """
        UPDATE draft_picks
        SET player_id = ?, player_name = ?, position = ?, nfl_team = ?, player_rank = ?,
            reasoning = ?, is_autopick = 0, drafted_at = datetime('now')
        WHERE id = ?
        """,
        (player["id"], player["full_name"], player["position"], player["nfl_team"],
         player[rank_col], reasoning, pick_row["id"]),
    )
    db.commit()
    advance_after_pick(db, league_id)

    return jsonify(build_state(league_id))


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=8010)
