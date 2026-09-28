"""Walking-distance tests: the routed graph must produce a real metric.

ADR-0009 materializes walking distances, so unlike the on-demand haversine of
ADR-0008 these rows can go stale or be built from a broken graph without
anything complaining. The checks here are the ones that actually caught bugs
during the build: pairs stranded on disconnected path stubs, and routes that
shortcut through geometry a guest cannot walk.

    .venv/bin/python tests/test_walking.py

Non-zero exit if anything diverges.
"""

from __future__ import annotations

import sqlite3
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import stats  # noqa: E402
from app.db import DB_PATH  # noqa: E402

# Walking between two points cannot be shorter than the straight line between
# them. Since ADR-0009 counts both snap offsets in the total, the built data
# respects that, so this is a real invariant rather than a loose sanity bound —
# a violation means the router crossed something a guest cannot.
MIN_PLAUSIBLE_RATIO = 0.95


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT attraction_a_id a, attraction_b_id b, park_id, walk_meters FROM attractiondistance"
    ).fetchall()
    usable = [r for r in rows if r["walk_meters"] is not None]
    withheld = [r for r in rows if r["walk_meters"] is None]
    if not rows:
        conn.close()
        return ["attractiondistance is empty — run `python -m app.walking --rebuild`"]
    if not [r for r in rows if r["walk_meters"] is not None]:
        conn.close()
        return ["every stored pair was withheld — the graph build is broken"]

    # 1. Canonical storage: a < b, each unordered pair exactly once.
    check(
        all(r["a"] < r["b"] for r in rows),
        "some rows are not stored with attraction_a_id < attraction_b_id",
    )
    check(
        len({(r["a"], r["b"]) for r in rows}) == len(rows),
        "attractiondistance contains a duplicated pair",
    )

    # 2. Both endpoints are in the row's park — walking distance is within-park.
    cross = conn.execute(
        """
        SELECT count(*) FROM attractiondistance d
        JOIN attraction a ON a.id = d.attraction_a_id
        JOIN attraction b ON b.id = d.attraction_b_id
        WHERE a.park_id != d.park_id OR b.park_id != d.park_id
        """
    ).fetchone()[0]
    check(cross == 0, f"{cross} rows join attractions from another park")

    # 3. Completeness: every geocoded pair in a built park is present. An
    #    incomplete park means rides stranded on disconnected path stubs, which
    #    is exactly the bug that made a first build route 11 of 276 pairs.
    built_parks = {r["park_id"] for r in rows}
    stored = {p: 0 for p in built_parks}
    for r in rows:
        stored[r["park_id"]] += 1
    for park_id in built_parks:
        n = conn.execute(
            "SELECT count(*) FROM attraction WHERE park_id = ? AND latitude IS NOT NULL",
            (park_id,),
        ).fetchone()[0]
        expected = n * (n - 1) // 2
        check(
            stored[park_id] == expected,
            f"park {park_id}: {stored[park_id]} pairs stored, expected {expected} "
            f"({expected - stored[park_id]} unreachable)",
        )

    # 4. No implausible shortcuts: the walk must not undercut the straight line
    #    by more than snapping can explain.
    worst = conn.execute(
        f"""
        SELECT a.name an, b.name bn, d.walk_meters w,
               {stats._HAVERSINE_M_SQL} AS line
        FROM attractiondistance d
        JOIN attraction a ON a.id = d.attraction_a_id
        JOIN attraction b ON b.id = d.attraction_b_id
        WHERE {stats._GEOCODED} AND {stats._HAVERSINE_M_SQL} > 50
          AND d.walk_meters IS NOT NULL
        ORDER BY d.walk_meters / {stats._HAVERSINE_M_SQL}
        LIMIT 1
        """
    ).fetchone()
    if worst is not None:
        ratio = worst["w"] / worst["line"]
        check(
            ratio >= MIN_PLAUSIBLE_RATIO,
            f"implausible shortcut: {worst['an']} -> {worst['bn']} "
            f"line {worst['line']:.0f} m, walk {worst['w']:.0f} m ({ratio:.2f}x)",
        )
    conn.close()

    # 5. Symmetry: argument order must not matter.
    sample = usable[: min(len(usable), 200)]
    asym = [
        r for r in sample
        if stats.walking_distance_between(r["a"], r["b"])
        != stats.walking_distance_between(r["b"], r["a"])
    ]
    check(not asym, f"{len(asym)} pairs disagree when the arguments are swapped")

    # 6. Triangle inequality — shortest paths satisfy it exactly, so any
    #    violation means the stored rows did not all come from one graph.
    park = usable[0]["park_id"]
    ids = sorted({r["a"] for r in usable if r["park_id"] == park})[:8]
    violations = 0
    for x, y, z in combinations(ids, 3):
        xy = stats.walking_distance_between(x, y)
        yz = stats.walking_distance_between(y, z)
        xz = stats.walking_distance_between(x, z)
        if None in (xy, yz, xz):
            continue
        if xz > xy + yz + 0.5:
            violations += 1
    check(violations == 0, f"{violations} triangle-inequality violations")

    # 7. nearest_walking is same-park and ordered by distance.
    origin = usable[0]["a"]
    near = stats.nearest_walking(origin, 5)
    check(bool(near), "nearest_walking returned nothing for a built attraction")
    check(
        near == sorted(near, key=lambda r: r["meters"]),
        "nearest_walking is not ordered by distance",
    )
    check(
        len({r["park_id"] for r in near}) <= 1,
        "nearest_walking crossed a park boundary",
    )

    # 8. walking_pairs' ratio is walk / line.
    pairs = stats.walking_pairs(park_id=park)
    bad_ratio = [
        p for p in pairs
        if p["ratio"] is not None
        and abs(p["ratio"] - p["walk_meters"] / p["line_meters"]) > 0.01
    ]
    check(not bad_ratio, f"{len(bad_ratio)} rows where ratio != walk / line")
    check(
        pairs == sorted(pairs, key=lambda p: p["walk_meters"]),
        "walking_pairs is not ordered by walking distance",
    )

    # 9. Withheld pairs must read as "unknown" everywhere, never as a number.
    #    This is the whole point of storing NULL rather than the routed value.
    leaked = [
        r for r in withheld[:50]
        if stats.walking_distance_between(r["a"], r["b"]) is not None
    ]
    check(not leaked, f"{len(leaked)} withheld pairs still return a distance")
    pair_keys = {(p["a_id"], p["b_id"]) for p in stats.walking_pairs()}
    exposed = [r for r in withheld if (r["a"], r["b"]) in pair_keys]
    check(not exposed, f"{len(exposed)} withheld pairs appear in walking_pairs()")
    check(
        len(stats.walking_withheld()) == len(withheld),
        "walking_withheld() does not list exactly the withheld rows",
    )
    near_ids = {r["id"] for r in stats.nearest_walking(usable[0]["a"], 50)}
    bad = [
        r for r in withheld
        if r["a"] == usable[0]["a"] and r["b"] in near_ids
    ]
    check(not bad, f"{len(bad)} withheld pairs surfaced through nearest_walking")

    return out


def test_walking():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for p in problems:
        print("FAIL:", p)
    print("walking: OK" if not problems else f"walking: {len(problems)} failure(s)")
    sys.exit(1 if problems else 0)
