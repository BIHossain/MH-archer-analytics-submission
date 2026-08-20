"""
Builds the Archer productivity analytical model from the source extracts.

Core grain: one row per site per UTC calendar day.

Outputs are written to outputs/. Run: python src/build.py
"""

from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
OUT = REPO / "outputs"

# Materiality thresholds. Set well above observed day-to-day noise;
# see README for how they were chosen.
DELIVERY_DROP_PP = 2.0   # delivery rate drop vs site baseline (percentage points)
VALID_DROP_PP = 2.0      # valid rate drop vs site baseline (percentage points)
P95_RATIO = 1.5          # daily p95 vs site baseline
RETRY_ALERT = 2          # max retry_count at/above this is notable
WOW_VOLUME_PCT = 15.0    # week-over-week received-volume swing, review only
DQ_BREACH_PCT = 1.0      # a rule counts as breaching above this fail rate


def load() -> dict[str, pd.DataFrame]:
    daily = pd.read_csv(DATA / "lake_transfer_daily.csv", parse_dates=["event_date"])
    events = pd.read_csv(DATA / "lake_transfer_events.csv", parse_dates=["event_ts_utc"])
    quality = pd.read_csv(DATA / "lake_quality_results.csv", parse_dates=["event_date"])
    changes = pd.read_csv(DATA / "lake_change_log.csv", parse_dates=["change_ts_utc"])
    return {"daily": daily, "events": events, "quality": quality, "changes": changes}


def build_batch_mart(events: pd.DataFrame) -> pd.DataFrame:
    """Aggregate batch events to site-day.

    Event record counts do not reconcile with the daily aggregate, so
    events are used only for failure/retry/duration diagnostics.
    """
    ev = events.assign(
        event_date=events["event_ts_utc"].dt.tz_convert("UTC").dt.normalize().dt.tz_localize(None)
    )
    return (
        ev.groupby(["event_date", "site_id"])
        .agg(
            batches_total=("transfer_id", "count"),
            batches_failed=("status", lambda s: int((s == "failed").sum())),
            batches_retried=("retry_count", lambda r: int((r > 0).sum())),
            max_retry_count=("retry_count", "max"),
            max_batch_duration_s=("duration_seconds", "max"),
        )
        .reset_index()
    )


def build_quality_mart(quality: pd.DataFrame) -> pd.DataFrame:
    """Add per-rule failure rate. Denominator is records_evaluated;
    rule populations do not tie back to the daily funnel."""
    q = quality.copy()
    q["rule_fail_pct"] = (q["records_failed"] / q["records_evaluated"] * 100).round(3)
    return q


