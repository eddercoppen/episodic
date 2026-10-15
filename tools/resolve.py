"""Resolve catalog episodes to show artwork, feed, and playable audio.

Input: data/era_*.json (show, title, year, genre, why, sources)
Output: data/catalog.json with added fields:
  feedUrl, artwork, appleUrl, audioUrl, pubDate, durationSec, matchedTitle, matchScore, status
"""
import glob
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
APP = os.path.join(HERE, "..", "app")

UA = {"User-Agent": "Mozilla/5.0 (podcast-catalog-resolver)"}
CACHE = {}


ITUNES_LOCK = threading.Lock()
_last = [0.0]


def get(url, timeout=25):
    if url in CACHE:
        return CACHE[url]
    is_itunes = "itunes.apple.com" in url
    for attempt in range(7):
        try:
            if is_itunes:
                with ITUNES_LOCK:  # Apple rate-limits: ~1 request per 1.2s
                    wait = 1.2 - (time.time() - _last[0])
                    if wait > 0:
                        time.sleep(wait)
                    _last[0] = time.time()
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            CACHE[url] = data
            return data
        except urllib.error.HTTPError as ex:
            if ex.code in (403, 429, 503) and attempt < 6:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as ex:
            if attempt < 2:
                time.sleep(2)
                continue
            raise


def tal_audio(title):
    """This American Life keeps its whole archive free on its site, though the podcast feed only has recent shows."""
    m = re.match(r"\s*(\d{1,4})\b", title)
    if not m:
        return None
    n = m.group(1)
    page = get(f"https://www.thisamericanlife.org/radio-archives/episode/{n}").decode("utf-8", "replace")
    a = re.search(r'https://www\.thisamericanlife\.org/sites/default/files/audio/' + n + r'/[^"\'\s]+?\.mp3', page)
    d = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page) or re.search(r'<span class="date-display-single"[^>]*>([^<]+)<', page)
    t = re.search(r'<h1[^>]*>([^<]+)</h1>', page)
    if not a:
        return None
    return {"audioUrl": a.group(0), "pubDate": d.group(1) if d else None,
            "matchedTitle": f"{n}: {html.unescape(t.group(1)).strip()}" if t else title}


