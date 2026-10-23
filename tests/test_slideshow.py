"""Unit tests for `slideshow.py`. Run with plain python, no deps:

    python tests/test_slideshow.py      (or `make test`)

The module chooses what the TV shows when nothing plays. The cases lean on the
ways that goes wrong on a wall: a portrait photo or a panorama strip, an HTML
error page or a logo where a photo should be, a photo with no credit, the wrong
season (or the wrong hemisphere's), and forty photos of one theme in a row.
The source shapes are trimmed copies of real answers (2026-10-22).
"""

import os
import struct
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slideshow as ss          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


# ── season ───────────────────────────────────────────────────────────────────
eq("October in Seattle is autumn", ss.season(date(2026, 10, 22), 47.6), "autumn")
eq("October in Sydney is spring", ss.season(date(2026, 10, 22), -33.9), "spring")
eq("no city reads as north", ss.season(date(2026, 1, 5)), "winter")
eq("December is winter", ss.season(date(2026, 12, 1), 40.0), "winter")
eq("the tropics have none", ss.season(date(2026, 10, 22), 1.35), "")
eq("just outside the tropics", ss.season(date(2026, 7, 1), -15.0), "winter")
eq("month gap wraps the year", ss.month_gap(12, 1), 1)
eq("month gap is symmetric", (ss.month_gap(2, 11), ss.month_gap(11, 2)), (3, 3))
eq("month gap of six", ss.month_gap(1, 7), 6)

# ── text ─────────────────────────────────────────────────────────────────────
eq("an artist link is reduced to the name",
   ss.clean_text('<a href="//commons.wikimedia.org/wiki/User:Podstawko" title="User:Podstawko">Podstawko</a>'),
   "Podstawko")
eq("entities and whitespace", ss.clean_text("Caf&eacute;\n  du   Monde"), "Café du Monde")
eq("capped with an ellipsis", ss.clean_text("x" * 50, cap=10), "xxxxxxxxx…")
eq("a file name becomes a title",
   ss._title_from_file("File:Bridge of the Gods October 2019 001.jpg"), "Bridge of the Gods October 2019")
eq("a leading number and a photographer go",
   ss._title_from_file("File:002 Jabiru feeding its babies Photo by Giles Laurent.jpg"),
   "Jabiru feeding its babies")
eq("a camera timestamp goes", ss.tidy_title("2012-03-21 23-20-56-circumpolaire"), "Circumpolaire")
eq("a date prefix goes", ss.tidy_title("20201019 Alte Brücke Saarbrücken"), "Alte Brücke Saarbrücken")
eq("a shoot's sequence number goes", ss.tidy_title("Seattle - September 2, 2023 - 80"), "Seattle - September 2, 2023")
eq("so does one after a comma", ss.tidy_title("Seattle metro, September 2023 - 081"), "Seattle metro, September 2023")
eq("a year at the end is not a sequence number", ss.tidy_title("Seattle skyline 2021"), "Seattle skyline 2021")
eq("a leading shoot number goes", ss.tidy_title("011 Jaguar drinking in Encontro das Águas"), "Jaguar drinking in Encontro das Águas")
eq("a leading street number stays", ss.tidy_title("1201 Third Avenue at dusk"), "1201 Third Avenue at dusk")
eq("a catalogue number is no caption", ss.tidy_title("Altenburg-310053-PSD"), "")
eq("a year in a real caption stays", ss.tidy_title("Fisherman's Restaurant and Bar, Seattle, 2023"),
   "Fisherman's Restaurant and Bar, Seattle, 2023")
eq("empty stays empty", ss.tidy_title(""), "")
eq("an aquarium is not scenery", ss.scenic("Clearfin lionfish at the Seattle Aquarium"), False)
eq("a skyline is", ss.scenic("Seattle skyline from Kerry Park"), True)
eq("'signal' is not 'sign'", ss.scenic("Signal Hill at dusk"), True)

# ── shape ────────────────────────────────────────────────────────────────────
eq("1080p fills a TV", ss.shape_ok(1920, 1080), True)
eq("3:2 fills a TV", ss.shape_ok(1920, 1280), True)
eq("portrait does not", ss.shape_ok(1080, 1920), False)
eq("a panorama strip does not", ss.shape_ok(1920, 219), False)
eq("too small", ss.shape_ok(1000, 600), False)
eq("square does not", ss.shape_ok(2000, 2000), False)