def build_core(daily: pd.DataFrame, batch_mart: pd.DataFrame, quality_mart: pd.DataFrame) -> pd.DataFrame:
    core = daily.copy()
    core["delivery_rate_pct"] = (core["records_delivered"] / core["records_received"] * 100).round(2)
    core["valid_rate_pct"] = (core["records_valid"] / core["records_delivered"] * 100).round(2)

    # Summarize quality to site-day before joining: worst rule rate,
    # how many rules breached, and which rule was worst.
    q_day = (
        quality_mart.groupby(["event_date", "site_id"])
        .agg(
            dq_worst_rule_fail_pct=("rule_fail_pct", "max"),
            dq_rules_breaching=("rule_fail_pct", lambda r: int((r > DQ_BREACH_PCT).sum())),
        )
        .reset_index()
    )
    worst_idx = quality_mart.groupby(["event_date", "site_id"])["rule_fail_pct"].idxmax()
    worst_rule = quality_mart.loc[worst_idx, ["event_date", "site_id", "rule_id"]].rename(
        columns={"rule_id": "dq_worst_rule_id"}
    )
    q_day = q_day.merge(worst_rule, on=["event_date", "site_id"], how="left")

    # Every breaching rule, not only the day's worst: a rule can breach
    # on several days without ever being the worst one.
    breaching = quality_mart[quality_mart["rule_fail_pct"] > DQ_BREACH_PCT]
    breach_ids = (
        breaching.groupby(["event_date", "site_id"])["rule_id"]
        .agg(lambda s: ",".join(sorted(s.unique())))
        .reset_index()
        .rename(columns={"rule_id": "dq_breaching_rule_ids"})
    )
    q_day = q_day.merge(breach_ids, on=["event_date", "site_id"], how="left")
    q_day["dq_breaching_rule_ids"] = q_day["dq_breaching_rule_ids"].fillna("")

    core = core.merge(batch_mart, on=["event_date", "site_id"], how="left")
    core = core.merge(q_day, on=["event_date", "site_id"], how="left")
    assert len(core) == len(daily), "join fan-out"

    # Use each site's median as its baseline; sites differ structurally.
    baselines = (
        core.groupby("site_id")
        .agg(
            baseline_delivery_pct=("delivery_rate_pct", "median"),
            baseline_valid_pct=("valid_rate_pct", "median"),
            baseline_p95_s=("p95_latency_seconds", "median"),
        )
        .round(2)
        .reset_index()
    )
    core = core.merge(baselines, on="site_id", how="left")

    # Flag site-days that materially deviate from baseline.
    core["flag_delivery"] = core["delivery_rate_pct"] < core["baseline_delivery_pct"] - DELIVERY_DROP_PP
    core["flag_valid"] = core["valid_rate_pct"] < core["baseline_valid_pct"] - VALID_DROP_PP
    core["flag_latency"] = core["p95_latency_seconds"] > P95_RATIO * core["baseline_p95_s"]
    core["flag_retry"] = (core["batches_failed"] > 0) | (core["max_retry_count"] >= RETRY_ALERT)
    core["any_flag"] = core[["flag_delivery", "flag_valid", "flag_latency", "flag_retry"]].any(axis=1)

    return core


def build_weekly(core: pd.DataFrame) -> pd.DataFrame:
    """Weekly site view. The window is exactly six complete Mon-Sun
    weeks, so weekly totals and week-over-week change are comparable."""
    wk = core.copy()
    wk["week_start"] = wk["event_date"] - pd.to_timedelta(wk["event_date"].dt.dayofweek, unit="D")

    weekly = (
        wk.groupby(["site_id", "week_start"])
        .agg(
            records_received=("records_received", "sum"),
            records_delivered=("records_delivered", "sum"),
            records_valid=("records_valid", "sum"),
            # Max/median of daily p95 values; not a true weekly p95.
            worst_daily_p95_seconds=("p95_latency_seconds", "max"),
            median_daily_p95_seconds=("p95_latency_seconds", "median"),
            batches_failed=("batches_failed", "sum"),
            batches_retried=("batches_retried", "sum"),
            flagged_days=("any_flag", "sum"),
        )
        .reset_index()
        .sort_values(["site_id", "week_start"])
    )
    # Weekly rates are recomputed from weekly sums, not averaged from daily rates.
    weekly["delivery_rate_pct"] = (weekly["records_delivered"] / weekly["records_received"] * 100).round(2)
    weekly["valid_rate_pct"] = (weekly["records_valid"] / weekly["records_delivered"] * 100).round(2)
    weekly["wow_received_pct"] = (
        weekly.groupby("site_id")["records_received"].pct_change() * 100
    ).round(1)
    weekly["flag_volume_swing"] = weekly["wow_received_pct"].abs() > WOW_VOLUME_PCT

    return weekly[[
        "site_id", "week_start", "records_received", "records_delivered", "records_valid",
        "wow_received_pct", "flag_volume_swing", "delivery_rate_pct", "valid_rate_pct",
        "worst_daily_p95_seconds", "median_daily_p95_seconds",
        "batches_failed", "batches_retried", "flagged_days",
    ]]


