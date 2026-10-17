#!/usr/bin/env python3
"""Geocode one-stop site addresses with the US Census geocoder (free, no key),
falling back to OpenStreetMap Nominatim (one request a second) for addresses
the Census geocoder can't match.

Reads ncsbe_cache/onestop_site_crosswalk.json and keeps the results in
ncsbe_cache/site_geocodes.json ({address: [lat, lon] or null}). Addresses
already in the cache aren't looked up again, so only new sites cost a request.
Only public polling-place addresses are sent.
"""

import json
import math
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from build_ballot_history import CACHE

URL = ("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
       "?benchmark=Public_AR_Current&format=json&address=")
NOMINATIM = "https://nominatim.openstreetmap.org/search?format=json&limit=5&countrycodes=us&"
CACHE_FILE = CACHE / "site_geocodes.json"


def one_line(a):
    return f"{a['street']}, {a['city']}, NC {a['zip']}"


def without_unit(addr):
    return re.sub(r"\s+(#|SUITE|STE|UNIT)\s*\S+", "", addr)


def lookup(addr):
    """[lat, lon] from the Census geocoder, retrying without a suite/unit; None if no match."""
    for q in dict.fromkeys([addr, without_unit(addr)]):
        try:
            with urllib.request.urlopen(URL + urllib.parse.quote(q), timeout=30) as r:
                m = json.load(r)["result"]["addressMatches"]
        except (OSError, ValueError, KeyError):
            continue
        if m:
            return [round(m[0]["coordinates"]["y"], 6), round(m[0]["coordinates"]["x"], 6)]
    return None


def lookup_osm(addr):
    """[lat, lon] from Nominatim (street + ZIP, then street + city); None if no match."""
    parts = [p.strip() for p in without_unit(addr).rsplit(",", 2)]
    street, city, zipc = parts[0], parts[-2] if len(parts) == 3 else "", parts[-1].split()[-1]
    for q in ({"street": street, "postalcode": zipc, "state": "NC"},
              {"street": street, "city": city, "state": "NC"}):
        time.sleep(1.1)  # Nominatim usage policy: at most one request a second
        req = urllib.request.Request(NOMINATIM + urllib.parse.urlencode(q),
                                     headers={"User-Agent": "wakeearlyvotewait site geocoder"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                m = json.load(r)
        except (OSError, ValueError):
            continue
        # Nominatim matches loosely: accept only a point in NC whose address names the city or ZIP.
        for hit in m:
            lat, lon, name = float(hit["lat"]), float(hit["lon"]), hit.get("display_name", "").upper()
            if 33.8 <= lat <= 36.6 and -84.4 <= lon <= -75.4 and (zipc in name or (city and city in name)):
                return [round(lat, 6), round(lon, 6)]
    return None


def geocode_all():
    """{site id: [lat, lon]} for every crosswalk site that could be geocoded."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    sites = json.load(open(CACHE / "onestop_site_crosswalk.json"))["sites"]
    addrs = {s["id"]: one_line(s["address"]) for s in sites if s["address"]}
    todo = sorted(set(addrs.values()) - set(cache))
    if todo:
        with ThreadPoolExecutor(8) as pool:
            cache.update(zip(todo, pool.map(lookup, todo)))
    # Census misses (new or from earlier runs) get one try with Nominatim; "_osm_tried" lists them.
    retry = [a for a in set(addrs.values()) if cache.get(a) is None and a not in cache.get("_osm_tried", [])]
    for a in sorted(retry):
        cache[a] = lookup_osm(a)
    cache["_osm_tried"] = sorted(set(cache.get("_osm_tried", [])) | set(retry))
    if todo or retry:
        CACHE_FILE.write_text(json.dumps(cache, indent=1))
        missing = sum(cache[a] is None for a in set(addrs.values()))
        print(f"geocoded {len(todo)} new site addresses; {missing} still without coordinates")
    return {k: cache[a] for k, a in addrs.items() if cache.get(a)}


def miles(p, q):
    """Straight-line (great-circle) distance in miles."""
    la1, lo1, la2, lo2 = map(math.radians, (*p, *q))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


if __name__ == "__main__":
    g = geocode_all()
    print(f"{len(g)} sites with coordinates")
