import json
import os
import re
import time
import hashlib
import warnings
import threading
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime as _rfc2822
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# ── Feed configuration (absolute path so Render/gunicorn can find it) ─────────
BASE_DIR = Path(__file__).parent
with open(BASE_DIR / "feeds.json") as f:
    FEED_CONFIG = json.load(f)

# ── Simple in-memory cache (30 min TTL) ───────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
_fetch_lock = threading.Lock()   # ensures only one feed-fetch runs at a time
CACHE_TTL = 1800


def _get_cache(key):
    with _cache_lock:
        if key in _cache:
            data, ts = _cache[key]
            if time.time() - ts < CACHE_TTL:
                return data
    return None


def _set_cache(key, data):
    with _cache_lock:
        _cache[key] = (data, time.time())


# ── Content classification ─────────────────────────────────────────────────────
NEWSY = {
    "announces", "announced", "launches", "releases", "signs", "signed",
    "passes", "passed", "approves", "approved", "votes", "voted", "urges",
    "calls for", "calls on", "responds", "reaction", "statement", "hearing",
    "testimony", "testifies", "bill", "legislation", "amendment", "rule",
    "regulation", "executive order", "lawsuit", "sues", "files suit",
    "issues warning", "alert", "breaking", "proposes", "proposed",
    "deadline", "new policy", "new rule", "confirms", "rejects", "blocks",
    "veto", "vetoes", "impeach", "resign", "resigns", "indicts", "charges",
}

EVERGREEN = {
    "guide", "how to", "fact sheet", "factsheet", "explainer", "overview",
    "introduction", "basics", "understanding", "what is", "why does",
    "history of", "background", "primer", "everything you need", "toolkit",
    "resources", "best practices", "framework", "lessons learned",
    "frequently asked", "faq", "101", "explained", "deep dive", "analysis of",
}


def classify(title: str, days_old: int) -> str:
    low = title.lower()
    newsy_hits = sum(1 for w in NEWSY if w in low)
    eg_hits = sum(1 for w in EVERGREEN if w in low)
    if days_old == 0 and newsy_hits > 0:
        return "breaking"
    if days_old <= 3 and newsy_hits >= eg_hits:
        return "newsy"
    if eg_hits > newsy_hits:
        return "evergreen"
    return "newsy" if days_old <= 7 else "evergreen"


# ── Feed fetching ─────────────────────────────────────────────────────────────
_HEADERS = {
    "User-Agent": "feedparser/6.0 +https://github.com/kurtmckee/feedparser",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _make_id(link: str, title: str) -> str:
    raw = (link or title).encode("utf-8", errors="replace")
    return hashlib.md5(raw).hexdigest()[:12]


def _parse_date(entry) -> datetime | None:
    # 1. feedparser pre-parsed struct_time (already normalised to UTC)
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime(*val[:6])
            except Exception:
                pass
    # 2. raw date strings — handles formats feedparser couldn't parse
    for field in ("published", "updated", "created"):
        raw = getattr(entry, field, None) or entry.get(field, "")
        if raw:
            try:
                return _rfc2822(raw).replace(tzinfo=None)
            except Exception:
                pass
    return None


def _fetch_one(cfg: dict, now: datetime) -> list[dict]:
    """Fetch and parse a single feed. Returns a list of item dicts."""
    import feedparser
    import requests as _req

    items = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            resp = _req.get(
                cfg["url"], headers=_HEADERS,
                timeout=10, verify=False, allow_redirects=True,
            )
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:
            pub = _parse_date(entry)
            if pub is None or pub > now:          # missing or future date
                pub = now
            pub = max(pub, now - timedelta(days=730))  # clamp absurdly old dates
            days_old = (now - pub).days
            title = _strip_html(entry.get("title", "Untitled"))
            link = entry.get("link", "#")
            raw = entry.get("summary") or entry.get("description") or ""
            summary = _strip_html(raw)
            if len(summary) > 380:
                summary = summary[:380].rsplit(" ", 1)[0] + "…"
            items.append({
                "id":           _make_id(link, title),
                "title":        title,
                "link":         link,
                "org":          cfg["name"],
                "org_type":     cfg["type"],
                "org_color":    cfg.get("color", "#4a5568"),
                "summary":      summary,
                "published":    pub.strftime("%b %d, %Y"),
                "published_ts": pub.timestamp(),
                "days_old":     days_old,
                "tag":          classify(title, days_old),
            })
    except Exception as exc:
        print(f"[warn] {cfg['name']}: {exc}")
    return items


def fetch_all() -> list[dict]:
    cached = _get_cache("items")
    if cached is not None:
        return cached

    # Only one fetch at a time — any concurrent caller waits then hits cache
    with _fetch_lock:
        cached = _get_cache("items")   # re-check after acquiring lock
        if cached is not None:
            return cached

        now = datetime.utcnow()
        results = []

        # Fetch all feeds in parallel — cap at 10 workers for free-tier stability
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_one, cfg, now): cfg for cfg in FEED_CONFIG}
            for future in as_completed(futures):
                results.extend(future.result())

        results.sort(key=lambda x: x["published_ts"], reverse=True)
        _set_cache("items", results)
        return results


