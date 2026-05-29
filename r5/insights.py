"""Pure geometry/logic helpers for isochrone insights.

No r5py / JVM dependency — safe to unit-test in a lightweight container.
"""
from __future__ import annotations

import shapely
import shapely.ops
from shapely.geometry import MultiPolygon, Polygon
from pyproj import Geod

_TRANSIT_MODES = {"RAIL", "BUS", "TRAM", "FERRY", "SUBWAY", "TRANSIT", "GONDOLA", "FUNICULAR"}
_WALK_MODES = {"WALK"}
_GEOD = Geod(ellps="WGS84")
_CATEGORIES = ("supermarket", "park", "food")


# ── 1. boundary_to_polygon ────────────────────────────────────────────────────

def boundary_to_polygon(geom) -> Polygon | MultiPolygon:
    """Convert a (Multi)LineString contour to a filled (Multi)Polygon.

    r5py.Isochrones returns boundaries, not surfaces.
    """
    poly = shapely.build_area(shapely.unary_union(geom))
    if poly.is_empty:
        poly = shapely.unary_union(list(shapely.ops.polygonize(geom)))
    return poly


# ── 2. area_km2 ───────────────────────────────────────────────────────────────

def area_km2(polygon) -> float:
    """Geodesic area of a (Multi)Polygon in km²."""
    area, _ = _GEOD.geometry_area_perimeter(polygon)
    return abs(area) / 1e6


# ── 3. categorize ─────────────────────────────────────────────────────────────

def categorize(tags: dict) -> str | None:
    """Map OSM tags to a POI category string, or None if irrelevant."""
    if tags.get("shop") == "supermarket":
        return "supermarket"
    if tags.get("leisure") == "park":
        return "park"
    if tags.get("amenity") in {"restaurant", "cafe"}:
        return "food"
    return None


# ── 4. count_pois ─────────────────────────────────────────────────────────────

class PoiIndex:
    """Spatial index of POI points with their categories."""

    def __init__(self):
        self._points: list = []
        self._cats: list[str] = []
        self._tree = None

    def add(self, point, category: str):
        self._points.append(point)
        self._cats.append(category)

    def build(self):
        from shapely import STRtree
        self._tree = STRtree(self._points)

    def query_covers(self, polygon) -> list[str]:
        """Return categories of all POIs covered by polygon."""
        if self._tree is None or not self._points:
            return []
        idxs = self._tree.query(polygon, predicate="covers")
        return [self._cats[i] for i in idxs]


def count_pois(polygon, poi_index: PoiIndex) -> dict:
    """Count POIs by category inside polygon."""
    cats = poi_index.query_covers(polygon)
    counts = {c: 0 for c in _CATEGORIES}
    for c in cats:
        if c in counts:
            counts[c] += 1
    return counts


# ── 5. itinerary_summary ─────────────────────────────────────────────────────

def itinerary_summary(legs: list[dict]) -> dict:
    """Summarize an itinerary from a list of leg dicts.

    Each leg must have: mode (str), duration_s (float).
    Optional: wait_s (float) — explicit wait time from r5py DetailedItineraries.
    Convention:
      - walk_min = sum of WALK legs
      - in_vehicle_min = sum of transit legs
      - wait_min = sum of wait_s if available, else max(0, total - walk - in_vehicle)
      - total_min = walk + in_vehicle + wait
      - transfers = max(0, transit_leg_count - 1)
    """
    walk_s = 0.0
    vehicle_s = 0.0
    wait_s_explicit = 0.0
    has_explicit_wait = any("wait_s" in leg for leg in legs)
    transit_count = 0

    for leg in legs:
        mode = leg.get("mode", "").upper()
        dur = float(leg.get("duration_s", 0))
        if mode in _WALK_MODES:
            walk_s += dur
        elif mode in _TRANSIT_MODES:
            vehicle_s += dur
            transit_count += 1
        if has_explicit_wait:
            wait_s_explicit += float(leg.get("wait_s", 0))

    if has_explicit_wait:
        wait_s = max(0.0, wait_s_explicit)
        total_s = walk_s + vehicle_s + wait_s
    else:
        total_s = walk_s + vehicle_s
        wait_s = 0.0

    return {
        "total_min": round(total_s / 60, 1),
        "walk_min": round(walk_s / 60, 1),
        "wait_min": round(wait_s / 60, 1),
        "in_vehicle_min": round(vehicle_s / 60, 1),
        "transfers": max(0, transit_count - 1),
    }


# ── 6. cache_key ─────────────────────────────────────────────────────────────

def cache_key(
    lat: float,
    lon: float,
    date: str,
    time: str,
    modes: list[str],
    cutoffs: list[int],
    robust: bool,
) -> tuple:
    """Stable, hashable cache key. Insensitive to order of modes/cutoffs."""
    return (
        round(lat, 4),
        round(lon, 4),
        date,
        time,
        tuple(sorted(modes)),
        tuple(sorted(cutoffs)),
        robust,
    )