def _jpeg(w, h):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def _png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


eq("JPEG header", ss.image_size(_jpeg(1920, 1080)), (1920, 1080))
eq("progressive JPEG header",
   ss.image_size(_jpeg(1920, 1080).replace(b"\xff\xc0", b"\xff\xc2")), (1920, 1080))
eq("PNG header", ss.image_size(_png(1600, 900)), (1600, 900))
eq("an HTML error page is not an image", ss.image_size(b"<!DOCTYPE html><html>"), None)
eq("a truncated JPEG", ss.image_size(_jpeg(1920, 1080)[:12]), None)
eq("GIF is refused", ss.image_size(b"GIF89a" + b"\x00" * 20), None)
eq("cache name is stable", ss.file_name("bing:OHR.X"), ss.file_name("bing:OHR.X"))
eq("cache name matches the served pattern", bool(ss.FILE_RE.match(ss.file_name("x"))), True)
eq("cache names differ", ss.file_name("a") != ss.file_name("b"), True)

# ── Commons ──────────────────────────────────────────────────────────────────


def _cpage(title, w, h, when="2023-10-14 12:00:00", artist="<a href='x'>Ann</a>",
           lic="CC BY-SA 4.0", name=""):
    meta = {"DateTimeOriginal": {"value": when}, "Artist": {"value": artist},
            "LicenseShortName": {"value": lic}}
    if name:
        meta["ObjectName"] = {"value": name}
    return {"title": title, "imageinfo": [{
        "width": w, "height": h, "thumburl": "https://upload.wikimedia.org/x/1920px-a.jpg",
        "thumbwidth": 1920, "thumbheight": int(1920 * h / w), "extmetadata": meta}]}


cjs = {"query": {"pages": {
    "1": _cpage("File:Bridge of the Gods October 2019 001.jpg", 6000, 4000),
    "2": _cpage("File:Tall tower.jpg", 3000, 4500),
    "3": _cpage("File:Pano.jpg", 12000, 1300),
    "4": _cpage("File:Clearfin lionfish (Pterois radiata).JPG", 4000, 2667, name="Lionfish in the aquarium"),
    "5": _cpage("File:Kerry Park.jpg", 4000, 2667, when="Taken on 30 December", artist="", lic="",
                name="<div class='fn'>Kerry Park view</div>"),
}}}
got = ss.parse_commons(cjs, "city")
eq("only TV-shaped scenery survives", [p["title"] for p in got],
   ["Bridge of the Gods October 2019", "Kerry Park view"])
eq("credit is author, licence, source", got[0]["credit"], "Ann · CC BY-SA 4.0 · Wikimedia Commons")
eq("an unknown author still gets a credit line", got[1]["credit"], "Unknown author · Wikimedia Commons")
eq("the month is read from the capture date", got[0]["month"], 10)
eq("an unparseable date is month 0", got[1]["month"], 0)
eq("ids are source-prefixed", got[0]["id"], "commons:File:Bridge of the Gods October 2019 001.jpg")
eq("no pages", ss.parse_commons({"batchcomplete": ""}, "city"), [])
eq("search url asks for random order and 1920 thumbs",
   all(k in ss.commons_search_url("x") for k in ("gsrsort=random", "iiurlwidth=1920")), True)
eq("city ladder: quality scenery widening, then the season's words, then featured",
   [(q.split()[0].split(",")[0], seasonal) for q, seasonal in ss.city_searches(45.7, -121.5, "autumn")],
   [("nearcoord:15km", False), ("nearcoord:40km", False), ("nearcoord:80km", False),
    ("nearcoord:25km", True), ("nearcoord:60km", True), ("nearcoord:150km", False)])
eq("the seasonal search names the season", "foliage" in ss.city_searches(45.7, -121.5, "autumn")[3][0], True)
eq("'fall' alone is not autumn (Fall Freeride)", " fall " in ss.SEASON_TERMS["autumn"] + " ", False)
eq("tropics: no seasonal search", [x[1] for x in ss.city_searches(1.3, 103.8, "")], [False] * 4)
eq("a Flickr timestamp goes", ss.tidy_title("2016.10.12.103846 Autumn foliage 14th Avenue Seattle"),
   "Autumn foliage 14th Avenue Seattle")
