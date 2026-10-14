#!/usr/bin/env python3
"""Build public/map-detail.json: Natural Earth 1:50m land, country borders, and
state/province borders (public domain, naturalearthdata.com v5.1.2), projected
into the map's 720x372 space for the zoomed-in view.

Usage: python3 scripts/build_map_detail.py <dir with ne_50m_*.geojson> public/map-detail.json
"""
import json, os, sys

TOL = 0.025  # simplification tolerance in map units (~0.0125 deg, ~1.4 km; under 1 px at max zoom)


def X(lon): return (lon + 180) / 360 * 720
def Y(lat): return (90 - lat) / 180 * 360 + 6


def simplify(pts, tol):
    # Iterative Douglas-Peucker (recursion depth would blow up on long rings).
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        L = (dx * dx + dy * dy) ** 0.5
        best, idx = 0.0, -1
        for k in range(a + 1, b):
            x, y = pts[k]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L if L else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def fmt(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def path_for(lines, closed):
    out = []
    for line in lines:
        pts = simplify([(X(lo), Y(la)) for lo, la in line], TOL)
        if len(pts) < (4 if closed else 2):
            continue
        out.append("M" + fmt(pts[0][0]) + " " + fmt(pts[0][1]) + "L" +
                   " ".join(fmt(x) + " " + fmt(y) for x, y in pts[1:]) + ("Z" if closed else ""))
    return "".join(out)


def rings(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon": return c
    if t == "MultiPolygon": return [r for poly in c for r in poly]
    if t == "LineString": return [c]
    if t == "MultiLineString": return c
    return []


def load(d, name):
    with open(os.path.join(d, name + ".geojson")) as f:
        return json.load(f)["features"]


src, out = sys.argv[1], sys.argv[2]
land = [r for f in load(src, "ne_50m_land") for r in rings(f["geometry"])]
countries = [r for f in load(src, "ne_50m_admin_0_boundary_lines_land") for r in rings(f["geometry"])]
states = [r for f in load(src, "ne_50m_admin_1_states_provinces_lines") for r in rings(f["geometry"])]
res = {
    "attribution": "Made with Natural Earth (public domain), 1:50m, v5.1.2.",
    "land": path_for(land, True),
    "countries": path_for(countries, False),
    "states": path_for(states, False),
}
with open(out, "w") as f:
    json.dump(res, f, separators=(",", ":"))
print({k: len(v) for k, v in res.items()})
