import os
import time
import sqlite3
import streamlit as st
import stripe

from typing import Optional

from db import init_db, list_games, create_game, delete_game, set_roster, load_game, apply_change

import csv
import io
import tempfile

from reportlab.lib.pagesizes import LETTER
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet


# ------------------
# App constants
# ------------------
STAT_KEYS = ["2PM","2PA","3PM","3PA","FTM","FTA","OREB","DREB","AST","TOV"]
DEFAULT_ROSTER = ["Player 1","Player 2","Player 3","Player 4","Player 5"]

DB_PATH = "hoopstats.db"
LOGO_PATH = "logo.png"
FAVICON_PATH = "favicon.png"

# Admin bypass: set in Terminal like:
# export ADMIN_EMAILS="you@example.com,coach@example.com"
ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
ADMIN_EMAILS = {e.strip().lower() for e in ADMIN_EMAILS_RAW.split(",") if e.strip()}


def empty_player_stats():
    return {k: 0 for k in STAT_KEYS}

def pct(m, a):
    return round((m / a) * 100, 1) if a else 0.0


# ------------------
# Page config (MUST be first Streamlit call)
# ------------------
page_icon = FAVICON_PATH if os.path.exists(FAVICON_PATH) else "🏀"
st.set_page_config(page_title="HoopStats", page_icon=page_icon, layout="wide")


# ------------------
# Stripe config (READ FROM TERMINAL ENV VARS)
# ------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")          # sk_test_... or sk_live_...
PRICE_ID = os.getenv("STRIPE_PRICE_ID")                  # price_...
APP_URL = os.getenv("APP_URL", "http://localhost:8501")  # localhost for now


# ------------------
# Local user DB (for Pro persistence)
# ------------------
def user_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            plan TEXT DEFAULT 'free',
            last_checked INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()

def upsert_user(email: str, customer_id: Optional[str] = None, plan: Optional[str] = None):
    email = normalize_email(email)
    if not email:
        return
    conn = user_db()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE email=?", (email,))
    exists = cur.fetchone() is not None
    now = int(time.time())

    if not exists:
        cur.execute(
            "INSERT INTO users(email, stripe_customer_id, plan, last_checked) VALUES (?, ?, ?, ?)",
            (email, customer_id, plan or "free", 0)
        )
    else:
        if customer_id is not None:
            cur.execute("UPDATE users SET stripe_customer_id=? WHERE email=?", (customer_id, email))
        if plan is not None:
            cur.execute("UPDATE users SET plan=?, last_checked=? WHERE email=?", (plan, now, email))

    conn.commit()

