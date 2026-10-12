"""Günün özü box + follow-ups on yesterday's suggestions (Ahmet 2026-10-12 23:36Z): fakes only."""
from datetime import date, datetime

import v11_daily_digest as dd
from test_v11_daily_digest import ACC, APP, BASE, Base, config, todo

TUE_0916 = datetime.fromisoformat("2026-10-13T09:16:00+03:00")
ALTIN = 40704523


def spark(day, avoid=()):
    return {"opportunities": [{"kind": "client", "brand": "Altınmarka", "headline": "Rakip marka yeni kakao serisini duyurdu.",
                               "proposal": "Şunu önerebiliriz: tadım reels serisi.", "url": "https://example.org/firsat",
                               "date": "2026-10-10"}],
            "ideas": [{"brand": "Altınmarka", "hook": "Dünya Gıda Günü", "format": "carousel",
                       "idea": "Kakaonun tarladan sofraya yolculuğu.", "url": "https://example.org/gida", "date": "2026-10-16"},
                      {"brand": "WAT Mobilite", "hook": "Mini vlog akımı", "format": "Reels",
                       "idea": "Bir günlük şarj rotası vlogu.", "url": "https://example.org/vlog", "date": "2026-10-09"}],
            "inspirations": [{"title": "Sabah ritüeli serisi", "company": "Örnek Kahve",
                              "why": "Kullanıcı videoları Bikaldi'ye uyarlanabilir.", "url": "https://example.org/ilham",
                              "date": "2026-10-08"}]}


