import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path("hoopstats.db")


# ============================================================
# CONNECTION
# ============================================================
def conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON;")
    return c


# ============================================================
# INIT DB
# ============================================================
def init_db():
    c = conn()
    cur = c.cursor()

    # ---------------- USERS ----------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT,
        is_pro INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ---------------- GAMES ----------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)

    # ---------------- PLAYERS ----------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        UNIQUE(game_id, name),
        FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
    )
    """)

    # ---------------- STATS ----------------
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

    c.commit()
    c.close()


# ============================================================
# USERS / PRO
# ============================================================
def get_or_create_user(email: str, name: Optional[str] = None) -> int:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Email required")

    c = conn()
    cur = c.cursor()

    row = cur.execute(
        "SELECT id FROM users WHERE email = ?",
        (email,)
    ).fetchone()

    if row:
        user_id = int(row[0])
        if name:
            cur.execute(
                "UPDATE users SET name = COALESCE(name, ?) WHERE id = ?",
                (name, user_id)
            )
        c.commit()
        c.close()
        return user_id

    cur.execute(
        "INSERT INTO users (email, name, is_pro) VALUES (?, ?, 0)",
        (email, name)
    )
    user_id = int(cur.lastrowid)
    c.commit()
    c.close()
    return user_id


def is_user_pro(user_id: int) -> bool:
    c = conn()
    row = c.execute(
        "SELECT is_pro FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    c.close()
    return bool(row and int(row[0]) == 1)


def set_user_pro(user_id: int, is_pro: bool = True):
    c = conn()
    c.execute(
        "UPDATE users SET is_pro = ? WHERE id = ?",
        (1 if is_pro else 0, user_id)
    )
    c.commit()
    c.close()


# ============================================================
# GAMES
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
    cur.execute(
        "INSERT INTO games (user_id, name) VALUES (?, ?)",
        (user_id, name)
    )
    game_id = int(cur.lastrowid)
    c.commit()
    c.close()
    return game_id


def delete_game(user_id: int, game_id: int):
    c = conn()
    c.execute(
        "DELETE FROM games WHERE id = ? AND user_id = ?",
        (game_id, user_id)
    )
    c.commit()
    c.close()


# ============================================================
# ROSTERS / STATS
# ============================================================
def set_roster(game_id: int, roster: list, stat_keys: list):
    c = conn()
    cur = c.cursor()

    existing = cur.execute(
        "SELECT id, name FROM players WHERE game_id = ?",
        (game_id,)
    ).fetchall()
    existing_by_name = {name: pid for pid, name in existing}

    new_set = set(roster)
    old_set = set(existing_by_name.keys())

    for name in (old_set - new_set):
        cur.execute(
            "DELETE FROM players WHERE game_id = ? AND name = ?",
            (game_id, name)
        )

    for name in roster:
        if name not in existing_by_name:
            cur.execute(
                "INSERT INTO players (game_id, name) VALUES (?, ?)",
                (game_id, name)
            )

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


def load_game(game_id: int, stat_keys: list):
    c = conn()
    cur = c.cursor()

    players = cur.execute(
        "SELECT id, name FROM players WHERE game_id = ? ORDER BY id",
        (game_id,)
    ).fetchall()

    roster = [name for _, name in players]
    name_to_pid = {name: pid for pid, name in players}

    stats_by_player = {}
    for pid, name in players:
        rows = cur.execute("""
            SELECT stat_key, stat_value
            FROM stats
            WHERE game_id = ? AND player_id = ?
        """, (game_id, pid)).fetchall()

        d = {k: 0 for k in stat_keys}
        d.update({k: int(v) for k, v in rows})
        stats_by_player[name] = d

    c.close()
    return roster, name_to_pid, stats_by_player


def apply_change(game_id: int, player_id: int, change: dict, direction: int = 1):
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
