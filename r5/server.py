"""Isochrone API backed by Conveyal R5 (via r5py).

OTP 2.x dropped the isochrone REST API (it was an OTP 1.x "analyst" feature).
R5 is the purpose-built successor for transit travel-time surfaces. r5py wraps it
and consumes the exact same OSM .pbf + GTFS .zip we already prepare.
"""
import os
import sys
import datetime
import threading
import subprocess
import math
import functools

# r5py reads its config from sys.argv at import time — set it before importing.
sys.argv = [
    sys.argv[0],
    "--r5-classpath", os.environ.get("R5_JAR", "/opt/r5/r5.jar"),
    "--max-memory", os.environ.get("R5_MAX_MEMORY", "6G"),
]

import shapely
import shapely.geometry
import geopandas
import r5py
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from insights import (
    boundary_to_polygon, area_km2, count_pois, PoiIndex,
    categorize, itinerary_summary, cache_key,
)

OSM = os.environ.get("OSM_PBF", "/data/sydney.osm.pbf")
OSM_INNER = os.environ.get("OSM_INNER_PBF", "/data/sydney-inner.osm.pbf")
GTFS = os.environ.get("GTFS_ZIP", "/data/gtfs-sydney.zip")
GRID_RESOLUTION = int(os.environ.get("GRID_RESOLUTION", "100"))  # metres
ROBUST_WINDOW_MIN = int(os.environ.get("ROBUST_WINDOW_MIN", "30"))
CROP_BUFFER_KM = float(os.environ.get("CROP_BUFFER_KM", "3"))

# inner Sydney bbox
_BBOX_LON_MIN, _BBOX_LON_MAX = 151.05, 151.35
_BBOX_LAT_MIN, _BBOX_LAT_MAX = -34.05, -33.75

MODE_MAP = {
    "TRANSIT": r5py.TransportMode.TRANSIT,
    "WALK": r5py.TransportMode.WALK,
    "BUS": r5py.TransportMode.BUS,
    "RAIL": r5py.TransportMode.RAIL,
    "FERRY": r5py.TransportMode.FERRY,
    "TRAM": r5py.TransportMode.TRAM,
    "SUBWAY": r5py.TransportMode.SUBWAY,
}

app = FastAPI()
network = None
poi_index: PoiIndex | None = None
# R5/jpype is not safe to drive from several threads at once; serialise requests.
lock = threading.Lock()


# ── OSM crop ─────────────────────────────────────────────────────────────────

def _crop_osm():
    """Generate sydney-inner.osm.pbf if absent, using osmium extract."""
    if os.path.exists(OSM_INNER):
        print(f"[startup] Using cached cropped OSM: {OSM_INNER}", flush=True)
        return

    # Convert buffer km → degrees
    buf_lat = CROP_BUFFER_KM / 111.0
    buf_lon = CROP_BUFFER_KM / (111.0 * math.cos(math.radians((_BBOX_LAT_MIN + _BBOX_LAT_MAX) / 2)))

    bbox = (
        f"{_BBOX_LON_MIN - buf_lon:.5f},{_BBOX_LAT_MIN - buf_lat:.5f},"
        f"{_BBOX_LON_MAX + buf_lon:.5f},{_BBOX_LAT_MAX + buf_lat:.5f}"
    )
    print(f"[startup] Cropping OSM with bbox={bbox} ...", flush=True)
    subprocess.run(
        ["osmium", "extract", "-b", bbox, OSM, "-o", OSM_INNER, "--overwrite"],
        check=True,
    )
    print(f"[startup] Crop done → {OSM_INNER}", flush=True)


# ── POI index ─────────────────────────────────────────────────────────────────

def _build_poi_index(osm_path: str) -> PoiIndex:
    """Read OSM nodes/ways via pyosmium and build a PoiIndex."""
    import osmium

    idx = PoiIndex()

    class _Handler(osmium.SimpleHandler):
        def node(self, n):
            tags = {k: v for k, v in n.tags}
            cat = categorize(tags)
            if cat and n.location.valid():
                idx.add(shapely.Point(n.location.lon, n.location.lat), cat)

        def way(self, w):
            tags = {k: v for k, v in w.tags}
            cat = categorize(tags)
            if cat:
                try:
                    coords = [(nd.lon, nd.lat) for nd in w.nodes if nd.location.valid()]
                    if len(coords) >= 2:
                        from shapely.geometry import LineString
                        centroid = LineString(coords).centroid
                        idx.add(centroid, cat)
                except Exception:
                    pass

    _Handler().apply_file(osm_path, locations=True)
    idx.build()
    return idx


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def load_network():
    global network, poi_index

    _crop_osm()

    osm_source = OSM_INNER if os.path.exists(OSM_INNER) else OSM
    print(f"[startup] Building R5 network from {osm_source} ...", flush=True)
    network = r5py.TransportNetwork(osm_source, [GTFS])
    print("[startup] Network ready.", flush=True)

    print(f"[startup] Building POI index from {osm_source} ...", flush=True)
    poi_index = _build_poi_index(osm_source)
    print("[startup] POI index ready.", flush=True)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "UP" if network is not None else "LOADING"}


