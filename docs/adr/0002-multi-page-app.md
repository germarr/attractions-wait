# Split the dashboard into multiple pages (Overview + Daily Performance)

The app started as a single page at `/`. We are adding a second route, `/day`
("Daily Performance"), a deep-dive into one day's wait/downtime/weather behaviour,
with simple nav between it and the original Overview page. Both pages share the
park→attraction selector and the `/api` layer.

## Why

The Daily Performance view (period-over-period cards, minute traces, weekday
comparison, correlation) is dense enough that folding it onto the existing page
would make one enormous scroll and duplicate cards/charts inline. Separate routes
keep the Overview a quick glance and the deep-dive self-contained.

## Consequences / trade-offs

- Some UI is **deliberately duplicated** across pages (the selector, the
  current/mean/median cards, a correlation matrix). A future reader will notice
  the overlap — it is intentional, not an accident: each page is usable alone.
- The app is now multi-page: a new template + route per view, a shared static
  CSS/JS surface, and nav. Reversing to a single page later means re-merging.

## Considered and rejected

- **One long single page** — simplest routing, but an unwieldy scroll and the
  same duplication, just stacked vertically.
- **Restructure into one unified view** — would dedupe, but loses the
  "quick glance vs deep dive" separation and is a larger rewrite.