def norm(s):
    s = html.unescape(s or "").lower()
    s = re.sub(r"[‘’“”]", "'", s)
    s = re.sub(r"^(#|ep\.?|episode)\s*\d+\s*[:\-–—|.]*\s*", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def num(s):
    m = re.search(r"(?:#|ep\.?\s*|episode\s*)(\d{1,4})\b", s or "", re.I) or re.match(r"\s*(\d{1,4})\s*[:\-–—.]", s or "")
    return m.group(1) if m else None


STOP = set("the a an of and to in on with for at by is it its from how what why who episode ep part pt".split())


def words(s):
    return {w for w in norm(s).split() if w not in STOP and len(w) > 1}


def sim(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    r = SequenceMatcher(None, na, nb).ratio()
    wa, wb = words(a), words(b)
    if len(wa) >= 2 and wa <= wb:  # every meaningful word of the requested title appears in the candidate
        r = max(r, 0.8)
    if na in nb or nb in na:
        r = max(r, 0.85)
    na_n, nb_n = num(a), num(b)
    if na_n and nb_n and na_n == nb_n:
        r = max(r, 0.8)
    return r


def find_show(show):
    q = urllib.parse.urlencode({"term": show, "media": "podcast", "entity": "podcast", "limit": 8})
    res = json.loads(get(f"https://itunes.apple.com/search?{q}"))["results"]
    if not res:
        return None
    best = max(res, key=lambda r: sim(show, r.get("collectionName", "")))
    if sim(show, best.get("collectionName", "")) < 0.6:
        return None
    return best


def parse_dur(d):
    if not d:
        return None
    d = d.strip()
    if d.isdigit():
        return int(d)
    parts = [int(p) for p in d.split(":") if p.isdigit()]
    t = 0
    for p in parts:
        t = t * 60 + p
    return t or None


NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


def feed_items(feed_url):
    raw = get(feed_url, timeout=40)
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        enc = it.find("enclosure")
        out.append({
            "title": (it.findtext("title") or "").strip(),
            "audioUrl": enc.get("url") if enc is not None else None,
            "pubDate": it.findtext("pubDate"),
            "durationSec": parse_dur(it.findtext("itunes:duration", namespaces=NS)),
        })
    return out


def episode_search(show, title, collection_id):
    q = urllib.parse.urlencode({"term": f"{show} {title}", "media": "podcast", "entity": "podcastEpisode", "limit": 25})
    res = json.loads(get(f"https://itunes.apple.com/search?{q}"))["results"]
    cands = [r for r in res if not collection_id or r.get("collectionId") == collection_id]
    if not cands:
        return None, 0
    best = max(cands, key=lambda r: sim(title, r.get("trackName", "")))
    return best, sim(title, best.get("trackName", ""))


def year_ok(pub, year):
    m = re.search(r"(19|20)\d{2}", pub or "")
    return (not m) or abs(int(m.group(0)) - year) <= 1


def resolve(e):
    e = dict(e)
    e["status"] = "unresolved"
    try:
        s = find_show(e["show"])
    except Exception as ex:
        e["error"] = f"show lookup: {ex}"
        return e
    if not s:
        e["status"] = "no_show"
        return e
    e["feedUrl"] = s.get("feedUrl")
    e["artwork"] = s.get("artworkUrl600") or s.get("artworkUrl100")
    e["appleShowUrl"] = s.get("collectionViewUrl")
    e["collectionId"] = s.get("collectionId")
    best, score = None, 0
    if e.get("feedUrl"):
        try:
            items = feed_items(e["feedUrl"])
            scored = [(sim(e["title"], i["title"]) + (0.05 if year_ok(i["pubDate"], e["year"]) else -0.15), i) for i in items if i["audioUrl"]]
            if scored:
                score, best = max(scored, key=lambda x: x[0])
        except Exception as ex:
            e["feedError"] = str(ex)[:120]
    if best and score >= 0.72:
        e.update({"audioUrl": best["audioUrl"], "pubDate": best["pubDate"], "durationSec": best["durationSec"],
                  "matchedTitle": best["title"], "matchScore": round(score, 2), "status": "feed"})
        return e
    if norm(e["show"]) == "this american life":
        try:
            t = tal_audio(e["title"])
            if t:
                e.update(t)
                e.update({"matchScore": 1.0, "status": "tal_site"})
                return e
        except Exception as ex:
            e["error"] = f"tal: {ex}"
    try:
        ep, sc = episode_search(e["show"], e["title"], e.get("collectionId"))
        if ep and sc >= 0.72:
            e.update({"audioUrl": ep.get("episodeUrl"), "pubDate": ep.get("releaseDate"),
                      "durationSec": (ep.get("trackTimeMillis") or 0) // 1000 or None,
                      "matchedTitle": ep.get("trackName"), "matchScore": round(sc, 2),
                      "appleEpisodeUrl": ep.get("trackViewUrl"), "status": "apple"})
            return e
    except Exception as ex:
        e["error"] = f"episode search: {ex}"
    if best:
        e["nearMiss"] = {"title": best["title"], "score": round(score, 2)}
    e["status"] = "not_in_feed"
    return e


def main():
    entries = []
    for f in sorted(glob.glob(os.path.join(DATA, "era_*.json"))):
        entries += json.load(open(f))
    # dedupe by show+title
    seen, uniq = set(), []
    for e in entries:
        k = (norm(e["show"]), norm(e["title"]))
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    # group by show so each feed downloads once
    shows = sorted({e["show"] for e in uniq})
    for sh in shows:  # warm show/feed cache serially per show, episodes in parallel overall
        pass
    with ThreadPoolExecutor(6) as ex:
        out = list(ex.map(resolve, uniq))
    for i, e in enumerate(out):
        e["id"] = f"e{i:03d}"
    json.dump(out, open(os.path.join(DATA, "catalog.json"), "w"), indent=1)
    from collections import Counter
    print(Counter(e["status"] for e in out))
    for e in out:
        if e["status"] not in ("feed", "apple", "tal_site"):
            print(f'  {e["status"]:12} {e["show"]} — {e["title"]}  {e.get("nearMiss", "")}')


if __name__ == "__main__":
    main()
