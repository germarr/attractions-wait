"""Geography tests: the stored coordinates must answer distance correctly.

ADR-0008 stores two floats per Attraction instead of a materialized distance
table, so every pairwise distance is computed on demand. That makes two things
worth pinning: the haversine is written twice (once in SQL for the pair queries,
once in Python for callers holding raw coordinates) and the two must never
drift, and the formula itself must be right rather than merely self-consistent.

    .venv/bin/python tests/test_geography.py

Non-zero exit if anything diverges.
"""

from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import stats  # noqa: E402
from app.db import DB_PATH  # noqa: E402

import sqlite3  # noqa: E402

# One degree of latitude on a sphere of EARTH_RADIUS_M, in metres. Independent
# of our data and our SQL: if the formula is wrong, this is what catches it.
DEGREE_OF_LATITUDE_M = 111195.0
MAGIC_KINGDOM = "75ea578a-adc8-4116-a54d-dccb60765ef9"


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    # 1. The formula is right, not just internally consistent.
    one_degree = stats.haversine_m(0.0, 0.0, 1.0, 0.0)
    check(
        abs(one_degree - DEGREE_OF_LATITUDE_M) < 1.0,
        f"1 degree of latitude = {one_degree:.1f} m, expected ~{DEGREE_OF_LATITUDE_M}",
    )
    # Antipodal points are half the circumference apart.
    half = stats.haversine_m(0.0, 0.0, 0.0, 180.0)
    expected_half = stats.EARTH_RADIUS_M * 3.141592653589793
    check(
        abs(half - expected_half) < 1.0,
        f"antipodal = {half:.1f} m, expected ~{expected_half:.1f}",
    )

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, park_id, latitude, longitude FROM attraction "
        "WHERE latitude IS NOT NULL AND park_id = ? ORDER BY id",
        (MAGIC_KINGDOM,),
    ).fetchall()
    conn.close()
    check(len(rows) > 1, "no geocoded attractions to test against")
    coords = {r["id"]: (r["latitude"], r["longitude"]) for r in rows}

    # 2. The SQL haversine and the Python one agree on every pair.
    worst = 0.0
    for a, b in combinations(sorted(coords), 2):
        sql_m = stats.distance_between(a, b)
        py_m = stats.haversine_m(*coords[a], *coords[b])
        worst = max(worst, abs(sql_m - py_m))
    check(worst < 0.1, f"SQL vs Python haversine differ by up to {worst:.4f} m")

    # 3. Metric properties: identity, symmetry, triangle inequality.
    first, second, third = sorted(coords)[:3]
    check(
        stats.distance_between(first, first) == 0.0,
        "distance from an attraction to itself is not 0",
    )
    check(
        stats.distance_between(first, second)
        == stats.distance_between(second, first),
        "distance is not symmetric",
    )
    ab = stats.distance_between(first, second)
    bc = stats.distance_between(second, third)
    ac = stats.distance_between(first, third)
    check(ac <= ab + bc + 1e-6, f"triangle inequality violated: {ac} > {ab} + {bc}")

    # 4. all_pairs yields each unordered pair exactly once.
    pairs = stats.all_pairs(park_id=MAGIC_KINGDOM)
    n = len(coords)
    check(
        len(pairs) == n * (n - 1) // 2,
        f"all_pairs returned {len(pairs)} for {n} attractions, "
        f"expected {n * (n - 1) // 2}",
    )
    check(
        len({frozenset((p["a_id"], p["b_id"])) for p in pairs}) == len(pairs),
        "all_pairs returned a duplicated pair",
    )
    check(
        pairs == sorted(pairs, key=lambda p: p["meters"]),
        "all_pairs is not ordered by distance",
    )

    # 5. nearest() agrees with a brute-force scan of the same park.
    origin = sorted(coords)[0]
    brute = sorted(
        (stats.haversine_m(*coords[origin], *c), i)
        for i, c in coords.items()
        if i != origin
    )[:5]
    got = stats.nearest(origin, 5)
    check(
        [g["id"] for g in got] == [i for _, i in brute],
        "nearest() disagrees with a brute-force scan",
    )

    # 6. Every Attraction the feed still lists is geocoded, so distance
    #    queries never silently drop a live ride.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ungeocoded = conn.execute(
        "SELECT count(*) FROM attraction WHERE latitude IS NULL AND last_seen >= "
        "(SELECT substr(max(last_seen), 1, 10) FROM attraction)"
    ).fetchone()[0]
    conn.close()
    check(ungeocoded == 0, f"{ungeocoded} attraction(s) in the live roster lack coordinates")

    return out


def test_geography():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for p in problems:
        print("FAIL:", p)
    print("geography: OK" if not problems else f"geography: {len(problems)} failure(s)")
    sys.exit(1 if problems else 0)
