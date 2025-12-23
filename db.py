import sqlite3
from pathlib import Path

DB_PATH = Path("hoopstats.db")


def conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON;")
    return c


def init_db():
    c = conn()
    cur = c.cursor()

    # --- Users (for Google login / Pro plan) ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT,
        is_pro INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # --- Games (now per-user) ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)

    # --- Players (per game) ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        UNIQUE(game_id, name),
        FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
    )
    """)

    # --- Stats (per game/player/stat_key) ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        game_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        stat_key TEXT NOT NULL,
        stat_value INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (game_id, player_id, stat_key),
        FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE,
        FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
    )
    """)

    # ------------------------------------------------------------
    # Lightweight migration support (if you have an older DB)
    # - Ensure games.user_id exists (older DBs won't have it)
    # ------------------------------------------------------------
    try:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(games)").fetchall()]
        if "user_id" not in cols:
            cur.execute("ALTER TABLE games ADD COLUMN user_id INTEGER")
    except Exception:
        # If something weird happens, we still want the app to run.
        pass

    c.commit()
    c.close()


# ============================================================
# USERS / PRO PLAN
# ============================================================
def get_or_create_user(email: str, name: str | None = None) -> int:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email required")

    c = conn()
    cur = c.cursor()

    row = cur.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        user_id = int(row[0])
        # optional: update name if blank
        if name:
            cur.execute("UPDATE users SET name = COALESCE(name, ?) WHERE id = ?", (name, user_id))
        c.commit()
        c.close()
        return user_id

    cur.execute("INSERT INTO users (email, name, is_pro) VALUES (?, ?, 0)", (email, name))
    user_id = int(cur.lastrowid)
    c.commit()
    c.close()
    return user_id


def is_user_pro(user_id: int) -> bool:
    c = conn()
    row = c.execute("SELECT is_pro FROM users WHERE id = ?", (user_id,)).fetchone()
    c.close()
    return bool(row and int(row[0]) == 1)


def set_user_pro(user_id: int, is_pro: bool = True):
    c = conn()
    c.execute("UPDATE users SET is_pro = ? WHERE id = ?", (1 if is_pro else 0, user_id))
    c.commit()
    c.close()


# ============================================================
# GAMES (scoped to user)
# ============================================================
def list_games(user_id: int):
    c = conn()
    rows = c.execute(
        "SELECT id, name, created_at FROM games WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    c.close()
    return rows


def create_game(user_id: int, name: str) -> int:
    c = conn()
    cur = c.cursor()
    cur.execute("INSERT INTO games (user_id, name) VALUES (?, ?)", (user_id, name))
    game_id = int(cur.lastrowid)
    c.commit()
    c.close()
    return game_id


def delete_game(user_id: int, game_id: int):
    c = conn()
    # only delete games owned by this user
    c.execute("DELETE FROM games WHERE id = ? AND user_id = ?", (game_id, user_id))
    c.commit()
    c.close()


# ============================================================
# ROSTERS / STATS
# ============================================================
def set_roster(game_id: int, roster: list[str], stat_keys: list[str]):
    """Ensure players exist, remove old players not in roster, ensure stat rows exist."""
    c = conn()
    cur = c.cursor()

    existing = cur.execute(
        "SELECT id, name FROM players WHERE game_id = ?",
        (game_id,)
    ).fetchall()
    existing_by_name = {name: pid for pid, name in existing}

    new_set = set(roster)
    old_set = set(existing_by_name.keys())

    # Delete removed players
    for name in (old_set - new_set):
        cur.execute("DELETE FROM players WHERE game_id = ? AND name = ?", (game_id, name))

    # Add missing players
    for name in roster:
        if name not in existing_by_name:
            cur.execute("INSERT INTO players (game_id, name) VALUES (?, ?)", (game_id, name))

    # Ensure stats rows for all players + stat keys
    players = cur.execute(
        "SELECT id FROM players WHERE game_id = ? ORDER BY id",
        (game_id,)
    ).fetchall()

    for (pid,) in players:
        for k in stat_keys:
            cur.execute("""
                INSERT OR IGNORE INTO stats (game_id, player_id, stat_key, stat_value)
                VALUES (?, ?, ?, 0)
            """, (game_id, pid, k))

    c.commit()
    c.close()


def load_game(game_id: int, stat_keys: list[str]):
    """Returns roster list, mapping name->player_id, and mapping name->stats dict."""
    c = conn()
    cur = c.cursor()

    players = cur.execute(
        "SELECT id, name FROM players WHERE game_id = ? ORDER BY id",
        (game_id,)
    ).fetchall()

    roster = [name for _, name in players]
    name_to_pid = {name: pid for pid, name in players}

    player_stats = {}
    for pid, name in players:
        rows = cur.execute("""
            SELECT stat_key, stat_value
            FROM stats
            WHERE game_id = ? AND player_id = ?
        """, (game_id, pid)).fetchall()
        d = {k: 0 for k in stat_keys}
        d.update({k: v for k, v in rows})
        player_stats[name] = d

    c.close()
    return roster, name_to_pid, player_stats


def apply_change(game_id: int, player_id: int, change: dict[str, int], direction: int = 1):
    c = conn()
    cur = c.cursor()
    for k, v in change.items():
        cur.execute("""
            UPDATE stats
            SET stat_value = stat_value + ?
            WHERE game_id = ? AND player_id = ? AND stat_key = ?
        """, (direction * int(v), game_id, player_id, k))
    c.commit()
    c.close()
