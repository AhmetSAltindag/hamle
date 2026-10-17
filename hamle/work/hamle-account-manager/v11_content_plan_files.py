"""İçerik Planı as files (Ahmet 2026-10-17 13:30Z: "PDF ve DOCX olarak yaratıp Basecamp'e ekleyebilirsin").

The same plan as the Basecamp document, as an A4 PDF (WeasyPrint, in the cloud image) for the presentation and as a
DOCX (stdlib zip, no new dependency) the team can edit in Word. No model call; the plan dict is the input.
"""
from __future__ import annotations

from datetime import date
import html
import io
import re
import unicodedata
import zipfile

import v11_content_plan as cp

PDF_MAX = 8 * 1024 * 1024
DOCX_MAX = 2 * 1024 * 1024
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class PlanFileError(Exception):
    pass


def file_name(name, first, ext):
    folded = unicodedata.normalize("NFKD", str(name).replace("ı", "i").replace("İ", "I"))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folded.encode("ascii", "ignore").decode()).strip("-")[:40] or "Marka"
    return f"{slug}-{first:%Y-%m}-Icerik-Plani-Hamle-taslagi.{ext}"


# ------------------------------------------------------------------ one outline for both files
def outline(name, first, plan, findings, score, material):
    """[(kind, payload)]: kind in title, sub, h1, p, li, note, day, kv."""
    lo, hi = material["brand"]["posts_per_month"]
    days = plan["days"]
    rec = sum(1 for r in days if r.get("recommended"))
    out = [("title", f"{name} · {cp.month_label(first)} İçerik Planı"),
           ("sub", "Hamle AI · iç taslak · müşteriye Hamle göndermez"),
           ("p", f"Her gün için bir öneri ({len(days)} gün); ⭐ {rec} gün sözleşmedeki {lo}-{hi} paylaşım için önerilen "
                 "seçim. Seçim, eleme ve ekleme sosyal medya sorumlusunda."),
           ("p", f"Hamle kontrolü: {score}/100")]
    out += [("note", ("🔴 " if lv == "kırmızı" else "🟡 ") + t) for lv, t in findings]
    out += [("h1", "Strateji hatırlatması")] + [("li", x) for x in plan["strategy"]] + [("p", cp.STRATEGY_NOTE)]
    if plan.get("last_month"):
        out += [("h1", "Geçen ay")] + [("li", x) for x in plan["last_month"]]
    out += [("h1", "İçerik sütunları")] + [("li", f"{p['name']}: {p['why']}") for p in plan["pillars"]]
    camps = material["brand"].get("campaigns") or []
    if camps:
        out += [("h1", "Kampanya takvimi")] + [
            ("li", f"{c['name']}: {cp._dm(c['start'])} - {cp._dm(c['end'])}" + (f" · {c['note']}" if c.get("note") else ""))
            for c in camps]
    if plan.get("brand_asks"):
        out += [("h1", "Markadan beklediklerimiz")] + [
            ("li", f"{a['what']} · kimden: {a['who']} · ne zamana: {a['by']}") for a in plan["brand_asks"]]
    algo = material.get("algorithm") or {}
    if algo.get("rules") or plan.get("algorithm_moves"):
        out += [("h1", "Bu ay algoritmada ne değişti, biz ne yapıyoruz")]
        out += [("li", f"{r['güven']} {cp.platform_label(r['platform'])}: {r['rule']}") for r in algo.get("rules") or []]
        out += [("li", "→ " + x) for x in plan.get("algorithm_moves") or []]
    if material.get("special_days"):
        out += [("h1", "Özel günler")] + [("li", f"{cp._dm(s['date'])} {s['name']}") for s in material["special_days"]]
    if plan.get("shoots"):
        out += [("h1", "Çekim planı")]
        for x in plan["shoots"]:
            out.append(("li", f"{x['title']}" + (f" · {', '.join(cp._dm(d) for d in x['dates'])}" if x["dates"] else "")
                        + f" · yer: {x['place']} · oyuncu: {x['cast']}"))
            out += [("p", "   " + y) for y in x["scenes"]]
    out.append(("h1", "Takvim"))
    for r in days:
        if r.get("missing"):
            out.append(("li", f"{cp._day(r)} · öneri yok"))
            continue
        out.append(("li", f"{'⭐ ' if r.get('recommended') else ''}{cp._day(r)} · "
                          f"{cp.FORMAT_LABELS.get(r['format'], r['format'])} · {cp.platform_label(r['platform'])} · "
                          f"{r['pillar']}" + (f" · Seri: {r['series']}" if r.get("series") else "") + f" · {r['topic']}"
                    + (f" · Özel gün: {r['special_day']}" if r.get("special_day") else "")))
    out.append(("h1", "İçerik detayları"))
    for r in days:
        if r.get("missing") or not r.get("caption"):
            continue
        out.append(("day", f"{'⭐ ' if r.get('recommended') else ''}{cp._day(r)} · {r['topic']}"))
        items = [("Neden", r.get("why")), ("Kanca", r.get("hook")),
                 ("Instagram metni" if r.get("linkedin_caption") else "Metin", r.get("caption")),
                 ("LinkedIn metni", r.get("linkedin_caption")), ("Hashtag", " ".join(r.get("hashtags") or [])),
                 ("Çağrı", r.get("cta")), ("Görsel brief", r.get("visual_brief")),
                 ("Ekran yazısı", r.get("on_screen_text")), ("Ölçü / süre", r.get("size")),
                 ("Başarı ölçüsü", r.get("success_metric")), ("Durum", "Taslak")]
        out += [("kv", (k, str(v))) for k, v in items if v]
    out.append(("p", "Kaynak: marka hafızası (son 90 gün), Basecamp, özel günler takvimi, Platform Kural Defteri."))
    return out


