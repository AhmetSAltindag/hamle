"""Attachments Hamle reads (Ahmet 2026-10-14, no exceptions): offline, files built in memory."""
import io
import unittest
import zipfile

import v11_attachments as at

try:
    from PIL import Image
    import pypdf
except ImportError:  # the Mac's local Python has no Pillow; the cloud image has both
    Image = pypdf = None

needs_pil = unittest.skipUnless(Image is not None and pypdf is not None, "Pillow and pypdf are in the cloud image")


def png(w=1080, h=1350):
    out = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 30, 255)).save(out, "PNG")
    return out.getvalue()


def pdf(pages=2):
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def office(parts):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOCX = office({"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>Paket: Rabam Plus</w:t></w:r></w:p>'
                                    '<w:p><w:r><w:t>Aylık 1.124 TL, yıllık tasarruf 7.110 TL</w:t></w:r></w:p>'
                                    '</w:body></w:document>'})
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
XLSX = office({"xl/sharedStrings.xml": f'<sst {S}><si><t>Gün</t></si><si><t>Paylaşım</t></si></sst>',
               "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c>'
                                           '</row><row><c><v>16</v></c><c t="inlineStr"><is><t>Story</t></is></c></row>'
                                           '</sheetData></worksheet>'})
P = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
PPTX = office({"ppt/slides/slide2.xml": f'<p:sld xmlns:p="x" {P}><a:t>İkinci</a:t></p:sld>',
               "ppt/slides/slide1.xml": f'<p:sld xmlns:p="x" {P}><a:t>Birinci</a:t></p:sld>'})
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Kinds(unittest.TestCase):
    def test_generic_type_falls_back_to_the_extension(self):
        self.assertEqual(at.kind_of("banner.PNG", "application/octet-stream"), "image/png")
        self.assertEqual(at.kind_of("brief.docx", ""), DOCX_TYPE)
        self.assertEqual(at.kind_of("a.jpg", "image/jpg"), "image/jpeg")
        self.assertEqual(at.kind_of("video.mp4", "video/mp4"), "video/mp4")


class Read(unittest.TestCase):
    def test_office_and_text_files_become_text_blocks(self):
        out = at.read_files([({"filename": "brief.docx", "content_type": DOCX_TYPE}, DOCX),
                             ({"filename": "plan.xlsx", "content_type": "application/octet-stream"}, XLSX),
                             ({"filename": "sunum.pptx", "content_type": "application/octet-stream"}, PPTX),
                             ({"filename": "not.txt", "content_type": "text/plain"}, "Logo sağ üstte".encode())])
        self.assertTrue(all(r["read"] for r in out["status"]), out["status"])
        texts = [b["text"] for b in out["blocks"]]
        self.assertIn("Aylık 1.124 TL, yıllık tasarruf 7.110 TL", texts[0])
        self.assertIn("Gün | Paylaşım\n16 | Story", texts[1])
        self.assertLess(texts[2].index("Birinci"), texts[2].index("İkinci"))  # slide order, not zip order
        self.assertIn("Logo sağ üstte", texts[3])

    def test_what_cannot_be_read_says_why(self):
        files = [({"filename": "video.mp4", "content_type": "video/mp4"}, b"x" * 10),
                 ({"filename": "kayip.png", "content_type": "image/png", "reason": "indirilemedi"}, None),
                 ({"filename": "bozuk.docx", "content_type": DOCX_TYPE}, b"not a zip"),
                 ({"filename": "dev.png", "content_type": "image/png"}, b"x" * (at.MAX_FILE_BYTES + 1))]
        files += [({"filename": f"f{i}.txt", "content_type": "text/plain"}, b"metin") for i in range(at.MAX_FILES)]
        status = at.read_files(files)["status"]
        why = {r["name"]: r.get("reason") for r in status}
        self.assertEqual(why["video.mp4"], at.UNSUPPORTED_TR)
        self.assertEqual(why["kayip.png"], "indirilemedi")
        self.assertEqual(why["bozuk.docx"], "dosya açılamadı")
        self.assertIn("çok büyük", why["dev.png"])
        self.assertIn("en çok 8", why[f"f{at.MAX_FILES - 1}.txt"])  # the 9th and later wait

    @needs_pil
    def test_images_and_pdfs_go_to_the_model(self):
        out = at.read_files([({"filename": "App Banner.png", "content_type": "image/png"}, png()),
                             ({"filename": "kampanya.pdf", "content_type": "application/pdf"}, pdf(3))])
        image, document = out["status"]
        self.assertEqual((image["read"], image["width"], image["height"], image["ratio"]), (True, 1080, 1350, "4:5"))
        self.assertEqual((document["read"], document["pages"]), (True, 3))
        kinds = [b["type"] for b in out["blocks"]]
        self.assertEqual(kinds, ["text", "image", "text", "document"])
        self.assertEqual(out["blocks"][1]["source"]["media_type"], "image/jpeg")

    @needs_pil
    def test_too_many_pdf_pages_in_one_mail_are_named(self):
        out = at.read_files([({"filename": f"p{i}.pdf", "content_type": "application/pdf"}, pdf(25)) for i in range(3)])
        self.assertEqual([r["read"] for r in out["status"]], [True, True, False])
        self.assertIn("sıra gelmedi", out["status"][2]["reason"])


