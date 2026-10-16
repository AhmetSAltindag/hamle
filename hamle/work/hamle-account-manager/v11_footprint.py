"""Dijital Ayak İzi (owner 2026-10-16): measured, free, public data for the research report, before any model.

The owner's ask: a digital-first report that opens with the brand's footprint: which social channels are active
and how regularly over the last 6 months, whether the channels are in balance, the site's freshness, and (for the
model to judge) the words the brand uses on each channel. Everything here is deterministic and costs no model call:

- site:      the homepage (title, meta description, h1, the social links it carries) and sitemap freshness.
- youtube:   YouTube Data API v3 (secret hamle-youtube-api-key): channel stats + every upload of the last 6 months.
- instagram: Instagram Graph API Business Discovery (secret hamle-instagram-graph, JSON {"ig_user_id", "token"} of
             the agency's own Instagram business account): public business accounts' last posts. Optional: without
             the secret the channel is "measured: false" and the report marks it as partial.
LinkedIn, TikTok and X have no official way to read another company's history: only the profile link found on the
site is passed on, and the model may add public facts it finds, marked as partial.

Nothing here writes anywhere. Errors become a short reason per channel; one channel never stops the others.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import quote, urljoin, urlsplit

MONTHS = 6
YT = "https://www.googleapis.com/youtube/v3"
GRAPH = "https://graph.facebook.com/v21.0"
YOUTUBE_SECRET = "hamle-youtube-api-key"
INSTAGRAM_SECRET = "hamle-instagram-graph"
HASHTAG = re.compile(r"(?<!\w)#\w+", re.U)
DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class FootprintError(ValueError):
    pass


def _need(ok, code):
    if not ok:
        raise FootprintError(code)


def _code(exc):
    return str(getattr(exc, "code", None) or (exc.args[0] if exc.args else type(exc).__name__))[:80]


def _clip(text, n):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


# ------------------------------------------------------------------ shared metrics
def month_keys(today, months=MONTHS):
    """['2026-05', ..., '2026-10'] for the last `months` calendar months including the current one."""
    y, m, out = today.year, today.month, []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def post_metrics(posts, today, *, followers=None, tz=timezone.utc):
    """posts: [{'at': aware datetime, 'format': str, 'caption': str, 'likes': int|None, 'comments': int|None,
    'views': int|None}] -> rhythm, gaps, format mix, caption habits, posting hours, engagement."""
    since = datetime(today.year, today.month, 1, tzinfo=tz)
    for _ in range(MONTHS - 1):
        since = (since - timedelta(days=1)).replace(day=1)
    rows = sorted((p for p in posts if p["at"] >= since), key=lambda p: p["at"])
    months = {k: 0 for k in month_keys(today)}
    for p in rows:
        k = p["at"].astimezone(tz).strftime("%Y-%m")
        if k in months:
            months[k] += 1
    end = datetime(today.year, today.month, today.day, 23, 59, tzinfo=tz)
    stamps = [since] + [p["at"] for p in rows] + [end]
    gaps = [(b - a).days for a, b in zip(stamps, stamps[1:])]
    caps = [p.get("caption") or "" for p in rows]
    tags = [len(HASHTAG.findall(c)) for c in caps]
    hours = Counter(p["at"].astimezone(tz).hour for p in rows)
    likes = [p["likes"] for p in rows if isinstance(p.get("likes"), int)]
    comments = [p["comments"] for p in rows if isinstance(p.get("comments"), int)]
    views = [p["views"] for p in rows if isinstance(p.get("views"), int)]
    out = {"posts_6m": len(rows), "per_month": months,
           "avg_per_month": round(len(rows) / MONTHS, 1),
           "last_post": rows[-1]["at"].astimezone(tz).date().isoformat() if rows else None,
           "longest_gap_days": max(gaps) if rows else None,
           "silent_months": [k for k, v in months.items() if v == 0],
           "formats": dict(Counter(p.get("format") or "other" for p in rows)),
           "avg_caption_chars": round(sum(len(c) for c in caps) / len(caps)) if caps else None,
           "avg_hashtags": round(sum(tags) / len(tags), 1) if tags else None,
           "top_hours": [h for h, _ in hours.most_common(3)],
           "avg_likes": round(sum(likes) / len(likes)) if likes else None,
           "avg_comments": round(sum(comments) / len(comments), 1) if comments else None,
           "avg_views": round(sum(views) / len(views)) if views else None,
           "top_hashtags": [t for t, _ in Counter(h.casefold() for c in caps for h in HASHTAG.findall(c))
                            .most_common(10)]}
    if followers and likes:
        out["engagement_rate_pct"] = round(100 * (sum(likes) + sum(comments)) / len(likes) / followers, 2)
    return out


# ------------------------------------------------------------------ site
def site_profile(domain, web):
    """Homepage title/meta/h1, the social profile links on it, and the newest sitemap date."""
    from v11_digital_karne import _Page, social_of
    page = web.get("https://" + domain, max_bytes=1_500_000)
    _need(page["status"] == 200, f"site_http_{page['status']}")
    html = page["body"].decode("utf-8", "replace")
    p = _Page()
    p.feed(html)
    p.close()
    meta = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', html, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']', html, re.I)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    socials = {}
    for href, _ in p.links:
        s = social_of(urljoin(page["url"], href or ""))
        if s and s[1] and s[0] not in socials:
            socials[s[0]] = {"url": urljoin(page["url"], href), "handle": s[1]}
    out = {"url": page["url"], "title": _clip(p.title, 200), "meta_description": _clip(meta and meta.group(1), 300),
           "h1": _clip(re.sub(r"<[^>]+>", " ", h1.group(1)) if h1 else "", 200), "socials": socials,
           "load_seconds": page.get("seconds"), "sitemap_latest": None}
    try:
        sm = web.get("https://" + domain + "/sitemap.xml", max_bytes=3_000_000)
        if sm["status"] == 200:
            dates = re.findall(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})", sm["body"].decode("utf-8", "replace"))
            out["sitemap_latest"] = max(dates) if dates else None
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ YouTube (official API, free quota)
def _seconds(iso):
    m = DURATION.fullmatch(iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def youtube_profile(link, key, web, today):
    """Channel stats + the last 6 months of uploads (shorts <= 180 s) for a channel link from the site."""
    _need(bool(key), "youtube_key_missing")
    path = [s for s in urlsplit(link).path.split("/") if s]
    if path and path[0] == "channel" and len(path) > 1:
        q = "id=" + quote(path[1])
    elif path and path[0].startswith("@"):
        q = "forHandle=" + quote(path[0])
    elif path and path[0] in ("c", "user") and len(path) > 1:
        q = ("forUsername=" if path[0] == "user" else "forHandle=@") + quote(path[1])
    else:
        _need(bool(path), "youtube_link_unreadable")
        q = "forHandle=@" + quote(path[0])
    ch = web.json(f"{YT}/channels?part=snippet,statistics,contentDetails&{q}&key={key}")
    items = ch.get("items") or []
    _need(len(items) == 1, "youtube_channel_not_found")
    c = items[0]
    stats, uploads = c.get("statistics") or {}, ((c.get("contentDetails") or {}).get("relatedPlaylists") or {})
    since = datetime(today.year, today.month, 1, tzinfo=timezone.utc) - timedelta(days=31 * (MONTHS - 1))
    ids, token = [], ""
    for _ in range(4):  # at most 200 uploads
        pl = web.json(f"{YT}/playlistItems?part=contentDetails&maxResults=50&playlistId="
                      f"{quote(uploads.get('uploads') or '')}&key={key}" + (f"&pageToken={token}" if token else ""))
        rows = pl.get("items") or []
        fresh = [r["contentDetails"]["videoId"] for r in rows
                 if datetime.fromisoformat(r["contentDetails"].get("videoPublishedAt", "1970-01-01T00:00:00+00:00")
                                           .replace("Z", "+00:00")) >= since]
        ids += fresh
        token = pl.get("nextPageToken") or ""
        if not token or len(fresh) < len(rows):
            break
    posts, titles, tags = [], [], Counter()
    for i in range(0, len(ids), 50):
        vs = web.json(f"{YT}/videos?part=snippet,contentDetails,statistics&id={','.join(ids[i:i + 50])}&key={key}")
        for v in vs.get("items") or []:
            sn, st = v.get("snippet") or {}, v.get("statistics") or {}
            secs = _seconds((v.get("contentDetails") or {}).get("duration"))
            posts.append({"at": datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")),
                          "format": "short" if secs is not None and secs <= 180 else "video",
                          "caption": (sn.get("title") or "") + "\n" + (sn.get("description") or "")[:500],
                          "likes": int(st["likeCount"]) if "likeCount" in st else None,
                          "comments": int(st["commentCount"]) if "commentCount" in st else None,
                          "views": int(st["viewCount"]) if "viewCount" in st else None})
            titles.append(_clip(sn.get("title"), 120))
            tags.update(t.casefold() for t in sn.get("tags") or [])
    subs = int(stats["subscriberCount"]) if stats.get("subscriberCount") else None
    return {"measured": True, "url": link, "name": _clip((c.get("snippet") or {}).get("title"), 120),
            "about": _clip((c.get("snippet") or {}).get("description"), 600), "followers": subs,
            "total_videos": int(stats.get("videoCount") or 0), "total_views": int(stats.get("viewCount") or 0),
            "recent_titles": titles[:12], "top_tags": [t for t, _ in tags.most_common(12)],
            **post_metrics(posts, today, followers=subs)}


# ------------------------------------------------------------------ Instagram (official Business Discovery)
def instagram_profile(username, creds, web, today):
    """Public business/creator account via the agency's own IG business account (no login to theirs)."""
    _need(isinstance(creds, dict) and creds.get("ig_user_id") and creds.get("token"), "instagram_setup_missing")
    fields = ("business_discovery.username(" + quote(username) + "){username,name,biography,followers_count,"
              "media_count,media.limit(100){timestamp,media_type,media_product_type,caption,like_count,"
              "comments_count}}")
    data = web.json(f"{GRAPH}/{quote(str(creds['ig_user_id']))}?fields={fields}&access_token={creds['token']}")
    bd = data.get("business_discovery") or {}
    _need(bd.get("username"), "instagram_not_business_or_not_found")
    posts = []
    for m in ((bd.get("media") or {}).get("data") or []):
        kind = m.get("media_product_type") or m.get("media_type") or "other"
        posts.append({"at": datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")),
                      "format": {"REELS": "reels", "CAROUSEL_ALBUM": "carousel", "FEED": m.get("media_type", "post")
                                 .lower()}.get(kind, str(kind).lower()),
                      "caption": m.get("caption") or "", "likes": m.get("like_count"),
                      "comments": m.get("comments_count"), "views": None})
    f = bd.get("followers_count")
    return {"measured": True, "url": "https://www.instagram.com/" + bd["username"], "name": _clip(bd.get("name"), 120),
            "about": _clip(bd.get("biography"), 600), "followers": f, "total_posts": bd.get("media_count"),
            "recent_captions": [_clip(p["caption"], 160) for p in sorted(posts, key=lambda p: p["at"])[-8:]],
            **post_metrics(posts, today, followers=f)}


