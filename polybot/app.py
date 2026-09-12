"""PolyBot dashboard — Streamlit UI for browsing positions, P&L, and current signals."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from polybot.portfolio import STARTING_BANKROLL, get_bankroll, summary
from polybot.signals import find_signals

DB_PATH = Path("polybot.db")

# ---------- theme ----------
ACCENT = "#5b8bd6"
BRIGHT = "#a8c5f5"
GREEN = "#4ade80"
RED = "#f87171"
DIM = "#7089b0"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_positions() -> pd.DataFrame:
    with _connect() as c:
        rows = c.execute("SELECT * FROM positions ORDER BY id DESC").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def load_bankroll_history() -> pd.DataFrame:
    with _connect() as c:
        rows = c.execute("SELECT * FROM bankroll_events ORDER BY id ASC").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def load_latest_prices() -> dict:
    """Return {market_id: yes_price} for latest snapshot of every market."""
    prices = {}
    with _connect() as c:
        rows = c.execute("""
            SELECT market_id, yes_price
            FROM snapshots s
            WHERE s.id = (SELECT MAX(id) FROM snapshots WHERE market_id = s.market_id)
        """).fetchall()
    for r in rows:
        prices[r["market_id"]] = r["yes_price"]
    return prices


st.set_page_config(page_title="PolyBot", page_icon="◆", layout="wide")

st.markdown("""
<style>
  :root {
    --bg: #01040f;
    --panel: #071539;
    --panel-soft: rgba(8, 18, 48, 0.55);
    --edge: rgba(80, 130, 220, 0.18);
    --edge-strong: rgba(80, 130, 220, 0.45);
    --accent: #5b8bd6;
    --accent-bright: #a8c5f5;
    --ink: #d8e2f5;
    --ink-dim: #7089b0;
  }
  .stApp { background: var(--bg); }
  header[data-testid="stHeader"] { background: transparent; }
  section[data-testid="stSidebar"] { background: var(--panel); border-right: 1px solid var(--edge); }

  /* Metric cards */
  [data-testid="stMetric"] {
    background: var(--panel-soft);
    border: 1px solid var(--edge);
    border-left: 3px solid var(--accent);
    border-radius: 6px;
    padding: 14px 18px;
  }
  [data-testid="stMetricLabel"] { color: var(--ink-dim) !important; letter-spacing: 2px; text-transform: uppercase; font-size: 11px !important; }
  [data-testid="stMetricValue"] { color: var(--accent-bright) !important; font-family: monospace; }

  /* Tabs */
  .stTabs [data-baseweb="tab-list"] {
    background: transparent;
    border-bottom: 1px solid var(--edge);
    gap: 8px;
  }
  .stTabs [data-baseweb="tab"] {
    background: transparent;
    color: var(--ink-dim);
    border: 1px solid transparent;
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    padding: 10px 18px;
    letter-spacing: 2px;
    text-transform: uppercase;
    font-family: monospace;
    font-size: 12px;
  }
  .stTabs [data-baseweb="tab"][aria-selected="true"] {
    color: var(--accent-bright);
    border-color: var(--edge);
    background: var(--panel-soft);
    border-bottom: 1px solid var(--bg);
  }

  /* Dataframes */
  [data-testid="stDataFrame"] {
    border: 1px solid var(--edge);
    border-radius: 6px;
    background: var(--panel-soft);
  }
  [data-testid="stDataFrame"] div[role="grid"] {
    background: transparent !important;
  }

  /* Divider */
  [data-testid="stHorizontalBlock"] hr, hr {
    border: none;
    height: 1px;
    background: linear-gradient(to right, var(--edge-strong), transparent);
    margin: 24px 0;
  }

  /* Alerts */
  [data-testid="stAlert"] {
    background: var(--panel-soft);
    border: 1px solid var(--edge);
    border-left: 3px solid var(--accent);
    color: var(--ink);
    border-radius: 6px;
  }

  /* Buttons */
  .stButton > button {
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 4px;
    letter-spacing: 2px;
    text-transform: uppercase;
    font-family: monospace;
    font-size: 12px;
  }
  .stButton > button:hover {
    background: var(--accent);
    color: var(--bg);
    box-shadow: 0 0 14px rgba(91, 139, 214, 0.55);
  }

  /* Sub-nav caption */
  .stCaption, [data-testid="stCaptionContainer"] {
    color: var(--ink-dim) !important;
    font-family: monospace;
    letter-spacing: 1px;
  }

  /* Plotly charts inherit dark bg */
  .js-plotly-plot .plotly, .modebar { background: transparent !important; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div style='padding:8px 0 24px 0;border-bottom:1px solid #262626;margin-bottom:24px'>"
    f"<span style='color:{ACCENT};font-size:28px;font-weight:700;letter-spacing:2px'>◆ POLYBOT</span>"
    f"<span style='color:{DIM};margin-left:16px;font-size:13px'>"
    "PREDICTION MARKET PAPER-TRADER  ·  v0.1</span>"
    "</div>",
    unsafe_allow_html=True,
)