# ------------------------------------------------------------------ PDF
CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm;
  @bottom-right { content: "Hamle AI · " counter(page) "/" counter(pages); font: 8pt sans-serif; color: #8a8f98; } }
body { font-family: "DejaVu Sans", sans-serif; font-size: 9.5pt; line-height: 1.45; color: #1d1f22; }
.eyebrow { font-size: 7.5pt; letter-spacing: .12em; text-transform: uppercase; color: #c2620a; font-weight: bold; }
h1.title { font-size: 20pt; margin: 4pt 0 6pt; }
h2 { font-size: 12.5pt; margin: 16pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5pt solid #c2620a; }
p { margin: 3pt 0; } ul { margin: 2pt 0 2pt 0; padding-left: 14pt; } li { margin: 1.5pt 0; }
.note { background: #fff2dc; border-radius: 4pt; padding: 4pt 7pt; margin: 3pt 0; }
.day { margin: 12pt 0 3pt; font-weight: bold; font-size: 10.5pt; page-break-after: avoid; }
.kv { display: block; margin: 1.5pt 0 1.5pt 8pt; white-space: pre-wrap; }
.kv b { color: #5d6168; font-size: 8pt; text-transform: uppercase; letter-spacing: .05em; }
"""


def plan_html(items):
    e = lambda s: html.escape(str(s), quote=False)
    out, ul = [], False
    for kind, v in items:
        if kind != "li" and ul:
            out.append("</ul>")
            ul = False
        if kind == "title":
            out.append(f"<h1 class='title'>{e(v)}</h1>")
        elif kind == "sub":
            out.insert(0, f"<div class='eyebrow'>{e(v)}</div>")
        elif kind == "h1":
            out.append(f"<h2>{e(v)}</h2>")
        elif kind == "li":
            if not ul:
                out.append("<ul>")
                ul = True
            out.append(f"<li>{e(v)}</li>")
        elif kind == "note":
            out.append(f"<div class='note'>{e(v)}</div>")
        elif kind == "day":
            out.append(f"<div class='day'>{e(v)}</div>")
        elif kind == "kv":
            out.append(f"<span class='kv'><b>{e(v[0])}</b><br>{e(v[1])}</span>")
        else:
            out.append(f"<p>{e(v)}</p>")
    if ul:
        out.append("</ul>")
    return "<!doctype html><html lang='tr'><head><meta charset='utf-8'></head><body>" + "".join(out) + "</body></html>"


def plan_pdf(items):
    try:
        import weasyprint
        data = weasyprint.HTML(string=plan_html(items)).write_pdf(stylesheets=[weasyprint.CSS(string=CSS)])
    except Exception as exc:
        raise PlanFileError("plan_pdf_failed:" + type(exc).__name__) from None
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-") or not 0 < len(data) <= PDF_MAX:
        raise PlanFileError("plan_pdf_invalid")
    return data


# ------------------------------------------------------------------ DOCX (stdlib)
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _run(text, *, bold=False, size=None, color=None):
    props = ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") + (
        f'<w:sz w:val="{size}"/>' if size else "")
    parts = str(text).split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{html.escape(p, quote=False)}</w:t>' for p in parts)
    return f"<w:r>{'<w:rPr>' + props + '</w:rPr>' if props else ''}{body}</w:r>"


def _para(runs, *, style=None, indent=None, space_before=None):
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + (f'<w:spacing w:before="{space_before}"/>'
                                                               if space_before else "") + (
        f'<w:ind w:left="{indent}"/>' if indent else "")
    return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{''.join(runs)}</w:p>"


STYLES = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {_W}>'
          '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
          '<w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>'
          '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>'
          '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:spacing w:before="320" w:after="80"/>'
          '<w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="C2620A"/><w:sz w:val="28"/></w:rPr></w:style>'
          '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:pPr><w:spacing w:before="240" w:after="40"/>'
          '<w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="23"/></w:rPr></w:style></w:styles>')


def plan_docx(items):
    body = []
    for kind, v in items:
        if kind == "title":
            body.append(_para([_run(v)], style="Title"))
        elif kind == "sub":
            body.append(_para([_run(v, bold=True, size=16, color="C2620A")]))
        elif kind == "h1":
            body.append(_para([_run(v)], style="Heading1"))
        elif kind == "day":
            body.append(_para([_run(v)], style="Heading2"))
        elif kind == "li":
            body.append(_para([_run("• " + v)], indent=280))
        elif kind == "note":
            body.append(_para([_run(v, bold=True)]))
        elif kind == "kv":
            body.append(_para([_run(v[0] + ": ", bold=True, color="5D6168"), _run(v[1])], indent=200))
        else:
            body.append(_para([_run(v)]))
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W}><w:body>' + "".join(body)
           + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1077" w:bottom="1134" '
             'w:left="1077" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/_rels/document.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "word/document.xml": doc, "word/styles.xml": STYLES}
    buf = io.BytesIO()
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, content in files.items():
            z.writestr(zipfile.ZipInfo(n, stamp), content.encode("utf-8"))
    data = buf.getvalue()
    if not 0 < len(data) <= DOCX_MAX:
        raise PlanFileError("plan_docx_invalid")
    return data


def plan_files(name, first, plan, findings, score, material):
    """[(file name, bytes, mime)]: PDF then DOCX."""
    items = outline(name, first, plan, findings, score, material)
    return [(file_name(name, first, "pdf"), plan_pdf(items), PDF_MIME),
            (file_name(name, first, "docx"), plan_docx(items), DOCX_MIME)]


def main(argv=None):
    """Local: python3 v11_content_plan_files.py report.json OUT_DIR -> the preview's plan as PDF + DOCX."""
    import json
    import sys
    from pathlib import Path
    args = argv if argv is not None else sys.argv[1:]
    rep = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    p = rep["plan_data"]
    first = date.fromisoformat(rep["month"] + "-01")
    out = Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    for n, data, _ in plan_files(p["name"], first, p["plan"], [tuple(x) for x in p["findings"]], p["score"],
                                 p["material"]):
        (out / n).write_bytes(data)
        print(out / n, len(data))


if __name__ == "__main__":
    main()
