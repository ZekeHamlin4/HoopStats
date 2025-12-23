import os
import csv
import io
import tempfile
from datetime import datetime

import streamlit as st
import stripe
import pandas as pd

# --- Keep this (you liked it) ---
st.sidebar.caption("✅ Google login active")

from reportlab.lib.pagesizes import LETTER
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

from db import (
    init_db,
    get_or_create_user,
    is_user_pro,
    set_user_pro,
    list_games,
    create_game,
    delete_game,
    set_roster,
    load_game,
    apply_change,
)

# ============================================================
# CONFIG
# ============================================================
STAT_KEYS = ["2PM","2PA","3PM","3PA","FTM","FTA","OREB","DREB","AST","TOV","STL","BLK","PF"]

DEFAULT_HOME = ["Player 1","Player 2","Player 3","Player 4","Player 5"]
DEFAULT_AWAY = ["Opponent 1","Opponent 2","Opponent 3","Opponent 4","Opponent 5"]

LOGO_PATH = "logo.png"
FAVICON_PATH = "favicon.png"

ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

# Stripe env (set these when you’re ready)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")  # subscription price id
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:8501/?upgraded=1")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "http://localhost:8501/?canceled=1")

HOME_PREFIX = "HOME::"
AWAY_PREFIX = "AWAY::"

PERIODS = ["Q1", "Q2", "Q3", "Q4", "OT"]

# ============================================================
# HELPERS
# ============================================================
def pct(m, a): return round((m / a) * 100, 1) if a else 0.0
def points(s): return int(s["2PM"])*2 + int(s["3PM"])*3 + int(s["FTM"])
def fgm(s): return int(s["2PM"]) + int(s["3PM"])
def fga(s): return int(s["2PA"]) + int(s["3PA"])
def total_reb(s): return int(s["OREB"]) + int(s["DREB"])

def team_totals(roster, stats):
    t = {k: 0 for k in STAT_KEYS}
    for name in roster:
        for k in STAT_KEYS:
            t[k] += int(stats[name].get(k, 0))
    return t

def clean_name(n: str) -> str:
    if n.startswith(HOME_PREFIX): return n[len(HOME_PREFIX):]
    if n.startswith(AWAY_PREFIX): return n[len(AWAY_PREFIX):]
    return n

def team_of(n: str) -> str:
    if n.startswith(HOME_PREFIX): return "Home"
    if n.startswith(AWAY_PREFIX): return "Away"
    return "Home"  # fallback for older one-team games

def add_prefix(team: str, player_name: str) -> str:
    player_name = player_name.strip()
    if not player_name:
        return ""
    if player_name.startswith(HOME_PREFIX) or player_name.startswith(AWAY_PREFIX):
        return player_name
    return (HOME_PREFIX if team == "Home" else AWAY_PREFIX) + player_name

def is_both_teams_game(roster):
    return any(n.startswith(HOME_PREFIX) for n in roster) and any(n.startswith(AWAY_PREFIX) for n in roster)

def current_user_email():
    """
    Hosted (Streamlit auth): uses st.user if available.
    Local fallback: stored email in session_state.
    """
    u = getattr(st, "user", None)
    if u is not None:
        email = getattr(u, "email", "") or (u.get("email") if hasattr(u, "get") else "")
        email = (email or "").strip().lower()
        if email:
            return email
    return (st.session_state.get("email") or "").strip().lower()

# --- Action labels + point values (for log + runs) ---
ACTION_LABELS = {
    "2PM": "✅ 2PT Made",
    "2PA": "❌ 2PT Miss",
    "3PM": "✅ 3PT Made",
    "3PA": "❌ 3PT Miss",
    "FTM": "✅ FT Made",
    "FTA": "❌ FT Miss",
    "OREB": "OREB +1",
    "DREB": "DREB +1",
    "AST": "AST +1",
    "TOV": "TOV +1",
    "STL": "STL +1",
    "BLK": "BLK +1",
    "PF":  "FOUL +1",
}
def change_points(change: dict) -> int:
    # points only from made shots
    return int(change.get("2PM", 0))*2 + int(change.get("3PM", 0))*3 + int(change.get("FTM", 0))*1

def nice_change_label(change: dict) -> str:
    # best-effort label for common combos
    keys = {k for k, v in change.items() if int(v) != 0}
    if keys == {"2PM","2PA","FTM","FTA"}:
        return "⭐ And-1 (2PT + FT)"
    if keys == {"3PA","FTA"}:
        return "⭐ 3-Foul (3PA + FT)"
    if keys == {"DREB","AST"}:
        return "⭐ DREB + AST"
    if keys == {"OREB","2PM","2PA"}:
        return "⭐ OREB + 2PT"
    # single-stat fallback
    if len(keys) == 1:
        k = list(keys)[0]
        return ACTION_LABELS.get(k, f"{k} +1")
    # otherwise show a short summary
    parts = []
    for k in ["2PM","2PA","3PM","3PA","FTM","FTA","OREB","DREB","AST","TOV","STL","BLK","PF"]:
        if k in keys:
            parts.append(k)
    return " + ".join(parts)[:45]

def format_scoreline(home_score, away_score):
    return f"{home_score}–{away_score}"

