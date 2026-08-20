# Archer Productivity Scorecard

**Week of Jul 6–Jul 12, 2026** (latest complete week). Rates and latency are compared against each site's own 6-week baseline (shown in parentheses). Status reflects this week's materiality flags.

| Site | Records received | WoW volume | Delivery rate | Valid rate | Worst daily p95 | Failed / retried batches | Status |
|---|---|---|---|---|---|---|---|
| SITE-01 | 118,677 | -0.7% | 98.9% (98.9%) | 98.3% (98.3%) | 296s (265s) | 0 / 0 | OK |
| SITE-02 | 99,157 | -0.9% | 98.8% (98.8%) | 98.2% (98.3%) | 319s (307s) | 0 / 0 | OK |
| SITE-03 | 68,695 | -3.2% | 98.9% (98.9%) | 98.3% (98.3%) | 367s (349s) | 0 / 0 | OK |
| SITE-04 | 61,837 | +5.3% | 99.0% (98.9%) | 98.4% (98.3%) | 402s (378s) | 0 / 0 | OK |
| SITE-05 | 36,265 | -3.4% | 98.9% (98.9%) | 98.2% (98.3%) | 1208s (395s) | 0 / 6 | REVIEW (2 flagged days) |
| SITE-06 | 24,811 | -0.5% | 98.9% (98.9%) | 98.3% (98.2%) | 474s (446s) | 0 / 0 | OK |

Fleet this week: 409,442 records received, 98.9% delivered, 98.3% of deliveries valid. WoW volume is context only; site status is based on daily flags, not volume.

## Notable changes (full 6-week window)

- **SITE-02, Jun 17–Jun 20** (delivery, latency and retries, 4 days): delivery fell to 95.1% (baseline 98.8%); worst daily p95 744s (baseline 307s); 4 failed batches. Recovered Jun 21.
- **SITE-04, Jun 28–Jul 1** (data quality, 4 days): valid rate fell to 90.5% (baseline 98.3%); rules DQ-01, DQ-03 breached. Recovered Jul 2.
- **SITE-05, Jul 5–Jul 7** (latency and retries, 3 days): worst daily p95 1208s (baseline 395s); all 9 batches in the window retried, none failed. Recovered Jul 8.

Each change above coincides with a change-log entry; timing alone does not establish cause. Daily evidence: `archer_flagged_days.csv`; trends: `archer_kpi_trends.png`.
