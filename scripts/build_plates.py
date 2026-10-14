#!/usr/bin/env python3
"""Rebuild public/plates.json from PB2002_boundaries.json
(https://github.com/fraxen/tectonicplates, ODC-BY 1.0; Bird 2003, Ahlenius/Nordpil).

Usage: python3 scripts/build_plates.py PB2002_boundaries.json public/plates.json
Projects to the map's 720x372 space and simplifies (Douglas-Peucker, 0.08 units)."""
import json,sys,collections
src,out=sys.argv[1],sys.argv[2]
d=json.load(open(src))
print(collections.Counter(f['properties']['Type'] for f in d['features']))
print(len(d['features']), sum(len(f['geometry']['coordinates']) for f in d['features']))
def X(lon): return (lon+180)/360*720
def Y(lat): return (90-lat)/180*360+6
def dp(pts,tol):
    if len(pts)<3: return pts
    (x1,y1),(x2,y2)=pts[0],pts[-1]; dx,dy=x2-x1,y2-y1; L=(dx*dx+dy*dy)**.5 or 1e-9
    i,dm=0,0
    for k in range(1,len(pts)-1):
        x,y=pts[k]; dd=abs(dy*x-dx*y+x2*y1-y2*x1)/L
        if dd>dm: i,dm=k,dd
    if dm<=tol: return [pts[0],pts[-1]]
    return dp(pts[:i+1],tol)[:-1]+dp(pts[i:],tol)
parts={'subduction':[],'other':[]}; npts=0
for f in d['features']:
    g=f['geometry']; lines=[g['coordinates']] if g['type']=='LineString' else g['coordinates']
    for line in lines:
        seg=[]; segs=[]
        for lon,lat in line:
            if seg and abs(lon-seg[-1][0])>180: segs.append(seg); seg=[]
            seg.append((lon,lat))
        segs.append(seg)
        for s in segs:
            p=dp([(X(a),Y(b)) for a,b in s],0.08)
            if len(p)<2: continue
            npts+=len(p)
            parts['subduction' if f['properties']['Type']=='subduction' else 'other'].append('M'+' '.join('%.1f %.1f'%q for q in p[:1])+'L'+' '.join('%.1f %.1f'%q for q in p[1:]))
def clean(ps): return (''.join(ps)+' ').replace('.0 ',' ').replace('.0L','L').replace('.0M','M').strip()
res={"attribution":"Plate boundaries: Bird (2003), PB2002; GeoJSON by Hugo Ahlenius / Nordpil. ODC-BY 1.0.","subduction":clean(parts['subduction']),"other":clean(parts['other'])}
json.dump(res,open(out,'w'),separators=(',',':'))
print('points',npts,'bytes',len(res['subduction'])+len(res['other']))
