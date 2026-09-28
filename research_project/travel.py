"""Walking-time matrix over the roster, including the park entrance.

Distances come from `attractiondistance` (ADR-0009), which routes over
OpenStreetMap footpaths rather than measuring straight lines — Big Thunder to
Haunted Mansion is 174 m apart and 391 m to walk, because Rivers of America is
between them.

**The entrance is aliased to an attraction rather than given its own geometry.**
The Walt Disney World Railroad – Main Street station sits over the entrance
turnstiles, so aliasing to it makes every entrance-to-ride leg a row that already
exists in `attractiondistance`: same graph, same routing, same triangle
inequality, nothing invented. `park.latitude/longitude` is 60 m from that station
and would also have served, but it is an unverified centroid that would need
snapping into the graph. The `entrance="osm"` mode does exactly that, and exists
as a control: it should move the answer by about a minute, and if it moves it
more than that, the alias assumption deserves a second look.

Converting metres to minutes is where an invented constant enters. ADR-0009 is
blunt that it has no basis in the data: "Distance is not time. Crowds, strollers,
and parade closures dominate walk time and none of them are in this graph."
`walk_speed_mps` is therefore swept, not trusted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np

from research_project import config

ENTRANCE = "__entrance__"


@dataclass(frozen=True)
class TravelMatrix:
    """Pairwise walking distances and times, with the entrance at index 0."""

    node_ids: tuple[str, ...]  # ENTRANCE first, then attraction ids
    meters: np.ndarray  # (n+1, n+1), 0 on the diagonal
    minutes: np.ndarray  # (n+1, n+1), 0 on the diagonal
    entrance_alias: str
    entrance_mode: Literal["alias", "osm"]

    def index(self, node_id: str) -> int:
        return self.node_ids.index(node_id)

    def minutes_between(self, a: str, b: str) -> float:
        return float(self.minutes[self.index(a), self.index(b)])

    def meters_between(self, a: str, b: str) -> float:
        return float(self.meters[self.index(a), self.index(b)])

    def as_lists(self) -> list[list[float]]:
        """Plain Python lists for the solver's hot loop.

        numpy scalar indexing is roughly an order of magnitude slower than list
        indexing at this size, and the forward pass is the inner loop of every
        move evaluation.
        """
        return [[float(x) for x in row] for row in self.minutes]

    @property
    def n_attractions(self) -> int:
        return len(self.node_ids) - 1


def load_walk_meters(
    conn: sqlite3.Connection,
    park_id: str,
    attraction_ids: Sequence[str],
) -> dict[frozenset[str], float]:
    """Every stored walking distance among the given attractions, in one query.

    One bulk SELECT rather than n(n-1)/2 calls to
    `app.stats.walking_distance_between`, which would open a connection per pair.
    A test pins this against that function on a sample, since it is the published
    definition.

    Withheld pairs (`walk_meters IS NULL`, ADR-0009) are simply absent from the
    result, so callers must treat a missing key as "unknown" rather than zero.
    """
    if not attraction_ids:
        return {}
    placeholders = ",".join("?" * len(attraction_ids))
    rows = conn.execute(
        f"""
        SELECT attraction_a_id, attraction_b_id, walk_meters
        FROM attractiondistance
        WHERE park_id = ?
          AND walk_meters IS NOT NULL
          AND attraction_a_id IN ({placeholders})
          AND attraction_b_id IN ({placeholders})
        """,
        (park_id, *attraction_ids, *attraction_ids),
    ).fetchall()
    return {
        frozenset((r["attraction_a_id"], r["attraction_b_id"])): float(r["walk_meters"])
        for r in rows
    }


def missing_pairs(
    conn: sqlite3.Connection,
    park_id: str,
    attraction_ids: Sequence[str],
) -> list[tuple[str, str]]:
    """Pairs with no usable walking distance — withheld, or never built."""
    known = load_walk_meters(conn, park_id, attraction_ids)
    out: list[tuple[str, str]] = []
    for i, a in enumerate(attraction_ids):
        for b in attraction_ids[i + 1 :]:
            if frozenset((a, b)) not in known:
                out.append((a, b))
    return out


def _resolve_entrance_alias(conn: sqlite3.Connection, park_id: str) -> str:
    """Attraction id of the entrance proxy, by name."""
    row = conn.execute(
        "SELECT id FROM attraction WHERE park_id = ? AND name = ?",
        (park_id, config.ENTRANCE_PROXY_NAME),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"entrance proxy {config.ENTRANCE_PROXY_NAME!r} not found in park {park_id}"
        )
    return row["id"]


def _entrance_meters_via_osm(
    conn: sqlite3.Connection,
    park_id: str,
    attraction_ids: Sequence[str],
) -> dict[str, float]:
    """Entrance-to-attraction metres routed from the park's own coordinate.

    A control for the alias assumption. Reads the cached OSM extract only — it
    asserts the cache exists and never passes `refresh=True`, so this package
    cannot write to `data/osm/` or generate Overpass traffic.
    """
    from app.walking import (  # imported lazily: only this mode needs them
        OSM_CACHE_DIR,
        OSM_CACHE_VERSION,
        SNAP_CANDIDATES,
        SNAP_RADIUS_M,
        _build_graph,
        _choose_component,
        _shortest_paths,
    )
    from app.stats import haversine_m

    cache = OSM_CACHE_DIR / f"{park_id}.{OSM_CACHE_VERSION}.json"
    if not cache.exists():
        raise RuntimeError(
            f"no cached OSM extract at {cache}; run `python -m app.walking "
            "--park <id> --refresh-osm` from the main app, not from here"
        )
    elements = json.loads(cache.read_text())["elements"]
    graph, nodes = _build_graph(elements)

    park = conn.execute(
        "SELECT latitude, longitude FROM park WHERE id = ?", (park_id,)
    ).fetchone()
    rows = conn.execute(
        f"""SELECT id, latitude, longitude FROM attraction
            WHERE id IN ({",".join("?" * len(attraction_ids))})""",
        tuple(attraction_ids),
    ).fetchall()
    points = [(park["latitude"], park["longitude"])] + [
        (r["latitude"], r["longitude"]) for r in rows
    ]
    component = _choose_component(graph, nodes, points)
    if not component:
        raise RuntimeError("no usable walkable component in the cached extract")
    walkable = {u: [(v, w) for v, w in graph[u] if v in component] for u in component}

    def candidates(lat: float, lon: float) -> list[tuple[float, int]]:
        ranked = sorted((haversine_m(*nodes[n], lat, lon), n) for n in component)
        picked = [(d, n) for d, n in ranked[:SNAP_CANDIDATES] if d <= SNAP_RADIUS_M]
        return picked or [ranked[0]]

    # Same virtual-node construction as app/walking.py: splice each point into the
    # graph so the two walk-to-path legs are ordinary edges and the result stays a
    # true shortest path.
    virtual = {}
    for i, (lat, lon) in enumerate(points):
        vid = -(i + 1)
        picks = candidates(lat, lon)
        walkable[vid] = [(n, d) for d, n in picks]
        for d, n in picks:
            walkable[n] = walkable[n] + [(vid, d)]
        virtual[i] = vid

    reach = _shortest_paths(walkable, virtual[0])
    return {
        row["id"]: float(reach[virtual[i + 1]])
        for i, row in enumerate(rows)
        if virtual[i + 1] in reach
    }


def build_travel_matrix(
    conn: sqlite3.Connection,
    park_id: str,
    attraction_ids: Sequence[str],
    *,
    assumptions: config.Assumptions | None = None,
    entrance: Literal["alias", "osm"] = "alias",
) -> TravelMatrix:
    """Metres and minutes between every node, with the entrance at index 0.

    `transition_overhead_min` is added to every off-diagonal edge — exiting a
    ride, re-orienting, finding the next queue entrance. Because a two-leg path
    collects two overheads where a one-leg path collects one, this preserves the
    triangle inequality, which the exact DP and the travel lower bound both rely
    on. A test asserts it rather than trusting the argument.
    """
    assumptions = assumptions or config.Assumptions()
    ids = tuple(attraction_ids)
    alias = _resolve_entrance_alias(conn, park_id)

    known = load_walk_meters(conn, park_id, tuple({*ids, alias}))
    absent = [(a, b) for (a, b) in missing_pairs(conn, park_id, ids)]
    if absent:
        raise RuntimeError(
            f"{len(absent)} attraction pair(s) have no walking distance; "
            f"first: {absent[0]}. Run `python -m app.walking --rebuild`."
        )

    if entrance == "osm":
        entrance_meters = _entrance_meters_via_osm(conn, park_id, ids)
    else:
        entrance_meters = {
            aid: (0.0 if aid == alias else known[frozenset((alias, aid))])
            for aid in ids
        }
    missing_entrance = [aid for aid in ids if aid not in entrance_meters]
    if missing_entrance:
        raise RuntimeError(
            f"no entrance distance for {len(missing_entrance)} attraction(s); "
            f"first: {missing_entrance[0]}"
        )

    size = len(ids) + 1
    meters = np.zeros((size, size))
    for i, aid in enumerate(ids, start=1):
        meters[0, i] = meters[i, 0] = entrance_meters[aid]
        for j, bid in enumerate(ids[i:], start=i + 1):
            value = known[frozenset((aid, bid))]
            meters[i, j] = meters[j, i] = value

    minutes = meters / (assumptions.walk_speed_mps * 60.0)
    off_diagonal = ~np.eye(size, dtype=bool)
    minutes[off_diagonal] += assumptions.transition_overhead_min

    return TravelMatrix(
        node_ids=(ENTRANCE, *ids),
        meters=meters,
        minutes=minutes,
        entrance_alias=alias,
        entrance_mode=entrance,
    )