# ── Cache ─────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=64)
def _cached_isochrone(key: tuple) -> list:
    """Compute isochrone features for a stable cache key. Returns serialisable list."""
    # Unpack key
    lat, lon, date, time_str, modes_t, cutoffs_t, robust = key
    mins = list(cutoffs_t)
    departure = datetime.datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
    transport_modes = [MODE_MAP[m] for m in modes_t]

    kwargs = dict(
        origins=shapely.Point(lon, lat),
        departure=departure,
        transport_modes=transport_modes,
        isochrones=mins,
        point_grid_resolution=GRID_RESOLUTION,
    )
    if robust:
        kwargs["departure_time_window"] = datetime.timedelta(minutes=ROBUST_WINDOW_MIN)
        kwargs["percentiles"] = [50]

    with lock:
        iso = r5py.Isochrones(network, **kwargs)

    features = []
    for _, row in iso.iterrows():
        minutes = int(round(row["travel_time"].total_seconds() / 60))
        poly = boundary_to_polygon(row.geometry)
        if poly is None or poly.is_empty:
            continue
        km2 = round(area_km2(poly), 2)
        pois = count_pois(poly, poi_index)
        features.append({
            "type": "Feature",
            "properties": {
                "cutoffMinutes": minutes,
                "area_km2": km2,
                "pois": pois,
            },
            "geometry": shapely.geometry.mapping(poly),
        })

    return features


# ── Isochrone endpoint ────────────────────────────────────────────────────────

@app.get("/isochrone")
def isochrone(
    lat: float,
    lon: float,
    date: str,
    time: str,
    cutoffs: str = "15,30,45,60",
    modes: str = "TRANSIT,WALK",
    robust: bool = False,
):
    if network is None:
        raise HTTPException(503, "Transport network still loading")

    try:
        mins = sorted({int(c) for c in cutoffs.split(",") if c})
        modes_list = [m.strip().upper() for m in modes.split(",")]
        # Validate modes early
        for m in modes_list:
            if m not in MODE_MAP:
                raise KeyError(m)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"Bad parameter: {e}")

    key = cache_key(lat, lon, date, time, modes_list, mins, robust)
    features = _cached_isochrone(key)
    return JSONResponse({"type": "FeatureCollection", "features": features})


# ── Itinerary endpoint ────────────────────────────────────────────────────────

@app.get("/itinerary")
def itinerary(
    lat: float,
    lon: float,
    toLat: float,
    toLon: float,
    date: str,
    time: str,
    modes: str = "TRANSIT,WALK",
):
    if network is None:
        raise HTTPException(503, "Transport network still loading")

    try:
        departure = datetime.datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        transport_modes = [MODE_MAP[m.strip().upper()] for m in modes.split(",")]
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"Bad parameter: {e}")

    origins_gdf = geopandas.GeoDataFrame(
        {"id": [0]},
        geometry=[shapely.Point(lon, lat)],
        crs="EPSG:4326",
    )
    destinations_gdf = geopandas.GeoDataFrame(
        {"id": [0]},
        geometry=[shapely.Point(toLon, toLat)],
        crs="EPSG:4326",
    )

    with lock:
        result = r5py.DetailedItineraries(
            network,
            origins=origins_gdf,
            destinations=destinations_gdf,
            departure=departure,
            transport_modes=transport_modes,
        )

    if result is None or len(result) == 0:
        return JSONResponse({"summary": None, "legs": []})

    # Columns: from_id, to_id, option, segment, transport_mode, departure_time,
    #          distance, travel_time, wait_time, feed, agency_id, route_id,
    #          start_stop_id, end_stop_id, geometry
    # DetailedItineraries returns multiple options. Pick the one with shortest
    # total travel_time sum (option 0 may be walk-only and slower).
    if "option" in result.columns:
        def _total_s(grp):
            return grp["travel_time"].apply(
                lambda x: x.total_seconds() if hasattr(x, "total_seconds") else float(x)
            ).sum()
        best_opt = min(result["option"].unique(), key=lambda o: _total_s(result[result["option"] == o]))
        best = result[result["option"] == best_opt]
    else:
        best = result

    legs = []
    for _, row in best.iterrows():
        raw_mode = str(row["transport_mode"]) if row["transport_mode"] else "UNKNOWN"
        # Normalize "TransportMode.WALK" → "WALK"
        mode = raw_mode.split(".")[-1].upper()

        travel_time = row.get("travel_time")
        dur_s = travel_time.total_seconds() if hasattr(travel_time, "total_seconds") else 0.0

        wait_time = row.get("wait_time")
        wait_s = wait_time.total_seconds() if hasattr(wait_time, "total_seconds") else 0.0

        leg = {"mode": mode, "duration_s": dur_s, "wait_s": wait_s}

        route_id = row.get("route_id")
        if route_id is not None and str(route_id) not in ("None", "nan", ""):
            leg["route"] = str(route_id)

        geom = row.get("geometry")
        if geom is not None and not (hasattr(geom, "is_empty") and geom.is_empty):
            try:
                leg["geometry"] = shapely.geometry.mapping(geom)
            except Exception:
                pass

        legs.append(leg)

    summary = itinerary_summary(legs)
    return JSONResponse({"summary": summary, "legs": legs})
