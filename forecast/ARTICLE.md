# Can you predict how long you'll queue three weeks from now?

Yes, but the honest version of that answer is smaller than it sounds. A model
trained on 92 days of wait history removes about **15% of the squared error** of
the estimate this project was already using, and it does that consistently at
every horizon out to a month. It also fails to beat *carrying last week's crowd
level forward unchanged* at short range, and it is beaten on headliners by a
plain park-wide average.

Five predictions were written down before the model was fitted. **One of them
held.** This is an account of building a wait-time forecaster for seven Orlando
theme parks, and of the several ways it turned out to be less impressive than
expected — which is most of what the exercise was worth.

---

## Part 1 — What predicting a wait time actually means

A wait time has a ride attached, an hour attached, and a date attached. Before
building anything it is worth asking how much of it each of those explains,
because the answer determines what is left for a model to do.

<!-- chart:variance_ladder -->

Knowing only **which ride** accounts for 71% of the variance in hourly log wait.
Adding **what hour** takes it to 80%. Adding **which weekday** buys two more
points. A lookup table indexed by ride and hour — no model, no learning, nothing
that could be called a prediction — is already most of a wait-time predictor.

What is left over is the day: whether today is busy. That residual is what a
forecast has to supply, and it is **28% of what remains** after ride and hour are
accounted for. So the ceiling on this whole enterprise is a slice of a slice.

This matters because it makes one number impossible to misread later. A model
predicting hourly waits across seven parks will happily report an R² above 0.8,
and that figure is almost entirely the lookup table. Quoting it as a measure of
forecasting skill would be a lie told with a true number.

> The unit of the problem is therefore not an hourly wait. It is a **park-day**:
> one number saying how busy a park was on a date. There are 644 of them.

Everything below follows from that. The model predicts a park-day level; the
per-ride hourly shape is measured separately and added back. Two stages, kept
deliberately distinct, because conflating them is how a project like this
convinces itself that 100,000 rows of training data exist when really there are
644.

---

## Part 2 — The target moves, and not in the way you'd guess

The natural assumption is that crowds are a weekly phenomenon: weekends busy,
midweek quiet. Plan around the weekday and you have most of it.

<!-- chart:season -->

That assumption is wrong here, and not marginally. Across the 92 days of history,
the all-park mean wait swings **20 minutes**, collapsing from around 32 in
mid-August to **15.5 minutes on 2 September** before recovering. Weekday spans
**3.2 minutes**, and explains **1.7%** of the variance between park-days.

The collapse is not mysterious — American schools return in late August, and
Orlando empties of families. But it has two consequences that shape everything
downstream.

First, **the estimator this project already had is structurally blind to it.** The
route optimizer uses a mean wait per (attraction, weekday, hour) computed over all
available history. Such a table cannot represent "this week is quiet" at all: it
has one number for every Tuesday, and September's Tuesdays are averaged together
with July's. That is precisely the gap a forecast fills.

Second, **92 days containing one regime change is a hard place to learn from.**
The model sees exactly one transition from peak season to shoulder season. It has
no second example to check that pattern against, no previous year, and no winter
at all. Any claim it makes about seasonality rests on a single observation of a
single transition.

---

## Part 3 — What you are allowed to know

A forecast may only use information that existed before the thing it predicts. It
sounds obvious. It is violated constantly, usually by accident, and the violation
is invisible in the metrics — which is the whole problem.

In this project the rule is a gate in code rather than a note in a document. Every
feature is declared admissible or not, and the design matrix refuses to build if
an inadmissible one is included. Three inadmissible features are listed
explicitly rather than quietly omitted, because "we didn't think of it" and "we
considered it and it cannot be used" are different states:

| Tempting feature | Verdict | Why |
|---|---|---|
| Weather on the day | **No** | Readings are pruned after 35 days, so they cover 35 of 91 training days. Worse, the table holds only *past observations* — there are no forecast rows, so a future date has no weather value at prediction time even where training overlapped. |
| Ride downtime (`n_down`) | **No** | Strongly related to waits, and completely unknowable for a date that hasn't happened. |
| The day's own readings | **No** | It *is* the target. |
| A linear time trend | **Rejected** | Admissible, but it would extrapolate the September recovery forward for a month on the strength of one observed transition. Left out deliberately; see Part 4. |

What survives is calendar arithmetic, the published schedule, and the state of
the world as of the forecast origin. Two of those deserve attention.

### The operator publishes its own forecast

<!-- chart:hours_signal -->

The single strongest knowable feature is **how long the park has announced it will
be open**. At Magic Kingdom the correlation between the published operating window
and how busy the day turns out is **0.69**.

