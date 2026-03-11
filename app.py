import json
import re
import time
import hashlib
import warnings
from datetime import datetime
from flask import Flask, render_template, jsonify

app = Flask(__name__)

# ── Feed configuration ────────────────────────────────────────────────────────
with open("feeds.json") as f:
    FEED_CONFIG = json.load(f)

# ── Simple in-memory cache (30 min TTL) ───────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 1800


def _get_cache(key):
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
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
    """Return 'breaking', 'newsy', or 'evergreen' for an item."""
    low = title.lower()
    newsy_hits = sum(1 for w in NEWSY if w in low)
    eg_hits = sum(1 for w in EVERGREEN if w in low)

    if days_old == 0 and newsy_hits > 0:
        return "breaking"
    if days_old <= 3 and newsy_hits >= eg_hits:
        return "newsy"
    if eg_hits > newsy_hits:
        return "evergreen"
    # Default: recent = newsy, older = evergreen
    return "newsy" if days_old <= 7 else "evergreen"


# ── Feed fetching ─────────────────────────────────────────────────────────────
def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_id(link: str, title: str) -> str:
    raw = (link or title).encode("utf-8", errors="replace")
    return hashlib.md5(raw).hexdigest()[:12]


def _parse_date(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime(*val[:6])
            except Exception:
                pass
    return datetime.utcnow()


_HEADERS = {
    "User-Agent": "feedparser/6.0 +https://github.com/kurtmckee/feedparser",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def fetch_all() -> list[dict]:
    cached = _get_cache("items")
    if cached is not None:
        return cached

    try:
        import feedparser
        import requests as _requests
    except ImportError as exc:
        return [{"error": f"Missing dependency: {exc} — run: pip3 install feedparser requests"}]

    results = []
    now = datetime.utcnow()

    for cfg in FEED_CONFIG:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                resp = _requests.get(
                    cfg["url"], headers=_HEADERS,
                    timeout=12, verify=False, allow_redirects=True,
                )
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:20]:
                pub = _parse_date(entry)
                days_old = max(0, (now - pub).days)

                title = _strip_html(entry.get("title", "Untitled"))
                link = entry.get("link", "#")

                # Build summary: prefer content > summary > description
                raw_summary = (
                    entry.get("summary")
                    or entry.get("description")
                    or ""
                )
                summary = _strip_html(raw_summary)
                if len(summary) > 380:
                    summary = summary[:380].rsplit(" ", 1)[0] + "…"

                results.append({
                    "id": _make_id(link, title),
                    "title": title,
                    "link": link,
                    "org": cfg["name"],
                    "org_type": cfg["type"],
                    "org_color": cfg.get("color", "#4a5568"),
                    "summary": summary,
                    "published": pub.strftime("%b %d, %Y"),
                    "published_ts": pub.timestamp(),
                    "days_old": days_old,
                    "tag": classify(title, days_old),
                })
        except Exception as exc:
            print(f"[warn] {cfg['name']}: {exc}")

    results.sort(key=lambda x: x["published_ts"], reverse=True)
    _set_cache("items", results)
    return results


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", feed_config=FEED_CONFIG)


@app.route("/api/items")
def api_items():
    items = fetch_all()
    org_types = sorted({i["org_type"] for i in items})
    return jsonify({
        "items": items,
        "org_types": org_types,
        "fetched_at": datetime.now().strftime("%b %d, %Y at %I:%M %p"),
        "count": len(items),
        "sources": len(FEED_CONFIG),
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    _cache.clear()
    items = fetch_all()
    return jsonify({
        "status": "ok",
        "count": len(items),
        "fetched_at": datetime.now().strftime("%b %d, %Y at %I:%M %p"),
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"PolicyPulse starting on http://localhost:{port}")
    app.run(debug=debug, port=port, use_reloader=False)
