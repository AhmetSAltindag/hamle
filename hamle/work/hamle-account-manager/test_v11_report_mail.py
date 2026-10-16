"""The Dijital Ayak İzi e-mail (owner's pick 2026-10-16, Neon Kontrol Odası): mail-safe, complete, small."""
import re
import unittest

import v11_prospect_watch as pw
import v11_report_mail as rm

MEASURED = {"channels": {
    "youtube": {"measured": True, "url": "https://www.youtube.com/@acme", "followers": 12400, "avg_per_month": 1.5,
                "per_month": {"2026-05": 2, "2026-06": 0, "2026-07": 1, "2026-08": 0, "2026-09": 4, "2026-10": 2},
                "last_post": "2026-10-10", "longest_gap_days": 52, "formats": {"short": 6, "video": 3},
                "engagement_rate_pct": 2.31},
    "linkedin": {"measured": False, "url": "https://www.linkedin.com/company/acme", "handle": "acme"}}}


def big_written():
    """Every field at its schema limit: the mail must still stay under Gmail's clipping size."""
    long = lambda n: ("Kanal ritmi rakiplerin gerisinde kalıyor ve video formatı neredeyse hiç kullanılmıyor [1]. "  # noqa: E731
                      "Bu açık 90 günde kapanabilir; önce takvim, sonra kısa video serisi gelir. " * 20)[:n]
    return {"score": {"total": 46, "parts": [{"name": n, "score": 30 + 10 * i, "note": long(300)}
                                             for i, n in enumerate(pw.SCORE_PARTS)]},
            "headline": long(300), "summary": [long(400)] * 3,
            "sections": [{"key": k, "rag": ("green", "amber", "red")[i % 3], "paragraphs": [long(900)] * 2,
                          "bullets": [long(400)] * 6} for i, (k, _) in enumerate(pw.SECTIONS)],
            "competitor_rows": [{"metric": long(120), "us": long(200), "them": [long(200)] * 3}] * 6,
            "moves": [{"title": long(160), "why": long(500), "what": long(600), "measure": long(300),
                       "first_30_days": long(400)}] * 3,
            "audience_segments": [{"name": long(80), "share": s, "why": long(300)} for s in ("high", "medium", "low")],
            "plan_30": [{"title": long(120), "what": long(400), "target": long(200)}] * 4,
            "plan_90": [{"title": long(120), "what": long(400), "target": long(200)}] * 4,
            "closing_summary": long(900), "meeting_notes": [long(300)] * 6, "mail": long(3000)}


class ReportMail(unittest.TestCase):
    def render(self, written, measured=MEASURED):
        sources = [f"https://acmeyapi.com.tr/kaynak-{i}" for i in range(1, 25)]
        return pw.render_report_mail("Acme Yapı", written, sources, measured, today="2026-10-16",
                                     link="https://3.basecamp.com/1/buckets/2/todos/3")

    def test_full_report_fits_gmail_and_is_mail_safe(self):
        html = self.render(big_written())
        self.assertLess(len(html.encode("utf-8")), 100_000)
        for bad in ("<script", "<svg", "@import", "fonts.googleapis", "position:absolute"):
            self.assertNotIn(bad, html)
        self.assertIn('style="max-width:680px;width:100%"', html)
        for title in ("YÖNETİCİ ÖZETİ", "KANAL PANOSU", "DİJİTAL VARLIK DEĞERLENDİRMESİ", "HEDEF KİTLE TAHMİNİ",
                      "RAKİP KIYASI", "SEKTÖR DİNAMİKLERİ", "BİZ YÖNETSEYDİK", "SON SÖZ", "KAYNAKLAR"):
            self.assertIn(title, html)
        self.assertIn("kısaltılmış özettir", html)  # the full text and the mail draft stay in Basecamp

    def test_typical_report_is_sent_whole(self):
        from test_v11_prospect_watch import written
        html = self.render(written())
        self.assertNotIn("kısaltılmış", html)
        self.assertIn("YALNIZ EKİP İÇİ", html)
        self.assertIn("Kuruma gidecek mail taslağı", html)
        self.assertIn("HAMLE 3 &gt;", html)

    def test_measured_tile_shows_only_measured_numbers(self):
        html = self.render(big_written())
        self.assertIn("12,4B", html)  # followers
        self.assertIn("10 Eki 2026", html)  # last post
        self.assertIn("52 gün", html)  # longest silence (amber/red colour)
        self.assertIn("Shorts %67", html)
        self.assertIn("%2,31", html)
        for gap in ("ÖLÇÜLEMEDİ", "Resmi okuma", "ölçülemeyen"):  # owner 2026-10-16: what we lack is never shown
            self.assertNotIn(gap, html)

    def test_sparse_report_skips_empty_panels(self):
        html = self.render({"score": {"total": 80, "parts": []}, "headline": "Kısa [Ö].", "summary": ["Bir [1]."]},
                           measured={})
        self.assertIn("GÜÇLÜ", html)
        self.assertNotIn("bulunamadı", html)
        self.assertNotIn("BİZ YÖNETSEYDİK", html)
        self.assertNotIn("RAKİP KIYASI", html)
        self.assertEqual(re.findall(r"// (\d\d)", html), ["01", "02"])  # no measured channel: no empty board

    def test_source_links_and_escaping(self):
        w = big_written()
        w["headline"] = "<b>Risk</b> & fırsat [2] [99]"
        html = self.render(w)
        self.assertIn("&lt;b&gt;Risk&lt;/b&gt; &amp; fırsat", html)
        self.assertIn('href="https://acmeyapi.com.tr/kaynak-2">[2]</a>', html)
        self.assertNotIn("[99]", html)


