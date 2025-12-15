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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        UNIQUE(game_id, name),
        FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
    )
    """)

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

def list_games():
    c = conn()
    rows = c.execute("SELECT id, name, created_at FROM games ORDER BY id DESC").fetchall()
    c.close()
    return rows

def create_game(name: str):
    c = conn()
    cur = c.cursor()
    cur.execute("INSERT INTO games (name) VALUES (?)", (name,))
    game_id = cur.lastrowid
    c.commit()
    c.close()
    return game_id

def delete_game(game_id: int):
    c = conn()
    c.execute("DELETE FROM games WHERE id = ?", (game_id,))
    c.commit()
    c.close()

def set_roster(game_id: int, roster: list[str], stat_keys: list[str]):
    """Ensure players exist, remove old players not in roster, ensure stat rows exist."""
    c = conn()
    cur = c.cursor()

    # Current players
    existing = cur.execute("SELECT id, name FROM players WHERE game_id = ?", (game_id,)).fetchall()
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
    players = cur.execute("SELECT id FROM players WHERE game_id = ? ORDER BY id", (game_id,)).fetchall()
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
        """, (direction * v, game_id, player_id, k))
    c.commit()
    c.close()

