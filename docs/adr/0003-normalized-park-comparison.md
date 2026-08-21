# Compare parks with size-normalized metrics, not raw totals

The Parks page (a new third route comparing a Destination's theme parks
side-by-side) deliberately expresses every comparative metric as a **rate or
normalized ratio**, never a raw total: reliability is **Park Downtime Rate**
(down minutes ÷ operating minutes), busyness vs. normal is **Crowd Index** (wait
÷ the attraction's own hour-of-day baseline), and the two radars use **absolute
fixed per-axis scales**. We chose this because the parks differ ~10× in size
(a ~40-attraction park vs. a handful of rides), so any raw total — total
downtime hours, summed guest-wait, attraction count as a busyness proxy — just
re-charts how big the park is, not how it's performing.

## Why this is worth recording

A future contributor will look at the page and reasonably ask "why is there no
'total downtime hours' or 'total guest-wait' bar — those are easy to compute and
intuitive." The answer isn't visible in the code: those totals are *confounded
by roster size* and would make the largest park always look the worst (or
busiest) regardless of actual performance. Recording it stops someone from
"helpfully" adding raw-total bars that quietly mislead.

## Consequences / trade-offs

- The page reads less literally. "Average wait" is still a raw minutes figure
  (it's already comparable), but downtime and crowd are normalized, so a glance
  doesn't tell you "this park lost 6 hours today" — by design.
- Roster size *is* still shown (as one Operations-radar axis and a live tile),
  but as context, not as a performance metric.
- **Crowd Index carries a temporal caveat:** its baseline is currently
  weekday-agnostic (hour-of-day mean over all collected history) because only a
  few days of data exist. This deviates from the weekday-aligned philosophy of
  WoW/MoM/YoY and should be revisited (~5+ weeks of history) — see the Crowd
  Index entry in [CONTEXT.md](../../CONTEXT.md).

## Considered and rejected

- **Raw totals (down-hours, summed wait, attraction count).** Intuitive and
  cheap, but dominated by park size — the comparison becomes "which park is
  bigger," which the user already knows. The glossary's `Downtime Rate` entry
  already rejects raw downtime minutes for the same reason.
- **Relative (min-max) radar scaling** instead of absolute. Always fills the
  chart, but the rim then means "worst among the parks shown," so a park is
  pinned to the edge even on a dead day and shapes shift when you toggle
  Destinations — it can express rank but never absolute intensity.
