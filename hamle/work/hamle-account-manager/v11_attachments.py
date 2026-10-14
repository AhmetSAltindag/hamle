"""Attachments Hamle must read (Ahmet 2026-10-14: "mailin her şeyini %100 incelemeli, ek dahil, istisnasız").

A client's mail or a Basecamp to-do is only understood together with its files. read_files() turns the files
into model inputs (images, PDFs, the text of Word/PowerPoint/Excel/text files) and says, for every file, whether
it was read. The callers enforce the rule in code, not in a prompt: an attachment that was not read, or that
the model did not describe, keeps the reply from going out by itself and is named in the team's note.
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_FILES = 8
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 30
MAX_PDF_PAGES_TOTAL = 60  # one model request
MAX_MODEL_BYTES = 22 * 1024 * 1024  # what goes into one request (base64 grows it by a third; the API takes 32 MB)
MAX_TEXT_CHARS = 20_000
MAX_XML_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")
IMAGE_SIDE = 1568
OFFICE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".gif": "image/gif", ".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv",
              ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
              ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
TEXT_TYPES = ("text/plain", "text/csv", "text/markdown")
UNSUPPORTED_TR = "bu dosya türünü okuyamıyorum"


def kind_of(name, content_type):
    """Normalized content type; a generic type falls back to the file extension."""
    kind = str(content_type or "").split(";")[0].strip().lower()
    if kind in ("", "application/octet-stream", "binary/octet-stream", "application/x-unknown"):
        ext = re.search(r"\.[A-Za-z0-9]{1,5}$", str(name or ""))
        kind = EXTENSIONS.get(ext.group(0).lower(), kind) if ext else kind
    return "image/jpeg" if kind == "image/jpg" else kind


def _ratio(w, h):
    from math import gcd
    g = gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def _image(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 80_000_000
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        width, height = im.size
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            im = canvas
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88)
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(out.getvalue()).decode("ascii")}}
    return block, {"width": width, "height": height, "ratio": _ratio(width, height)}


def _xml_text(raw, tags):
    root = ElementTree.fromstring(raw)
    parts = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in tags and el.text:
            parts.append(el.text)
        elif tag in ("p", "br", "row") and parts and not parts[-1].endswith("\n"):
            parts.append("\n")
    return "".join(parts)


def office_text(data, kind):
    """Plain text of a .docx/.pptx/.xlsx (zip of XML); no macros, no external links, bounded."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if sum(i.file_size for i in z.infolist()) > 80 * 1024 * 1024:
            raise ValueError("office_too_large")

        def read(name):
            info = z.getinfo(name)
            if info.file_size > MAX_XML_BYTES:
                raise ValueError("office_part_too_large")
            raw = z.read(name)
            if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:  # no entity expansion from a client's file
                raise ValueError("office_xml_entities")
            return raw
        if kind == "docx":
            return _xml_text(read("word/document.xml"), {"t"})
        if kind == "pptx":
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group(0)))
            return "\n\n".join(f"[Slayt {i}]\n" + _xml_text(read(n), {"t"}).strip()
                               for i, n in enumerate(slides, 1))
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(read("xl/sharedStrings.xml"))
            for si in root.iter():
                if si.tag.rsplit("}", 1)[-1] == "si":
                    shared.append("".join(t.text or "" for t in si.iter() if t.tag.rsplit("}", 1)[-1] == "t"))
        rows = []
        for n in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            root = ElementTree.fromstring(read(n))
            for row in root.iter():
                if row.tag.rsplit("}", 1)[-1] != "row":
                    continue
                cells = []
                for c in row:
                    if c.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    v = next((x.text for x in c.iter() if x.tag.rsplit("}", 1)[-1] in ("v", "t") and x.text), "")
                    if c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    cells.append(v)
                if any(cells):
                    rows.append(" | ".join(cells))
            if len(rows) > 2000:
                break
        return "\n".join(rows)


