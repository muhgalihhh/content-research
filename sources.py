import calendar, datetime, html, os, re, time
import feedparser, httpx

USER_AGENT = "xharp-content-research-bot/1.0 (+https://xharp.dev)"
MAX_AGE_DAYS = 7  # item RSS lebih tua dari ini dibuang -- feed yang jarang update otomatis nggak nyumbang item basi

RSS_FEEDS = [
    # agregator & komunitas dev (umum)
    "https://hnrss.org/frontpage?points=100",
    "https://www.reddit.com/r/programming/.rss",
    "https://dev.to/feed",
    # tutorial & web dev (pillar: Tech & Tutorial)
    "https://css-tricks.com/feed/",
    "https://laravel-news.com/feed",
    "https://nextjs.org/feed.xml",
    "https://github.blog/changelog/feed/",
    # tips, rekomendasi tools & AI assistant yang lebih ringan/non-teknis (pillar: Tech & Tutorial)
    "https://www.makeuseof.com/feed/",
    "https://www.freecodecamp.org/news/rss/",
    "https://zapier.com/blog/feed/",
    "https://www.producthunt.com/feed",
    "https://simonwillison.net/atom/everything/",
    # design & UI/UX (pillar: Design & UI/UX)
    "https://www.smashingmagazine.com/feed/",
    "https://uxdesign.cc/feed",
    "https://sidebar.io/feed.xml",
    # berita industri IT non-teknis (pillar: IT & Tech Industry)
    "https://www.zdnet.com/news/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    # konteks Indonesia (pillar: IT & Tech Industry)
    "https://dailysocial.id/feed",
    "https://news.google.com/rss/search?q=startup+teknologi+when:7d&hl=id&gl=ID&ceid=ID:id",
]

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


def _strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _entry_time(e):
    ts = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(ts) if ts else 0


def fetch_rss(limit_per_feed=8, timeout=15, max_age_days=MAX_AGE_DAYS):
    cutoff = time.time() - max_age_days * 86400
    items = []
    for url in RSS_FEEDS:
        try:
            resp = httpx.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"[sources] gagal fetch RSS {url}: {exc}")
            continue
        # item tanpa tanggal ikut dibuang: nggak bisa dipastikan fresh
        fresh = [e for e in feed.entries if _entry_time(e) >= cutoff and e.get("link")]
        if not fresh:
            print(f"[sources] {url}: 0 item dalam {max_age_days} hari terakhir (feed jarang update?)")
        for e in fresh[:limit_per_feed]:
            # reddit link post: pakai link artikel aslinya, bukan thread reddit (biar isinya bisa di-fetch)
            ext = re.search(r'<a href="([^"]+)">\[link\]</a>', e.get("summary", ""))
            items.append({
                "title": e.get("title", ""),
                "link": ext.group(1) if ext else e.link,
                "published": time.strftime("%Y-%m-%d", time.gmtime(_entry_time(e))),
                "summary": _strip_html(e.get("summary", ""))[:400],
            })
    print(f"[sources] fetched {len(items)} item RSS fresh dari {len(RSS_FEEDS)} feed")
    return items


def fetch_article(url, timeout=15, max_chars=6000):
    """Teks artikel dari isi tag <p>, supaya copy ditulis berdasarkan isi artikel, bukan cuma judul.
    Gagal / halaman JS-only (mis. link Google News) -> "" dan caller fallback ke summary RSS."""
    # ponytail: regex <p> cukup buat blog/news biasa; ganti ke trafilatura kalau hasilnya banyak noise
    try:
        r = httpx.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        r.raise_for_status()
    except Exception as exc:
        print(f"[sources] gagal fetch artikel {url}: {exc}")
        return ""
    paras = (_strip_html(p) for p in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", r.text, re.S | re.I))
    return "\n".join(dict.fromkeys(p for p in paras if len(p) > 40))[:max_chars]  # dict.fromkeys: buang paragraf dobel


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
        "published": x["created_at"][:10],
        "summary": x.get("description") or "",
    } for x in r.json().get("items", [])[:limit]]
    print(f"[sources] fetched {len(items)} repo trending GitHub (created since {since})")
    return items
