#!/usr/bin/env python3
"""Filter GTFS to inner Sydney — keeps full trips that touch the zone."""
import csv, io, os, sys, zipfile
from pathlib import Path

# Inner Sydney bbox (Chatswood/Manly → Ramsgate/La Perouse)
LAT_MIN, LAT_MAX = -34.05, -33.75
LON_MIN, LON_MAX = 151.05, 151.35

SRC = Path(__file__).parent.parent / "data" / "gtfs-complete.zip"
DST = Path(__file__).parent.parent / "data" / "gtfs-sydney.zip"

if DST.exists():
    DST.unlink()

def read_csv(zf, name):
    return csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig"))

def log(msg): print(msg, flush=True)

log("Filtering GTFS to inner Sydney...")

with zipfile.ZipFile(SRC) as zin:

    # 1. Zone stops (bbox)
    log("  stops.txt — zone stops...")
    all_stops = {}
    zone_stops = set()
    for row in read_csv(zin, "stops.txt"):
        sid = row["stop_id"]
        all_stops[sid] = row
        try:
            la, lo = float(row["stop_lat"]), float(row["stop_lon"])
            if LAT_MIN <= la <= LAT_MAX and LON_MIN <= lo <= LON_MAX:
                zone_stops.add(sid)
        except (ValueError, KeyError):
            pass
    log(f"    {len(zone_stops):,} zone stops / {len(all_stops):,} total")

    # 2. Trips that serve zone stops
    log("  stop_times.txt — find trips touching zone (pass 1)...")
    keep_trips = set()
    for row in read_csv(zin, "stop_times.txt"):
        if row["stop_id"] in zone_stops:
            keep_trips.add(row["trip_id"])
    log(f"    {len(keep_trips):,} trips kept")

    # 3. All stops referenced by those trips (may extend outside bbox)
    log("  stop_times.txt — collect all stops for kept trips (pass 2)...")
    keep_stops = set()
    for row in read_csv(zin, "stop_times.txt"):
        if row["trip_id"] in keep_trips:
            keep_stops.add(row["stop_id"])
    log(f"    {len(keep_stops):,} stops needed (incl. outside zone)")

    stops_rows = [all_stops[s] for s in keep_stops if s in all_stops]

    # 4. Trips metadata
    log("  trips.txt...")
    keep_routes, keep_shapes, keep_services = set(), set(), set()
    trips_rows = []
    for row in read_csv(zin, "trips.txt"):
        if row["trip_id"] in keep_trips:
            keep_routes.add(row["route_id"])
            if row.get("shape_id"): keep_shapes.add(row["shape_id"])
            keep_services.add(row["service_id"])
            trips_rows.append(row)
    log(f"    {len(keep_routes):,} routes, {len(keep_shapes):,} shapes")

    def filter_rows(name, field, keep):
        rows = [r for r in read_csv(zin, name) if r.get(field) in keep]
        log(f"  {name}: {len(rows):,} rows")
        return rows

    routes_rows    = filter_rows("routes.txt",         "route_id",   keep_routes)
    calendar_rows  = filter_rows("calendar.txt",       "service_id", keep_services)
    caldates_rows  = filter_rows("calendar_dates.txt", "service_id", keep_services)
    agency_ids     = {r["agency_id"] for r in routes_rows if r.get("agency_id")}
    agency_rows    = filter_rows("agency.txt",         "agency_id",  agency_ids)

    # pathways.txt dropped on purpose: rows reference in-station nodes (platforms,
    # entrances) that never appear in stop_times.txt, so they're absent from the
    # filtered feed → EntityReferenceNotFoundException kills the OTP graph build.
    # Pathways only model in-station walking detail, irrelevant for isochrones.
    try:
        levels_rows = list(read_csv(zin, "levels.txt"))
        log(f"  levels.txt: {len(levels_rows):,} rows")
    except KeyError:
        levels_rows = None

    # 5. Write
    log(f"\nWriting {DST.name}...")

    def write_rows(zout, name, rows):
        if not rows: return
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
        zout.writestr(name, buf.getvalue())

    with zipfile.ZipFile(DST, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        write_rows(zout, "agency.txt",         agency_rows)
        write_rows(zout, "stops.txt",          stops_rows)
        write_rows(zout, "routes.txt",         routes_rows)
        write_rows(zout, "trips.txt",          trips_rows)
        write_rows(zout, "calendar.txt",       calendar_rows)
        write_rows(zout, "calendar_dates.txt", caldates_rows)
        if levels_rows:  write_rows(zout, "levels.txt",   levels_rows)

        log("  stop_times.txt (streaming)...")
        buf = io.StringIO(); w = None; n = 0
        for row in read_csv(zin, "stop_times.txt"):
            if row["trip_id"] in keep_trips:
                if w is None:
                    w = csv.DictWriter(buf, fieldnames=row.keys())
                    w.writeheader()
                w.writerow(row); n += 1
        if w: zout.writestr("stop_times.txt", buf.getvalue())
        log(f"    {n:,} rows")

        log("  shapes.txt (streaming)...")
        buf = io.StringIO(); w = None; n = 0
        for row in read_csv(zin, "shapes.txt"):
            if row["shape_id"] in keep_shapes:
                if w is None:
                    w = csv.DictWriter(buf, fieldnames=row.keys())
                    w.writeheader()
                w.writerow(row); n += 1
        if w: zout.writestr("shapes.txt", buf.getvalue())
        log(f"    {n:,} rows")

src_mb = os.path.getsize(SRC) / 1024 / 1024
dst_mb = os.path.getsize(DST) / 1024 / 1024
log(f"\nDone: {src_mb:.0f} MB → {dst_mb:.0f} MB  ({dst_mb/src_mb*100:.0f}%)")
