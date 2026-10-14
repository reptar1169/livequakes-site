#!/usr/bin/env python3
"""Generate the per-region pages (public/<slug>.html) and sitemap.xml.

public/index.html is the single source of truth. Each region page is a copy of
it with the <!-- region:NAME --> ... <!-- /region:NAME --> blocks swapped for
region-specific SEO tags, heading, and intro text, plus a small config script
(window.LQ_REGION) that makes the app filter the USGS feed to that region and
zoom the map to it.

Run after editing index.html, before deploying:

    python3 scripts/build_regions.py && firebase deploy --only hosting

To add a region, add an entry to REGIONS and a link in index.html's .region-nav.
"""
import datetime
import html
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
SITE = "https://livequakes.com"

# match: regex (case-insensitive) tested against the USGS "place" string, e.g.
#   "8 km SW of Westlake Village, CA" / "53 km S of Cantwell, Alaska".
# bbox: [west, south, east, north] used only for the initial map zoom.
REGIONS = [
    {
        "slug": "california",
        "name": "California",
        # Lookbehind keeps Mexico's "Gulf of California" / "Baja California" out.
        "match": r", CA$|(?<!Gulf of |Baja )California",
        "bbox": [-125.5, 32.0, -114.0, 42.2],
        "about": (
            "California sits on the boundary between the Pacific and North American plates, where the "
            "San Andreas fault system and many related faults take up their motion. The state records "
            "thousands of earthquakes every year, most too small to feel. Notable historic quakes include "
            "1906 San Francisco (M7.9), 1989 Loma Prieta (M6.9), 1994 Northridge (M6.7), and the 2019 "
            "Ridgecrest sequence (M7.1)."
        ),
    },
    {
        "slug": "nevada",
        "name": "Nevada",
        "match": r"Nevada$",
        "bbox": [-120.1, 35.0, -114.0, 42.0],
        "about": (
            "Nevada is among the most seismically active U.S. states. It sits in the Basin and Range "
            "Province, where the crust is being stretched east to west, and the Walker Lane fault zone along "
            "the California border takes up part of the motion between the Pacific and North American "
            "plates. Notable quakes include the 1954 Fairview Peak (M7.1) and Dixie Valley (M6.8) "
            "earthquakes, the 2008 Wells earthquake (M6.0), and the 2020 Monte Cristo Range earthquake (M6.5)."
        ),
    },
    {
        "slug": "oregon",
        "name": "Oregon",
        "match": r"Oregon",
        "bbox": [-129.5, 41.8, -116.4, 46.4],
        "about": (
            "Oregon faces the Cascadia Subduction Zone, which runs offshore from northern California to "
            "British Columbia and last produced a roughly M9 earthquake and tsunami in January 1700. "
            "Onshore quakes are less frequent than in California but include the 1993 Scotts Mills (M5.6) "
            "and Klamath Falls (up to M6.0) earthquakes. Many of the moderate quakes listed here occur well "
            "offshore along the Blanco Fracture Zone."
        ),
    },
    {
        "slug": "washington",
        "name": "Washington",
        "match": r"Washington",
        "bbox": [-127.0, 45.5, -116.9, 49.1],
        "about": (
            "Washington's earthquakes come from three sources: shallow faults in the crust (such as the "
            "Seattle Fault), deeper quakes inside the subducting Juan de Fuca Plate (like the 2001 Nisqually "
            "earthquake, M6.8), and the offshore Cascadia Subduction Zone, which last ruptured in a roughly "
            "M9 earthquake in January 1700. Mount St. Helens and other Cascade volcanoes also produce "
            "frequent small quakes."
        ),
    },
    {
        "slug": "alaska",
        "name": "Alaska",
        "match": r"Alaska|Aleutian",
        # The far western Aleutians cross 180° and fall outside this box;
        # they're still listed, just off the initial map view.
        "bbox": [-180.0, 51.0, -129.0, 71.5],
        "about": (
            "Alaska is the most seismically active state in the U.S. and has more large earthquakes than "
            "the rest of the country combined. Most come from the Aleutian subduction zone, where the "
            "Pacific Plate dives beneath the North American Plate. The 1964 Great Alaska earthquake (M9.2) "
            "is the largest ever recorded in North America and the second largest recorded worldwide; the "
            "2018 Anchorage earthquake (M7.1) is a more recent example."
        ),
    },
    {
        "slug": "hawaii",
        "name": "Hawaii",
        "match": r"Hawaii",
        "bbox": [-160.5, 18.5, -154.5, 22.5],
        "about": (
            "Most of Hawaii's earthquakes happen on the Island of Hawai\u02bbi (the Big Island), driven by "
            "K\u012blauea and Mauna Loa volcanoes and by the weight of the island pressing down on the ocean "
            "crust beneath it. Thousands of small quakes occur each year, many under the volcanoes' summits "
            "and rift zones. Larger events include the 1868 Ka\u02bb\u016b earthquake (about M7.9), the 1975 "
            "Kalapana earthquake (M7.7), and the M6.9 earthquake during K\u012blauea's 2018 eruption."
        ),
    },
]