try:
    import weasyprint  # noqa: F401  (in the cloud image; optional on a laptop)
    HAVE_PDF = True
except Exception:
    HAVE_PDF = False


class ClientVersionAndMailText(unittest.TestCase):
    def test_client_version_has_no_team_parts(self):
        html = pw.render_report_mail("Acme Yapı", big_written(), ["https://acmeyapi.com.tr/x"], MEASURED,
                                     today="2026-10-16", link="https://3.basecamp.com/1", client=True, full=True)
        for team in ("YALNIZ EKİP İÇİ", "Görüşme notu", "Kuruma gidecek mail taslağı", "Basecamp", "Hamle gönder",
                     "kısaltılmış", "EKİP İÇİ"):
            self.assertNotIn(team, html)
        self.assertIn("ÖZEL RAPOR", html)
        self.assertIn("Bu rapor Acme Yapı için hazırlanmıştır.", html)
        self.assertGreater(len(html.encode()), rm.LIMIT)  # full: never shortened

    def test_mail_text_has_two_versions_by_score(self):
        cfg = pw.load_config()
        low = pw.report_mail_text(cfg, language="tr", first="Deniz", company="Acme Yapı", score=58)
        high = pw.report_mail_text(cfg, language="tr", first="Deniz", company="Acme Yapı", score=75)
        self.assertTrue(low.startswith("Merhaba Deniz,\n\nAcme Yapı için dijital ayak izi raporunu hazırladık"))
        self.assertIn("dışarıdan görünen puanınız 58/100.", low)
        self.assertNotIn("/100", high)
        self.assertIn("Bizce dijital varlıklarınız gayet güzel görünüyor.", high)
        self.assertEqual(low.split("\n\n")[3:], high.split("\n\n")[3:])  # only the middle paragraph differs
        self.assertTrue(low.endswith("Raporun tamamı ekte. Biz yönetseydik ilk 30 ve 90 günde neler yapacağımızı "
                                     "da yazdık. 20 dakikalık bir görüşmede birlikte üzerinden geçelim mi?\n\n"
                                     + cfg["signatures"]["tr"]))
        en = pw.report_mail_text(cfg, language="en", first=None, company="Acme", score=40)
        self.assertTrue(en.startswith("Hello,\n\nWe have prepared a digital footprint report for Acme"))
        self.assertIn("40/100", en)
        self.assertEqual(pw.check_report_mail(low, settings={"cfg": cfg}, language="tr", first="Deniz"), [])
        self.assertEqual(pw.check_report_mail(high, settings={"cfg": cfg}, language="tr", first="Deniz"), [])
        self.assertEqual(pw.check_report_mail(en, settings={"cfg": cfg}, language="en", first=None), [])

    def test_pdf_name_is_ascii(self):
        import v11_report_pdf
        self.assertEqual(v11_report_pdf.pdf_name("Ak Portföy İşletme", "2026-10-16"),
                         "Hamle-AI-Dijital-Ayak-Izi-Raporu-Ak-Portfoy-Isletme-2026-10-16.pdf")

    def test_plain_mail_writer_carries_one_pdf(self):
        import base64
        import hashlib
        from email import policy
        from email.parser import BytesParser
        from v11_daily_digest import DigestMailWriter
        data = b"%PDF-1.7 fixture"
        att = {"filename": "Hamle-AI-Dijital-Ayak-Izi-Raporu-Acme-2026-10-16.pdf", "content_type": "application/pdf",
               "data_b64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
        w = DigestMailWriter(token_context=None, session_factory=None)
        raw = w._raw({"to": ["x@example.com"], "subject": "s", "body": "b", "html": "<p>b</p>", "attachment": att})
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        files = list(msg.iter_attachments())
        self.assertEqual([f.get_filename() for f in files], [att["filename"]])
        self.assertEqual(files[0].get_payload(decode=True), data)
        with self.assertRaises(Exception):
            w._raw({"to": ["x@example.com"], "subject": "s", "body": "b", "attachment": dict(att, sha256="0" * 64)})

    @unittest.skipUnless(HAVE_PDF, "WeasyPrint is installed in the cloud image")
    def test_pdf_attachment_round_trip(self):
        import base64
        import hashlib
        att = pw.report_attachment("Acme Yapı", big_written(), ["https://acmeyapi.com.tr/x"], MEASURED,
                                   "2026-10-16")
        data = base64.b64decode(att["data_b64"])
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), att["sha256"])
        self.assertEqual(att["content_type"], "application/pdf")


if __name__ == "__main__":
    unittest.main()