def read_files(files):
    """[(meta, data|None)] -> {'blocks', 'status'}.

    meta: {'filename', 'content_type', 'byte_size'?, 'reason'?}; data None means it could not be fetched (reason).
    status: one row per file {'name', 'type', 'read': bool, 'reason'?, 'pages'?, 'width'?, 'height'?, 'ratio'?}.
    """
    blocks, status, total, sent, pages_total = [], [], 0, 0, 0
    for index, (meta, data) in enumerate(files):
        name = str(meta.get("filename") or "(adsız ek)")[:255]
        kind = kind_of(name, meta.get("content_type"))
        row = {"name": name, "type": kind, "read": False}
        status.append(row)
        if index >= MAX_FILES:
            row["reason"] = f"tek mailde en çok {MAX_FILES} eki okuyorum"
            continue
        if data is None:
            row["reason"] = str(meta.get("reason") or "dosyayı indiremedim")[:200]
            continue
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            row["reason"] = f"dosya çok büyük ({len(data) / 1048576:.1f} MB)"
            continue
        try:
            if kind in IMAGE_TYPES:
                block, info = _image(data)
                size = len(block["source"]["data"]) * 3 // 4
                if sent + size > MAX_MODEL_BYTES:
                    row["reason"] = "ekler birlikte çok büyük; bu dosyaya sıra gelmedi"
                    continue
                sent += size
                blocks.append({"type": "text", "text": f"Ek {index + 1}: {name} (görsel)"})
                blocks.append(block)
                row.update(read=True, **info)
            elif kind == "application/pdf":
                from pypdf import PdfReader
                pages = len(PdfReader(io.BytesIO(data)).pages)
                if len(data) > MAX_PDF_BYTES or not 1 <= pages <= MAX_PDF_PAGES:
                    row["reason"] = f"PDF çok uzun ({pages} sayfa; en çok {MAX_PDF_PAGES})"
                    continue
                if sent + len(data) > MAX_MODEL_BYTES or pages_total + pages > MAX_PDF_PAGES_TOTAL:
                    row["reason"] = "ekler birlikte çok büyük; bu dosyaya sıra gelmedi"
                    continue
                sent, pages_total = sent + len(data), pages_total + pages
                blocks.append({"type": "text", "text": f"Ek {index + 1}: {name} (PDF, {pages} sayfa)"})
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": base64.b64encode(data).decode("ascii")}})
                row.update(read=True, pages=pages)
            elif kind in OFFICE or kind in TEXT_TYPES:
                text = office_text(data, OFFICE[kind]) if kind in OFFICE else data.decode("utf-8", "replace")
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if not text:
                    row["reason"] = "dosyada okunacak metin yok"
                    continue
                cut = len(text) > MAX_TEXT_CHARS
                sent += len(text[:MAX_TEXT_CHARS].encode("utf-8"))
                blocks.append({"type": "text", "text": f"Ek {index + 1}: {name}\n" + text[:MAX_TEXT_CHARS]
                               + ("\n[... devamı kesildi]" if cut else "")})
                row.update(read=True, **({"note": "metnin başı okundu"} if cut else {}))
            else:
                row["reason"] = UNSUPPORTED_TR
        except Exception:
            row["reason"] = "dosya açılamadı"
    return {"blocks": blocks, "status": status}


def unread(status, described):
    """Names the rule stops on: files not read, plus read files the model did not describe."""
    said = {str(d.get("name") or "").strip().casefold() for d in described or [] if isinstance(d, dict)
            and str(d.get("what") or "").strip()}
    out = []
    for row in status or []:
        if not row.get("read") or row["name"].strip().casefold() not in said:
            out.append(row["name"])
    return out


def status_lines(status, described):
    """Note lines: every attachment with what Hamle understood, or why it was not read."""
    what = {str(d.get("name") or "").strip().casefold(): str(d.get("what") or "").strip()
            for d in described or [] if isinstance(d, dict)}
    lines = []
    for row in status or []:
        if not row.get("read"):
            lines.append(f"- {row['name']}: OKUNAMADI ({row.get('reason') or 'bilinmiyor'})")
        elif what.get(row["name"].strip().casefold()):
            lines.append(f"- {row['name']}: {what[row['name'].strip().casefold()]}")
        else:
            lines.append(f"- {row['name']}: okundu ama Hamle ne olduğunu yazmadı")
    return lines


DESCRIBED_SCHEMA = {"type": "array", "maxItems": 12, "items": {
    "type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 255},
                                     "what": {"type": "string", "minLength": 1, "maxLength": 500}},
    "required": ["name", "what"], "additionalProperties": False}}
