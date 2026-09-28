"""Walking distances between Attractions, routed over OpenStreetMap footpaths.

See docs/adr/0009-osm-walking-distances.md. Straight-line distance (ADR-0008) is
computed on demand from stored coordinates; walking distance cannot be, because
it comes from a shortest path over a footpath graph. So it is materialized into
`attractiondistance` by this module and rebuilt only when the roster or the OSM
extract changes — not nightly.

    python -m app.walking                 # build any park whose pairs are missing
    python -m app.walking --rebuild       # rebuild every park
    python -m app.walking --refresh-osm   # re-download the extracts first
    python -m app.walking --park <uuid>   # one park

OSM extracts are cached under data/osm/ so a rebuild costs no Overpass traffic.
"""

from __future__ import annotations

import argparse
import heapq
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from app.db import DATA_DIR, engine, init_db
from app.models import Attraction, AttractionDistance, Park
from app.stats import haversine_m

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "attractions-dashboard/0.1 (wait-time dashboard; walking distances)"
# Bumped when the cached extract's shape changes, so a stale cache from an older
# query is never silently reused.
OSM_CACHE_VERSION = "v2"
OSM_CACHE_DIR = DATA_DIR / "osm"

# Ways a guest can walk. The first line is the guest network proper; the second
# is connective tissue that measurement showed is NOT optional. With only the
# first line, Universal Studios Florida's footways fragment into 162 components
# and its rides snap a median 202 m from their own entrances — because the paths
# that join one themed area to the next are tagged `service` or `unclassified`
# there. Adding them takes the median snap to 25 m without changing Magic
# Kingdom's routes at all. Major roads (motorway/primary/secondary/tertiary) stay
# out: a guest cannot walk those, and including them would invent shortcuts.
WALKABLE_HIGHWAYS = {
    "footway", "path", "pedestrian", "steps",
    "corridor", "living_street", "service", "unclassified", "residential",
}

# Queue paths are mapped in OSM (named "Standby Queue", "Lightning Lane Queue",
# "<ride> Queue", "Attraction Exit"). They are walkable geometry but not a
# through-route: a router will happily send someone up the Haunted Mansion
# standby line and out the exit, which understates the real walk.
QUEUE_MARKERS = ("queue", "attraction exit")

# Pad a park's attraction bounding box so perimeter walkways are in the graph.
# ~0.006 deg ≈ 650 m.
BBOX_PAD_DEG = 0.006

# Components smaller than this are stubs (a single ride's forecourt), never the
# park's guest network, and scoring them would pick noise.
MIN_COMPONENT_NODES = 50
COMPONENT_CANDIDATES = 12

# Rides are snapped to several candidate path nodes, not one. A ride's *nearest*
# node is often the wrong access point — Gran Fiesta Tour sits inside the Mexico
# pavilion, and its nearest node can be a service path behind the building while
# the real entrance is on the plaza — which sends the route all the way around.
#
# Each ride therefore enters the graph as its own node, joined to every candidate
# by an edge weighted with the straight-line walk to it. Routing ride-node to
# ride-node (rather than minimising over candidate pairs) is what keeps the
# result a true shortest path: a min over pairs may enter and leave the middle
# ride at different candidates, which breaks the triangle inequality outright.
SNAP_CANDIDATES = 8
SNAP_RADIUS_M = 120.0

# A snap further than this means the ride's coordinate sits nowhere near a mapped
# path, so any route through it is suspect. Reported, never silently used.
MAX_SNAP_M = 120.0

# A short straight line with a very long walk means the graph is missing a
# connection (typically an unmapped walkway, or an indoor route between two rides
# in the same pavilion) rather than a genuine detour. Such a pair is stored with
# a NULL walk_meters — withheld, not published: handing back 1,187 m for two
# rides 70 m apart is worse than admitting we do not know.
SUSPECT_RATIO = 4.0
SUSPECT_LINE_M = 200.0
# ...and only when the detour is absolutely large too. Bruce's Shark World and
# SeaBase Aquarium are 3 m apart and route 14 m — a 4.2x ratio, and completely
# fine. On short pairs the ratio is dominated by its denominator, so absolute
# excess is what separates a real gap from arithmetic.
SUSPECT_EXCESS_M = 150.0

OVERPASS_ATTEMPTS = 3
OVERPASS_BACKOFF_SECONDS = 20


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bbox(coords: list[tuple[float, float]]) -> str:
    """Overpass bbox (south,west,north,east) around a park's attractions."""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return (
        f"{min(lats) - BBOX_PAD_DEG},{min(lons) - BBOX_PAD_DEG},"
        f"{max(lats) + BBOX_PAD_DEG},{max(lons) + BBOX_PAD_DEG}"
    )