eq("a source tail goes", ss.tidy_title("Autumn color - Flickr - CAJC, in the PNW"), "Autumn color")
eq("a Flickr id goes", ss.tidy_title("Fall Color along Cape Horn Trail (24490877053)"), "Fall Color along Cape Horn Trail")
eq("one shoot is one key", ss.series_key("Fall 2007 023"), ss.series_key("Fall 2007 024"))
eq("at most two of one shoot",
   [p["id"] for p in ss.diverse([{"id": i, "title": f"Maryhill Freeride {i}"} for i in range(5)]
                               + [{"id": 9, "title": ""}, {"id": 10, "title": ""}])],
   [0, 1, 9, 10])
eq("a restaurant is not scenery", ss.scenic("Jade Garden Restaurant, Seattle, 2024"), False)
eq("a bar is not", ss.scenic("Fisherman's Restaurant and Bar"), False)
eq("a sandbar is", ss.scenic("Sandbar at low tide"), True)
eq("nature names the season", "autumn" in ss.nature_search("autumn"), True)
eq("tropical nature is landscapes", "landscapes" in ss.nature_search(""), True)
eq("potd asks for each day", ss.potd_url(ss.recent_days(date(2026, 10, 22), 3)).count("Template%3APotd%2F"), 3)
eq("recent days run backwards", ss.recent_days(date(2026, 3, 1), 2), [date(2026, 3, 1), date(2026, 2, 28)])

# ── Bing ─────────────────────────────────────────────────────────────────────
bjs = {"images": [
    {"startdate": "20261022", "url": "/th?id=OHR.NaejangsanNP_EN-US7010419904_1920x1080.jpg&rf=LaDigue",
     "urlbase": "/th?id=OHR.NaejangsanNP_EN-US7010419904",
     "copyright": "Autumn in Naejangsan National Park, South Korea (© Twenty47studio/Getty Images)"},
    {"startdate": "20261021", "urlbase": "/th?id=OHR.VietnamBridge_EN-GB6907946681",
     "copyright": "Nguyen Hoang Bridge, Hue, Vietnam"},
    {"startdate": "bad", "urlbase": "/th?id=OHR.X_EN-US1"},
    {"startdate": "20261020", "urlbase": "/elsewhere/X"},
]}
got = ss.parse_bing(bjs)
eq("bing keeps the two real days", [p["id"] for p in got], ["bing:OHR.NaejangsanNP", "bing:OHR.VietnamBridge"])
eq("bing src is the 1080p rendition", got[0]["src"],
   "https://www.bing.com/th?id=OHR.NaejangsanNP_EN-US7010419904_1920x1080.jpg")
eq("bing caption and credit split", (got[0]["title"], got[0]["credit"]),
   ("Autumn in Naejangsan National Park, South Korea", "© Twenty47studio/Getty Images · Bing"))
eq("bing without a copyright", got[1]["credit"], "Bing")
eq("bing date", got[0]["date"], "2026-10-22")
eq("market from country", (ss.bing_market("de"), ss.bing_market("XX"), ss.bing_market("")),
   ("de-DE", "en-US", "en-US"))
eq("bing pages back two weeks", [u[-5:] for u in ss.bing_urls("en-US")], ["idx=0", "idx=7"])

# ── Pexels ───────────────────────────────────────────────────────────────────
pjs = {"photos": [
    {"id": 1, "width": 6000, "height": 4000, "photographer": "Jo Doe", "alt": "Portland skyline at dusk",
     "src": {"original": "https://images.pexels.com/photos/1/a.jpeg"}},
    {"id": 2, "width": 3000, "height": 4500, "photographer": "Al", "src": {"original": "https://p/2.jpeg"}},
]}
got = ss.parse_pexels(pjs)
eq("pexels keeps landscape only", [p["id"] for p in got], ["pexels:1"])
eq("pexels credit", got[0]["credit"], "Photo by Jo Doe on Pexels")
eq("pexels asks for a 1920 rendition", got[0]["src"].endswith("?auto=compress&cs=tinysrgb&w=1920"), True)
eq("pexels query names the region and season",
   ss.pexels_query({"name": "Portland", "admin1": "Oregon"}, "autumn"), "Portland Oregon autumn")
eq("pexels query in the tropics", ss.pexels_query({"name": "Singapore"}, ""), "Singapore")