The reason is worth stating plainly, because it is the most useful idea in this
article: Disney and Universal set opening hours weeks ahead **from their own
demand forecasts**. They have booking data, resort occupancy, and historical
patterns nobody outside the company can see. When they extend a park to 22:00
they are telling you they expect a crowd. The schedule feed is a leaked forecast,
published for free, and reading it is worth more than any amount of cleverness
applied to the wait history.

It is also not uniform: the same correlation is **0.07 at EPCOT** and **−0.17 at
Epic Universe**, a park barely a year old whose hours are still being tuned for
reasons unrelated to demand. A feature can be load-bearing at one site and inert
at another, and pooling them hides that.

### Last week's crowds have a shelf life

The other obvious feature is recency: if the last week was quiet, predict quiet.
This works, briefly.

<!-- chart:horizon_decay -->

The correlation between a park's trailing-7-day level and its actual level starts
at **0.52** one day out, decays steadily, **crosses zero at 19 days**, and goes
mildly negative after that. Three weeks out, last week's crowds are not merely
uninformative — they are faintly misleading, because what you are mostly detecting
by then is mean reversion.

This has to be measured on **park-demeaned** levels, incidentally. Pooling the
seven parks raw gives a correlation near 0.7 at every horizon, which looks like a
wonderful signal and is nothing of the kind: it is just the fact that Epic
Universe averages 44 minutes and Magic Kingdom 21, so busy parks stay busy. The
first version of this figure made exactly that mistake and showed no decay at
all.

The fix is to shrink the recency term toward the park's own average as the horizon
grows, at a rate fitted on training data. That single decaying feature is also why
a raw time trend was left out: a trend says "the last thing that happened keeps
happening", and this figure is a direct measurement of how fast that stops being
true.

---

## Part 4 — The model, in the smallest form that works

<!-- chart:decomposition -->

Two stages.

**The shape** is, for each attraction and hour, how far above or below its park's
day level that ride sits — averaged over training dates, in log minutes. Because
each observation is centered by its own park-day before averaging, the shape
carries no information about how busy the training days happened to be. Every
estimate is labelled with how specific its evidence is: **81%** come from that
exact ride at that exact hour, and none fall back to a park-wide average.

**The level** is one number per park-day, predicted by **ridge regression** on 16
features. The full pipeline is: predict the level, add the shape, exponentiate.

Ridge, not a boosted ensemble, and the reason is the 644. With a few hundred rows,
sixteen features and one regime change, a deep model has nothing to learn from.
Ridge gives coefficients that can be printed and argued about, and a prediction
that is a dot product — which matters, because it means the serving path needs
numpy and nothing else. A gradient-boosted version was fitted anyway and scored
side by side, so that "did you try a real model?" is answered with a measurement
rather than an opinion.

### Two details that would otherwise silently break it

**Waits are modelled in logs, which biases the answer low.** `exp(mean of logs)`
is a *median*, not a mean, and understates the average by roughly `exp(σ²/2)`.
Duan's smearing correction — multiply by the mean of `exp(residual)` — puts it
back. Here the factor is **1.048**, so without it every number on the page would
be about 5% too low, consistently, and nothing would look obviously wrong.

**Training examples must have the shape of the question.** The first version fitted
the day model on every park-day up to the origin, with features computed at that
origin. Every training row therefore had a horizon of zero or less — the range was
−77 to 0 — while every actual prediction is made at +1 to +21 days. Two features
had training ranges disjoint from their serving ranges, and the persistence term,
which carries `exp(−horizon/τ)`, reached **−429** in training against 2.6 to 3.4 at
serving. The fitted model was extrapolating wildly outside its own data and scored
**worse than predicting the park average**.

The fix is to train on `(past origin, the dates that followed it)` pairs, so every
training example is a genuine forecast. It is a 20-line change and it is the
difference between a model that works and a model that is worse than nothing.

---

## Part 5 — How it was scored, decided before it was built

An evaluation designed after seeing the score is not an evaluation. So: three
baselines, a protocol, and five written-down predictions, all fixed in advance.

The baselines are chosen so that beating them means something:

| | What it does |
|---|---|
| **Park average** | Each park's mean level over the training window. Knows nothing about the date. |
| **Weekday × hour, 28-day window** | The incumbent — what the route optimizer uses — over a rolling 28-day window. |
| **Last week carried forward** | The trailing-7-day level, unchanged. |

The incumbent deserves a note. Measured over a 14-day holdout, the all-history
version of that table scores MAE 7.01 and the **28-day rolling** version scores
**6.14** — a 12% improvement from restricting it to recent data, one line of code,
no model. Comparing against the all-history form would have been flattering
myself, so the headline comparison uses the rolling one. A baseline you have not
tried to make strong is not a baseline.