s = summary()
positions = load_positions()
bankroll_hist = load_bankroll_history()
latest_prices = load_latest_prices()

# Unrealized P&L on open positions using current YES/NO price
open_pos = positions[positions["status"] == "OPEN"].copy() if not positions.empty else positions
if not open_pos.empty:
    def _mark(row):
        p = latest_prices.get(row["market_id"])
        if p is None: return None
        return p if row["side"] == "YES" else (1 - p)
    open_pos["mark_price"] = open_pos.apply(_mark, axis=1)
    open_pos["market_value"] = open_pos["shares"] * open_pos["mark_price"]
    open_pos["unrealized_pnl"] = open_pos["market_value"] - open_pos["stake"]
    unrealized_total = float(open_pos["unrealized_pnl"].fillna(0).sum())
    market_value_total = float(open_pos["market_value"].fillna(0).sum())
else:
    unrealized_total = 0.0
    market_value_total = 0.0

equity = s["bankroll"] + market_value_total
total_return_pct = (equity / STARTING_BANKROLL - 1) * 100

# ---------- top metrics row ----------
col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    st.markdown(f"<div style='color:{DIM};font-size:11px;letter-spacing:2px'>EQUITY</div>"
                f"<div style='color:{BRIGHT};font-size:28px;font-weight:600'>${equity:,.2f}</div>",
                unsafe_allow_html=True)
with col2:
    ret_color = GREEN if total_return_pct >= 0 else RED
    st.markdown(f"<div style='color:{DIM};font-size:11px;letter-spacing:2px'>TOTAL RETURN</div>"
                f"<div style='color:{ret_color};font-size:28px;font-weight:600'>{total_return_pct:+.2f}%</div>",
                unsafe_allow_html=True)
with col3:
    st.markdown(f"<div style='color:{DIM};font-size:11px;letter-spacing:2px'>CASH</div>"
                f"<div style='color:{BRIGHT};font-size:28px;font-weight:600'>${s['bankroll']:,.2f}</div>",
                unsafe_allow_html=True)
with col4:
    ur_color = GREEN if unrealized_total >= 0 else RED
    st.markdown(f"<div style='color:{DIM};font-size:11px;letter-spacing:2px'>UNREALIZED P&L</div>"
                f"<div style='color:{ur_color};font-size:28px;font-weight:600'>${unrealized_total:+,.2f}</div>",
                unsafe_allow_html=True)
with col5:
    st.markdown(f"<div style='color:{DIM};font-size:11px;letter-spacing:2px'>OPEN POSITIONS</div>"
                f"<div style='color:{BRIGHT};font-size:28px;font-weight:600'>{s['open_positions']}</div>",
                unsafe_allow_html=True)

st.divider()

tab_port, tab_hist, tab_sig, tab_equity = st.tabs(["PORTFOLIO", "HISTORY", "SIGNALS", "EQUITY CURVE"])