def compute_takeaways(home_roster, away_roster, stats):
    # simple “story” comparisons
    th = team_totals(home_roster, stats) if home_roster else {k:0 for k in STAT_KEYS}
    ta = team_totals(away_roster, stats) if away_roster else {k:0 for k in STAT_KEYS}

    home_reb = th["OREB"] + th["DREB"]
    away_reb = ta["OREB"] + ta["DREB"]
    reb_diff = home_reb - away_reb

    home_tov = th["TOV"]
    away_tov = ta["TOV"]
    tov_diff = away_tov - home_tov  # “forced” feel if away has more

    home_3p_pct = pct(th["3PM"], th["3PA"])
    away_3p_pct = pct(ta["3PM"], ta["3PA"])

    home_ft_pct = pct(th["FTM"], th["FTA"])
    away_ft_pct = pct(ta["FTM"], ta["FTA"])

    items = []
    if reb_diff != 0:
        lead = "Home" if reb_diff > 0 else "Away"
        items.append(f"{lead} +{abs(reb_diff)} REB advantage")
    if tov_diff != 0:
        lead = "Home" if tov_diff > 0 else "Away"
        items.append(f"{lead} forced {abs(tov_diff)} more TOV")
    if th["3PA"] >= 3 or ta["3PA"] >= 3:
        lead = "Home" if home_3p_pct > away_3p_pct else "Away"
        items.append(f"{lead} better from 3 ({home_3p_pct}% vs {away_3p_pct}%)")
    if th["FTA"] >= 3 or ta["FTA"] >= 3:
        lead = "Home" if home_ft_pct > away_ft_pct else "Away"
        items.append(f"{lead} better at FT ({home_ft_pct}% vs {away_ft_pct}%)")

    # cap to 3 for clean UI
    return items[:3]

def leaders_from_roster(roster_list, stats):
    # returns dict of leader rows for PTS/REB/AST
    if not roster_list:
        return {"PTS": None, "REB": None, "AST": None}

    def statline(name):
        s = stats[name]
        return {
            "name": clean_name(name),
            "PTS": points(s),
            "REB": total_reb(s),
            "AST": int(s["AST"]),
            "TOV": int(s["TOV"]),
            "FGM": fgm(s),
            "FGA": fga(s),
            "3PM": int(s["3PM"]),
            "3PA": int(s["3PA"]),
            "FTM": int(s["FTM"]),
            "FTA": int(s["FTA"]),
        }

    lines = [statline(n) for n in roster_list]
    pts = max(lines, key=lambda x: x["PTS"])
    reb = max(lines, key=lambda x: x["REB"])
    ast = max(lines, key=lambda x: x["AST"])
    return {"PTS": pts, "REB": reb, "AST": ast}

def runs_from_log(log_entries, team: str, last_n: int = 6):
    # “Run” = points in last N scoring events (simple & fast)
    if not log_entries:
        return 0
    pts = 0
    count = 0
    for e in reversed(log_entries):
        if e.get("pts", 0) <= 0:
            continue
        count += 1
        if e.get("team") == team:
            pts += int(e.get("pts", 0))
        if count >= last_n:
            break
    return pts

# ============================================================
# PAGE SETUP / STYLE (removes the “top bar” look you hated)
# ============================================================
st.set_page_config(
    page_title="HoopStats",
    page_icon=FAVICON_PATH if os.path.exists(FAVICON_PATH) else "🏀",
    layout="wide",
)

