"""Dijital Ayak İzi measurements (v11_footprint): deterministic, offline (fake web)."""
import json
import socket
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import v11_footprint as fp
from v11_digital_karne import KarneError

TODAY = date(2026, 10, 16)
KEY = "AIza" + "x" * 35

HOME = b"""<html><head><title>Acme Yapi</title><meta name="description" content="Saglam yapi malzemeleri">
</head><body><h1>Saglam <b>yapi</b></h1>
<a href="https://www.instagram.com/acmeyapi/">IG</a><a href="https://www.youtube.com/@acmeyapi">YT</a>
<a href="https://www.linkedin.com/company/acme-yapi/">LI</a><a href="/iletisim">x</a></body></html>"""
SITEMAP = b"<urlset><url><lastmod>2026-09-30</lastmod></url><url><lastmod>2026-10-02T10:00:00Z</lastmod></url></urlset>"


def at(y, m, d, h=10):
    return f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:00:00Z"


class Web:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        if url == "https://acmeyapi.com.tr":
            return {"status": 200, "url": url, "body": HOME, "seconds": 1.2}
        if url.endswith("/sitemap.xml"):
            return {"status": 200, "url": url, "body": SITEMAP}
        raise KarneError("web_unreachable")

    def json(self, url, **kw):
        self.urls.append(url)
        if "/channels?" in url:
            assert "forHandle=%40acmeyapi" in url, url
            return {"items": [{"snippet": {"title": "Acme Yapı", "description": "Kanal"},
                               "statistics": {"subscriberCount": "1200", "videoCount": "40", "viewCount": "90000"},
                               "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        if "/playlistItems?" in url:
            return {"items": [{"contentDetails": {"videoId": "v1", "videoPublishedAt": at(2026, 10, 1)}},
                              {"contentDetails": {"videoId": "v2", "videoPublishedAt": at(2026, 6, 3)}},
                              {"contentDetails": {"videoId": "v0", "videoPublishedAt": at(2025, 1, 1)}}]}
        if "/videos?" in url:
            return {"items": [
                {"snippet": {"publishedAt": at(2026, 10, 1), "title": "Kısa #yapi", "tags": ["Yapı"]},
                 "contentDetails": {"duration": "PT45S"}, "statistics": {"likeCount": "10", "commentCount": "2",
                                                                         "viewCount": "500"}},
                {"snippet": {"publishedAt": at(2026, 6, 3), "title": "Uzun video", "tags": ["yapı", "beton"]},
                 "contentDetails": {"duration": "PT12M3S"}, "statistics": {"likeCount": "30", "viewCount": "1500"}}]}
        if "graph.facebook.com" in url:
            assert "username(acmeyapi)" in url, url
            return {"business_discovery": {"username": "acmeyapi", "name": "Acme", "biography": "Yapı",
                                           "followers_count": 1000, "media_count": 300, "media": {"data": [
                                               {"timestamp": "2026-10-10T09:00:00+0000", "media_type": "VIDEO",
                                                "media_product_type": "REELS", "caption": "Yeni #yapi #beton",
                                                "like_count": 40, "comments_count": 5},
                                               {"timestamp": "2026-08-01T18:00:00+0000", "media_type": "IMAGE",
                                                "media_product_type": "FEED", "caption": "Ürün", "like_count": 20,
                                                "comments_count": 0}]}}}
        raise KarneError("web_http_404")


class Footprint(unittest.TestCase):
    def setUp(self):
        p = patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden"))
        p.start()
        self.addCleanup(p.stop)

    def test_post_metrics_rhythm_gaps_formats_and_engagement(self):
        posts = [{"at": datetime(2026, 5, 2, 9, tzinfo=timezone.utc), "format": "reels", "caption": "a #x #y",
                  "likes": 10, "comments": 0, "views": None},
                 {"at": datetime(2026, 9, 20, 9, tzinfo=timezone.utc), "format": "image", "caption": "b #x",
                  "likes": 30, "comments": 4, "views": None},
                 {"at": datetime(2026, 1, 1, tzinfo=timezone.utc), "format": "image", "caption": "eski"}]
        m = fp.post_metrics(posts, TODAY, followers=1000)
        self.assertEqual(m["posts_6m"], 2)  # the January post is outside the 6 months
        self.assertEqual(list(m["per_month"]), ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"])
        self.assertEqual(m["silent_months"], ["2026-06", "2026-07", "2026-08", "2026-10"])
        self.assertEqual((m["last_post"], m["longest_gap_days"]), ("2026-09-20", 141))
        self.assertEqual((m["formats"], m["avg_hashtags"], m["top_hashtags"]), ({"reels": 1, "image": 1}, 1.5,
                                                                                ["#x", "#y"]))
        self.assertEqual(m["engagement_rate_pct"], 2.2)

    def test_collect_measures_site_youtube_instagram_and_marks_linkedin_partial(self):
        web = Web()
        out = fp.collect("acmeyapi.com.tr", web, TODAY, youtube_key=KEY,
                         instagram={"ig_user_id": "1784000000", "token": "t" * 40})
        site = out["site"]
        self.assertEqual((site["title"], site["meta_description"], site["h1"], site["sitemap_latest"]),
                         ("Acme Yapi", "Saglam yapi malzemeleri", "Saglam yapi", "2026-10-02"))
        self.assertEqual(set(site["socials"]), {"instagram", "youtube", "linkedin"})
        yt = out["channels"]["youtube"]
        self.assertEqual((yt["measured"], yt["followers"], yt["posts_6m"], yt["formats"]),
                         (True, 1200, 2, {"short": 1, "video": 1}))
        self.assertEqual(yt["top_tags"][0], "yapı")
        ig = out["channels"]["instagram"]
        self.assertEqual((ig["measured"], ig["posts_6m"], ig["formats"]), (True, 2, {"reels": 1, "image": 1}))
        self.assertEqual(out["channels"]["linkedin"], {"measured": False, "url": "https://www.linkedin.com/company/acme-yapi/",
                                                       "handle": "acme-yapi", "reason": "resmi_okuma_yolu_yok"})
        json.dumps(out)  # storable as is

    def test_missing_credentials_and_errors_never_stop_the_others_or_leak_the_key(self):
        out = fp.collect("acmeyapi.com.tr", Web(), TODAY, youtube_key=None, instagram=None)
        self.assertEqual(out["channels"]["youtube"]["reason"], "youtube_key_missing")
        self.assertEqual(out["channels"]["instagram"]["reason"], "instagram_setup_missing")
        self.assertNotIn(KEY, json.dumps(out))
        dead = fp.collect("yok.com.tr", Web(), TODAY)
        self.assertEqual((dead["site"], dead["channels"], dead["notes"]), (None, {}, ["site: web_unreachable"]))

    def test_secret_loaders_accept_only_well_formed_values(self):
        class S:
            def __init__(self, v):
                self.v = v

            def access(self, name):
                if self.v is None:
                    raise KeyError(name)
                return self.v
        self.assertEqual(fp.load_youtube_key(S(KEY.encode())), KEY)
        self.assertIsNone(fp.load_youtube_key(S(b"nope")))
        self.assertIsNone(fp.load_youtube_key(S(None)))
        good = {"ig_user_id": "17841400000", "token": "t" * 40}
        self.assertEqual(fp.load_instagram(S(json.dumps(good).encode())), good)
        self.assertIsNone(fp.load_instagram(S(b'{"ig_user_id": "x", "token": "t"}')))


if __name__ == "__main__":
    unittest.main()