# ---------- PORTFOLIO tab ----------
with tab_port:
    st.markdown(f"<div style='color:{ACCENT};letter-spacing:2px;margin-bottom:8px'>◆ OPEN POSITIONS</div>",
                unsafe_allow_html=True)
    if open_pos.empty:
        st.info("No open positions yet.")
    else:
        display = open_pos[[
            "id", "question", "side", "entry_price", "mark_price",
            "shares", "stake", "market_value", "unrealized_pnl", "opened_at", "opened_reason"
        ]].copy()
        display.columns = ["#", "Market", "Side", "Entry", "Mark", "Shares", "Stake", "Value", "Unrealized P&L", "Opened", "Reason"]
        display["Entry"] = display["Entry"].round(3)
        display["Mark"] = display["Mark"].round(3)
        display["Stake"] = display["Stake"].round(2)
        display["Value"] = display["Value"].round(2)
        display["Unrealized P&L"] = display["Unrealized P&L"].round(2)
        st.dataframe(display, use_container_width=True, hide_index=True)

# ---------- HISTORY tab ----------
with tab_hist:
    st.markdown(f"<div style='color:{ACCENT};letter-spacing:2px;margin-bottom:8px'>◆ CLOSED TRADES</div>",
                unsafe_allow_html=True)
    closed = positions[positions["status"] != "OPEN"] if not positions.empty else positions
    if closed.empty:
        st.info("No closed trades yet.")
    else:
        display = closed[["id","question","side","entry_price","close_price","stake","pnl","status","close_reason","closed_at"]].copy()
        display.columns = ["#", "Market", "Side", "Entry", "Close", "Stake", "P&L", "Status", "Reason", "Closed"]
        for col in ("Entry", "Close", "Stake", "P&L"):
            display[col] = display[col].astype(float).round(3)
        st.dataframe(display, use_container_width=True, hide_index=True)

# ---------- SIGNALS tab ----------
with tab_sig:
    st.markdown(f"<div style='color:{ACCENT};letter-spacing:2px;margin-bottom:8px'>"
                f"◆ CURRENT SIGNALS (what the bot sees right now)</div>",
                unsafe_allow_html=True)
    open_ids = set(open_pos["market_id"]) if not open_pos.empty else set()
    sigs = find_signals(exclude_market_ids=open_ids)
    if not sigs:
        st.info("No signals matching current filters. Bot will not open new positions this cycle.")
    else:
        st.write(f"**{len(sigs)}** actionable signals right now (excluding markets already in your book):")
        sig_df = pd.DataFrame(sigs)
        sig_df = sig_df[["strategy","question","side","yes_price","no_price","volume","end_date","reason"]]
        sig_df["yes_price"] = sig_df["yes_price"].round(3)
        sig_df["no_price"] = sig_df["no_price"].round(3)
        sig_df["volume"] = sig_df["volume"].astype(float).round(0)
        st.dataframe(sig_df, use_container_width=True, hide_index=True)

# ---------- EQUITY CURVE tab ----------
with tab_equity:
    st.markdown(f"<div style='color:{ACCENT};letter-spacing:2px;margin-bottom:8px'>◆ BANKROLL OVER TIME</div>",
                unsafe_allow_html=True)
    if bankroll_hist.empty:
        st.info("No bankroll history yet.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(bankroll_hist["at"]),
            y=bankroll_hist["balance_after"],
            mode="lines+markers",
            line=dict(color=ACCENT, width=2),
            marker=dict(size=6),
            name="Bankroll",
        ))
        fig.add_hline(y=STARTING_BANKROLL, line_dash="dash", line_color=DIM,
                      annotation_text="Starting bankroll", annotation_font_color=DIM)
        fig.update_layout(
            height=420, template="plotly_dark",
            paper_bgcolor="#0a0a0a", plot_bgcolor="#0a0a0a",
            font=dict(color="#e8e8e8"),
            xaxis_title="", yaxis_title="Bankroll ($)",
            margin=dict(l=20, r=20, t=10, b=20),
        )
        st.plotly_chart(fig, use_container_width=True)

st.caption(f"PolyBot v0.1 · autonomous paper-trader running hourly · starting bankroll ${STARTING_BANKROLL:,.0f}")