class MailFiles(unittest.TestCase):
    def test_every_listed_file_is_fetched_or_named_with_why(self):
        listed = [{"filename": "not.txt", "mime_type": "text/plain", "size_bytes": 20, "attachment_id": "a1"},
                  {"filename": "kopuk.pdf", "mime_type": "application/pdf", "size_bytes": 20, "attachment_id": None},
                  {"filename": "dev.mov", "mime_type": "video/quicktime", "size_bytes": at.MAX_FILE_BYTES + 1,
                   "attachment_id": "a3"},
                  {"filename": "hata.png", "mime_type": "image/png", "size_bytes": 20, "attachment_id": "a4"}]
        asked = []

        def fetch(message_id, attachment_id):
            asked.append((message_id, attachment_id))
            if attachment_id == "a4":
                raise TimeoutError("synthetic")
            return "Logo sağ üstte".encode()
        out = at.mail_files({"message_id": "m1", "attachments": listed}, fetch)
        self.assertEqual(asked, [("m1", "a1"), ("m1", "a4")])  # nothing fetched for a missing id or a huge file
        why = {r["name"]: r.get("reason") for r in out["status"]}
        self.assertEqual(why, {"not.txt": None, "kopuk.pdf": "ek mailin içinden alınamadı",
                               "dev.mov": "dosya çok büyük (20.0 MB)", "hata.png": "indirilemedi (TimeoutError)"})
        self.assertIn("Logo sağ üstte", out["blocks"][0]["text"])
        self.assertEqual(at.mail_files({"message_id": "m1"}, fetch), {"blocks": [], "status": []})


class Rule(unittest.TestCase):
    STATUS = [{"name": "App Banner.png", "read": True}, {"name": "brief.docx", "read": True},
              {"name": "video.mp4", "read": False, "reason": at.UNSUPPORTED_TR}]

    def test_unread_or_undescribed_files_stop_the_auto_send(self):
        said = [{"name": "app banner.png", "what": "Rabam Plus paket görseli, 1.124 TL"}]
        self.assertEqual(at.unread(self.STATUS, said), ["brief.docx", "video.mp4"])
        said.append({"name": "brief.docx", "what": "Kampanya metni"})
        self.assertEqual(at.unread(self.STATUS, said), ["video.mp4"])
        self.assertEqual(at.unread([], []), [])

    def test_note_lines_say_what_each_file_is(self):
        lines = at.status_lines(self.STATUS, [{"name": "App Banner.png", "what": "Paket görseli"}])
        self.assertEqual(lines, ["- App Banner.png: Paket görseli", "- brief.docx: okundu ama Hamle ne olduğunu yazmadı",
                                 f"- video.mp4: OKUNAMADI ({at.UNSUPPORTED_TR})"])


if __name__ == "__main__":
    unittest.main()
