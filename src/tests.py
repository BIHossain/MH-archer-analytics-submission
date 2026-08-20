"""
Targeted data-quality checks for the Archer productivity model.

Tests cover the assumptions the KPIs and the site-day grain depend on.
Run after build.py: python src/tests.py (exit code 0 = all pass).
"""

from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
OUT = REPO / "outputs"

FAILURES: list[str] = []


def check(name: str, condition: bool, why_it_matters: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        FAILURES.append(f"{name} - protects: {why_it_matters}")


def main() -> None:
    daily = pd.read_csv(DATA / "lake_transfer_daily.csv", parse_dates=["event_date"])
    events = pd.read_csv(DATA / "lake_transfer_events.csv", parse_dates=["event_ts_utc"])
    quality = pd.read_csv(DATA / "lake_quality_results.csv", parse_dates=["event_date"])

    # Daily source grain and completeness.
    check(
        "daily: unique on (event_date, site_id)",
        not daily.duplicated(["event_date", "site_id"]).any(),
        "the site-day grain itself; duplicates would double-count volume",
    )
    check(
        "daily: complete site-day grid (no gaps)",
        len(daily) == daily["event_date"].nunique() * daily["site_id"].nunique(),
        "trend and week-over-week comparisons assume no missing site-days",
    )
    check(
        "daily: window is complete Mon-Sun weeks",
        daily["event_date"].min().dayofweek == 0
        and daily["event_date"].max().dayofweek == 6
        and daily["event_date"].nunique() % 7 == 0,
        "weekly totals are only comparable if every week is complete",
    )

    # Validate the received -> delivered -> valid funnel.
    check(
        "daily: received >= delivered >= valid >= 0 on every row",
        (
            (daily["records_received"] >= daily["records_delivered"])
            & (daily["records_delivered"] >= daily["records_valid"])
            & (daily["records_valid"] >= 0)
        ).all(),
        "delivery and valid rates are only meaningful if the funnel is monotonic",
    )
    check(
        "daily: no nulls in KPI columns",
        not daily[
            ["records_received", "records_delivered", "records_valid", "p95_latency_seconds"]
        ].isna().any().any(),
        "silent nulls would understate volume and corrupt rates",
    )
    check(
        "daily: p95 latency strictly positive",
        (daily["p95_latency_seconds"] > 0).all(),
        "zero/negative latency indicates a broken extract, not a fast transfer",
    )

    # Batch-event checks.
    ev = events.assign(
        event_date=events["event_ts_utc"].dt.tz_convert("UTC").dt.normalize().dt.tz_localize(None)
    )
    ev_keys = ev[["event_date", "site_id"]].drop_duplicates()
    daily_keys = daily[["event_date", "site_id"]].drop_duplicates()
    check(
        "events: batch events and daily cover the same site-days",
        len(ev_keys.merge(daily_keys, on=["event_date", "site_id"])) == len(daily_keys) == len(ev_keys),
        "reliability aggregates are left-joined onto the core; missing or orphan site-days would create nulls or dropped diagnostics",
    )
    check(
        "events: status values are only success/failed",
        set(events["status"].unique()) <= {"success", "failed"},
        "an unexpected status would silently fall out of failure counts",
    )
    check(
        "events: retry_count non-negative",
        (events["retry_count"] >= 0).all(),
        "retry flags assume a sane retry counter",
    )

    # Quality-rule checks.
    check(
        "quality: records_failed <= records_evaluated",
        (quality["records_failed"] <= quality["records_evaluated"]).all(),
        "rule failure rate must be a valid proportion",
    )
    check(
        "quality: results exist for every site-day",
        len(quality[["event_date", "site_id"]].drop_duplicates().merge(daily_keys)) == len(daily_keys),
        "the core's quality summary columns assume rule results are present each day",
    )

    # Validate the built core model when available.
    core_path = OUT / "archer_site_day_core.csv"
    if core_path.exists():
        core = pd.read_csv(core_path)
        check(
            "core: row count equals daily row count (no join fan-out)",
            len(core) == len(daily),
            "every KPI would silently inflate if a join fanned out the grain",
        )
        check(
            "core: rates within [0, 100]",
            core["delivery_rate_pct"].between(0, 100).all()
            and core["valid_rate_pct"].between(0, 100).all(),
            "out-of-range rates indicate a broken denominator",
        )
    else:
        print("[SKIP] core model tests - run build.py first")

    # Validate the weekly rollup against the core it is built from.
    weekly_path = OUT / "archer_weekly_scorecard.csv"
    if weekly_path.exists() and core_path.exists():
        weekly = pd.read_csv(weekly_path, parse_dates=["week_start"])
        c = pd.read_csv(core_path, parse_dates=["event_date"])
        c["week_start"] = c["event_date"] - pd.to_timedelta(c["event_date"].dt.dayofweek, unit="D")
        expected = (
            c.groupby(["site_id", "week_start"])
            .agg(
                exp_received=("records_received", "sum"),
                exp_delivered=("records_delivered", "sum"),
                exp_valid=("records_valid", "sum"),
                days_in_week=("event_date", "count"),
            )
            .reset_index()
        )
        m = weekly.merge(expected, on=["site_id", "week_start"], how="outer", indicator=True)

        check(
            "weekly: one row per site-week, each covering exactly 7 days",
            len(weekly) == c["site_id"].nunique() * c["week_start"].nunique()
            and (m["_merge"] == "both").all()
            and (m["days_in_week"] == 7).all(),
            "week-over-week comparison assumes complete, equal-length weeks",
        )
        check(
            "weekly: volume sums tie back to the site-day core",
            (m["records_received"] == m["exp_received"]).all()
            and (m["records_delivered"] == m["exp_delivered"]).all()
            and (m["records_valid"] == m["exp_valid"]).all(),
            "the weekly rollup must neither drop nor double-count records",
        )
        check(
            "weekly: rates recomputed from weekly sums, not averaged from daily rates",
            (m["delivery_rate_pct"] == (m["exp_delivered"] / m["exp_received"] * 100).round(2)).all()
            and (m["valid_rate_pct"] == (m["exp_valid"] / m["exp_delivered"] * 100).round(2)).all(),
            "averaging daily rates weights light days equally with heavy ones; on this window the two differ on 17 of 36 site-weeks by up to 0.43pp",
        )
    else:
        print("[SKIP] weekly aggregation tests - run build.py first")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} TEST(S) FAILED:")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