class Ozu(Base):
    def setUp(self):
        super().setUp()
        projects, _ = self.bc.pages[f"{BASE}/projects.json"]
        projects.append({"id": ALTIN, "name": "Altınmarka", "status": "active",
                         "dock": [{"name": "todoset", "id": 51, "enabled": True}]})
        self.bc.pages[f"{BASE}/buckets/{ALTIN}/todosets/51/todolists.json"] = ([{"id": 501}], {})
        self.bc.pages[f"{BASE}/buckets/{ALTIN}/todolists/501/todos.json"] = ([], {})
        rows, _ = self.bc.pages[f"{BASE}/buckets/2/todolists/201/todos.json"]
        rows.append(todo(2003, "Lansman planı", "2026-10-14", [(3, "Özge Yağmur")], created_at="2026-10-01T09:00:00Z"))
        self.bc.pages[f"{BASE}/buckets/2/recordings/2003/comments.json"] = (
            [{"created_at": "2026-10-05T10:00:00Z", "creator": {"name": "Özge"}, "content": "Taslak hazır."}], {})

    def build(self, day=date(2026, 10, 12), **kw):
        kw.setdefault("cards", self.cards)
        kw.setdefault("calendar", self.calendar)
        return dd.build_digest(self.reader(), day=day, day_note=None, **kw)

    def test_box_opens_the_mail_with_five_sourced_lines_and_is_stored(self):
        seen = []
        body, stats = self.build(spark_source=lambda day, avoid: seen.append(list(avoid)) or spark(day))
        lines = body.splitlines()
        box = lines[lines.index("GÜNÜN ÖZÜ") + 1:lines.index("GÜNÜN ÖZÜ") + 6]
        self.assertEqual([l.split(":")[0] for l in box], ["📌 Sayı", "⚠️ Risk", "💼 Fırsat", "💡 Fikir", "✨ İlham"])
        self.assertEqual(box[0], "📌 Sayı: Bugün 1 iş teslim, 3 iş gecikmiş.")
        self.assertEqual(box[1], f"⚠️ Risk: “Lansman planı” ({APP}/buckets/2/todos/2003) (WAT Mobilite) teslime 2 gün var, "
                                 "7 gündür hareket yok. @Özge bugün durum yazabilir mi?")
        self.assertIn("Rakip marka yeni kakao serisini duyurdu. (https://example.org/firsat) Şunu önerebiliriz: "
                      "tadım reels serisi. @Cansu", box[2])
        self.assertEqual(box[3], "💡 Fikir: Altınmarka × Dünya Gıda Günü (https://example.org/gida): Kakaonun tarladan "
                                 "sofraya yolculuğu. (carousel) @Esma")
        self.assertIn("Örnek Kahve: Sabah ritüeli serisi (https://example.org/ilham). Kullanıcı videoları", box[4])
        self.assertLess(lines.index("GÜNÜN ÖZÜ"), lines.index("BUGÜN"))
        self.assertIn("GÜNÜN HAMLESİ (çekime hazır fikirler)", body)
        self.assertIn("- WAT Mobilite × Mini vlog akımı (https://example.org/vlog): Bir günlük şarj rotası vlogu. (Reels) @Özge",
                      body)
        self.assertEqual(seen, [[]])
        self.assertEqual(stats["spark"], "2_ideas_1_opportunities_1_inspirations")
        self.assertEqual(stats["ozu"]["risk"]["id"], 2003)
        self.assertEqual([(x["brand"], x["project_id"], x["who"]) for x in stats["ozu"]["fikirler"]],
                         [("Altınmarka", ALTIN, "Esma"), ("WAT Mobilite", 40694338, "Özge")])
        self.assertEqual(stats["ozu"]["firsat"]["who"], "Cansu")
        html = stats["html"]
        self.assertIn("Günün özü", html)
        self.assertIn('<a href="https://example.org/gida"', html)
        self.assertLess(html.index("Günün özü"), html.index("📌 Bugün teslim ("))

    def test_missing_research_says_so_and_never_invents(self):
        def boom(day, avoid):
            raise RuntimeError("x")
        body, stats = self.build(spark_source=boom)
        self.assertTrue(stats["spark"].startswith("provider_failed"))
        for needle in (dd.NO_OPPORTUNITY, dd.NO_IDEA, dd.NO_INSPIRATION):
            self.assertIn(needle, body)
        self.assertNotIn("GÜNÜN HAMLESİ", body)
        trend = [{"title": "Mini vlog (TikTok)", "url": "https://example.org/t", "published_at": "2026-10-10",
                  "idea": "Altınmarka: Hazırlık anlarını 10 saniyede göster."}]
        body, _ = self.build(spark_source=boom, trend_source=lambda day: trend)
        self.assertIn("💡 Fikir: Altınmarka × Mini vlog (TikTok) (https://example.org/t): Hazırlık anlarını 10 saniyede "
                      "göster. @Esma", body)

    def test_risk_falls_back_to_fresh_red_overdue_item(self):
        self.bc.pages[f"{BASE}/buckets/2/recordings/2003/comments.json"] = (
            [{"created_at": "2026-10-11T10:00:00Z", "creator": {"name": "Özge"}, "content": "Revize geldi."}], {})
        def fake(model_input):
            return {"items": [{"ref": it["ref"], "status": "action_on_us", "reason": "Bizde.", "action": "Bitir."}
                              for it in model_input["items"]]}
        body, stats = self.build(classifier=fake)
        self.assertIn("aksiyon bizde: 2", body)
        self.assertIn("⚠️ Risk: “Lansman kartı” (https://3.basecamp.com/5898865/buckets/2/card_tables/cards/3001) "
                      "(WAT Mobilite) 4 gündür gecikmiş, son yorum 2 Ekim. @Özge bugün durum yazabilir mi?", body)

    def test_risk_prefers_live_client_work_over_stalled_own_work(self):
        self.bc.pages[f"{BASE}/buckets/2/recordings/2003/comments.json"] = (
            [{"created_at": "2026-10-11T10:00:00Z", "creator": {"name": "Özge"}, "content": "Revize geldi."}], {})
        items = self.reader().collect()["items"]
        overdue = [dict(t, days_overdue=d, hamle_assigned=False, status="action_on_us", comments=c)
                   for t, d, c in ((items[0], 13, [{"at": "2026-08-31"}]), (items[1], 7, [{"at": "2026-10-12"}]))]
        overdue[0]["project_id"], overdue[1]["project_id"] = 40704459, 43886681  # Gamechangers vs BWI
        risk = dd.pick_risk(self.reader(), [], overdue, date(2026, 10, 13))
        self.assertEqual((risk["id"], risk["days_overdue"]), (overdue[1]["id"], 7))

    def test_tuesday_follows_up_mondays_suggestions(self):
        nb = lambda day: {"status": "ok", "brands": [{"brand": "Hisense", "sector": "elektronik",
                                                      "reason": "Türkiye lansmanı.", "url": "https://example.org/h"}]}
        r = self.run_digest(spark_source=lambda day, avoid: spark(day), new_business_source=nb)
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertNotIn("ÖNERİLERİM NE OLDU", self.gmail.text())  # no Friday digest exists
        rows, _ = self.bc.pages[f"{BASE}/buckets/{ALTIN}/todolists/501/todos.json"]
        rows.append(todo(5001, "Dünya Gıda Günü carousel", "2026-10-16", [(4, "Esma Zeki")],
                         created_at="2026-10-12T14:00:00Z"))
        self.now = TUE_0916
        seen = []
        r = self.run_digest(spark_source=lambda day, avoid: seen.append(list(avoid)) or spark(day))
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual(seen, [["Altınmarka × Dünya Gıda Günü", "WAT Mobilite × Mini vlog akımı"]])
        text = self.gmail.text()
        section = text[text.index("DÜNKÜ ÖNERİLERİM NE OLDU?"):text.index("BUGÜN")]
        for needle in (f"- 👏 Altınmarka × Dünya Gıda Günü: “Dünya Gıda Günü carousel” ({APP}/buckets/{ALTIN}/todos/5001) "
                       "açılmış, harika!",
                       "- @Özge, dün önerdiğim WAT Mobilite × Mini vlog akımı (https://example.org/vlog) fikri için bir "
                       "gelişme var mı?",
                       "- @Cansu, dün paylaştığım Altınmarka fırsatı (https://example.org/firsat) için bir adım attık mı?",
                       f"- @Özge, dün riskli dediğim “Lansman planı” ({APP}/buckets/2/todos/2003) için durum nedir?",
                       "- @Cansu, dün New Business'ta önerdiğim Hisense için bir adım attık mı?", dd.FOLLOW_UP_HINT):
            self.assertIn(needle, section)
        self.assertIn("🔁 Dünkü önerilerim ne oldu?", self.gmail.html())
        self.assertEqual(self.ops.backend.data["daily-digest-20261013"]["stats"]["follow_ups"], 5)

    def test_closed_or_commented_risk_is_praised_and_monday_says_friday(self):
        previous = {"day": "2026-10-09", "new_business_brands": [],
                    "ozu": {"risk": {"item_kind": "todo", "id": 999, "project_id": 2, "title": "Eski iş",
                                     "app_url": f"{APP}/buckets/2/todos/999", "who": ["Berkay"]},
                            "firsat": None, "fikirler": []}}
        items = self.reader().collect()["items"]
        out = dd.follow_ups(previous, reader=self.reader(), items=items, day=date(2026, 10, 12))
        self.assertEqual(out["title"], "Cuma günkü önerilerim ne oldu?")
        self.assertEqual(out["rows"][0][0], "✅")
        self.assertIn("tamamlanmış olmalı. Teşekkürler @Berkay!", out["rows"][0][4])
        self.assertEqual(dd._upper_tr(out["title"]), "CUMA GÜNKÜ ÖNERİLERİM NE OLDU?")
        previous["ozu"]["risk"].update(id=3001, item_kind="card", title="Lansman kartı")
        self.bc.pages[f"{BASE}/buckets/2/recordings/3001/comments.json"] = (
            [{"created_at": "2026-10-10T10:00:00Z", "creator": {"name": "Özge"}, "content": "Gönderdim."}], {})
        out = dd.follow_ups(previous, reader=self.reader(), items=items, day=date(2026, 10, 12))
        self.assertEqual(out["rows"][0][0], "👍")
        self.assertIsNone(dd.follow_ups(None, reader=self.reader(), items=items, day=date(2026, 10, 12)))