def _incident_windows(core: pd.DataFrame) -> list[dict]:
    """Group flagged site-days into contiguous windows and describe each."""
    flagged = core[core["any_flag"]].sort_values(["site_id", "event_date"])
    windows = []
    for site, g in flagged.groupby("site_id"):
        g = g.sort_values("event_date").copy()
        gap = g["event_date"].diff().dt.days.ne(1).cumsum()
        for _, run in g.groupby(gap):
            dims = []
            details = []
            base = run.iloc[0]
            if run["flag_delivery"].any():
                dims.append("delivery")
                details.append(
                    f"delivery fell to {run['delivery_rate_pct'].min():.1f}% "
                    f"(baseline {base['baseline_delivery_pct']:.1f}%)"
                )
            if run["flag_valid"].any():
                dims.append("data quality")
                rules = sorted({
                    r
                    for ids in run["dq_breaching_rule_ids"].fillna("")
                    for r in str(ids).split(",")
                    if r
                })
                details.append(
                    f"valid rate fell to {run['valid_rate_pct'].min():.1f}% "
                    f"(baseline {base['baseline_valid_pct']:.1f}%)"
                    + (f"; rules {', '.join(rules)} breached" if rules else "")
                )
            if run["flag_latency"].any():
                dims.append("latency")
                details.append(
                    f"worst daily p95 {int(run['p95_latency_seconds'].max())}s "
                    f"(baseline {int(base['baseline_p95_s'])}s)"
                )
            if run["flag_retry"].any():
                dims.append("retries")
                n_failed = int(run["batches_failed"].sum())
                n_retried = int(run["batches_retried"].sum())
                details.append(
                    f"{n_failed} failed batches" if n_failed
                    else f"all {n_retried} batches in the window retried, none failed"
                )
            start, end = run["event_date"].min(), run["event_date"].max()
            recovered = end < core["event_date"].max()
            windows.append({
                "site": site,
                "start": start,
                "end": end,
                "dims": dims,
                "details": details,
                "recovered": (end + pd.Timedelta(days=1)) if recovered else None,
                "days": len(run),
            })
    windows.sort(key=lambda w: w["start"])
    return windows