def _fetch_osm(park_id: str, bbox: str, refresh: bool = False) -> list[dict]:
    """Every highway way in a bbox plus its nodes, cached under data/osm/.

    Fetches all of `highway=*` and filters in `_build_graph` rather than
    filtering in the query, so changing WALKABLE_HIGHWAYS is a rebuild against
    the cache instead of a re-download.

    `out body; >; out skel qt;` returns the ways with their node refs and then
    every referenced node — which is what makes shared nodes join two ways into
    one graph. `out geom` would give coordinates but no shared identity.
    """
    OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = OSM_CACHE_DIR / f"{park_id}.{OSM_CACHE_VERSION}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())["elements"]

    query = f'[out:json][timeout:180];(way["highway"]({bbox}););out body; >; out skel qt;'
    payload = urllib.parse.urlencode({"data": query}).encode()
    last_error: Exception | None = None
    for attempt in range(OVERPASS_ATTEMPTS):
        try:
            request = urllib.request.Request(
                OVERPASS_URL, payload, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=240) as response:
                body = json.loads(response.read())
            cache.write_text(json.dumps(body))
            return body["elements"]
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Overpass rate-limits and sheds load under contention; both are
            # transient and a backoff is the documented way to handle them.
            last_error = exc
            if attempt < OVERPASS_ATTEMPTS - 1:
                time.sleep(OVERPASS_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(
        f"Overpass failed after {OVERPASS_ATTEMPTS} attempts: {last_error}"
    )


def _build_graph(elements: list[dict]) -> tuple[dict, dict]:
    """Return (adjacency, node coordinates) for the walkable network."""
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in elements if e["type"] == "node"}
    graph: dict[int, list[tuple[int, float]]] = {}
    for element in elements:
        if element["type"] != "way":
            continue
        tags = element.get("tags") or {}
        if tags.get("highway") not in WALKABLE_HIGHWAYS or tags.get("foot") == "no":
            continue
        if any(marker in tags.get("name", "").lower() for marker in QUEUE_MARKERS):
            continue
        refs = [n for n in element.get("nodes", []) if n in nodes]
        for u, v in zip(refs, refs[1:]):
            weight = haversine_m(*nodes[u], *nodes[v])
            graph.setdefault(u, []).append((v, weight))
            graph.setdefault(v, []).append((u, weight))
    return graph, nodes


def _components(graph: dict) -> list[set[int]]:
    """Connected components, largest first."""
    seen: set[int] = set()
    found: list[set[int]] = []
    for start in graph:
        if start in seen:
            continue
        stack, component = [start], set()
        while stack:
            u = stack.pop()
            if u in component:
                continue
            component.add(u)
            for v, _ in graph.get(u, ()):
                if v not in component:
                    stack.append(v)
        seen |= component
        found.append(component)
    return sorted(found, key=len, reverse=True)


def _choose_component(
    graph: dict, nodes: dict, coords: list[tuple[float, float]]
) -> set[int] | None:
    """Pick the component that actually serves this Park's rides.

    Deliberately not the largest. At Universal Studios Florida the largest
    component is off-property sidewalk: snapping the rides to it leaves them a
    median 202 m from their own entrances. Scoring candidates by median snap
    distance instead selects the in-park guest network (median 25 m).

    Snapping every ride into ONE component is also what makes the result usable
    at all — snapping each ride to its globally nearest node strands rides on
    little disconnected forecourt stubs, which is why an earlier build could
    route only 11 of Universal Studios Florida's 276 pairs.
    """
    best_score, best = None, None
    for component in _components(graph)[:COMPONENT_CANDIDATES]:
        if len(component) < MIN_COMPONENT_NODES:
            continue
        score = statistics.median(
            min(haversine_m(*nodes[n], lat, lon) for n in component)
            for lat, lon in coords
        )
        if best_score is None or score < best_score:
            best_score, best = score, component
    return best


def _shortest_paths(graph: dict, source: int) -> dict[int, float]:
    """Dijkstra from one node to everything reachable."""
    dist = {source: 0.0}
    queue = [(0.0, source)]
    settled: set[int] = set()
    while queue:
        d, u = heapq.heappop(queue)
        if u in settled:
            continue
        settled.add(u)
        for v, weight in graph.get(u, ()):
            nd = d + weight
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                heapq.heappush(queue, (nd, v))
    return dist


