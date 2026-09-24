"""Charts for the lease post, from data/lease_panel.csv (REVIEWED rows only).

  1_rent_over_time      rent per kW of IT load per month, by announcement date
  2_rent_by_credit      rent by credit type (from the filings)
  3_gross_to_it         gross power / critical IT load, per lease that states both

Output: output/charts/*.png and *.svg
Run:  python3 scripts/lease_charts.py
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402
from leases_analyze import metrics  # noqa: E402

OUT = ROOT / "output" / "charts"
ACCENT, GREY, DARK = "#1f5fa8", "#9aa3ad", "#222222"
SOURCE = "Source: SEC filings (8-K/6-K press releases and decks); lease panel by Aadhav Rajesh. Rent = contract value / term / MW (or filed average annual revenue / MW)."
TICKERS = [("Core Scientific", "CORZ"), ("Mawson", "MIGI"), ("Galaxy", "GLXY"), ("Applied Digital", "APLD"),
           ("TERAWULF", "WULF"), ("Cipher", "CIFR"), ("Hut 8", "HUT"), ("WhiteFiber", "WYFI"), ("Riot", "RIOT"),
           ("Digi Power", "DGXX"), ("CLEANSPARK", "CLSK"), ("CleanCore", "ZONE"), ("Bitdeer", "BTDR"),
           ("HEALTHY CHOICE", "Host Digital"), ("DUOS", "DUOT")]
CREDIT_ORDER = [("ig_tenant", "Investment-grade\ntenant"), ("parent_guarantee", "Parent\nguarantee"),
                ("third_party_backstop", "Third-party\nbackstop"), ("none_stated", "None\nstated"),
                ("anticipated", "Anticipated,\nnot final")]

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.edgecolor": "#888888", "axes.titleweight": "bold",
                     "axes.titlesize": 13, "axes.titlelocation": "left"})


def ticker(landlord: str) -> str:
    return next((t for k, t in TICKERS if k.lower() in landlord.lower()), landlord.split()[0])


def short_tenant(t: str) -> str:
    t = t.split(" (")[0].strip()
    if t.lower().startswith("undisclosed"):
        return "undisclosed IG tenant" if "invest" in t.lower() else "undisclosed"
    return {"Amazon Data Services": "AWS", "Amazon Web Services": "AWS"}.get(t, t)


def load():
    with open(ROOT / "data" / "lease_panel.csv", newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["status"] == "REVIEWED"]
    m = metrics(rows)
    for x in m:
        x["date"] = dt.date.fromisoformat(x["announce_date"])
        x["tick"] = ticker(x["landlord"])
        x["mw"] = x["it_mw"] or x["mw_unstated"] or x["gross_mw"]
    return m


def titles(fig, ax, title, subtitle):
    fig.subplots_adjust(top=0.84)
    fig.text(0.01, 0.97, title, fontsize=13, fontweight="bold", ha="left", va="top", color=DARK)
    fig.text(0.01, 0.915, subtitle, fontsize=9, ha="left", va="top", color="#555555")


def footer(fig, text=SOURCE):
    fig.text(0.01, 0.01, text, fontsize=7.5, color="#666666", ha="left", va="bottom", wrap=True)


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"{name}.{ext}", dpi=200, bbox_inches="tight")
    plt.close(fig)


def chart_rent_over_time(m):
    it = [x for x in m if x["mw_basis"] == "IT"]
    un = [x for x in m if x["mw_basis"] == "unstated"]
    fig, ax = plt.subplots(figsize=(10, 5.6))
    size = lambda x: 20 + x["mw"] * 0.9  # noqa: E731
    ax.scatter([x["date"] for x in it], [x["rent_usd_per_kw_month"] for x in it], s=[size(x) for x in it],
               color=ACCENT, alpha=0.75, edgecolor="white", linewidth=0.8, label="MW stated as critical IT load", zorder=3)
    ax.scatter([x["date"] for x in un], [x["rent_usd_per_kw_month"] for x in un], s=[size(x) for x in un],
               facecolor="none", edgecolor=ACCENT, linewidth=1.2, label="MW basis not stated", zorder=3)
    for x in m:
        ax.annotate(x["tick"], (x["date"], x["rent_usd_per_kw_month"]), xytext=(6, 4), textcoords="offset points",
                    fontsize=7.5, color="#444444")
    # Half-year medians (IT basis only), as horizontal segments
    halves = {}
    for x in it:
        halves.setdefault(x["half_year"], []).append(x["rent_usd_per_kw_month"])
    last = max(x["date"] for x in m)
    for hy, v in sorted(halves.items()):
        y, h = int(hy[:4]), int(hy[-1])
        a, b = dt.date(y, 1 if h == 1 else 7, 1), min(dt.date(y, 6 if h == 1 else 12, 30), last)
        md = statistics.median(v)
        ax.plot([a, b], [md, md], color=DARK, linewidth=2, zorder=4)
        ax.text(a + (b - a) / 2, 93, f"${md:.0f}  (n={len(v)})", fontsize=8, ha="center", color=DARK, fontweight="bold")
    med = statistics.median(x["rent_usd_per_kw_month"] for x in it)
    ax.axhline(med, color=GREY, linestyle=":", linewidth=1, zorder=1)
    ax.text(dt.date(2024, 6, 1), med + 3, f"median, IT basis: ${med:.0f}", fontsize=8, color="#555555")
    ax.set_ylabel("Rent, $ per kW per month")
    ax.set_ylim(88, 245)
    ax.axhspan(88, 98, color="#f4f6f8", zorder=0)
    ax.text(dt.date(2024, 4, 20), 99.5, "half-year median", fontsize=7, color="#666666")
    ax.set_xlim(dt.date(2024, 4, 15), last + dt.timedelta(days=45))
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    titles(fig, ax, "What a powered megawatt costs: rent per kW of IT load, by announcement date",
           "Each dot is one signed lease; size = MW. Black lines = half-year medians of IT-basis leases.")
    ax.legend(frameon=False, loc="upper left", fontsize=8.5, markerscale=0.6, bbox_to_anchor=(0, 0.97))
    ax.grid(axis="y", color="#eeeeee")
    footer(fig)
    save(fig, "1_rent_over_time")


def chart_rent_by_credit(m):
    it = [x for x in m if x["mw_basis"] == "IT"]
    fig, ax = plt.subplots(figsize=(9, 5.4))
    for i, (key, label) in enumerate(CREDIT_ORDER):
        v = sorted((x["rent_usd_per_kw_month"], x["tick"]) for x in it if x["credit_type"] == key)
        if not v:
            continue
        n = len(v)
        xs = [i + (j - (n - 1) / 2) * min(0.07, 0.5 / max(n, 1)) for j in range(n)]
        ax.scatter(xs, [r for r, _ in v], s=55, color=ACCENT, alpha=0.8, edgecolor="white", zorder=3)
        med = statistics.median(r for r, _ in v)
        ax.plot([i - 0.3, i + 0.3], [med, med], color=DARK, linewidth=2.2, zorder=4)
        ax.text(i + 0.33, med, f"${med:.0f}", va="center", fontsize=8.5, color=DARK)
        ax.text(i, 95, f"n={n}", ha="center", fontsize=8.5, color="#555555")
    ax.set_xticks(range(len(CREDIT_ORDER)))
    ax.set_xticklabels([lab for _, lab in CREDIT_ORDER])
    ax.set_ylabel("Rent, $ per kW per month")
    ax.set_ylim(90, 245)
    ax.set_xlim(-0.6, len(CREDIT_ORDER) - 0.3)
    titles(fig, ax, "Rent by the credit support stated in the filing",
           "IT-basis leases only. Black lines = medians. Small groups: read as 'consistent with', not as proof.")
    ax.grid(axis="y", color="#eeeeee")
    footer(fig)
    save(fig, "2_rent_by_credit")


def chart_gross_to_it(m):
    site = {r["lease_id"]: r["site"] for r in csv.DictReader(open(ROOT / "data" / "lease_panel.csv", newline="", encoding="utf-8"))}
    def lab(x):
        st = site.get(x["lease_id"], "").split(" (")[0]
        if x.get("deal_key", "").endswith("-P2"):
            st += " (expansion)"
        return f"{x['tick']} – {short_tenant(x['tenant'])}" + (f", {st}" if st else "")
    v = sorted(((x["gross_to_it"], lab(x), x) for x in m if x["gross_to_it"]), key=lambda t: t[0])
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(v) + 1.6))
    ys = range(len(v))
    ax.hlines(ys, 1.0, [r for r, _, _ in v], color="#cccccc", linewidth=2, zorder=1)
    ax.scatter([r for r, _, _ in v], ys, s=60, color=ACCENT, zorder=3)
    for y, (r, lab, x) in zip(ys, v):
        ax.text(r + 0.012, y, f"{r:.2f}  ({x['gross_mw']:.0f} gross / {x['it_mw']:.0f} IT MW)", va="center", fontsize=8, color="#444444")
    ax.set_yticks(list(ys))
    ax.set_yticklabels([lab for _, lab, _ in v], fontsize=8.5)
    med = statistics.median(r for r, _, _ in v)
    ax.axvline(med, color=GREY, linestyle=":", linewidth=1)
    ax.text(med, len(v) - 0.4, f"median {med:.2f}", fontsize=8, color="#555555", ha="center")
    ax.axvline(1.0, color=DARK, linewidth=1)
    ax.set_xlim(0.98, 1.72)
    ax.set_xlabel("Gross power ÷ critical IT load (same lease, same filing)")
    titles(fig, ax, "'A megawatt' is not one unit",
           f"Leases that state both numbers (n={len(v)}). A gross-MW figure can be up to ~50% larger than the IT load it supports.")
    footer(fig, "Source: SEC filings; lease panel by Aadhav Rajesh.")
    save(fig, "3_gross_to_it")


def main() -> int:
    m = load()
    chart_rent_over_time(m)
    chart_rent_by_credit(m)
    chart_gross_to_it(m)
    print(f"charts: {OUT} (3 charts, PNG and SVG)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
