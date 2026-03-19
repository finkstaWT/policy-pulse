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
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as _futures_wait
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# ── Feed configuration (absolute path so Render/gunicorn can find it) ─────────
BASE_DIR = Path(__file__).parent
with open(BASE_DIR / "feeds.json") as f:
    FEED_CONFIG = json.load(f)

# ── Server-side custom feeds storage ──────────────────────────────────────────
# Stored in DATA_DIR/custom_feeds.json so feeds survive server restarts.
# Set DATA_DIR env var to a Railway Volume mount path for full persistence
# across redeployments.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
_CUSTOM_FEEDS_FILE = DATA_DIR / "custom_feeds.json"
_custom_feeds_lock = threading.Lock()


def _load_custom_feeds() -> list:
    try:
        if _CUSTOM_FEEDS_FILE.exists():
            with open(_CUSTOM_FEEDS_FILE) as f:
                return json.load(f)
    except Exception as e:
        print(f"[warn] Could not read custom feeds: {e}")
    return []


def _save_custom_feeds(feeds: list) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CUSTOM_FEEDS_FILE, "w") as f:
            json.dump(feeds, f, indent=2)
    except Exception as e:
        print(f"[warn] Could not save custom feeds: {e}")


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


def _run_fetch() -> list[dict]:
    """Execute one parallel round of feed fetching. No locking — callers manage that."""
    results = []
    try:
        now = datetime.utcnow()
        pool = ThreadPoolExecutor(max_workers=len(FEED_CONFIG))
        futures = [pool.submit(_fetch_one, cfg, now) for cfg in FEED_CONFIG]
        done, _ = _futures_wait(futures, timeout=25)
        for fut in done:
            try:
                results.extend(fut.result())
            except Exception:
                pass
        pool.shutdown(wait=False)
        results.sort(key=lambda x: x["published_ts"], reverse=True)
    except Exception as exc:
        print(f"[error] _run_fetch: {exc}")
    return results


def fetch_all() -> list[dict]:
    cached = _get_cache("items")
    if cached is not None:
        return cached

    # Only one fetch at a time — concurrent callers wait then hit cache
    with _fetch_lock:
        cached = _get_cache("items")
        if cached is not None:
            return cached
        results = _run_fetch()
        _set_cache("items", results)
        return results


# ── Background cache warm-up on startup ───────────────────────────────────────
def _warm_cache():
    """
    Pre-fetch all feeds without holding _fetch_lock, so a hang here can
    never block /api/refresh.  A hard-deadline timer thread guarantees the
    cache is set (possibly empty) within 35 s regardless of what happens
    inside _run_fetch — this is the ultimate escape hatch from warming:true.
    """
    print("[startup] Pre-fetching feeds in background…")

    def _backstop():
        if _get_cache("items") is None:
            print("[startup] Backstop: warm-up exceeded 35 s — setting empty cache")
            _set_cache("items", [])

    backstop = threading.Timer(35.0, _backstop)
    backstop.daemon = True
    backstop.start()

    try:
        results = _run_fetch()
        _set_cache("items", results)
        backstop.cancel()
        print(f"[startup] Warm-up complete: {len(results)} items")
        if not results:
            # Transient boot-time failure — retry once after a short pause
            print("[startup] 0 items, retrying in 8 s…")
            time.sleep(8)
            with _cache_lock:
                _cache.pop("items", None)
            results = _run_fetch()
            _set_cache("items", results)
            print(f"[startup] Retry complete: {len(results)} items")
    except Exception as exc:
        _set_cache("items", [])
        backstop.cancel()
        print(f"[startup] Warm-up failed: {exc}")