def get_user(email: str):
    email = normalize_email(email)
    if not email:
        return None
    conn = user_db()
    cur = conn.cursor()
    cur.execute("SELECT email, stripe_customer_id, plan, last_checked FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return None
    return {"email": row[0], "customer_id": row[1], "plan": row[2], "last_checked": row[3]}

def set_last_checked(email: str):
    email = normalize_email(email)
    if not email:
        return
    conn = user_db()
    conn.execute("UPDATE users SET last_checked=? WHERE email=?", (int(time.time()), email))
    conn.commit()

def set_plan(email: str, plan: str):
    upsert_user(email, plan=plan)

def verify_subscription_for_user(email: str, cache_seconds: int = 300) -> str:
    """
    Returns 'pro' or 'free' by checking Stripe subscriptions for the user's customer.
    Uses a small cache in SQLite to avoid hammering Stripe.
    """
    email = normalize_email(email)
    if not email:
        return "free"

    u = get_user(email)
    if not u or not u.get("customer_id"):
        return u["plan"] if u else "free"

    if not stripe.api_key:
        return u["plan"]

    now = int(time.time())
    if u["last_checked"] and (now - u["last_checked"] < cache_seconds):
        return u["plan"]

    customer_id = u["customer_id"]

    try:
        subs = stripe.Subscription.list(customer=customer_id, status="active", limit=25)
        is_pro = False

        if PRICE_ID:
            for s in subs.data:
                for item in (s["items"]["data"] or []):
                    if item["price"]["id"] == PRICE_ID:
                        is_pro = True
                        break
                if is_pro:
                    break
        else:
            is_pro = len(subs.data) > 0

        plan = "pro" if is_pro else "free"
        set_plan(email, plan)
        set_last_checked(email)
        return plan
    except Exception:
        return u["plan"]


# ------------------
# Initialize your stats DB (from db.py)
# ------------------
init_db()

st.title("HoopStats – Live Stat Tracker (Saved Games)")


# ------------------
# Sidebar: Logo + Branding
# ------------------
if os.path.exists(LOGO_PATH):
    st.sidebar.image(LOGO_PATH, use_container_width=True)

st.sidebar.markdown("## 🏀 HoopStats")
st.sidebar.caption("Live stat tracking + Pro exports")
st.sidebar.divider()


# ------------------
# Sidebar: Account (email) + Pro persistence
# ------------------
st.sidebar.header("Account")

if "user_email" not in st.session_state:
    st.session_state.user_email = ""

email_input = st.sidebar.text_input(
    "Email (to save Pro access)",
    value=st.session_state.user_email,
    placeholder="you@example.com"
)
email_norm = normalize_email(email_input)

if st.sidebar.button("Save Email"):
    st.session_state.user_email = email_norm
    if email_norm:
        upsert_user(email_norm)
        st.sidebar.success("Saved ✅")
    else:
        st.sidebar.info("Cleared.")


# ------------------
# Handle Stripe redirect (success_url includes session_id)
# ------------------
if st.query_params.get("success") and st.query_params.get("session_id") and stripe.api_key:
    session_id = st.query_params.get("session_id")
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        customer_id = session.get("customer")

        customer_email = None
        if session.get("customer_details") and session["customer_details"].get("email"):
            customer_email = session["customer_details"]["email"]
        if not customer_email and session.get("customer_email"):
            customer_email = session.get("customer_email")

        customer_email = normalize_email(customer_email)

        if customer_email:
            upsert_user(customer_email, customer_id=customer_id, plan="pro")
            st.session_state.user_email = customer_email
            st.sidebar.success("Payment successful! Pro unlocked 🎉 (saved to your email)")
        else:
            st.sidebar.warning("Payment succeeded, but I couldn’t read an email from the checkout session.")
    except Exception:
        st.sidebar.warning("Payment returned, but I couldn’t verify the session. Try refreshing or saving your email.")

if st.query_params.get("canceled"):
    st.sidebar.info("Checkout canceled.")


# ------------------
# Plan: determine current plan (persisted) + ADMIN BYPASS
# ------------------
current_plan = "free"
admin_mode = (email_norm in ADMIN_EMAILS) if email_norm else False

if admin_mode:
    current_plan = "pro"
else:
    if email_norm:
        u = get_user(email_norm) or {}
        if u.get("customer_id"):
            current_plan = verify_subscription_for_user(email_norm)
        else:
            current_plan = u.get("plan", "free")

is_pro = (current_plan == "pro")

if admin_mode:
    st.sidebar.success("ADMIN MODE ✅ (Pro forced)")
st.sidebar.write(f"Current plan: **{current_plan.upper()}**")


# ------------------
# Sidebar: Billing
# ------------------
st.sidebar.header("Billing")

if not stripe.api_key or not PRICE_ID:
    st.sidebar.caption("Stripe not configured (missing STRIPE_SECRET_KEY or STRIPE_PRICE_ID).")
else:
    if not is_pro:
        if st.sidebar.button("Upgrade to Pro ($9.99/month)"):
            checkout_kwargs = {
                "mode": "subscription",
                "line_items": [{"price": PRICE_ID, "quantity": 1}],
                "success_url": f"{APP_URL}/?success=true&session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url": f"{APP_URL}/?canceled=true",
            }
            if email_norm:
                checkout_kwargs["customer_email"] = email_norm

            session = stripe.checkout.Session.create(**checkout_kwargs)
            st.sidebar.link_button("Open Stripe Checkout", session.url)
    else:
        u = get_user(email_norm) if email_norm else None
        if u and u.get("customer_id"):
            if st.sidebar.button("Manage subscription (Portal)"):
                try:
                    portal = stripe.billing_portal.Session.create(
                        customer=u["customer_id"],
                        return_url=APP_URL
                    )
                    st.sidebar.link_button("Open Customer Portal", portal.url)
                except Exception:
                    st.sidebar.warning("Could not open portal. Check your Stripe settings.")


# ------------------
# Tabs
# ------------------
tab_home, tab_live, tab_box, tab_export = st.tabs(["🏠 Home", "📊 Live", "📋 Box Score", "⬇️ Export"])

with tab_home:
    st.markdown("### Track basketball stats in real time — then export clean reports")
    st.markdown(
        """
**HoopStats** is a lightweight stat tracker you can run on any laptop during games.

**Free**
- Live stat tracking
- Saved games + roster
- Box score view

**Pro**
- Export CSV
- Export PDF
- Pro access persists by email
"""
    )
    st.markdown("#### Tip")
    st.write("Enter your email in the sidebar so Pro access persists after checkout.")


# ------------------
# Sidebar: Games
# ------------------
st.sidebar.divider()
st.sidebar.header("Games")

games = list_games()
game_labels = ["➕ Create new game..."] + [f"{gid}: {name}" for gid, name, _ in games]
choice = st.sidebar.selectbox("Select game", game_labels, index=0)

if "active_game_id" not in st.session_state:
    st.session_state.active_game_id = None

if choice == "➕ Create new game...":
    new_name = st.sidebar.text_input("New game name", value="Team vs Opponent (Date)")
    if st.sidebar.button("Create Game"):
        gid = create_game(new_name.strip() or "New Game")
        st.session_state.active_game_id = gid
        st.toast("Game created ✅")
        st.rerun()
else:
    gid = int(choice.split(":")[0])
    st.session_state.active_game_id = gid

if st.session_state.active_game_id is None:
    st.info("Create or select a game in the left sidebar to start.")
    st.stop()

active_game_id = st.session_state.active_game_id

if st.sidebar.button("🗑️ Delete selected game"):
    delete_game(active_game_id)
    st.session_state.active_game_id = None
    st.toast("Game deleted 🗑️")
    st.rerun()


# ------------------
# Load game data
# ------------------
if "history" not in st.session_state:
    st.session_state.history = []  # {"player_id": int, "change": {...}}

roster, name_to_pid, player_stats = load_game(active_game_id, STAT_KEYS)

if not roster:
    set_roster(active_game_id, DEFAULT_ROSTER, STAT_KEYS)
    roster, name_to_pid, player_stats = load_game(active_game_id, STAT_KEYS)


# ------------------
# Sidebar: Roster editing
# ------------------
st.sidebar.header("Roster")
roster_text = st.sidebar.text_area("One player per line", value="\n".join(roster), height=180)

if st.sidebar.button("Update Roster"):
    new_roster = [line.strip() for line in roster_text.splitlines() if line.strip()]
    if not new_roster:
        new_roster = DEFAULT_ROSTER.copy()
    set_roster(active_game_id, new_roster, STAT_KEYS)
    st.session_state.history = []
    st.toast("Roster updated ✅")
    st.rerun()

# Reload after roster change
roster, name_to_pid, player_stats = load_game(active_game_id, STAT_KEYS)


# ------------------
# Helpers (undo/reset/change)
# ------------------
def record(player_id, change):
    st.session_state.history.append({"player_id": player_id, "change": change})

def undo():
    if not st.session_state.history:
        st.toast("Nothing to undo.", icon="ℹ️")
        return False
    last = st.session_state.history.pop()
    apply_change(active_game_id, last["player_id"], last["change"], direction=-1)
    return True

def reset_game():
    roster2, name_to_pid2, player_stats2 = load_game(active_game_id, STAT_KEYS)
    for name in roster2:
        pid = name_to_pid2[name]
        current = player_stats2[name]
        for k, v in current.items():
            if v != 0:
                apply_change(active_game_id, pid, {k: v}, direction=-1)
    st.session_state.history = []


# ------------------
# LIVE TAB: ENTRY FIRST, TOTALS BELOW + OREB/DREB shown
# ------------------
with tab_live:
    if "selected_player" not in st.session_state or st.session_state.selected_player not in roster:
        st.session_state.selected_player = roster[0]

    default_index = roster.index(st.session_state.selected_player) if st.session_state.selected_player in roster else 0
    selected_name = st.selectbox("Selected Player", roster, index=default_index)
    st.session_state.selected_player = selected_name
    selected_pid = name_to_pid[selected_name]

    def do_change(change, toast_text):
        apply_change(active_game_id, selected_pid, change)
        record(selected_pid, change)
        st.toast(toast_text, icon="✅")
        st.rerun()

    st.subheader("Enter Stats")
    e1, e2, e3 = st.columns(3)

    with e1:
        st.markdown("### Scoring")
        if st.button("2PT Made"):
            do_change({"2PM": 1, "2PA": 1}, "2PT made")
        if st.button("2PT Miss"):
            do_change({"2PA": 1}, "2PT missed")
        if st.button("3PT Made"):
            do_change({"3PM": 1, "3PA": 1}, "3PT made")
        if st.button("3PT Miss"):
            do_change({"3PA": 1}, "3PT missed")
        if st.button("FT Made"):
            do_change({"FTM": 1, "FTA": 1}, "FT made")
        if st.button("FT Miss"):
            do_change({"FTA": 1}, "FT missed")

    with e2:
        st.markdown("### Hustle")
        if st.button("Offensive Rebound"):
            do_change({"OREB": 1}, "Offensive rebound")
        if st.button("Defensive Rebound"):
            do_change({"DREB": 1}, "Defensive rebound")
        if st.button("Assist"):
            do_change({"AST": 1}, "Assist")

    with e3:
        st.markdown("### Mistakes")
        if st.button("Turnover"):
            do_change({"TOV": 1}, "Turnover")

        st.divider()
        if st.button("⬅️ Undo"):
            if undo():
                st.toast("Undone ↩️")
                st.rerun()
        if st.button("🔄 Reset Game"):
            reset_game()
            st.toast("Game reset 🔄")
            st.rerun()

    st.divider()

    # Refresh from DB after any changes
    roster, name_to_pid, player_stats = load_game(active_game_id, STAT_KEYS)
    ps = player_stats[selected_name]

    # Player totals
    p_pts = ps["2PM"] * 2 + ps["3PM"] * 3 + ps["FTM"]
    p_fgm = ps["2PM"] + ps["3PM"]
    p_fga = ps["2PA"] + ps["3PA"]
    p_reb = ps["OREB"] + ps["DREB"]

    st.subheader(f"{selected_name} – Totals")
    m1, m2, m3, m4, m5, m6, m7, m8 = st.columns(8)
    m1.metric("PTS", p_pts)
    m2.metric("FG", f"{p_fgm}/{p_fga} ({pct(p_fgm, p_fga)}%)")
    m3.metric("3PT", f"{ps['3PM']}/{ps['3PA']} ({pct(ps['3PM'], ps['3PA'])}%)")
    m4.metric("FT", f"{ps['FTM']}/{ps['FTA']} ({pct(ps['FTM'], ps['FTA'])}%)")
    m5.metric("OREB", ps["OREB"])
    m6.metric("DREB", ps["DREB"])
    m7.metric("REB", p_reb)
    m8.metric("AST", ps["AST"])

    # Team totals
    tot = empty_player_stats()
    for name in roster:
        for k in STAT_KEYS:
            tot[k] += player_stats[name][k]

    t_pts = tot["2PM"] * 2 + tot["3PM"] * 3 + tot["FTM"]
    t_fgm = tot["2PM"] + tot["3PM"]
    t_fga = tot["2PA"] + tot["3PA"]
    t_reb = tot["OREB"] + tot["DREB"]

    st.divider()
    st.subheader("Team Totals")
    tm1, tm2, tm3, tm4, tm5, tm6, tm7, tm8 = st.columns(8)
    tm1.metric("PTS", t_pts)
    tm2.metric("FG", f"{t_fgm}/{t_fga} ({pct(t_fgm, t_fga)}%)")
    tm3.metric("3PT", f"{tot['3PM']}/{tot['3PA']} ({pct(tot['3PM'], tot['3PA'])}%)")
    tm4.metric("FT", f"{tot['FTM']}/{tot['FTA']} ({pct(tot['FTM'], tot['FTA'])}%)")
    tm5.metric("OREB", tot["OREB"])
    tm6.metric("DREB", tot["DREB"])
    tm7.metric("REB", t_reb)
    tm8.metric("AST", tot["AST"])


# ------------------
# BOX SCORE TAB
# ------------------
with tab_box:
    roster, name_to_pid, player_stats = load_game(active_game_id, STAT_KEYS)

    rows = []
    for name in roster:
        s = player_stats[name]
        pts = s["2PM"] * 2 + s["3PM"] * 3 + s["FTM"]
        fgm = s["2PM"] + s["3PM"]
        fga = s["2PA"] + s["3PA"]
        rows.append({
            "Player": name,
            "PTS": pts,
            "FG": f"{fgm}/{fga}",
            "3PT": f"{s['3PM']}/{s['3PA']}",
            "FT": f"{s['FTM']}/{s['FTA']}",
            "OREB": s["OREB"],
            "DREB": s["DREB"],
            "REB": s["OREB"] + s["DREB"],
            "AST": s["AST"],
            "TOV": s["TOV"],
        })

    st.subheader("Box Score (All Players)")
    st.dataframe(rows, use_container_width=True)
    st.caption("HoopStats MVP – Saved Games (SQLite)")


# ------------------
# EXPORT TAB (PRO)
# ------------------
with tab_export:
    roster, name_to_pid, player_stats = load_game(active_game_id, STAT_KEYS)

    st.subheader("Export")

    def build_boxscore_rows(roster, player_stats):
        out = []
        for name in roster:
            s = player_stats[name]
            pts = s["2PM"] * 2 + s["3PM"] * 3 + s["FTM"]
            fgm = s["2PM"] + s["3PM"]
            fga = s["2PA"] + s["3PA"]
            out.append({
                "Player": name,
                "PTS": pts,
                "FGM": fgm,
                "FGA": fga,
                "3PM": s["3PM"],
                "3PA": s["3PA"],
                "FTM": s["FTM"],
                "FTA": s["FTA"],
                "OREB": s["OREB"],
                "DREB": s["DREB"],
                "REB": s["OREB"] + s["DREB"],
                "AST": s["AST"],
                "TOV": s["TOV"],
            })
        return out

    export_rows = build_boxscore_rows(roster, player_stats)

    # CSV
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=export_rows[0].keys())
    writer.writeheader()
    writer.writerows(export_rows)

    st.download_button(
        label=("⬇️ Download Box Score (CSV)" if is_pro else "🔒 Download Box Score (CSV) — Pro"),
        data=csv_buffer.getvalue(),
        file_name="box_score.csv",
        mime="text/csv",
        disabled=not is_pro,
    )

    st.divider()
    st.subheader("Export PDF")

    def build_pdf(roster, player_stats):
        styles = getSampleStyleSheet()
        elements = []
        elements.append(Paragraph("<b>Box Score</b>", styles["Title"]))

        data = [["Player", "PTS", "FG", "3PT", "FT", "OREB", "DREB", "REB", "AST", "TOV"]]
        for name in roster:
            s = player_stats[name]
            pts = s["2PM"] * 2 + s["3PM"] * 3 + s["FTM"]
            fgm = s["2PM"] + s["3PM"]
            fga = s["2PA"] + s["3PA"]
            data.append([
                name,
                pts,
                f"{fgm}/{fga}",
                f"{s['3PM']}/{s['3PA']}",
                f"{s['FTM']}/{s['FTA']}",
                s["OREB"],
                s["DREB"],
                s["OREB"] + s["DREB"],
                s["AST"],
                s["TOV"],
            ])

        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ]))

        elements.append(table)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        doc = SimpleDocTemplate(tmp.name, pagesize=LETTER)
        doc.build(elements)
        return tmp.name

    if st.button(("⬇️ Generate PDF" if is_pro else "🔒 Generate PDF — Pro"), disabled=not is_pro):
        pdf_path = build_pdf(roster, player_stats)
        with open(pdf_path, "rb") as f:
            st.download_button(
                label="Click to download PDF",
                data=f,
                file_name="box_score.pdf",
                mime="application/pdf"
            )
        os.remove(pdf_path)
        st.toast("PDF generated ✅", icon="✅")