def build_park(session: Session, park: Park, refresh_osm: bool = False) -> dict:
    """Recompute every within-park walking distance for one Park."""
    attractions = session.exec(
        select(Attraction)
        .where(Attraction.park_id == park.id)
        .where(Attraction.latitude != None)  # noqa: E711
    ).all()
    if len(attractions) < 2:
        return {"park": park.name, "pairs": 0, "note": "fewer than two geocoded rides"}

    coords = [(a.latitude, a.longitude) for a in attractions]
    elements = _fetch_osm(park.id, _bbox(coords), refresh=refresh_osm)
    graph, nodes = _build_graph(elements)
    component = _choose_component(graph, nodes, coords)
    if not component:
        return {"park": park.name, "pairs": 0, "note": "no usable walkable network"}

    walkable = {u: [(v, w) for v, w in graph[u] if v in component] for u in component}

    candidates: dict[str, list[tuple[float, int]]] = {}
    position: dict[str, tuple[float, float]] = {}
    snaps: list[float] = []
    far: list[str] = []
    for attraction in attractions:
        point = (attraction.latitude, attraction.longitude)
        ranked = sorted((haversine_m(*nodes[n], *point), n) for n in component)
        # Keep the nearest node unconditionally so a ride is never candidate-less.
        picks = [(d, n) for d, n in ranked[:SNAP_CANDIDATES] if d <= SNAP_RADIUS_M]
        candidates[attraction.id] = picks or [ranked[0]]
        nearest_m = ranked[0][0]
        snaps.append(nearest_m)
        if nearest_m > MAX_SNAP_M:
            far.append(f"{attraction.name} ({nearest_m:.0f} m)")
        position[attraction.id] = point

    names = {a.id: a.name for a in attractions}
    ordered = sorted(candidates)

    # Splice each ride into the graph as a node of its own. Negative keys keep
    # them distinct from OSM node ids without mixing types in the heap.
    ride_node = {a_id: -(i + 1) for i, a_id in enumerate(ordered)}
    for a_id, picks in candidates.items():
        vid = ride_node[a_id]
        walkable[vid] = [(node, offset) for offset, node in picks]
        for offset, node in picks:
            walkable[node] = walkable[node] + [(vid, offset)]

    built_at = _now_utc()
    rows: list[AttractionDistance] = []
    unreachable = 0
    withheld: list[str] = []
    for i, a_id in enumerate(ordered):
        # One shortest-path tree per ride, over the augmented graph. The two
        # walk-to-path legs are edges like any other, so the distance is a real
        # graph metric: symmetric, and obeying the triangle inequality. It also
        # can no longer come out shorter than the straight line, which pure
        # node-to-node distance produced routinely.
        reach = _shortest_paths(walkable, ride_node[a_id])
        for b_id in ordered[i + 1:]:
            meters = reach.get(ride_node[b_id])
            if meters is None:
                unreachable += 1
                continue
            line = haversine_m(*position[a_id], *position[b_id])
            implausible = (
                line > 1
                and meters / line > SUSPECT_RATIO
                and line < SUSPECT_LINE_M
                and meters - line > SUSPECT_EXCESS_M
            )
            if implausible:
                withheld.append(
                    f"{names[a_id]} -> {names[b_id]}: "
                    f"{line:.0f} m line, {meters:.0f} m walk ({meters / line:.1f}x)"
                )
            rows.append(
                AttractionDistance(
                    attraction_a_id=a_id,
                    attraction_b_id=b_id,
                    park_id=park.id,
                    walk_meters=None if implausible else round(meters, 1),
                    built_at=built_at,
                )
            )

    session.exec(
        delete(AttractionDistance).where(AttractionDistance.park_id == park.id)
    )
    session.add_all(rows)
    session.commit()
    return {
        "park": park.name,
        "attractions": len(attractions),
        "graph_nodes": len(component),
        "pairs": len(rows),
        "unreachable": unreachable,
        "median_snap_m": round(statistics.median(snaps), 1),
        "worst_snap_m": round(max(snaps), 1),
        "withheld": len(withheld),
        "far_from_path": far,
        "withheld_pairs": withheld,
    }


def run_rebuild(
    park_id: str | None = None, rebuild: bool = False, refresh_osm: bool = False
) -> list[dict]:
    """Build walking distances for the parks that need them."""
    init_db()
    results = []
    with Session(engine) as session:
        for park in session.exec(select(Park)).all():
            if park_id is not None and park.id != park_id:
                continue
            if not rebuild and park_id is None:
                existing = session.exec(
                    select(AttractionDistance).where(
                        AttractionDistance.park_id == park.id
                    )
                ).first()
                if existing is not None:
                    continue
            results.append(build_park(session, park, refresh_osm=refresh_osm))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--park", help="only this park uuid")
    parser.add_argument("--rebuild", action="store_true", help="rebuild every park")
    parser.add_argument(
        "--refresh-osm", action="store_true", help="re-download OSM extracts first"
    )
    args = parser.parse_args()
    for result in run_rebuild(
        park_id=args.park, rebuild=args.rebuild, refresh_osm=args.refresh_osm
    ):
        far = result.pop("far_from_path", [])
        withheld = result.pop("withheld_pairs", [])
        print("[walking]", json.dumps(result))
        for entry in far:
            print(f"[walking]   far from any path: {entry}")
        for entry in withheld[:5]:
            print(f"[walking]   withheld (likely unmapped connection): {entry}")
        if len(withheld) > 5:
            print(f"[walking]   ... and {len(withheld) - 5} more withheld pairs")


if __name__ == "__main__":
    main()