**The protocol is rolling origin.** Seventeen times, step to a date, throw away
everything after it, rebuild the shape, refit every baseline and every model
including their hyperparameters, and predict forward 1 to 21 days. Nothing is
fitted once and reused; that `train_max_date <= origin < target_date` holds is
asserted on every one of 300,000 prediction rows rather than trusted.

### Why a random split would have flattered everything

<!-- chart:leakage -->

The same model and the same features, scored three ways, tell the whole story
about why this matters.

Split **rows** at random and R² on the day level is **0.979**, with about a fifth
the error of the honest protocol. Nothing is wrong with the code. The problem is
that roughly 130 rows share each park-day, so for every row in the test set, other
rows from that same day — carrying that day's crowd level — sit in the training
set. The model is not forecasting; it is reading the answer off a neighbour.

Split whole **days** at random and it still scores **0.724**. No row of the target
day is used now. But a held-out date in mid-August has training dates on *both
sides* of it, so the model interpolates between two known points instead of
extrapolating past the last one. That is a different task, and an easier one.

Only the third protocol — trained strictly on the past — answers the question a
visitor actually asks, and it scores **0.664**.

Both leaks are quiet. Neither throws an error, neither produces implausible
numbers, and both would survive code review. The only defence is a protocol chosen
before the scores arrive.

### The five predictions

Written into this file before the backtest ran, and each pinned by a test
assertion, so a flipped result fails the suite rather than quietly disappearing.

| | Prediction | Verdict |
|---|---|---|
| **P1** | Beats the park average at ≤ 7 days | **Refuted** — skill +0.101, but the interval spans zero |
| **P2** | Beats the incumbent at ≤ 7 days, skill > 0.05 | **Confirmed** — +0.113, interval excludes zero |
| **P3** | Skill at ≥ 15 days is indistinguishable from zero | **Refuted** — +0.163, interval excludes zero |
| **P4** | `hours` is the largest coefficient after park identity | **Refuted** — it is third, behind the park's climatology and the persistence term |
| **P5** | A random-row split overstates R² by more than 2× | **Refuted** — it overstates it by 1.48× (though the *error* is 5.5× too low) |

One of five. That is a worse hit rate than guessing, and it is the most useful
output of the project. P3 is the interesting failure: I expected the model to have
no skill a fortnight out, and it retains skill against the incumbent — not because
the model is good at long range, but because the incumbent gets *worse* faster than
the model does. Skill is a ratio, and a ratio improves when the denominator rots.

---

## Part 6 — Results

<!-- chart:skill_by_horizon -->

Against the incumbent weekday-by-hour estimate, the model removes **12% to 17%**
of squared error, and the interval excludes zero at every horizon:

| Days ahead | Skill vs incumbent | 90% interval | n |
|---|---|---|---|
| 1–7 | **+0.116** | +0.017 to +0.197 | 118,395 |
| 8–14 | **+0.174** | +0.099 to +0.237 | 98,451 |
| 15–29 | **+0.162** | +0.065 to +0.240 | 80,307 |

In minutes, mean absolute error goes from 7.13 to **6.86** at a week out, and from
8.82 to **8.02** at a month. The bias improves more than the error does: the
incumbent runs +2.1 to +3.6 minutes high, the model +1.2 to +1.6.

Those intervals come from a bootstrap over the seventeen **origins**, not over the
300,000 rows. Resampling rows would treat predictions that share a fitted model as
independent evidence and return an interval perhaps a tenth as wide. Seventeen is
the real sample size for a claim about skill, and the intervals are wide because
of it.

### Where it doesn't win

Against **carrying last week forward**, the intervals mostly span zero: −0.012 at
1–7 days, +0.034 at 8–14. Only at 15–29 days does the model clearly win (+0.057),
which is exactly where the decay figure predicted persistence would fail. At short
range, a one-line heuristic is as good as the model, and the model's real
contribution is that it *degrades gracefully* rather than turning harmful.

The gradient-boosted version is the mirror image: slightly better at 1–7 days
(MAE 6.69 against 6.86), clearly worse at 15–29 (8.69 against 8.02), and
significantly worse than plain persistence out there (skill −0.093, interval
excluding zero). It overfits the recency features exactly where they stop working.
So the shipped model is the ridge — the simpler model, chosen on a measurement.

<!-- chart:calibration_bands -->

The error is very unevenly distributed, and this is the figure to look at before
trusting any single number:

| Band | MAE | Bias |
|---|---|---|
| Walk-on (< 10 min) | **3.81** | +3.39 |
| Middle | 5.88 | +2.66 |
| Headliner (≥ 25 min) | **11.53** | −1.25 |