# Region pages default to M1.0+ (the homepage uses M2.5+) since regional
# viewers care about the smaller, locally felt quakes too.
REGION_DEFAULT_MAG = "1.0"


def replace_block(page, name, new):
    pattern = re.compile(r"<!-- region:%s -->.*?<!-- /region:%s -->" % (name, name), re.S)
    out, n = pattern.subn(lambda _: "<!-- region:%s -->%s<!-- /region:%s -->" % (name, new, name), page)
    if n != 1:
        raise SystemExit("index.html: expected exactly one region:%s block, found %d" % (name, n))
    return out


def build(region, template):
    name, slug = region["name"], region["slug"]
    url = "%s/%s" % (SITE, slug)
    title = "%s Earthquakes Today &mdash; Live Map &amp; List | LiveQuakes" % name
    desc = (
        "Live map and list of earthquakes in %s and just offshore, updated every minute from the USGS "
        "Earthquake Hazards Program. Filter by magnitude and time, see felt reports, and share any quake."
        % name
    )
    e = html.escape

    head = "\n".join([
        "",
        "<title>%s</title>" % title,
        '<meta name="description" content="%s" />' % e(desc),
        '<meta name="robots" content="index, follow" />',
        '<link rel="canonical" href="%s" />' % url,
        '<meta property="og:type" content="website" />',
        '<meta property="og:site_name" content="LiveQuakes" />',
        '<meta property="og:url" content="%s" />' % url,
        '<meta property="og:title" content="%s" />' % title,
        '<meta property="og:description" content="%s" />' % e(desc),
        '<meta property="og:image" content="%s/og-image.png" />' % SITE,
        '<meta property="og:image:width" content="1200" />',
        '<meta property="og:image:height" content="630" />',
        '<meta property="og:locale" content="en_US" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        '<meta name="twitter:title" content="%s" />' % title,
        '<meta name="twitter:description" content="%s" />' % e(desc),
        '<meta name="twitter:image" content="%s/og-image.png" />' % SITE,
        "",
    ])

    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "name": "%s Earthquakes Today" % name,
                "url": url,
                "description": desc,
                "isPartOf": {"@type": "WebSite", "name": "LiveQuakes", "url": SITE + "/"},
                "about": {"@type": "Place", "name": name},
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "LiveQuakes", "item": SITE + "/"},
                    {"@type": "ListItem", "position": 2, "name": "%s earthquakes" % name, "item": url},
                ],
            },
        ],
    }
    jsonld_html = '\n<script type="application/ld+json">\n%s\n</script>\n' % json.dumps(jsonld, indent=2)

    # The brand stays visible but stops being the page's <h1>; the region
    # heading in the intro takes that role.
    brand = '<a class="brand-name" href="/">LiveQuakes</a>'
    about = (
        '\n  <h1 class="region-title">%s earthquakes, live</h1>\n'
        '  <p class="about-line">%s This page shows every quake the '
        '<a href="https://earthquake.usgs.gov/earthquakes/feed/v1.0/" target="_blank" rel="noopener">USGS '
        "Earthquake Hazards Program</a> reports in %s and just offshore, updated automatically. Click any "
        "quake for details and a shareable link.</p>\n  "
    ) % (name, e(region["about"]), name)

    config = {k: region[k] for k in ("slug", "name", "match", "bbox")}
    config_html = "<script>window.LQ_REGION = %s;</script>" % json.dumps(config)

    page = template
    page = replace_block(page, "head", head)
    page = replace_block(page, "jsonld", jsonld_html)
    page = replace_block(page, "h1", brand)
    page = replace_block(page, "about", about)
    page = replace_block(page, "config", config_html)

    # Swap the default magnitude selection.
    mag_select = re.search(r'<select id="magSel">.*?</select>', page, re.S)
    if not mag_select:
        raise SystemExit("index.html: #magSel not found")
    sel = mag_select.group(0).replace(" selected>", ">")
    sel, n = re.subn(r'(<option value="%s")>' % re.escape(REGION_DEFAULT_MAG), r"\1 selected>", sel)
    if n != 1:
        raise SystemExit("index.html: #magSel has no option %s" % REGION_DEFAULT_MAG)
    page = page[: mag_select.start()] + sel + page[mag_select.end():]
    return page


def write_sitemap(today):
    urls = [(SITE + "/", "1.0")] + [("%s/%s" % (SITE, r["slug"]), "0.8") for r in REGIONS]
    body = "".join(
        "  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>\n    <changefreq>always</changefreq>\n"
        "    <priority>%s</priority>\n  </url>\n" % (loc, today, prio)
        for loc, prio in urls
    )
    (PUBLIC / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s</urlset>\n' % body
    )


def main():
    template = (PUBLIC / "index.html").read_text()
    for region in REGIONS:
        out = PUBLIC / ("%s.html" % region["slug"])
        out.write_text(build(region, template))
        print("wrote", out.relative_to(ROOT))
    write_sitemap(datetime.date.today().isoformat())
    print("wrote public/sitemap.xml")


if __name__ == "__main__":
    main()
