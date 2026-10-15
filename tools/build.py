"""Turn data/catalog.json (resolver output) + data/stations.json into app/catalog.json."""
import datetime
import html
import json
import os
import re
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
APP = os.path.join(HERE, "..", "app")

src = json.load(open(os.path.join(DATA, "catalog.json")))
stations = json.load(open(os.path.join(DATA, "stations.json"))) if os.path.exists(os.path.join(DATA, "stations.json")) else []
overrides = json.load(open(os.path.join(DATA, "overrides.json"))) if os.path.exists(os.path.join(DATA, "overrides.json")) else {}


def pub_year(d):
    if not d:
        return None
    try:
        return parsedate_to_datetime(d).year
    except Exception:
        m = re.search(r"(19|20)\d{2}", d)
        return int(m.group(0)) if m else None


HTTPS_OK = ("open.live.bbc.co.uk",)


def secure(u):
    """The app is served over https, so plain-http audio must be upgraded or it won't play."""
    if not u or u.startswith("https://"):
        return u
    host = u[7:].split("/")[0]
    return "https://" + u[7:] if host in HTTPS_OK else None


out = []
for e in src:
    key = f'{e["show"]} | {e["title"]}'
    e.update(overrides.get(key, {}))
    if e.get("drop"):
        continue
    ok = e["status"] in ("feed", "apple", "tal_site", "manual") and bool(secure(e.get("audioUrl")))
    py = pub_year(e.get("pubDate"))
    same_era = py is not None and abs(py - e["year"]) <= 1
    title = html.unescape((e.get("matchedTitle") if ok else None) or e["title"]).strip()
    out.append({
        "id": e["id"], "show": e["show"], "title": title, "year": e["year"], "genre": e["genre"], "why": e["why"],
        "sources": e.get("sources", [])[:4], "artwork": e.get("artwork"), "appleShowUrl": e.get("appleShowUrl"),
        "audioUrl": secure(e.get("audioUrl")) if ok else None,
        "pubDate": e.get("pubDate") if (ok and same_era) else None,
        "durationSec": e.get("durationSec") if ok else None,
    })

ids = {e["id"] for e in out}
for s in stations:
    s["ids"] = [i for i in s["ids"] if i in ids]
meta = {"built": datetime.date.today().isoformat(), "stations": [s for s in stations if s["ids"]]}
json.dump({"meta": meta, "episodes": out}, open(os.path.join(APP, "catalog.json"), "w"), ensure_ascii=False, separators=(",", ":"))
play = sum(1 for e in out if e["audioUrl"])
print(f"{len(out)} episodes, {play} playable, {len(meta['stations'])} curated stations")
