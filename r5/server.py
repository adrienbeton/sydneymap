"""Isochrone API backed by Conveyal R5 (via r5py).

OTP 2.x dropped the isochrone REST API (it was an OTP 1.x "analyst" feature).
R5 is the purpose-built successor for transit travel-time surfaces. r5py wraps it
and consumes the exact same OSM .pbf + GTFS .zip we already prepare.
"""
import os
import sys
import datetime
import threading

# r5py reads its config from sys.argv at import time — set it before importing.
sys.argv = [
    sys.argv[0],
    "--r5-classpath", os.environ.get("R5_JAR", "/opt/r5/r5.jar"),
    "--max-memory", os.environ.get("R5_MAX_MEMORY", "6G"),
]

import shapely
import r5py
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

OSM = os.environ.get("OSM_PBF", "/data/sydney.osm.pbf")
GTFS = os.environ.get("GTFS_ZIP", "/data/gtfs-sydney.zip")
GRID_RESOLUTION = int(os.environ.get("GRID_RESOLUTION", "100"))  # metres

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
# R5/jpype is not safe to drive from several threads at once; serialise requests.
lock = threading.Lock()


@app.on_event("startup")
def load_network():
    global network
    network = r5py.TransportNetwork(OSM, [GTFS])


@app.get("/health")
def health():
    return {"status": "UP" if network is not None else "LOADING"}


def _to_polygon(geom):
    """r5py returns isochrone boundaries as (Multi)LineString — rebuild filled areas."""
    poly = shapely.build_area(shapely.unary_union(geom))
    if poly.is_empty:
        poly = shapely.unary_union(list(shapely.ops.polygonize(geom)))
    return poly


@app.get("/isochrone")
def isochrone(
    lat: float,
    lon: float,
    date: str,
    time: str,
    cutoffs: str = "15,30,45,60",
    modes: str = "TRANSIT,WALK",
):
    if network is None:
        raise HTTPException(503, "Transport network still loading")

    try:
        mins = sorted({int(c) for c in cutoffs.split(",") if c})
        departure = datetime.datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        transport_modes = [MODE_MAP[m.strip().upper()] for m in modes.split(",")]
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"Bad parameter: {e}")

    with lock:
        iso = r5py.Isochrones(
            network,
            origins=shapely.Point(lon, lat),
            departure=departure,
            transport_modes=transport_modes,
            isochrones=mins,
            point_grid_resolution=GRID_RESOLUTION,
        )

    features = []
    for _, row in iso.iterrows():
        minutes = int(round(row["travel_time"].total_seconds() / 60))
        poly = _to_polygon(row.geometry)
        if poly is None or poly.is_empty:
            continue
        features.append({
            "type": "Feature",
            "properties": {"cutoffMinutes": minutes},
            "geometry": shapely.geometry.mapping(poly),
        })

    return JSONResponse({"type": "FeatureCollection", "features": features})
