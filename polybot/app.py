"""PolyBot dashboard — Streamlit UI for browsing positions, P&L, and current signals."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from polybot import __version__
from polybot.calibration import HORIZONS_DAYS, LONGSHOT_MAX
from polybot.calibration import report as calibration_report
from polybot.db import connect
from polybot.portfolio import STARTING_BANKROLL, summary
from polybot.resolutions import counts as resolution_counts
from polybot.signals import find_signals, mispricing_candidates
from polybot.storage import init_db

# ---------- theme ----------
ACCENT = "#5b8bd6"
BRIGHT = "#a8c5f5"
GREEN = "#4ade80"
RED = "#f87171"
DIM = "#7089b0"


def load_positions() -> pd.DataFrame:
    with connect() as c:
        rows = c.execute("SELECT * FROM positions ORDER BY id DESC").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def load_bankroll_history() -> pd.DataFrame:
    with connect() as c:
        rows = c.execute("SELECT * FROM bankroll_events ORDER BY id ASC").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def load_latest_prices() -> dict:
    """Return {market_id: yes_price} for latest snapshot of every market."""
    prices = {}
    with connect() as c:
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
    f"PREDICTION MARKET PAPER-TRADER  ·  v{__version__}</span>"
    "</div>",
    unsafe_allow_html=True,
)

init_db()
s = summary()
positions = load_positions()
bankroll_hist = load_bankroll_history()
latest_prices = load_latest_prices()

# Unrealized P&L on open positions using current YES/NO price
open_pos = positions[positions["status"] == "OPEN"].copy() if not positions.empty else positions
if not open_pos.empty:
    def _mark(row):
        p = latest_prices.get(row["market_id"])
        if p is None:
            return None
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

tab_port, tab_hist, tab_sig, tab_equity, tab_cal = st.tabs(
    ["PORTFOLIO", "HISTORY", "SIGNALS", "EQUITY CURVE", "CALIBRATION"]
)

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
        st.dataframe(display, width="stretch", hide_index=True)

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
        st.dataframe(display, width="stretch", hide_index=True)

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
        sig_df = sig_df[["strategy", "question", "side", "entry_price", "yes_price", "volume", "end_date", "reason"]]
        for col in ("entry_price", "yes_price"):
            sig_df[col] = sig_df[col].round(3)
        sig_df["volume"] = sig_df["volume"].astype(float).round(0)
        st.dataframe(sig_df, width="stretch", hide_index=True)

    st.markdown(f"<div style='color:{ACCENT};letter-spacing:2px;margin:24px 0 8px 0'>"
                f"◆ MISPRICING WATCHLIST (neg-risk events, not traded)</div>",
                unsafe_allow_html=True)
    st.caption("Edges use executable bid/ask across every leg seen. Legs outside the "
               "top markets by volume are missing, so treat these as leads, not arbs.")
    cands = mispricing_candidates()
    if not cands:
        st.info("No neg-risk events with a basket edge right now.")
    else:
        st.dataframe(pd.DataFrame(cands), width="stretch", hide_index=True)

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
        st.plotly_chart(fig, width="stretch")

# ---------- CALIBRATION tab ----------
with tab_cal:
    st.markdown(f"<div style='color:{ACCENT};letter-spacing:2px;margin-bottom:8px'>"
                f"◆ HOW WELL DO MARKET PRICES PREDICT OUTCOMES?</div>",
                unsafe_allow_html=True)
    st.caption("YES price recorded N days before each market resolved, against what happened. "
               "Points on the dashed line are perfectly calibrated.")
    horizon = st.radio("Horizon", HORIZONS_DAYS, index=1, horizontal=True,
                       format_func=lambda d: f"{d} day" + ("s" if d > 1 else "") + " before")
    rep = calibration_report(horizon)
    rc = resolution_counts()
    if not rep["n"]:
        st.info(f"No resolved markets with a snapshot {horizon}+ days before resolution yet. "
                f"Resolutions recorded so far: {rc.get('YES', 0) + rc.get('NO', 0)} "
                f"({rc.get('PENDING', 0)} pending). On an existing database, run "
                "`python -m polybot.resolutions --backfill` once.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        skill = rep["brier_skill"]
        tiles = [
            (c1, "MARKETS SCORED", f"{rep['n']:,}", BRIGHT),
            (c2, "BRIER SCORE", f"{rep['brier']:.4f}", BRIGHT),
            (c3, "SKILL VS BASE RATE", f"{skill:+.3f}" if skill is not None else "n/a",
             (GREEN if (skill or 0) >= 0 else RED)),
            (c4, "YES BASE RATE", f"{rep['base_rate']:.1%}", BRIGHT),
        ]
        for col, label, value, color in tiles:
            with col:
                st.markdown(f"<div style='color:{DIM};font-size:11px;letter-spacing:2px'>{label}</div>"
                            f"<div style='color:{color};font-size:28px;font-weight:600'>{value}</div>",
                            unsafe_allow_html=True)
        st.caption("Brier: 0 is perfect, 0.25 is always guessing 50%. "
                   "Skill: 1 is perfect, 0 is no better than predicting the base rate.")

        bins = pd.DataFrame(rep["bins"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", hoverinfo="skip",
                                 line=dict(color=DIM, width=1, dash="dash"), showlegend=False))
        fig.add_trace(go.Scatter(
            x=bins["mean_price"], y=bins["observed_rate"], mode="lines+markers",
            line=dict(color=ACCENT, width=2),
            marker=dict(size=[max(9, min(26, 6 + n ** 0.5 * 2)) for n in bins["count"]],
                        color=ACCENT, line=dict(color="#01040f", width=2)),
            customdata=bins[["bin", "count"]],
            hovertemplate=("Price bucket %{customdata[0]}<br>Avg price %{x:.3f}<br>"
                           "YES happened %{y:.1%}<br>%{customdata[1]} markets<extra></extra>"),
            showlegend=False,
        ))
        fig.update_layout(
            height=460, template="plotly_dark", paper_bgcolor="#01040f", plot_bgcolor="#01040f",
            font=dict(color="#d8e2f5"), margin=dict(l=20, r=20, t=10, b=20),
            xaxis=dict(title="Market YES price", range=[0, 1], gridcolor="rgba(80,130,220,0.12)"),
            yaxis=dict(title="Share that resolved YES", range=[0, 1], tickformat=".0%",
                       gridcolor="rgba(80,130,220,0.12)"),
        )
        st.plotly_chart(fig, width="stretch")
        st.caption("Marker size grows with the number of markets in the bucket.")

        ls = rep["longshot"]
        if ls:
            verdict = ("overpriced: supports fading them" if ls["mean_price"] > ls["observed_rate"]
                       else "not overpriced: no support for fading them")
            st.markdown(
                f"<div style='color:{DIM};font-size:11px;letter-spacing:2px;margin-top:8px'>LONGSHOT CHECK</div>"
                f"<div style='color:#d8e2f5'>Contracts priced at or below {LONGSHOT_MAX:.2f} averaged "
                f"<b>{ls['mean_price']:.1%}</b> and resolved YES <b>{ls['observed_rate']:.1%}</b> of the time "
                f"({ls['n']} markets), so longshots look {verdict}.</div>",
                unsafe_allow_html=True)

        with st.expander("Bucket table"):
            table = bins.rename(columns={"bin": "Price bucket", "mean_price": "Avg price",
                                         "observed_rate": "YES rate", "count": "Markets"})
            st.dataframe(table.round(3), width="stretch", hide_index=True)

st.caption(f"PolyBot v{__version__} · autonomous paper-trader running hourly · starting bankroll ${STARTING_BANKROLL:,.0f}")
