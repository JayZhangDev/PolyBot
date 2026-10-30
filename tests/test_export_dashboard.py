"""The exporter's paper_pnl.txt: the v2 agent's P&L as one number, read by a local desktop wallpaper."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "export_dashboard", Path(__file__).resolve().parent.parent / "scripts" / "export_dashboard.py")
export_dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_dashboard)


def agent(equity: float) -> dict:
    return {"enabled": True, "summary": {"equity": equity, "starting_bankroll": 1000.0}}


def test_writes_equity_minus_starting_bankroll(tmp_path):
    path = tmp_path / "paper_pnl.txt"
    assert export_dashboard.write_paper_pnl(agent(987.654), path) == -12.35
    assert path.read_text(encoding="utf-8") == "-12.35\n"
    assert not (tmp_path / "paper_pnl.txt.tmp").exists()


def test_overwrites_and_never_writes_negative_zero(tmp_path):
    path = tmp_path / "paper_pnl.txt"
    export_dashboard.write_paper_pnl(agent(1003.5), path)
    export_dashboard.write_paper_pnl(agent(999.9999999), path)
    assert path.read_text(encoding="utf-8") == "0.00\n"


def test_nothing_written_before_the_agent_has_run(tmp_path):
    path = tmp_path / "paper_pnl.txt"
    assert export_dashboard.write_paper_pnl({"enabled": False}, path) is None
    assert not path.exists()