threading.Thread(target=_warm_cache, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", feed_config=FEED_CONFIG)


@app.route("/api/items")
def api_items():
    # Never block the worker — return whatever is in cache instantly.
    # If the cache is still cold (server just woke up), return warming=True
    # so the frontend knows to retry in ~35 s once the background fetch finishes.
    items = _get_cache("items")
    warming = items is None
    if warming:
        items = []
    org_types = sorted({i["org_type"] for i in items})
    return jsonify({
        "items":      items,
        "org_types":  org_types,
        "warming":    warming,
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


@app.route("/api/discover-feeds")
def api_discover_feeds():
    """
    Discover all RSS/Atom feeds available at a site.
    Accepts ?q= as either a full URL or bare domain (e.g. vox.com).
    Returns {"feeds": [{url, name, count, sample}, …]} — up to 6 results.
    """
    import feedparser as _fp
    import requests as _req

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"feeds": [], "error": "No URL or domain provided"})

    url = q if q.startswith(("http://", "https://")) else "https://" + q

    from urllib.parse import urlparse as _parse
    parsed = _parse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # --- Step 1: fetch the site's homepage and harvest <link> feed tags ---
    candidates = [url]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = _req.get(base, headers=_HEADERS, timeout=10,
                         verify=False, allow_redirects=True)
        html = r.content.decode("utf-8", errors="replace")
        for pattern in [
            r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]*type=["\']application/(?:rss|atom)\+xml["\']',
        ]:
            for m in re.finditer(pattern, html, re.IGNORECASE):
                candidates.append(urljoin(base, m.group(1)))
    except Exception:
        pass

    # --- Step 2: common feed path patterns to try on the base domain ---
    _FEED_PATHS = [
        "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
        "/feeds/all.atom.xml", "/index.xml", "/?feed=rss2",
        "/rss/feed.xml", "/news.rss", "/blog/feed", "/blog/rss.xml",
    ]
    for path in _FEED_PATHS:
        candidates.append(base + path)

    # Deduplicate while preserving order (HTML-discovered feeds first)
    seen_c: set = set()
    unique_candidates = []
    for c in candidates:
        if c not in seen_c:
            seen_c.add(c)
            unique_candidates.append(c)

    # --- Step 3: probe all candidates in parallel ---
    now = datetime.utcnow()

    def _probe_one(candidate: str):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = _req.get(candidate, headers=_HEADERS, timeout=8,
                             verify=False, allow_redirects=True)
            feed = _fp.parse(r.content)
            if not feed.entries:
                return None
            name = _strip_html(getattr(feed.feed, "title", "") or "") or candidate
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
            return {"url": candidate, "name": name,
                    "count": len(feed.entries), "sample": sample}
        except Exception:
            return None

    pool = ThreadPoolExecutor(max_workers=8)
    fut_map = {pool.submit(_probe_one, c): c for c in unique_candidates}
    done, _ = _futures_wait(list(fut_map.keys()), timeout=20)

    results = []
    seen_names: set = set()
    for fut in done:
        try:
            res = fut.result()
            if res and res["name"] not in seen_names:
                seen_names.add(res["name"])
                results.append(res)
        except Exception:
            pass
    pool.shutdown(wait=False)

    # Sort by item count (richer feeds first), cap at 6
    results.sort(key=lambda x: x["count"], reverse=True)

    # --- Step 4: Google News fallback when no direct feeds found ---
    # Google publishes a free RSS feed for any site: ?q=site:domain.com
    # This is a legitimate workaround for sites with no native RSS feed.
    if not results:
        netloc = parsed.netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        gnews_url = (
            f"https://news.google.com/rss/search"
            f"?q=site:{netloc}&hl=en-US&gl=US&ceid=US:en"
        )
        gnews_res = _probe_one(gnews_url)   # sync — only one URL to check
        if gnews_res:
            gnews_res["via_google"] = True
            gnews_res["name"] = netloc      # clean domain, not Google's title
            results.append(gnews_res)

    return jsonify({"feeds": results[:6]})


@app.route("/api/custom-feeds", methods=["GET"])
def api_get_custom_feeds():
    with _custom_feeds_lock:
        feeds = _load_custom_feeds()
    return jsonify({"feeds": feeds})


@app.route("/api/custom-feeds", methods=["POST"])
def api_add_custom_feed():
    feed = request.get_json(force=True, silent=True) or {}
    url = feed.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Missing URL"}), 400
    with _custom_feeds_lock:
        feeds = _load_custom_feeds()
        if any(f["url"] == url for f in feeds):
            return jsonify({"ok": True, "duplicate": True})
        feeds.append({
            "name":  feed.get("name", url),
            "url":   url,
            "type":  feed.get("type", "Media"),
            "color": feed.get("color", "#4a5568"),
        })
        _save_custom_feeds(feeds)
    return jsonify({"ok": True})


@app.route("/api/custom-feeds", methods=["PATCH"])
def api_update_custom_feed():
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Missing URL"}), 400
    with _custom_feeds_lock:
        feeds = _load_custom_feeds()
        for f in feeds:
            if f["url"] == url:
                if "name"  in body: f["name"]  = body["name"]
                if "type"  in body: f["type"]  = body["type"]
                if "color" in body: f["color"] = body["color"]
                break
        else:
            return jsonify({"ok": False, "error": "Feed not found"}), 404
        _save_custom_feeds(feeds)
    return jsonify({"ok": True})


@app.route("/api/custom-feeds", methods=["DELETE"])
def api_remove_custom_feed():
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Missing URL"}), 400
    with _custom_feeds_lock:
        feeds = _load_custom_feeds()
        feeds = [f for f in feeds if f["url"] != url]
        _save_custom_feeds(feeds)
    return jsonify({"ok": True})


@app.route("/api/custom-fetch", methods=["POST"])
def api_custom_fetch():
    """Fetch items from caller-supplied feed configs (user-added sources)."""
    body = request.get_json(force=True, silent=True) or {}
    feeds = body.get("feeds", [])
    if not feeds:
        return jsonify({"items": []})

    now = datetime.utcnow()
    results = []
    pool = ThreadPoolExecutor(max_workers=5)
    futures = [pool.submit(_fetch_one, cfg, now) for cfg in feeds]
    done, _ = _futures_wait(futures, timeout=30)
    for fut in done:
        try:
            results.extend(fut.result())
        except Exception:
            pass
    pool.shutdown(wait=False)

    results.sort(key=lambda x: x["published_ts"], reverse=True)
    return jsonify({"items": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"PolicyPulse starting on http://localhost:{port}")
    app.run(debug=debug, port=port, use_reloader=False)
