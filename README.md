# Archer Productivity Analytics

An analysis-ready model of Archer transfer productivity built from four
internal lake extracts, covering 2026-06-01 to 2026-07-12 (six complete
Mon–Sun weeks) across six sites and one workflow (EHR_TO_EDC). It
answers: how much data is Archer moving, how quickly, how reliably, at
what quality — and where outcomes changed materially during the window.

## Setup and run

Requires Python 3.10+ with `pandas` and `matplotlib`.

```
pip install pandas matplotlib
python src/build.py    # rebuilds every output deterministically into outputs/
python src/tests.py    # runs 16 targeted data-quality checks
```

## Why this stack

The dataset is 252 site-days, 756 batch events, and 1,008 rule results. A
full rebuild runs in under a second in memory, so a warehouse buys nothing
here and costs the reviewer setup: two commands and two libraries is the
whole dependency surface. SQL against DuckDB was the main alternative and
was rejected for the same reason — no performance benefit at this size,
and more to install before anything runs.

The structure, not the tool, is what is meant to survive: `load` →
per-source marts → `build_core` → `build_weekly` is one dbt model per
function, and the assertions in `src/tests.py` are dbt schema and data
tests, which is what the production notes below assume. matplotlib
because the submission has exactly one chart and a plotting framework
would be disproportionate to that.

## Analytical model

**Core grain: one row = one site per UTC calendar day** (252 rows).
This is the native grain of `lake_transfer_daily`, which the exercise
treats as authoritative for volume, delivery, validity, and daily p95.
Finer-grained sources are aggregated to site-day before joining, so
joins cannot multiply rows.

Source responsibilities:

- `lake_transfer_daily` — volume, delivery, validity, daily p95.
- `lake_transfer_events` — failure, retry, and duration diagnostics
  only. Batch record counts do not reconcile with the daily aggregate
  (0.19x–3.65x, with per-site bias), so events are never used for
  volume.
- `lake_quality_results` — rule-level validation evidence. Each rule's
  evaluated population does not tie back to the daily funnel
  (0.2x–5.3x of records received) and rules overlap, so rule rates use
  `records_evaluated` as the denominator and are kept separate from
  funnel metrics.
- `lake_change_log` — context for interpreting metric movement, not
  evidence of cause.

Generated models:

| File | Grain | Purpose |
|---|---|---|
| `archer_site_day_core.csv` | site-day | KPIs, per-site baselines, materiality flags |
| `archer_batch_reliability.csv` | site-day | failure/retry drill-down from events |
| `archer_quality_rules.csv` | site-day-rule | which rules failed and by how much |
| `archer_weekly_scorecard.csv` | site-week | weekly rollup of the core |
| `archer_flagged_days.csv` | flagged site-day | the 11 days breaching any threshold |
| `archer_productivity_scorecard.md` | — | primary stakeholder output |
| `archer_kpi_trends.png` | — | supporting trend view |

## KPI definitions

Rates are aggregated as sum(numerator) / sum(denominator), never as an
average of daily rates, and fleet numbers are always shown with a site
breakdown (SITE-01 carries 29.3% of volume, SITE-06 6.2%).

| KPI | Definition | Meaning | Key limitation |
|---|---|---|---|
| Volume | Σ records received, Σ delivered; weekly per site with WoW change | How much Archer moves | Week-over-week only: Sunday volume runs ~30% above Monday, so day-over-day comparison is misleading |
| Speed | Daily p95 transfer latency per site | End-to-end movement latency | Weekly views report the worst and median of the seven daily p95 values, clearly named; a summary of daily p95s is not a true weekly p95, which would need record-level latencies |
| Delivery rate | Σ delivered / Σ received | Share of eligible records that arrive | Says nothing about correctness or effort; those are separate KPIs |
| Retry / failure rate | Batches retried (and failed) / total batches, from events | Recovery effort — degradation visible before failures | Batch counts, never record-weighted, since event volumes don't reconcile to daily |
| Valid-delivery rate | Σ valid / Σ delivered | Share of delivered records passing validation | Denominator is delivered, not received, so a transfer outage doesn't also register as a quality drop |

Supporting evidence: per-rule DQ failure rate (failed / evaluated).
Diagnostic for which rules degrade; not a headline KPI because rule
populations don't tie to the funnel.

