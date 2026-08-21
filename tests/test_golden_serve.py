"""Golden test: the Serving Store must answer identically to the live computation.

ADR-0004 gated its rollup cutover by asserting rollup-plus-today-merge equalled
the all-live computation. ADR-0007 reuses the technique one layer out: for every
one of the nine endpoints, app/serve.py reading published payloads must equal
app/stats.py computing from SQLite.

The collector writes a Reading every five minutes, so a naive
publish-then-compare races it: the publish takes ~40 s and any poll landing
mid-run legitimately changes `current`, `as_of`, and the trace window. The test
therefore pins itself to a single poll — it records the newest Reading before
comparing and re-runs if the database advances underneath it.

    .venv/bin/python tests/test_golden_serve.py

Non-zero exit if anything diverges, so it can gate a deploy.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import publish, serve, stats  # noqa: E402

TOL = 0.051   # payloads round to 1dp; allow a half-step of float noise
ATTEMPTS = 3


def diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing in stats")
            elif k not in b:
                out.append(f"{path}.{k}: missing in serve")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    if isinstance(a, bool) or isinstance(b, bool):
        return [] if a == b else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return [] if abs(a - b) <= TOL else [f"{path}: {a} != {b}"]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def sample_targets(n_attractions: int = 6) -> list[str]:
    """Every Park Average (both Destinations) plus a spread of Attractions."""
    groups = stats.get_attractions()
    parks = [g["options"][0]["id"] for g in groups]
    rides: list[str] = []
    for g in groups:
        rides += [o["id"] for o in g["options"][1:]]
    step = max(1, len(rides) // n_attractions)
    return parks + rides[::step][:n_attractions]


def compare_all() -> list[str]:
    """One pinned comparison round. Returns diffs, or raises Drift if a poll landed."""
    failures: list[str] = []

    for target in sample_targets():
        pairs = [
            ("stats", stats.get_stats(target), serve.get_stats(target)),
            ("day_live", stats.get_day_live(target), serve.get_day_live(target)),
            ("recent", stats.get_recent(target), serve.get_recent(target)),
            ("reliability", stats.get_reliability(target), serve.get_reliability(target)),
        ]
        for w in ("today", "week", "month"):
            pairs.append((f"series/{w}", stats.get_series(target, w), serve.get_series(target, w)))

        # day_summary is assembled from two passes: compare only the live half
        # against stats, then assert the nightly blocks came back structurally
        # intact and that `compare` was re-projected correctly.
        live_half = stats.get_day_summary(target, include_slow=False)
        served = serve.get_day_summary(target)
        pairs.append((
            "day_summary/live",
            live_half,
            {k: served.get(k) for k in live_half},
        ))
        failures += _check_compare_block(target, served)

        for name, a, b in pairs:
            failures += diff(a, b, f"{target[:12]}/{name}")

    for dest in stats.get_destinations():
        for w in ("today", "week", "month"):
            failures += diff(
                stats.get_park_comparison(dest["id"], w),
                serve.get_park_comparison(dest["id"], w),
                f"{dest['name']}/compare/{w}",
            )

    failures += diff(stats.get_attractions(), serve.get_attractions(), "attractions")
    failures += diff(stats.get_destinations(), serve.get_destinations(), "destinations")
    return failures


def _check_compare_block(target: str, served: dict) -> list[str]:
    """`compare` is rebuilt by serve.py from today's trace + nightly reference
    maps, so verify its invariants rather than its (drifting) values."""
    out = []
    c = served.get("compare")
    if not isinstance(c, dict) or "labels" not in c:
        return [f"{target[:12]}/day_summary.compare: missing or malformed"]
    n = len(c["labels"])
    for key in ("today", "yesterday", "wow", "mom"):
        if key not in c:
            out.append(f"{target[:12]}/day_summary.compare.{key}: missing")
        elif len(c[key]) != n:
            out.append(f"{target[:12]}/day_summary.compare.{key}: len {len(c[key])} != labels {n}")
    if c["labels"] != sorted(c["labels"]):
        out.append(f"{target[:12]}/day_summary.compare.labels: not sorted")
    for key in ("correlation", "weekday"):
        if key not in served:
            out.append(f"{target[:12]}/day_summary.{key}: missing")
    return out


def main() -> int:
    for attempt in range(1, ATTEMPTS + 1):
        print(f"attempt {attempt}: publishing live pass...")
        print(" ", publish.publish_live())

        pinned = stats._latest_observed_at()
        failures = compare_all()
        after = stats._latest_observed_at()

        if after != pinned:
            print(f"  a poll landed mid-comparison ({pinned} -> {after}); retrying")
            continue

        print(f"\npinned at {pinned} — {len(failures)} diffs")
        for f in failures[:40]:
            print("  FAIL", f)
        if len(failures) > 40:
            print(f"  ... and {len(failures) - 40} more")
        return 1 if failures else 0

    print("could not pin a poll after 3 attempts")
    return 2


def test_golden():  # pytest entry point
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