Headliners carry three times the absolute error of walk-ons, and the signs differ:
the model **over-predicts quiet rides and under-predicts busy ones**. That is
textbook regression toward the mean, and it is the clearest sign that the day level
is doing most of the work while ride-specific deviations are being smoothed away.
Notably, the incumbent is *better calibrated on headliners* (bias +0.15 against
−1.25) even though its overall error is larger.

By park, MAE runs from **4.73** at Magic Kingdom — the park with the most
attractions and the densest history — to **13.19** at Epic Universe, which is new,
has the highest waits in Florida, and whose published hours carry no signal.
Reporting a single pooled MAE would have hidden a factor of three.

The stated 80% bands contained the outcome **79% to 88%** of the time, measured on
six origins held out from the eleven that defined the bands. Slightly
conservative, particularly for headliners. The same check against the bands' own
rows returns exactly 80.00%, which is arithmetic rather than evidence, and is the
kind of number that should always be viewed with suspicion.

---

## Part 7 — What this doesn't do

**Ninety-two days is one season.** The history runs 25 June to 25 September 2026.
There is no Christmas, no spring break, no winter, and no previous year to compare
against. Every claim here is "under the late-summer-2026 regime". A model asked
about late December would be extrapolating past everything it has seen, which is
why it isn't asked.

**The horizon stops at 2026-10-26, and that is a refusal rather than a limit.** The
schedule feed publishes park hours about a month ahead. Beyond that there are no
real opening and closing times — and those are the model's strongest input. The
date picker simply doesn't offer such dates, and the API returns an error rather
than imputing hours from the weekday and handing back a confident number resting
on an invented input.

**The origin is not today.** Hourly rollups are built overnight and lag the live
feed by a day or two, so the model's information ends on the last completed
rollup. A forecast for "tomorrow" is really two days ahead. Every response, the
page banner and the CLI all state the origin and the true horizon, because a
horizon quietly off by two days would make the skill figures wrong.

**The effective sample size is 644 park-days, not 300,000 rows.** Every interval
here is computed over origins for that reason, and no p-value is quoted off the
row count.

**Averages are not experiences.** This predicts the mean posted wait in an hour. A
breakdown, a parade, or a ride reopening after refurbishment will beat it. Posted
waits are also not actual waits — they are what the operator chose to display,
which is a decision, not a measurement.

**No weather, and not by choice.** Fixing it needs a durable weather rollup and a
historical backfill, plus capturing a forecast rather than only observations. Until
then the model describes a day with the weather averaged out.

**Attractions that opened after the origin have no estimate**, and are reported as
having no estimate rather than given a number. At the last origin that is a
handful of rides; the page says how many.

---

## Part 8 — The stack, and reproducing this

| | |
|---|---|
| Panel | 103,705 rows · 92 dates · 123 attractions · 7 parks |
| Modelling unit | 644 park-days |
| Features | 16, all knowable in advance, gated by an assertion |
| Model | ridge regression, α = 10, shrinkage τ = 3 days |
| Training examples | 8,687 (origin, target) pairs |
| Evaluation | 17 rolling origins · 300,000 predictions · bootstrap over origins |
| Artifact | JSON + CSV, no pickle |
| Serving | numpy + pandas only |

The artifact is deliberately not a pickle. A pickled estimator binds to the numpy
and scikit-learn versions that wrote it, cannot be reviewed in a diff, and cannot
be read without scikit-learn installed — which matters here, because
scikit-learn is installed into the venv but is *not* a declared project
dependency, so a future `uv sync` will remove it. The shipped model is sixteen
coefficients and a table of offsets. It is stored as JSON and CSV, prediction is a
dot product, and the web app and the `predict` command keep working the day the
library disappears.

```bash
uv pip install scikit-learn                       # fit-time only
python -m forecast.cli backtest --out forecast/results
python -m forecast.cli leakage  --out forecast/results
python -m forecast.cli train
python -m forecast.cli predict --date 2026-10-15 --explain
python -m research_project.webapp.app             # 127.0.0.1:3467
```

Every result row carries a hash of the assumptions that produced it, and the
hyperparameters are selected by an inner temporal split of the training data
rather than by looking at the backtest — which would be the same leak this article
is about, one level up.

The companion piece, [on routing a day through Magic Kingdom](/article), uses the
weekday-by-hour estimate this model is measured against. Wiring the forecast into
the optimizer is the obvious next step, and is deliberately not done yet: it would
change that project's inputs and invalidate its published numbers, which deserves
its own evaluation rather than a footnote in this one.
