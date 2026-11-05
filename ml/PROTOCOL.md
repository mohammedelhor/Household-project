# ML evaluation protocol

This file says what the numbers in `ml.report` mean, what they do **not**
prove, and how any future model must be compared. Read it before quoting a
result.

## 1. The task

> This household (or user) just bought product P. In how many days will
> **they** mark it as purchased again?

* **Unit of prediction:** (scope, product). A scope is the owner of a shopping
  list: a household or a single user. The same product in two households is two
  separate series; their intervals are never mixed.
* **Target:** days until the next *purchase mark* of the same product in the
  same scope. It is a purchase rhythm, not a consumption measurement.
* **Sample definition** (recorded in every experiment as `sample_definition`):
  `settled_purchases_v1` — a sample starts at every purchase that still stands
  in the final state (a purchase reverted and never re-bought is not a start).
  The history shown to a predictor is rebuilt *as known at the cutoff*, so it
  does include a tap that was reverted *later*.
* **The scope id is never a model input.** It only keeps series apart
  (guarded by a test).

## 2. Words we use

| Term | Meaning |
|---|---|
| Historical Median Baseline | next gap = median of past gaps (`median of intervals`). The rule a model must beat. |
| Retrospective Oracle Reference | the best single number per (scope, product), chosen in hindsight with the future. A reference line, **not a floor**: a real predictor can beat it by reacting to change. |
| `history_strength` | how much history the suggestion used: `none` / `short` (1-2 gaps) / `moderate` (3-6) / `long` (7+). **Not a probability of being right.** |
| UNSTABLE | a tier with fewer than 30 samples: do not quote it. |

Thresholds live in one file, `ml/evidence.py`. The suggestions start at 1 past
gap; the headline score uses samples with at least 2; the report therefore also
scores 1 / 2 / 3-6 / 7+ gaps separately, so every `history_strength` shown to a
user is backed by a measured error.

## 3. What each kind of data can prove

| Claim | Synthetic data | Real data |
|---|---|---|
| The pipeline recovers a known answer per scope (code is correct) | **yes** | no |
| Households with different rhythms are never mixed | **yes** | no |
| No leakage from the future / from other scopes | **yes** (tests) | n/a |
| The baseline is accurate for real households | **no** | yes, with enough scopes |
| A model beats the baseline in practice | **no** | yes, with the protocol below |

Every manifest and experiment record carries `data_origin`
(`real` / `synthetic` / `unknown`). The report prints a banner for synthetic
data: *it verifies the code, not real-world performance.*

## 4. Evidence and sufficiency

Samples of one series share history, and samples of one household share
habits, so they are **not independent**. The statistical unit is the interval,
and the number of *scopes* matters more than the number of rows. Every report
and record therefore lists: total samples, samples per scope and per series,
number of scopes, share of the largest scope / series, and flags
(`single_scope`, `scope_concentration`, `series_concentration`). A result with
`single_scope` describes one household, not households in general.

## 5. Comparing a model with the baseline

1. Fix the **smallest improvement worth having** δ (days of absolute error)
   *before* looking at results. Never move it afterwards.
2. Hold out the **final period** of time. It is not used to choose features,
   tune anything or pick δ; it is evaluated once, at the end.
3. Compare **paired**: both predictors on the same samples, difference
   `|err A| − |err B|`.
4. Get the interval by **cluster bootstrap by scope** (`ml/compare.py`):
   resample whole households, not rows.
5. Decide by the fixed rule: **adopt B** if the whole 95 % interval is above δ;
   **keep A** if it is below δ; otherwise **inconclusive** (more data needed).
   With a single scope the answer is always inconclusive.
6. Models that use *elapsed time* as a feature need samples drawn at the
   serving moment (daily cutoffs), not only right after a purchase; a model
   trained only on purchase moments would be asked a different question at
   serving time.

## 6. How much data is enough?

For a paired comparison with per-sample difference standard deviation σ and
smallest worthwhile improvement δ (α = 0.05, power 80 %):

    n = ((1.96 + 0.84) · σ / δ)²  ×  (1 + (m − 1) · ρ)

* first factor, independent samples: n ≈ 7.85 · (σ/δ)² → 32 at σ/δ = 2,
  71 at 3, 126 at 4;
* second factor, the **design effect**, for m samples per household with
  intra-household correlation ρ: with m = 30 and ρ = 0.2 it is 6.8, so 71
  becomes about 481 samples ≈ 17 households.

A fixed number such as "64" or "300" samples is not "enough" in itself: with
σ/δ = 4, 64 samples give only about 52 % power. σ and ρ must be **measured**
on the data (`ml/power.py` computes everything above; quote a range, not one
number, until real data exists).