## Materiality thresholds

Normal day-to-day noise was measured first, across the 241 site-days
that carry no flag: delivery and valid rates stay within 0.4pp of a
site's baseline (σ 0.17pp and 0.24pp; worst observed deviations 0.32pp
and 0.41pp), and daily p95 stays within 1.13x of a site's median.
Thresholds sit well above that noise, rounded to values a leader can
remember.

**Flagging thresholds** — a site-day is flagged if any of these breach:

- Delivery rate more than 2pp below site baseline
- Valid rate more than 2pp below site baseline
- Daily p95 above 1.5x site baseline
- Any failed batch, or retry_count ≥ 2

**Review and diagnostic thresholds** — these do not flag a site-day:

- WoW received volume beyond ±15%: context for volume movement only
- DQ rule above a 1% fail rate: decides which rules are worth *naming*
  in an incident description, not whether an incident occurred. Rule
  populations do not tie to the funnel (see above), so a rule breach is
  not on the same footing as a funnel-metric breach — and empirically
  the line is close to noise: 15 rule-days across the fleet exceed 1%
  outside the SITE-04 window, topping out at 1.247%. Wiring it into
  flagging would take the flagged set from 11 site-days to 25 and
  manufacture incident windows on five sites.

Baselines are per-site medians over the full window: sites differ
structurally (median daily p95 ranges 265s to 446s), so a fleet-wide
threshold would misfire, and the median limits the influence of the
short incident windows. Thresholds are tuned on 42 days and would be
recalibrated on more history.

The four flagging thresholds flag 11 of 252 site-days, forming three
windows.

**Breaching a threshold and moving abnormally are not the same thing.**
These are absolute percentage-point rules, deliberately set conservative
so that a flag means "look at this now" rather than "this is slightly
unusual." Against a delivery-rate σ of 0.17pp, a 2pp trigger is roughly
12σ — so a movement can be far outside normal variation and still not
flag. That happens once in this window, on SITE-04 delivery during
Jun 28 – Jul 1 (Finding 2), and the findings below report it on the
evidence rather than on whether a flag fired. A dispersion-scaled rule
would close that gap; see production considerations.

## Stakeholder output

`outputs/archer_productivity_scorecard.md` is the primary output: one
row per site for the latest complete week, each rate and latency shown
against that site's baseline, a status column driven by the daily
flags, and a short list of notable changes across the window. A leader
should be able to answer "where should I look, what is happening, and
why does it matter" in about 30 seconds. WoW volume appears as context
but never drives status, since one unusual week distorts the next
week's comparison.

`outputs/archer_kpi_trends.png` supports it with daily trends by site,
incident windows shaded.

## Findings

**1. SITE-02, Jun 17–20: volume, delivery, and latency degradation.**
Received volume fell ~41% below same-weekday expectations across the
four days, representing roughly 23,500 fewer records received; the
containing week finished 24.2% below the prior week, with no
subsequent catch-up spike. Among records received, delivery fell from
~98.9% to 95.1–95.6%, while daily p95 roughly doubled (280s → up to
744s). The 13:00 UTC batch failed each day with retries exhausted; the
other batches succeeded, and validity of delivered records was
unaffected. Recovered Jun 21.

The change log shows an Archer-wide parallelization release on Jun 17,
but it deployed at 14:00 UTC — an hour after the first failed batch —
and an Archer-wide change does not by itself explain a SITE-02-specific
effect. Attribution would require per-site rollout timing, the cause of
the 13:00 batch failures, whether the missing volume was later
recovered, and whether a fix coincided with recovery.

**2. SITE-04, Jun 28 – Jul 1: validity degradation, with delivery also affected.**
Valid-delivery rate fell from ~98.3% to 90.5–91.1% for four
days. Rule results show the failures concentrated in DQ-01 and DQ-03,
rising from ~0.25% to as high as 6.2% and 6.8%. Latency and batch
behaviour stayed normal throughout.

