# PolicyPulse — Political & Trade News Tracker

A fast, clean news aggregator for journalists covering politics and policy.
Pulls RSS feeds from think tanks, trade associations, and advocacy orgs and
presents them in a reporter-friendly dashboard.

## Quick Start

```bash
cd news-tracker
pip install -r requirements.txt
python app.py
# → open http://localhost:5050
```

## Features

| Feature | Description |
|---|---|
| **Auto-classification** | Items tagged Breaking / Newsy / Evergreen based on keywords + age |
| **Tag filters** | Toggle Breaking, Newsy, and Evergreen on/off independently |
| **Org-type filters** | Filter by Think Tank, Trade Org, Advocacy, etc. |
| **Live search** | Instant full-text search across titles, summaries, and org names |
| **⭐ Bookmarks** | Star items for later; persisted in browser localStorage |
| **Copy button** | One click copies headline + source + date + URL + summary to clipboard |
| **Export** | Copies all visible items as a clean formatted text block |
| **Grid / Compact view** | Toggle between card grid and compact list |
| **30-min cache** | Feeds are cached so refreshing the page is fast; hit ↻ Refresh to force re-fetch |

## Adding or Removing Sources

Edit `feeds.json`. Each entry needs:

```json
{
  "name":  "Short display name",
  "url":   "https://example.org/feed/",
  "type":  "Think Tank",          ← becomes a filter pill
  "color": "#1a5276"              ← org badge background color
}
```

Most WordPress-based org sites expose a feed at `/feed/`. For others, look for
an RSS icon on the site or append `?format=rss` or `/rss.xml`.

## Included Sources

### Think Tanks
- Brookings Institution
- Heritage Foundation
- Center for American Progress
- Cato Institute
- Economic Policy Institute
- Urban Institute
- Bipartisan Policy Center
- Third Way
- Center on Budget & Policy Priorities

### Research
- Pew Research Center

### Trade Orgs
- U.S. Chamber of Commerce
- National Retail Federation
- NFIB (small business)
- American Farm Bureau

### Trade Unions
- AFL-CIO

### Advocacy
- ACLU
- AARP
- Sierra Club

## Notes

- Broken or unavailable feeds are silently skipped (check terminal output for warnings).
- The `tag` classification is heuristic — adjust `NEWSY` / `EVERGREEN` keyword sets
  in `app.py` to tune it for your beat.
- Cache TTL is 30 minutes. Change `CACHE_TTL` in `app.py` if you want faster updates.
# policy-pulse
