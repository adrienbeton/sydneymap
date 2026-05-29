"""Tests for r5/insights.py — pure logic, no r5py/JVM dependency."""
import pytest
import shapely
from shapely.geometry import LineString, MultiLineString, Point, box

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from insights import boundary_to_polygon, area_km2, categorize, PoiIndex, count_pois, itinerary_summary, cache_key


# ── 1. boundary_to_polygon ────────────────────────────────────────────────────

def _square_ring(x0, y0, x1, y1):
    """Return a closed LineString forming a square."""
    coords = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return LineString(coords)


def test_boundary_to_polygon_linestring():
    ring = _square_ring(0, 0, 1, 1)
    poly = boundary_to_polygon(ring)
    assert not poly.is_empty
    assert poly.geom_type in ("Polygon", "MultiPolygon")


def test_boundary_to_polygon_multilinestring():
    ring1 = _square_ring(0, 0, 1, 1)
    ring2 = _square_ring(2, 2, 3, 3)
    mls = MultiLineString([ring1, ring2])
    poly = boundary_to_polygon(mls)
    assert not poly.is_empty
    assert poly.geom_type in ("Polygon", "MultiPolygon")


# ── 2. area_km2 ───────────────────────────────────────────────────────────────

def test_area_km2_approx():
    # 0.1° × 0.1° box near Sydney (~-33.87, 151.20) ≈ 123 km² (within 5%)
    poly = box(151.10, -33.92, 151.20, -33.82)
    area = area_km2(poly)
    assert 100 < area < 150, f"area_km2 returned {area}"


# ── 3. categorize ─────────────────────────────────────────────────────────────

def test_categorize_supermarket():
    assert categorize({"shop": "supermarket"}) == "supermarket"

def test_categorize_park():
    assert categorize({"leisure": "park"}) == "park"

def test_categorize_restaurant():
    assert categorize({"amenity": "restaurant"}) == "food"

def test_categorize_cafe():
    assert categorize({"amenity": "cafe"}) == "food"

def test_categorize_none():
    assert categorize({"highway": "residential"}) is None


# ── 4. count_pois ─────────────────────────────────────────────────────────────

def test_count_pois_inside_outside():
    idx = PoiIndex()
    idx.add(Point(151.20, -33.87), "supermarket")   # inside
    idx.add(Point(151.20, -33.87), "park")           # inside
    idx.add(Point(151.20, -33.87), "food")           # inside
    idx.add(Point(0.0, 0.0), "supermarket")          # outside
    idx.build()

    poly = box(151.10, -33.92, 151.30, -33.82)
    counts = count_pois(poly, idx)
    assert counts == {"supermarket": 1, "park": 1, "food": 1}

def test_count_pois_empty():
    idx = PoiIndex()
    idx.build()
    poly = box(0, 0, 1, 1)
    counts = count_pois(poly, idx)
    assert counts == {"supermarket": 0, "park": 0, "food": 0}


# ── 5. itinerary_summary ─────────────────────────────────────────────────────

def test_itinerary_summary_basic():
    legs = [
        {"mode": "WALK", "duration_s": 300},        # 5 min walk
        {"mode": "BUS", "duration_s": 600},         # 10 min in vehicle
        {"mode": "WALK", "duration_s": 120},        # 2 min walk
    ]
    summary = itinerary_summary(legs)
    assert summary["walk_min"] == pytest.approx(7.0, abs=0.1)
    assert summary["in_vehicle_min"] == pytest.approx(10.0, abs=0.1)
    assert summary["transfers"] == 0
    assert summary["wait_min"] >= 0
    assert summary["total_min"] >= summary["walk_min"] + summary["in_vehicle_min"]

def test_itinerary_summary_transfers():
    legs = [
        {"mode": "WALK", "duration_s": 60},
        {"mode": "RAIL", "duration_s": 300},
        {"mode": "WALK", "duration_s": 60},
        {"mode": "BUS", "duration_s": 240},
        {"mode": "WALK", "duration_s": 60},
    ]
    summary = itinerary_summary(legs)
    assert summary["transfers"] == 1   # 2 transit legs → 1 transfer
    assert summary["in_vehicle_min"] == pytest.approx(9.0, abs=0.1)

def test_itinerary_summary_no_transit():
    legs = [{"mode": "WALK", "duration_s": 600}]
    summary = itinerary_summary(legs)
    assert summary["transfers"] == 0
    assert summary["in_vehicle_min"] == 0
    assert summary["wait_min"] == 0


# ── 6. cache_key ─────────────────────────────────────────────────────────────

def test_cache_key_stable():
    k1 = cache_key(-33.8731, 151.2069, "2026-05-29", "08:00", ["TRANSIT", "WALK"], [15, 30], False)
    k2 = cache_key(-33.8731, 151.2069, "2026-05-29", "08:00", ["WALK", "TRANSIT"], [30, 15], False)
    assert k1 == k2

def test_cache_key_hashable():
    k = cache_key(-33.87, 151.20, "2026-05-29", "08:00", ["TRANSIT"], [15], True)
    assert isinstance(k, tuple)
    hash(k)  # must not raise

def test_cache_key_lat_lon_rounded():
    k1 = cache_key(-33.87310001, 151.20690001, "2026-05-29", "08:00", ["TRANSIT"], [15], False)
    k2 = cache_key(-33.8731, 151.2069, "2026-05-29", "08:00", ["TRANSIT"], [15], False)
    assert k1 == k2

def test_cache_key_robust_differs():
    k1 = cache_key(-33.87, 151.20, "2026-05-29", "08:00", ["TRANSIT"], [15], False)
    k2 = cache_key(-33.87, 151.20, "2026-05-29", "08:00", ["TRANSIT"], [15], True)
    assert k1 != k2