def build_scorecard(core: pd.DataFrame, weekly: pd.DataFrame) -> str:
    """Primary stakeholder output: latest complete week per site,
    compared against each site's full-window baseline, plus notable
    changes across the window."""
    latest = weekly["week_start"].max()
    wk = weekly[weekly["week_start"] == latest].copy()
    base = core.groupby("site_id")[
        ["baseline_delivery_pct", "baseline_valid_pct", "baseline_p95_s"]
    ].first().reset_index()
    wk = wk.merge(base, on="site_id")

    week_end = latest + pd.Timedelta(days=6)
    week_label = f"{latest.strftime('%b')} {latest.day}–{week_end.strftime('%b')} {week_end.day}, {week_end.year}"

    lines = [
        "# Archer Productivity Scorecard",
        "",
        f"**Week of {week_label}** (latest complete week). "
        "Rates and latency are compared against each site's own 6-week baseline "
        "(shown in parentheses). Status reflects this week's materiality flags.",
        "",
        "| Site | Records received | WoW volume | Delivery rate | Valid rate | Worst daily p95 | Failed / retried batches | Status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, r in wk.sort_values("site_id").iterrows():
        status = "OK" if r["flagged_days"] == 0 else f"REVIEW ({int(r['flagged_days'])} flagged days)"
        lines.append(
            f"| {r['site_id']} "
            f"| {int(r['records_received']):,} "
            f"| {r['wow_received_pct']:+.1f}% "
            f"| {r['delivery_rate_pct']:.1f}% ({r['baseline_delivery_pct']:.1f}%) "
            f"| {r['valid_rate_pct']:.1f}% ({r['baseline_valid_pct']:.1f}%) "
            f"| {int(r['worst_daily_p95_seconds'])}s ({int(r['baseline_p95_s'])}s) "
            f"| {int(r['batches_failed'])} / {int(r['batches_retried'])} "
            f"| {status} |"
        )

    fleet_del = wk["records_delivered"].sum() / wk["records_received"].sum() * 100
    fleet_val = wk["records_valid"].sum() / wk["records_delivered"].sum() * 100
    lines += [
        "",
        f"Fleet this week: {int(wk['records_received'].sum()):,} records received, "
        f"{fleet_del:.1f}% delivered, {fleet_val:.1f}% of deliveries valid. "
        "WoW volume is context only; site status is based on daily flags, not volume.",
        "",
        "## Notable changes (full 6-week window)",
        "",
    ]
    for w in _incident_windows(core):
        span = f"{w['start'].strftime('%b')} {w['start'].day}–{w['end'].strftime('%b')} {w['end'].day}"
        rec = (
            f" Recovered {w['recovered'].strftime('%b')} {w['recovered'].day}."
            if w["recovered"] is not None else " Ongoing at window end."
        )
        dim_label = ", ".join(w["dims"][:-1]) + " and " + w["dims"][-1] if len(w["dims"]) > 1 else w["dims"][0]
        lines.append(
            f"- **{w['site']}, {span}** ({dim_label}, {w['days']} days): "
            + "; ".join(w["details"]) + f".{rec}"
        )
    lines += [
        "",
        "Each change above coincides with a change-log entry; timing alone does not "
        "establish cause. Daily evidence: `archer_flagged_days.csv`; trends: `archer_kpi_trends.png`.",
        "",
    ]
    return "\n".join(lines)


def create_stakeholder_trend_chart(core: pd.DataFrame) -> None:
    """Secondary diagnostic view: daily KPI trends by site with
    incident windows shaded."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    panels = [
        ("delivery_rate_pct", "Delivery rate % (delivered / received)", (94, 100)),
        ("valid_rate_pct", "Valid-delivery rate % (valid / delivered)", (89, 100)),
        ("p95_latency_seconds", "Daily p95 transfer latency (seconds)", None),
    ]
    for ax, (col, title, ylim) in zip(axes, panels):
        for site, g in core.groupby("site_id"):
            g = g.sort_values("event_date")
            ax.plot(g["event_date"], g[col], label=site, linewidth=1.4)
        ax.set_title(title, fontsize=11, loc="left")
        if ylim:
            ax.set_ylim(*ylim)
        ax.grid(alpha=0.25)
    axes[0].legend(ncol=6, fontsize=8, loc="lower left")
    for w in _incident_windows(core):
        for ax in axes:
            ax.axvspan(w["start"], w["end"] + pd.Timedelta(days=1), alpha=0.10, color="red")
    fig.suptitle("Archer productivity KPIs by site - incident windows shaded", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT / "archer_kpi_trends.png", dpi=140)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    t = load()

    batch_mart = build_batch_mart(t["events"])
    quality_mart = build_quality_mart(t["quality"])
    core = build_core(t["daily"], batch_mart, quality_mart)
    weekly = build_weekly(core)

    core.to_csv(OUT / "archer_site_day_core.csv", index=False)
    batch_mart.to_csv(OUT / "archer_batch_reliability.csv", index=False)
    quality_mart.to_csv(OUT / "archer_quality_rules.csv", index=False)
    weekly.to_csv(OUT / "archer_weekly_scorecard.csv", index=False)
    core[core["any_flag"]].to_csv(OUT / "archer_flagged_days.csv", index=False)
    (OUT / "archer_productivity_scorecard.md").write_text(build_scorecard(core, weekly), encoding="utf-8")
    create_stakeholder_trend_chart(core)

    print(f"Core model: {len(core)} site-days | flagged: {int(core['any_flag'].sum())}")
    print(f"Weekly scorecard: {len(weekly)} site-weeks")
    print("Outputs written to", OUT)


if __name__ == "__main__":
    main()