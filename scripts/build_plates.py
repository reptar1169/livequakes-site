#!/usr/bin/env python3
"""Rebuild public/plates.geojson from PB2002_boundaries.json
(https://github.com/fraxen/tectonicplates, ODC-BY 1.0; Bird 2003, Ahlenius/Nordpil).

Usage: python3 scripts/build_plates.py PB2002_boundaries.json public/plates.geojson

Outputs plain lon/lat GeoJSON LineStrings (simplified with Douglas-Peucker,
~0.03 deg) with a boolean "sub" property marking subduction zones. The map
draws them as a MapLibre layer; the risk lookup measures distance to them.
"""
import json
import sys

TOL = 0.03  # degrees (~3 km)


def simplify(pts, tol):
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


src, out = sys.argv[1], sys.argv[2]
with open(src) as f:
    data = json.load(f)

features = []
for feat in data["features"]:
    g = feat["geometry"]
    lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
    sub = feat["properties"].get("Type") == "subduction"
    for line in lines:
        # Split anywhere a segment jumps across the antimeridian.
        segs, seg = [], []
        for lon, lat in line:
            if seg and abs(lon - seg[-1][0]) > 180:
                segs.append(seg)
                seg = []
            seg.append((lon, lat))
        segs.append(seg)
        for s in segs:
            pts = simplify(s, TOL)
            if len(pts) < 2:
                continue
            features.append({
                "type": "Feature",
                "properties": {"sub": sub},
                "geometry": {"type": "LineString", "coordinates": [[round(x, 3), round(y, 3)] for x, y in pts]},
            })

res = {
    "type": "FeatureCollection",
    "attribution": "Plate boundaries: Bird (2003), PB2002; GeoJSON by Hugo Ahlenius / Nordpil. ODC-BY 1.0.",
    "features": features,
}
with open(out, "w") as f:
    json.dump(res, f, separators=(",", ":"))
print(len(features), "lines,", sum(len(f["geometry"]["coordinates"]) for f in features), "points")
