import datetime, os
import feedparser, httpx

USER_AGENT = "xharp-content-research-bot/1.0 (+https://xharp.dev)"

RSS_FEEDS = [
    "https://hnrss.org/frontpage",
    "https://www.reddit.com/r/programming/.rss",
    "https://dev.to/feed",
    "http://export.arxiv.org/rss/cs.AI",
    # tutorial & web dev (pillar: Tech & Tutorial)
    "https://css-tricks.com/feed/",
    "https://laravel-news.com/feed",
    "https://nextjs.org/feed.xml",
    # tips & rekomendasi tools yang lebih ringan/non-teknis (pillar: Tech & Tutorial)
    "https://www.makeuseof.com/category/programming/feed/",
    "https://www.freecodecamp.org/news/rss/",
    "https://zapier.com/blog/feed/",
    # design & UI/UX (pillar: Design & UI/UX)
    "https://www.smashingmagazine.com/feed/",
    "https://uxdesign.cc/feed",
    # berita industri IT non-teknis (pillar: IT & Tech Industry)
    "https://www.zdnet.com/news/rss.xml",
    "https://techcrunch.com/feed/",
]

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


def fetch_rss(limit_per_feed=8, timeout=15):
    items = []
    for url in RSS_FEEDS:
        try:
            resp = httpx.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"[sources] gagal fetch RSS {url}: {exc}")
            continue
        for e in feed.entries[:limit_per_feed]:
            items.append({
                "title": e.title,
                "link": e.link,
                "summary": getattr(e, "summary", "")[:400],
            })
    print(f"[sources] fetched {len(items)} item RSS dari {len(RSS_FEEDS)} feed")
    return items


def fetch_github_trending(timeout=20, since_days=7, min_stars=50, limit=10):
    """Approximates 'trending' via the official GitHub Search API:
    repos created in the last `since_days` days with at least `min_stars` stars, sorted by stars.
    since_days=1 + no star floor previously let 1-2 star repos (including SEO-spam) through --
    a wider window with a star floor filters out noise while staying on the official API
    (avoids fragile/ToS-risky scraping of github.com/trending)."""
    since = (datetime.date.today() - datetime.timedelta(days=since_days)).isoformat()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params = {"q": f"created:>{since} stars:>{min_stars}", "sort": "stars", "order": "desc", "per_page": limit}
    r = httpx.get(GITHUB_SEARCH_URL, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    items = [{
        "title": x["full_name"],
        "link": x["html_url"],
        "summary": x.get("description") or "",
    } for x in r.json().get("items", [])[:limit]]
    print(f"[sources] fetched {len(items)} repo trending GitHub (created since {since})")
    return items