st.markdown(
    """
    <style>
      /* tighter + cleaner */
      .block-container { padding-top: 0.6rem; padding-bottom: 2rem; max-width: 1400px; }

      /* button feel */
      div.stButton > button {
        padding: 0.9rem 1.0rem;
        font-weight: 800;
        border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.08);
      }

      /* metrics typography */
      div[data-testid="stMetricValue"] { font-size: 1.75rem; }
      div[data-testid="stMetricLabel"] { font-size: 0.9rem; opacity: 0.9; }

      .subtle { opacity: 0.82; font-size: 0.93rem; }

      /* make sidebar feel like an app nav */
      section[data-testid="stSidebar"] { padding-top: 0.6rem; }

      /* slightly bigger tabs/nav labels */
      .stRadio label { font-size: 0.98rem; }

      /* hide Streamlit default footer */
      footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)

init_db()

# ============================================================
# SIDEBAR BRANDING
# ============================================================
if os.path.exists(LOGO_PATH):
    st.sidebar.image(LOGO_PATH, use_container_width=True)

st.sidebar.markdown("## 🏀 HoopStats")
st.sidebar.caption("Fast live stat tracking")
st.sidebar.divider()

# ============================================================
# LOGIN (keep stable)
# ============================================================
has_login = hasattr(st, "login") and callable(getattr(st, "login"))
has_logout = hasattr(st, "logout") and callable(getattr(st, "logout"))

st.session_state.setdefault("email", "")
email_now = current_user_email()

if not email_now:
    st.sidebar.subheader("Account")
    if has_login:
        if st.sidebar.button("Log in with Google", key="login_google", use_container_width=True):
            try:
                st.login()
            except Exception:
                st.sidebar.error(
                    "Google login isn’t configured on Streamlit Cloud for this app yet. "
                    "Use the Email fallback below for now (or set up Auth in Manage app → Settings → Authentication)."
                )

    st.sidebar.caption("Local dev fallback:")
    local_email = st.sidebar.text_input("Email", value=st.session_state.email).strip().lower()
    if st.sidebar.button("Save Email", key="save_email"):
        st.session_state.email = local_email
        st.rerun()

    email_now = current_user_email()

if not email_now:
    st.title("HoopStats")
    st.info("Log in to start tracking games.")
    st.stop()

user_id = get_or_create_user(email_now, name=email_now.split("@")[0])

is_admin = email_now in ADMIN_EMAILS
if is_admin:
    set_user_pro(user_id, True)

is_pro = is_admin or is_user_pro(user_id)

# Stripe success redirect
qp = st.query_params
if qp.get("upgraded") == "1":
    set_user_pro(user_id, True)
    st.success("✅ Upgrade successful! Pro unlocked.")
    st.query_params.clear()
    is_pro = True

# ============================================================
# GAMES (keep in sidebar, but “Account” details moved to Account page)
# ============================================================
st.sidebar.subheader("Games")

games = list_games(user_id)
labels = ["➕ New Game"] + [f"{gid}: {name}" for gid, name, _ in games]
choice = st.sidebar.selectbox("Select game", labels, key="game_select")

st.session_state.setdefault("game_id", None)

if choice == "➕ New Game":
    new_name = st.sidebar.text_input("Game name", "Home vs Away", key="new_game_name")

    tracking_mode = st.sidebar.selectbox(
        "Tracking mode",
        ["Both Teams (Full Box Score)", "One Team (Standard)"],
        index=0,
        key="tracking_mode_new",
    )

    if st.sidebar.button("Create", key="create_game_btn"):
        st.session_state.game_id = create_game(user_id, new_name)

        if tracking_mode == "Both Teams (Full Box Score)":
            combined = [add_prefix("Home", p) for p in DEFAULT_HOME] + [add_prefix("Away", p) for p in DEFAULT_AWAY]
            set_roster(st.session_state.game_id, combined, STAT_KEYS)
        else:
            set_roster(st.session_state.game_id, [add_prefix("Home", p) for p in DEFAULT_HOME], STAT_KEYS)

        st.rerun()
else:
    st.session_state.game_id = int(choice.split(":")[0])

if not st.session_state.game_id:
    st.stop()

gid = st.session_state.game_id

if st.sidebar.button("🗑️ Delete game", key="delete_game_btn"):
    delete_game(user_id, gid)
    st.session_state.game_id = None
    st.rerun()

# ============================================================
# LOAD GAME + ROSTERS
# ============================================================
roster, name_to_pid, stats = load_game(gid, STAT_KEYS)
if not roster:
    combined = [add_prefix("Home", p) for p in DEFAULT_HOME] + [add_prefix("Away", p) for p in DEFAULT_AWAY]
    set_roster(gid, combined, STAT_KEYS)
    roster, name_to_pid, stats = load_game(gid, STAT_KEYS)

both_mode = is_both_teams_game(roster)
home_roster = [n for n in roster if team_of(n) == "Home"]
away_roster = [n for n in roster if team_of(n) == "Away"]

# ============================================================
# PER-GAME STATE KEYS (avoid widget assignment errors)
# ============================================================
live_team_key = f"live_team_{gid}"
poss_key = f"possession_{gid}"
period_value_key = f"period_value_{gid}"
period_widget_key = f"period_widget_{gid}"

sel_value_key = f"selected_player_value_{gid}"
sel_widget_key = f"selected_player_widget_{gid}"

shortcuts_key = f"shortcuts_{gid}"

player_search_key = f"player_search_{gid}"
roster_view_key = f"roster_view_{gid}"

log_key = f"log_{gid}"
undo_key = f"undo_{gid}"

# Seed defaults
st.session_state.setdefault(live_team_key, "Home")
st.session_state.setdefault(poss_key, "Home")
st.session_state.setdefault(period_value_key, "Q1")
st.session_state.setdefault(shortcuts_key, True)
st.session_state.setdefault(player_search_key, "")
st.session_state.setdefault(roster_view_key, "All")
st.session_state.setdefault(log_key, [])
st.session_state.setdefault(undo_key, [])

# ============================================================
# SIDEBAR: NAVIGATION (this fixes “tabs disappear”)
# Live shows first automatically.
# ============================================================
st.sidebar.divider()
st.sidebar.subheader("Navigate")

nav_items = ["Live", "Summary", "Box Score", "Player", "Export", "Season / Reports", "Account"]
page = st.sidebar.radio("", nav_items, index=0, key=f"nav_{gid}")

# ============================================================
# SIDEBAR: ROSTER EDITOR (clean + tucked away)
# ============================================================
st.sidebar.divider()
with st.sidebar.expander("Roster", expanded=False):
    if both_mode:
        home_text = st.text_area(
            "Home (one per line)",
            value="\n".join([clean_name(n) for n in home_roster]),
            height=120,
            key=f"home_roster_text_{gid}",
        )
        away_text = st.text_area(
            "Away (one per line)",
            value="\n".join([clean_name(n) for n in away_roster]),
            height=120,
            key=f"away_roster_text_{gid}",
        )
        if st.button("Update Rosters", key=f"update_rosters_{gid}", use_container_width=True):
            new_home = [add_prefix("Home", x) for x in home_text.splitlines() if x.strip()]
            new_away = [add_prefix("Away", x) for x in away_text.splitlines() if x.strip()]
            if not new_home or not new_away:
                st.error("Both Home and Away rosters must have at least 1 player.")
            else:
                set_roster(gid, new_home + new_away, STAT_KEYS)
                st.rerun()
    else:
        roster_text = st.text_area(
            "Home (one per line)",
            value="\n".join([clean_name(n) for n in home_roster]),
            height=160,
            key=f"home_only_roster_text_{gid}",
        )
        if st.button("Update Roster", key=f"update_roster_{gid}", use_container_width=True):
            new_roster = [add_prefix("Home", line.strip()) for line in roster_text.splitlines() if line.strip()]
            if not new_roster:
                st.error("Roster cannot be empty.")
            else:
                set_roster(gid, new_roster, STAT_KEYS)
                st.rerun()

# ============================================================
# SHARED: SCORE COMPUTE
# ============================================================
home_score = sum(points(stats[n]) for n in home_roster) if home_roster else 0
away_score = sum(points(stats[n]) for n in away_roster) if away_roster else 0

# ============================================================
# ACTION APPLICATION (stats + log + undo) — safe with widgets
# ============================================================
pid_to_name = {pid: name for name, pid in name_to_pid.items()}

def _push_log(team: str, player_name: str, change: dict):
    entry = {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "period": st.session_state.get(period_value_key, "Q1"),
        "team": team,
        "player": clean_name(player_name),
        "label": nice_change_label(change),
        "pts": change_points(change),
    }
    st.session_state[log_key].append(entry)
    # cap for performance
    if len(st.session_state[log_key]) > 500:
        st.session_state[log_key] = st.session_state[log_key][-500:]

def apply_delta(change: dict, direction: int):
    """
    direction: +1 add, -1 subtract
    """
    # use controlled selection (value key), not the widget key
    current_player = st.session_state.get(sel_value_key, None)
    if not current_player:
        # fallback
        current_player = (home_roster[0] if home_roster else roster[0])

    current_pid = name_to_pid.get(current_player)
    if current_pid is None:
        return

    apply_change(gid, current_pid, change, direction=direction)

    # undo stack includes whether we also logged
    did_log = False
    if direction == +1:
        _push_log(team_of(current_player), current_player, change)
        did_log = True

    st.session_state[undo_key].append({
        "pid": current_pid,
        "change": change,
        "dir": direction,
        "logged": did_log
    })
    if len(st.session_state[undo_key]) > 200:
        st.session_state[undo_key] = st.session_state[undo_key][-200:]

    st.rerun()

def undo_last():
    stack = st.session_state.get(undo_key, [])
    if not stack:
        return
    last = stack.pop()
    st.session_state[undo_key] = stack

    apply_change(gid, last["pid"], last["change"], direction=(-1 * last["dir"]))

    # pop log if we created one
    if last.get("logged") and st.session_state.get(log_key):
        st.session_state[log_key].pop()

    st.rerun()

# ============================================================
# PAGE: LIVE (premium scoreboard + possession + runs + log)
# ============================================================
if page == "Live":
    st.header("Live Game Mode")
    st.caption("Phone-first stat entry. Tap fast. Fix mistakes instantly.")

    # --- Scoreboard row (sleek) ---
    sb = st.container()
    with sb:
        c1, c2, c3, c4, c5 = st.columns([1.2, 1.2, 1.8, 1.2, 1.2])

        with c1:
            st.metric("HOME", home_score)
        with c2:
            st.metric("AWAY", away_score)

        with c3:
            # Period selector (widget -> value sync)
            def _sync_period():
                st.session_state[period_value_key] = st.session_state.get(period_widget_key, "Q1")

            cur = st.session_state.get(period_value_key, "Q1")
            idx = PERIODS.index(cur) if cur in PERIODS else 0
            st.selectbox(
                "Period",
                PERIODS,
                index=idx,
                key=period_widget_key,
                on_change=_sync_period,
            )

        with c4:
            # Possession toggle (premium feel)
            st.write("")
            if st.button(
                "🏀 Poss: HOME" if st.session_state[poss_key] == "Home" else "🏀 Poss: AWAY",
                key=f"poss_btn_{gid}",
                use_container_width=True,
            ):
                st.session_state[poss_key] = "Away" if st.session_state[poss_key] == "Home" else "Home"
                st.rerun()

        with c5:
            st.write("")
            st.button(
                "↩️ Undo last",
                key=f"undo_last_btn_{gid}",
                use_container_width=True,
                disabled=len(st.session_state.get(undo_key, [])) == 0,
                on_click=undo_last,
            )

    # --- Runs + quick context ---
    log_entries = st.session_state.get(log_key, [])
    run_home = runs_from_log(log_entries, "Home", last_n=6)
    run_away = runs_from_log(log_entries, "Away", last_n=6)

    r1, r2, r3 = st.columns([1.2, 1.2, 1.6])
    with r1:
        st.metric("Home run (last 6 scores)", run_home)
    with r2:
        st.metric("Away run (last 6 scores)", run_away)
    with r3:
        poss = st.session_state.get(poss_key, "Home")
        st.caption(f"**Possession:** {poss} • **Score:** {format_scoreline(home_score, away_score)}")

    st.divider()

    # --- Team toggle if both teams mode ---
    if both_mode:
        t1, t2, t3 = st.columns([1.2, 1.2, 3])
        with t1:
            if st.button(
                "🏠 HOME",
                use_container_width=True,
                type="primary" if st.session_state[live_team_key] == "Home" else "secondary",
                key=f"btn_home_{gid}"
            ):
                st.session_state[live_team_key] = "Home"
                st.rerun()
        with t2:
            if st.button(
                "🚌 AWAY",
                use_container_width=True,
                type="primary" if st.session_state[live_team_key] == "Away" else "secondary",
                key=f"btn_away_{gid}"
            ):
                st.session_state[live_team_key] = "Away"
                st.rerun()
        with t3:
            st.checkbox("Shortcuts", key=shortcuts_key, help="Turn on/off quick combo buttons.")
        active_team = st.session_state.get(live_team_key, "Home")
        active_roster = home_roster if active_team == "Home" else away_roster
    else:
        # one-team mode: still use “Home” roster
        active_team = "Home"
        active_roster = home_roster if home_roster else roster
        st.checkbox("Shortcuts", key=shortcuts_key, help="Turn on/off quick combo buttons.")

    # --- Filters row ---
    f1, f2 = st.columns([2, 1])
    with f1:
        st.text_input("Search player", key=player_search_key, placeholder="Type a name…")
    with f2:
        st.selectbox("View", ["All"], key=roster_view_key)

    search_txt = (st.session_state.get(player_search_key, "") or "").strip().lower()
    filtered_roster = list(active_roster)
    if search_txt:
        filtered_roster = [p for p in filtered_roster if search_txt in clean_name(p).lower()]
    if not filtered_roster:
        filtered_roster = list(active_roster)

    # --- Controlled selection keys (prevents session_state widget errors) ---
    st.session_state.setdefault(sel_value_key, filtered_roster[0] if filtered_roster else (active_roster[0] if active_roster else roster[0]))

    if st.session_state[sel_value_key] not in filtered_roster and filtered_roster:
        st.session_state[sel_value_key] = filtered_roster[0]

    def _sync_selected_player():
        st.session_state[sel_value_key] = st.session_state.get(sel_widget_key, st.session_state[sel_value_key])

    col_players, col_actions = st.columns([1.05, 1.95])

    with col_players:
        st.subheader(f"{active_team} Players" if both_mode else "Players")

        def _player_label(name: str) -> str:
            s = stats.get(name, {k: 0 for k in STAT_KEYS})
            return f"{clean_name(name)} • {points(s)} pts"

        # set radio index based on current value
        current = st.session_state.get(sel_value_key)
        idx = filtered_roster.index(current) if current in filtered_roster else 0

        st.radio(
            "",
            filtered_roster,
            index=idx,
            format_func=_player_label,
            key=sel_widget_key,
            on_change=_sync_selected_player,
            label_visibility="collapsed",
        )

        # tiny pro tease box (kept)
        with st.expander("🔒 Pro preview (tap to see what you’ll unlock)", expanded=False):
            st.write("Season leaderboards • Shareable reports • Team trends • More exports")
            st.info("Upgrade to Pro from the Account page.")

        # last 10 plays quick view (premium feel, no clock needed)
        st.caption("Recent plays")
        recent = list(reversed(log_entries[-10:])) if log_entries else []
        if recent:
            for e in recent[:10]:
                st.write(f"**{e['period']}** • {e['team']} • {e['player']} — {e['label']}")
        else:
            st.write("—")

    # Selected player
    player = st.session_state.get(sel_value_key, filtered_roster[0])
    s = stats[player]

    with col_actions:
        # header row: stat strip like you wanted
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("PTS", points(s))
        m2.metric("FG%", pct(fgm(s), fga(s)))
        m3.metric("FG", f"{fgm(s)}/{fga(s)}")
        m4.metric("3PT%", pct(int(s["3PM"]), int(s["3PA"])))
        m5.metric("FT%", pct(int(s["FTM"]), int(s["FTA"])))
        m6.metric("REB", total_reb(s))

        st.markdown(
            f"<div class='subtle'><b>Selected:</b> {clean_name(player)} • Tracking <b>{active_team}</b> • Period: <b>{st.session_state.get(period_value_key,'Q1')}</b></div>",
            unsafe_allow_html=True
        )
        st.divider()

        # helper: plus/minus row
        def pm_row(label: str, group_key: str, plus_change: dict, minus_change: dict = None):
            if minus_change is None:
                minus_change = plus_change
            c1, c2 = st.columns([5, 1])
            with c1:
                st.button(
                    label,
                    key=f"{gid}_{name_to_pid[player]}_{group_key}_plus",
                    use_container_width=True,
                    on_click=apply_delta,
                    args=(plus_change, +1),
                )
            with c2:
                st.button(
                    "−",
                    key=f"{gid}_{name_to_pid[player]}_{group_key}_minus",
                    use_container_width=True,
                    on_click=apply_delta,
                    args=(minus_change, -1),
                )

        # Quick Actions (premium, lightweight)
        if st.session_state.get(shortcuts_key, True):
            st.subheader("Quick Actions")
            qa1, qa2, qa3, qa4 = st.columns(4)

            with qa1:
                if st.button("And-1 (2+FT)", key=f"qa_and1_{gid}_{name_to_pid[player]}", use_container_width=True):
                    apply_delta({"2PM": 1, "2PA": 1, "FTM": 1, "FTA": 1}, +1)
            with qa2:
                if st.button("3-Foul (3A+FT)", key=f"qa_3foul_{gid}_{name_to_pid[player]}", use_container_width=True):
                    apply_delta({"3PA": 1, "FTA": 1}, +1)
            with qa3:
                if st.button("DREB + AST", key=f"qa_dreb_ast_{gid}_{name_to_pid[player]}", use_container_width=True):
                    apply_delta({"DREB": 1, "AST": 1}, +1)
            with qa4:
                if st.button("OREB + 2PT", key=f"qa_putback_{gid}_{name_to_pid[player]}", use_container_width=True):
                    apply_delta({"OREB": 1, "2PM": 1, "2PA": 1}, +1)

            st.divider()

        g1, g2, g3 = st.columns([1, 1, 1])

        with g1:
            st.markdown("## Scoring")
            pm_row("✅ 2PT Made", "2pm", {"2PM": 1, "2PA": 1})
            pm_row("❌ 2PT Miss", "2pa", {"2PA": 1})
            pm_row("✅ 3PT Made", "3pm", {"3PM": 1, "3PA": 1})
            pm_row("❌ 3PT Miss", "3pa", {"3PA": 1})
            pm_row("✅ FT Made", "ftm", {"FTM": 1, "FTA": 1})
            pm_row("❌ FT Miss", "fta", {"FTA": 1})

        with g2:
            st.markdown("## Hustle")
            pm_row("OREB +1", "oreb", {"OREB": 1})
            pm_row("DREB +1", "dreb", {"DREB": 1})
            pm_row("AST +1", "ast", {"AST": 1})
            pm_row("TOV +1", "tov", {"TOV": 1})
            st.write("")
            st.markdown("## Fouls")
            pm_row("FOUL +1", "pf", {"PF": 1})

        with g3:
            st.markdown("## Defense")
            pm_row("STL +1", "stl", {"STL": 1})
            pm_row("BLK +1", "blk", {"BLK": 1})

# ============================================================
# PAGE: SUMMARY (game story + takeaways + leaders)
# ============================================================
elif page == "Summary":
    st.header("Game Summary")

    headline = "Home vs Away"
    if both_mode:
        if home_score != away_score:
            leader = "Home" if home_score > away_score else "Away"
            headline = f"{leader} leads {format_scoreline(home_score, away_score)}"
        else:
            headline = f"Tied {format_scoreline(home_score, away_score)}"
    else:
        headline = f"Home score: {home_score}"

    st.subheader(headline)

    # Takeaways chips
    if both_mode:
        takeaways = compute_takeaways(home_roster, away_roster, stats)
        if takeaways:
            c = st.columns(len(takeaways))
            for i, t in enumerate(takeaways):
                with c[i]:
                    st.info(t)
        else:
            st.caption("No takeaways yet — add more stats and this will populate.")

    # Leaders
    st.divider()
    st.subheader("Leaders")

    if both_mode:
        lh = leaders_from_roster(home_roster, stats)
        la = leaders_from_roster(away_roster, stats)

        left, right = st.columns(2)
        with left:
            st.markdown("### Home")
            st.write(f"**PTS:** {lh['PTS']['name']} — {lh['PTS']['PTS']}")
            st.write(f"**REB:** {lh['REB']['name']} — {lh['REB']['REB']}")
            st.write(f"**AST:** {lh['AST']['name']} — {lh['AST']['AST']}")
        with right:
            st.markdown("### Away")
            st.write(f"**PTS:** {la['PTS']['name']} — {la['PTS']['PTS']}")
            st.write(f"**REB:** {la['REB']['name']} — {la['REB']['REB']}")
            st.write(f"**AST:** {la['AST']['name']} — {la['AST']['AST']}")
    else:
        lh = leaders_from_roster(home_roster, stats)
        st.write(f"**PTS:** {lh['PTS']['name']} — {lh['PTS']['PTS']}")
        st.write(f"**REB:** {lh['REB']['name']} — {lh['REB']['REB']}")
        st.write(f"**AST:** {lh['AST']['name']} — {lh['AST']['AST']}")

    # Recent plays (shareable story)
    st.divider()
    st.subheader("Recent Plays")
    log_entries = st.session_state.get(log_key, [])
    if log_entries:
        df = pd.DataFrame(list(reversed(log_entries[-25:])))
        st.dataframe(df[["period","team","player","label","pts","ts"]], use_container_width=True, hide_index=True)
    else:
        st.caption("No plays yet.")

# ============================================================
# PAGE: BOX SCORE (clean + sortable + optional “advanced”)
# ============================================================
elif page == "Box Score":
    st.header("Box Score")

    def build_rows(roster_list, stats):
        out = []
        for name in roster_list:
            s = stats[name]
            out.append({
                "Player": clean_name(name),
                "PTS": points(s),
                "FG": f"{fgm(s)}/{fga(s)}",
                "FG%": pct(fgm(s), fga(s)),
                "3PT": f"{int(s['3PM'])}/{int(s['3PA'])}",
                "3PT%": pct(int(s["3PM"]), int(s["3PA"])),
                "FT": f"{int(s['FTM'])}/{int(s['FTA'])}",
                "FT%": pct(int(s["FTM"]), int(s["FTA"])),
                "OREB": int(s["OREB"]),
                "DREB": int(s["DREB"]),
                "REB": int(s["OREB"]) + int(s["DREB"]),
                "AST": int(s["AST"]),
                "TOV": int(s["TOV"]),
                "STL": int(s["STL"]),
                "BLK": int(s["BLK"]),
                "PF": int(s["PF"]),
            })
        return out

    adv = st.toggle("Show advanced (eFG%, AST/TO)", value=False, key=f"adv_box_{gid}")
    sort_by = st.selectbox("Sort by", ["PTS","REB","AST","TOV","STL","BLK","FG%","3PT%","FT%"], index=0, key=f"sort_box_{gid}")

    if both_mode:
        left, right = st.columns(2)

        with left:
            st.subheader("Home")
            home_rows = build_rows(home_roster, stats)
            dfh = pd.DataFrame(home_rows)
            if adv:
                # eFG% = (FGM + 0.5*3PM) / FGA
                dfh["eFG%"] = dfh.apply(lambda r: pct((r["FG"].split("/")[0] if isinstance(r["FG"], str) else 0), 1), axis=1)
                # simpler: compute from raw stats again
                dfh["eFG%"] = [pct(fgm(stats[n]) + 0.5*int(stats[n]["3PM"]), fga(stats[n])) for n in home_roster]
                dfh["AST/TO"] = [round(int(stats[n]["AST"]) / int(stats[n]["TOV"]), 2) if int(stats[n]["TOV"]) else float(int(stats[n]["AST"])) for n in home_roster]
            dfh = dfh.sort_values(by=sort_by, ascending=False)
            st.dataframe(dfh, use_container_width=True, hide_index=True)

        with right:
            st.subheader("Away")
            away_rows = build_rows(away_roster, stats)
            dfa = pd.DataFrame(away_rows)
            if adv:
                dfa["eFG%"] = [pct(fgm(stats[n]) + 0.5*int(stats[n]["3PM"]), fga(stats[n])) for n in away_roster]
                dfa["AST/TO"] = [round(int(stats[n]["AST"]) / int(stats[n]["TOV"]), 2) if int(stats[n]["TOV"]) else float(int(stats[n]["AST"])) for n in away_roster]
            dfa = dfa.sort_values(by=sort_by, ascending=False)
            st.dataframe(dfa, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Totals")
        th = team_totals(home_roster, stats)
        ta = team_totals(away_roster, stats)
        totals_rows = [
            {"Team": "Home", "PTS": points(th), "REB": th["OREB"]+th["DREB"], "AST": th["AST"], "TOV": th["TOV"], "STL": th["STL"], "BLK": th["BLK"]},
            {"Team": "Away", "PTS": points(ta), "REB": ta["OREB"]+ta["DREB"], "AST": ta["AST"], "TOV": ta["TOV"], "STL": ta["STL"], "BLK": ta["BLK"]},
        ]
        st.dataframe(pd.DataFrame(totals_rows), use_container_width=True, hide_index=True)

        # rows for export/reports
        rows_for_export = home_rows + away_rows
    else:
        rows_for_export = build_rows(home_roster if home_roster else roster, stats)
        df = pd.DataFrame(rows_for_export)
        if adv:
            rr = (home_roster if home_roster else roster)
            df["eFG%"] = [pct(fgm(stats[n]) + 0.5*int(stats[n]["3PM"]), fga(stats[n])) for n in rr]
            df["AST/TO"] = [round(int(stats[n]["AST"]) / int(stats[n]["TOV"]), 2) if int(stats[n]["TOV"]) else float(int(stats[n]["AST"])) for n in rr]
        df = df.sort_values(by=sort_by, ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)

# ============================================================
# PAGE: PLAYER (player profile + last actions)
# ============================================================
elif page == "Player":
    st.header("Player Profile")

    all_players = roster[:] if roster else []
    if not all_players:
        st.info("No players yet.")
        st.stop()

    chosen = st.selectbox("Choose player", all_players, format_func=clean_name, key=f"profile_pick_{gid}")
    s = stats[chosen]

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("PTS", points(s))
    p2.metric("REB", total_reb(s))
    p3.metric("AST", int(s["AST"]))
    p4.metric("TOV", int(s["TOV"]))

    st.divider()
    st.subheader("Shooting")
    a1, a2, a3 = st.columns(3)
    a1.metric("FG", f"{fgm(s)}/{fga(s)}")
    a2.metric("3PT", f"{int(s['3PM'])}/{int(s['3PA'])}")
    a3.metric("FT", f"{int(s['FTM'])}/{int(s['FTA'])}")

    st.caption(
        f"FG% {pct(fgm(s), fga(s))}% • 3PT% {pct(int(s['3PM']), int(s['3PA']))}% • FT% {pct(int(s['FTM']), int(s['FTA']))}%"
    )

    st.divider()
    st.subheader("Recent Actions")
    log_entries = st.session_state.get(log_key, [])
    if log_entries:
        player_log = [e for e in log_entries if e.get("player") == clean_name(chosen)]
        if player_log:
            df = pd.DataFrame(list(reversed(player_log[-50:])))
            st.dataframe(df[["period","team","label","pts","ts"]], use_container_width=True, hide_index=True)
        else:
            st.caption("No logged actions for this player yet.")
    else:
        st.caption("No plays logged yet.")

    st.divider()
    if not is_pro:
        st.info("🔒 Pro idea: season averages + game-by-game trend for this player.")
    else:
        st.success("Pro unlocked ✅ (next step: season trend across games)")

# ============================================================
# PAGE: EXPORT (Pro)
# ============================================================
elif page == "Export":
    st.header("Export")

    if not is_pro:
        st.warning("🔒 Pro feature: Export CSV & PDF box scores + printable reports.")
        st.stop()

    # Build export rows from current game (same as box score build)
    def export_rows_for(roster_list):
        out = []
        for name in roster_list:
            s = stats[name]
            out.append({
                "Player": clean_name(name),
                "PTS": points(s),
                "FG": f"{fgm(s)}/{fga(s)}",
                "FG%": pct(fgm(s), fga(s)),
                "3PT": f"{int(s['3PM'])}/{int(s['3PA'])}",
                "3PT%": pct(int(s["3PM"]), int(s["3PA"])),
                "FT": f"{int(s['FTM'])}/{int(s['FTA'])}",
                "FT%": pct(int(s["FTM"]), int(s["FTA"])),
                "OREB": int(s["OREB"]),
                "DREB": int(s["DREB"]),
                "REB": int(s["OREB"]) + int(s["DREB"]),
                "AST": int(s["AST"]),
                "TOV": int(s["TOV"]),
                "STL": int(s["STL"]),
                "BLK": int(s["BLK"]),
                "PF": int(s["PF"]),
            })
        return out

    rows = export_rows_for(home_roster) + (export_rows_for(away_roster) if both_mode else [])

    # CSV
    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    st.download_button("⬇️ Download CSV", csv_buf.getvalue(), "box_score.csv", "text/csv", use_container_width=True)

    # PDF
    def build_pdf():
        styles = getSampleStyleSheet()
        data = [list(rows[0].keys())]
        for r in rows:
            data.append(list(r.values()))

        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ]))

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        doc = SimpleDocTemplate(tmp.name, pagesize=LETTER)
        subtitle = f"Game ID {gid} • {st.session_state.get(period_value_key,'')}".strip()
        doc.build([
            Paragraph("HoopStats Box Score", styles["Title"]),
            Paragraph(subtitle, styles["Normal"]),
            Spacer(1, 10),
            table
        ])
        return tmp.name

    if st.button("⬇️ Generate PDF", key=f"pdf_{gid}", use_container_width=True):
        path = build_pdf()
        with open(path, "rb") as f:
            st.download_button("Download PDF", f, "box_score.pdf", "application/pdf", use_container_width=True)

# ============================================================
# PAGE: SEASON / REPORTS (Pro teaser)
# ============================================================
elif page == "Season / Reports":
    st.header("Season / Reports")

    if not is_pro:
        st.info("🔒 Pro: season totals across games, leaderboards, player averages, share links.")
        st.caption("Upgrade from the Account page.")
        st.stop()

    st.success("Pro unlocked ✅ (next step: aggregate totals across games in db.py)")
    st.caption("When you're ready, we’ll add cross-game aggregation + leaderboards here.")

# ============================================================
# PAGE: ACCOUNT (clean, no sidebar email clutter)
# ============================================================
elif page == "Account":
    st.header("Account")

    st.write(f"Signed in as **{email_now}**")
    st.write("Plan: **Pro ✅**" if is_pro else "Plan: **Free**")

    # Stripe upgrade
    def start_stripe_checkout():
        if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
            st.error("Stripe not configured yet. Set STRIPE_SECRET_KEY + STRIPE_PRICE_ID.")
            return

        stripe.api_key = STRIPE_SECRET_KEY
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=STRIPE_SUCCESS_URL,
            cancel_url=STRIPE_CANCEL_URL,
            client_reference_id=str(user_id),
            customer_email=email_now,
            allow_promotion_codes=True,
        )
        st.link_button("Continue to Checkout", session.url, use_container_width=True)

    if not is_pro:
        st.subheader("Upgrade to Pro")
        st.write("Exports • Season leaderboards • Shareable reports • Team trends")
        start_stripe_checkout()
        st.divider()

    # Share link instructions (Streamlit Community Cloud)
    st.subheader("Share this with a friend (Streamlit Cloud)")
    st.write(
        "When you deploy on Streamlit Community Cloud, your app gets a public URL.\n\n"
        "Open your deployed app and copy the browser URL — that’s the link to send."
    )
    st.caption("Tip: If your friend needs Google login access while the OAuth app is in testing, add them as a Test User in Google OAuth Consent Screen.")

    st.divider()
    if has_logout:
        st.button("Log out", key="logout_btn", use_container_width=True, on_click=st.logout)
    else:
        st.caption("Logout not available in this environment.")

# ============================================================
# DONE
# ============================================================