# ── Library ──────────────────────────────────────────────────────────────────
got = ss.library_photos([("a", "Dune (2021)", "/abc.jpg"), ("b", "No art", ""),
                         ("c", "Dune again", "/abc.jpg"), ("d", "Bad", "/x/../y.jpg")],
                        "/api/metadata/img")
eq("library goes through the artwork proxy", [p["src"] for p in got], ["/api/metadata/img/original/abc.jpg"])
eq("library credits TMDb", got[0]["credit"], "Image: TMDb")

# ── choosing ─────────────────────────────────────────────────────────────────


def _p(i, month=0, d="", theme="city"):
    return {"id": f"x:{i}", "theme": theme, "src": "", "title": "", "credit": "c", "month": month, "date": d}


oct_ = date(2026, 10, 22)
many = [_p(i, 10) for i in range(15)] + [_p(100 + i, 4) for i in range(10)] + [_p(200, 0)]
got = ss.pick_city(many, oct_, "autumn", cap=20)
eq("in-season first", [p["month"] for p in got[:15]], [10] * 15)
eq("then known months nearest first, unknown last", got[15]["month"], 4)
few = [_p(1, 10), _p(2, 12), _p(3, 4), _p(4, 0), _p(5, 8)]
got = ss.pick_city(few, oct_, "autumn")
eq("too few in season → two months counts", [p["id"] for p in got[:3]], ["x:1", "x:2", "x:5"])
eq("unknown month comes last", got[-1]["id"], "x:4")
eq("tropics: as found", ss.pick_city(few, oct_, ""), few)

old = [_p(i, d=f"2026-10-{i:02d}", theme="daily") for i in range(1, 11)]
new = [_p(i, d=f"2026-10-{i:02d}", theme="daily") for i in range(8, 16)] + [_p(900 + i, theme="daily") for i in range(30)]
got = ss.merge_dated(old, new, cap=20)
eq("merged pool keeps the cap", len(got), 20)
eq("undated hold half", sum(1 for p in got if not p["date"]), 10)
eq("dated newest first", [p["date"][-2:] for p in got[:3]], ["15", "14", "13"])
eq("nothing new, old dated kept", [p["id"] for p in ss.merge_dated(old, [], cap=3)], ["x:10", "x:9", "x:8"])

pools = {"library": [_p(f"l{i}", theme="library") for i in range(6)],
         "daily": [_p(f"d{i}", theme="daily") for i in range(2)],
         "city": [_p(f"c{i}") for i in range(3)]}
pl = ss.playlist(pools, ["library", "daily", "city"], hidden={"x:l0"}, seed=5)
eq("every photo once, hidden left out", sorted(p["id"] for p in pl),
   sorted(["x:l1", "x:l2", "x:l3", "x:l4", "x:l5", "x:d0", "x:d1", "x:c0", "x:c1", "x:c2"]))
eq("themes are dealt round-robin", [p["theme"] for p in pl[:3]], ["library", "daily", "city"])
eq("a theme not ticked is not shown",
   {p["theme"] for p in ss.playlist(pools, ["daily"], (), 1)}, {"daily"})
eq("same day, same order", [p["id"] for p in ss.playlist(pools, ["library"], (), 7)],
   [p["id"] for p in ss.playlist(pools, ["library"], (), 7)])
eq("another day, another order",
   [p["id"] for p in ss.playlist(pools, ["library"], (), ss.day_seed(oct_))] !=
   [p["id"] for p in ss.playlist(pools, ["library"], (), ss.day_seed(date(2026, 10, 23)))], True)
eq("a duplicate across themes shows once",
   len(ss.playlist({"a": [_p(1)], "b": [_p(1)]}, ["a", "b"], (), 1)), 1)

eq("signature moves with the city",
   ss.signature(["city"], {"lat": 1, "lon": 2}, "autumn", False) !=
   ss.signature(["city"], {"lat": 1, "lon": 3}, "autumn", False), True)
eq("signature ignores theme order",
   ss.signature(["a", "b"], None, "", False), ss.signature(["b", "a"], None, "", False))
eq("signature moves with the season",
   ss.signature([], None, "autumn", False) != ss.signature([], None, "winter", False), True)
eq("themes catalogue matches the ids", ss.THEME_IDS, ("library", "daily", "city", "nature", "space"))
eq("defaults leave out the theme that needs a city", "city" in ss.DEFAULT_THEMES, False)

print("slideshow: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