# ── Background cache warm-up on startup ───────────────────────────────────────
def _warm_cache():
    print("[startup] Pre-fetching feeds in background…")
    fetch_all()
    print("[startup] Cache warm-up complete.")

threading.Thread(target=_warm_cache, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", feed_config=FEED_CONFIG)


@app.route("/api/items")
def api_items():
    items = fetch_all()
    org_types = sorted({i["org_type"] for i in items})
    return jsonify({
        "items":      items,
        "org_types":  org_types,
        "fetched_at": datetime.now().strftime("%b %d, %Y at %I:%M %p"),
        "count":      len(items),
        "sources":    len(FEED_CONFIG),
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    with _cache_lock:
        _cache.clear()
    items = fetch_all()
    return jsonify({
        "status":     "ok",
        "count":      len(items),
        "fetched_at": datetime.now().strftime("%b %d, %Y at %I:%M %p"),
    })


@app.route("/api/probe-feed")
def api_probe_feed():
    """Validate a URL as an RSS feed, with HTML auto-discovery fallback."""
    import feedparser
    import requests as _req

    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL provided"})
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    now = datetime.utcnow()

    def try_parse(feed_url):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = _req.get(feed_url, headers=_HEADERS, timeout=10, verify=False, allow_redirects=True)
        return feedparser.parse(r.content), r

    try:
        feed, resp = try_parse(url)
        feed_url = url

        if not feed.entries:
            # Auto-discover RSS/Atom link from HTML
            html = resp.content.decode("utf-8", errors="replace")
            discovered = False
            for pattern in [
                r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
                r'<link[^>]+href=["\']([^"\']+)["\'][^>]*type=["\']application/(?:rss|atom)\+xml["\']',
            ]:
                for m in re.finditer(pattern, html, re.IGNORECASE):
                    candidate = urljoin(url, m.group(1))
                    feed, _ = try_parse(candidate)
                    if feed.entries:
                        feed_url = candidate
                        discovered = True
                        break
                if discovered:
                    break
            if not discovered:
                return jsonify({"ok": False, "error": "No RSS feed found at this URL"})

        feed_name = _strip_html(getattr(feed.feed, "title", "") or "") or url
        sample = []
        for entry in feed.entries[:3]:
            pub = _parse_date(entry)
            if pub is None or pub > now:
                pub = now
            sample.append({
                "title":     _strip_html(entry.get("title", "Untitled")),
                "link":      entry.get("link", "#"),
                "published": pub.strftime("%b %d, %Y"),
            })

        return jsonify({
            "ok":       True,
            "feed_url": feed_url,
            "name":     feed_name,
            "count":    len(feed.entries),
            "sample":   sample,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/custom-fetch", methods=["POST"])
def api_custom_fetch():
    """Fetch items from caller-supplied feed configs (user-added sources)."""
    body = request.get_json(force=True, silent=True) or {}
    feeds = body.get("feeds", [])
    if not feeds:
        return jsonify({"items": []})

    now = datetime.utcnow()
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_fetch_one, cfg, now) for cfg in feeds]
        for future in as_completed(futures):
            results.extend(future.result())

    results.sort(key=lambda x: x["published_ts"], reverse=True)
    return jsonify({"items": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"PolicyPulse starting on http://localhost:{port}")
    app.run(debug=debug, port=port, use_reloader=False)
