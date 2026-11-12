"""The two charts in the calibration report, drawn with matplotlib and saved as PNG.

Both follow the same few rules so they read as one set: a single series colour,
thin marks, hairline gridlines that stay in the background, text in ink colours
rather than the data colour, and a table of the same numbers in the report so
nobody has to read a value off a picture.
"""

from __future__ import annotations

import logging
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch
from matplotlib.path import Path as ShapePath
from matplotlib.ticker import PercentFormatter
from matplotlib.transforms import IdentityTransform

# Colours (a validated palette: the series blue passes contrast and colour-blindness checks on this surface).
SURFACE = "#fcfcfb"
SERIES = "#2a78d6"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

DPI = 150
THIN_BUCKET = 5  # a bucket with fewer markets than this is drawn in gray: mostly noise
FONTS = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # a missing font just falls back


def _figure(width: float, height: float) -> tuple[Figure, object]:
    """A blank chart on the surface colour with quiet axes."""
    fig = Figure(figsize=(width, height), dpi=DPI, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.set_axisbelow(True)
    for label in (*ax.get_xticklabels(), *ax.get_yticklabels()):
        label.set_fontfamily(FONTS)
    return fig, ax


def _titles(fig: Figure, title: str, subtitle: str) -> None:
    height = fig.get_figheight()  # in inches; the offsets below are inches from the top edge
    fig.text(
        0.02, 1 - 0.17 / height, title, fontsize=12.5, fontweight="semibold", color=INK, fontfamily=FONTS, va="top"
    )
    fig.text(0.02, 1 - 0.48 / height, subtitle, fontsize=9.5, color=INK_SECONDARY, fontfamily=FONTS, va="top")


def _horizon_name(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def calibration_curve(horizon: dict, path: Path) -> None:
    """Average price against the share that resolved YES, one point per price bucket.

    Points on the diagonal mean the market was right about how likely things
    were. Above it, events happened more often than the price said.
    """
    fig, ax = _figure(6.4, 5.2)
    fig.subplots_adjust(left=0.12, right=0.96, top=0.84, bottom=0.11)
    market = horizon["market"]
    _titles(
        fig,
        f"Market prices {_horizon_name(horizon['horizon_days'])} before the outcome",
        f"{market['n']} resolved markets. On the line means the price was right on average.",
    )

    ax.plot([0, 1], [0, 1], color=AXIS, linewidth=1, solid_capstyle="round", zorder=1)
    ax.text(
        0.80,
        0.765,
        "perfectly calibrated",
        color=MUTED,
        fontsize=8.5,
        fontfamily=FONTS,
        rotation=41,
        rotation_mode="anchor",
        ha="center",
        va="top",
    )

    solid = [b for b in horizon["buckets"] if b["n"] >= THIN_BUCKET]
    for left, right in zip(solid, solid[1:], strict=False):
        if abs(right["low"] - left["high"]) < 1e-9:  # neighbouring buckets, nothing thin in between
            ax.plot(
                [left["mean_probability"], right["mean_probability"]],
                [left["yes_rate"], right["yes_rate"]],
                color=SERIES,
                linewidth=2,
                solid_capstyle="round",
                zorder=2,
            )
    thin = [b for b in horizon["buckets"] if b["n"] < THIN_BUCKET]
    marker = {"marker": "o", "markersize": 8.5, "markeredgecolor": SURFACE, "markeredgewidth": 2, "linestyle": "none"}
    if solid:
        xs, ys = [b["mean_probability"] for b in solid], [b["yes_rate"] for b in solid]
        ax.plot(xs, ys, color=SERIES, zorder=3, label=f"bucket with {THIN_BUCKET} or more markets", **marker)
    if thin:
        ax.plot(
            [b["mean_probability"] for b in thin],
            [b["yes_rate"] for b in thin],
            color=MUTED,
            zorder=3,
            label=f"fewer than {THIN_BUCKET} markets (mostly noise)",
            **marker,
        )

    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.03, 1.03)
    ticks = [0, 0.2, 0.4, 0.6, 0.8, 1.0]
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_xlabel("Average price in the bucket", color=INK_SECONDARY, fontsize=9.5, fontfamily=FONTS, labelpad=6)
    ax.set_ylabel("Share that resolved YES", color=INK_SECONDARY, fontsize=9.5, fontfamily=FONTS, labelpad=6)
    legend = ax.legend(loc="upper left", frameon=False, fontsize=9, handletextpad=0.3, borderaxespad=0.6)
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)
        text.set_fontfamily(FONTS)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)


def _bar_with_rounded_end(fig: Figure, ax, y: float, value: float, thickness: float, radius: float) -> None:
    """One horizontal bar: square where it meets the axis, rounded at the end that shows the value.

    Drawn in pixel coordinates so the corner stays round whatever the axis scales are.
    """
    (x0, y0), (x1, y1) = ax.transData.transform([(0, y - thickness / 2), (value, y + thickness / 2)])
    r = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
    move, line, curve = ShapePath.MOVETO, ShapePath.LINETO, ShapePath.CURVE3
    points = [(x0, y0), (x1 - r, y0), (x1, y0), (x1, y0 + r), (x1, y1 - r), (x1, y1), (x1 - r, y1), (x0, y1), (x0, y0)]
    codes = [move, line, curve, curve, line, curve, curve, line, ShapePath.CLOSEPOLY]
    fig.add_artist(
        PathPatch(ShapePath(points, codes), transform=IdentityTransform(), facecolor=SERIES, edgecolor="none", zorder=3)
    )


def brier_by_category(horizon: dict, path: Path) -> None:
    """The market's Brier score for all markets and for each category. Shorter bars are better."""
    market = horizon["market"]
    rows = [{"category": "all markets", **market}, *horizon["categories"]]
    fig, ax = _figure(6.4, 1.5 + 0.42 * len(rows))
    top = 1 - 1.02 / fig.get_figheight()
    fig.subplots_adjust(left=0.36, right=0.93, top=top, bottom=0.62 / fig.get_figheight())
    _titles(
        fig,
        f"Brier score by category, price {_horizon_name(horizon['horizon_days'])} before",
        "Lower is better. 0 is perfect; always saying 50% scores 0.25.",
    )

    limit = max(0.30, max(r["brier"] for r in rows) * 1.25)
    positions = list(range(len(rows) - 1, -1, -1))  # first row at the top
    ax.set_xlim(0, limit)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_yticks(positions)
    ax.set_yticklabels(
        [f"{r['category']}  ({r['n']} markets)" for r in rows], color=INK_SECONDARY, fontsize=9.5, fontfamily=FONTS
    )
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.spines["bottom"].set_visible(False)
    ax.axvline(0.25, color=AXIS, linewidth=1, zorder=1)
    ax.text(0.25, len(rows) - 0.45, "always 50%", color=MUTED, fontsize=8.5, fontfamily=FONTS, ha="center", va="bottom")
    ax.set_xlabel("Brier score", color=INK_SECONDARY, fontsize=9.5, fontfamily=FONTS, labelpad=6)

    fig.canvas.draw()  # fix the layout first, so the bars can be placed in pixels
    for position, row in zip(positions, rows, strict=True):
        _bar_with_rounded_end(fig, ax, position, row["brier"], thickness=0.46, radius=6)
        ax.text(
            row["brier"] + limit * 0.012,
            position,
            f"{row['brier']:.3f}",
            color=INK,
            fontsize=9.5,
            fontfamily=FONTS,
            va="center",
            ha="left",
        )
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