# ------------------------------------------------------------------ all of it
def collect(domain, web, today, *, youtube_key=None, instagram=None):
    """{'site', 'channels': {platform: {...}}, 'notes': [...]}; never raises."""
    out = {"site": None, "channels": {}, "notes": [], "measured_on": today.isoformat(), "months": MONTHS}
    try:
        out["site"] = site_profile(domain, web)
    except Exception as exc:
        out["notes"].append("site: " + _code(exc))
        return out
    socials = out["site"]["socials"]
    for platform, link in socials.items():
        row = {"measured": False, "url": link["url"], "handle": link["handle"]}
        try:
            if platform == "youtube":
                row = youtube_profile(link["url"], youtube_key, web, today)
            elif platform == "instagram":
                row = instagram_profile(link["handle"], instagram, web, today)
            else:
                row["reason"] = "resmi_okuma_yolu_yok"
        except Exception as exc:
            row["reason"] = _code(exc)
        out["channels"][platform] = row
    return out


def load_youtube_key(secrets):
    try:
        v = secrets.access(YOUTUBE_SECRET).decode("ascii").strip()
        return v if re.fullmatch(r"AIza[0-9A-Za-z_\-]{35}", v) else None
    except Exception:
        return None


def load_instagram(secrets):
    try:
        v = json.loads(secrets.access(INSTAGRAM_SECRET).decode("utf-8"))
        return v if isinstance(v, dict) and re.fullmatch(r"\d{5,30}", str(v.get("ig_user_id") or "")) \
            and isinstance(v.get("token"), str) and 20 <= len(v["token"]) <= 600 else None
    except Exception:
        return None