Delivery did not: it ran 97.5–97.9% across the same four days against a
site baseline of 98.9% and a non-incident spread of 98.6–99.2%
(σ 0.17pp). That is 1.0–1.4pp below baseline, or 6–8σ, where the worst
non-incident deviation on this site is 0.28pp. It is unambiguously
abnormal, and it is visible in the top panel of the trend chart. It did
not flag, because the flagging threshold is 2pp — see the note on
thresholds above. Both dimensions of the funnel degraded on the same
four days at the same site, which makes this a broader effect than a
quality-only reading suggests. Recovered Jul 2. A SITE-04-scoped mapping
configuration change landed the same day, and the site-specific scope
matches the site-specific effect — the strongest correlation of the
three, but still short of cause without the deploy time versus the
first failing batch, the content of the mapping change, and whether a
revert explains the recovery.

**3. SITE-05, Jul 5–7: latency and retries without failures.** Daily
p95 roughly tripled (≈400s → 1159–1208s) for three days while
delivery, validity, and volume stayed normal and every batch reported
success. All nine batches in the window retried twice before
succeeding: retries and latency show degradation that failure counts
alone would not surface, which is why retry rate is tracked as its own
KPI. Recovered Jul 8. An Archer-wide validation release coincides with
the window start, but again only one site was affected, and it is not
obvious why a validation change would slow transfers without raising
validation failures. Retry reasons (timeouts vs rejections) and
infrastructure metrics for SITE-05 would be needed.

**Steady-state loss.** Outside any incident — excluding all 11 flagged
site-days — about 2.8% of received records (65,315 of 2,362,236) never
become valid deliveries: roughly 1.1% lost between received and
delivered and 1.7% between delivered and valid. Nothing in the data indicates whether this is
expected. In record terms it exceeds any single incident, so it is
worth a product-level answer.

## Assumptions and limitations

1. `lake_transfer_daily` is authoritative; where sources disagree, it
   wins.
2. Events and quality results are diagnostics at their own grains and
   are never used for volume or combined with funnel counts.
3. All dates are UTC calendar dates.
4. Co-timing with change-log entries is treated as context, not cause.
5. Flags catch step changes, not slow drift: gradual degradation would
   move the baseline along with the daily values. Acceptable over six
   weeks; a production version would track baselines against a fixed
   reference period.
6. Thresholds were calibrated on this window's noise; real data would
   need recalibration and likely a persistence rule (e.g. two
   consecutive breach days) to suppress one-day blips.

## Data-quality tests

16 checks in `src/tests.py`, each recording which conclusion it
protects. They cover: uniqueness and completeness of the site-day
grid; a whole number of Mon–Sun weeks; funnel monotonicity
(received ≥ delivered ≥ valid); nulls and non-positive latencies;
events and quality covering the same site-days as the daily table;
valid status and retry values; rule failures not exceeding evaluated
counts; and, on the built model, no join fan-out and rates within
bounds.

Three of the sixteen cover the weekly rollup: one row per site-week
each covering exactly seven days; weekly volumes tying back to the
site-day core; and weekly rates recomputed from weekly sums rather
than averaged from daily rates. The last is not a formality — the two
methods disagree on 17 of 36 site-weeks here, by up to 0.43pp.

## Production considerations

- Incremental, idempotent loads keyed on (event_date, site_id) with
  late-arrival handling, replacing full rebuilds.
- Move transforms into the warehouse (the staged structure maps
  directly onto dbt models), with these tests as schema/data tests
  plus freshness checks.
- Monitor the events-to-daily volume reconciliation instead of only
  documenting that it fails today.
- Baselines against a fixed reference period, and thresholds scaled to
  each metric's measured dispersion rather than fixed percentage
  points, so a 6σ move cannot sit under a 12σ trigger as SITE-04
  delivery does here. Plus a persistence rule before alerting.
- Enrich the change log with per-site deployment timestamps and links
  to rollbacks so attribution analysis is routine.
- Obtain record- or batch-level latency so true weekly and fleet p95s
  can be computed.

## AI-assisted development

AI was used throughout the exercise to help summarize requirements, explain unfamiliar concepts, assist with implementing my analytical logic and requirements in the build and test scripts, and refine the overall professional delivery.

The analytical decisions are mine, including the site-day grain, KPI definitions and denominators, source responsibilities, materiality thresholds, and final findings.

I independently verified the key findings and figures against the raw extracts, including the incident windows, change-log timing, source reconciliation differences, and threshold behavior. I also rebuilt the outputs from scratch and ran the automated tests to validate the final implementation.

I am responsible for the submitted solution.
