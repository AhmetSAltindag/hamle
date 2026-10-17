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


# ------------------------------------------------------------------ one reading for every file
def notes(plan, findings):
    """'Ekibin bakması gerekenler': the plan's own team notes, then the code checks. [(tag, text)]"""
    out = [(str(t), str(x)) for t, x in plan.get("team_notes") or []]
    return out + [("Düzelt" if lv == "kırmızı" else "Kontrol", t) for lv, t in findings]


def counts(plan):
    days = [r for r in plan["days"] if not r.get("missing")]
    fmt = {}
    for r in days:
        label = cp.FORMAT_LABELS.get(r["format"], r["format"])
        fmt[label] = fmt.get(label, 0) + 1
    return {"days": len(plan["days"]), "rec": sum(1 for r in days if r.get("recommended")),
            "ig": sum(1 for r in days if r["platform"] == "instagram"),
            "li": sum(1 for r in days if r["platform"] == "linkedin"),
            "both": sum(1 for r in days if r["platform"] == cp.BOTH),
            "formats": " · ".join(f"{k} {v}" for k, v in sorted(fmt.items(), key=lambda kv: -kv[1]))}


def detail_fields(r):
    """One day's detail in reading order: [(label, text, kind)], kind in kv, ig, li, brief."""
    ig_label = ("Paylaşım metni (Instagram)" if r.get("linkedin_caption") else
                "Paylaşım metni (LinkedIn)" if r["platform"] == "linkedin" else "Paylaşım metni")
    items = [("Neden", r.get("why"), "kv"), ("Kanca", r.get("hook"), "kv"),
             (ig_label, r.get("caption"), "li" if r["platform"] == "linkedin" else "ig"),
             ("Paylaşım metni (LinkedIn)", r.get("linkedin_caption"), "li"),
             ("Tasarımcıya kısa brief", r.get("short_brief"), "brief"),
             ("Hashtag", " ".join(r.get("hashtags") or []), "kv"), ("Çağrı", r.get("cta"), "kv"),
             ("Görsel brief (detay)", r.get("visual_brief"), "kv"),
             ("Ekran yazısı", r.get("on_screen_text"), "kv"), ("Ölçü / süre", r.get("size"), "kv"),
             ("Başarı ölçüsü", r.get("success_metric"), "kv")]
    return [(k, str(v), kind) for k, v, kind in items if v]


def extras(plan, material):
    """Sections between strategy and calendar; an exact (approved, as reviewed) plan shows only its own."""
    out = []
    if plan.get("last_month"):
        out.append(("Geçen ay", list(plan["last_month"])))
    camps = [] if plan.get("exact") else material["brand"].get("campaigns") or []
    if camps:
        out.append(("Kampanya takvimi", [f"{c['name']}: {cp._dm(c['start'])} - {cp._dm(c['end'])}"
                                         + (f" · {c['note']}" if c.get("note") else "") for c in camps]))
    if plan.get("brand_asks"):
        out.append(("Markadan beklediklerimiz", [f"{a['what']} · kimden: {a['who']} · ne zamana: {a['by']}"
                                                 for a in plan["brand_asks"]]))
    algo = {} if plan.get("exact") else material.get("algorithm") or {}
    if algo.get("rules") or plan.get("algorithm_moves"):
        out.append(("Bu ay algoritmada ne değişti, biz ne yapıyoruz",
                    [f"{r['güven']} {cp.platform_label(r['platform'])}: {r['rule']}" for r in algo.get("rules") or []]
                    + ["→ " + x for x in plan.get("algorithm_moves") or []]))
    if plan.get("shoots"):
        out.append(("Çekim planı", [f"{x['title']}" + (f" · {', '.join(cp._dm(d) for d in x['dates'])}" if x["dates"] else "")
                                    + f" · yer: {x['place']} · oyuncu: {x['cast']}"
                                    + "".join("\n   " + y for y in x["scenes"]) for x in plan["shoots"]]))
    return out


def outline(name, first, plan, findings, score, material):
    """[(kind, payload)] for the DOCX: kind in title, sub, h1, p, li, note, day, kv."""
    lo, hi = material["brand"]["posts_per_month"]
    n = counts(plan)
    out = [("sub", "Hamle AI · iç taslak · müşteriye gönderilmez"),
           ("title", f"{name} · {cp.month_label(first)} İçerik Planı"),
           ("p", f"Ayın her günü için bir öneri var. ⭐ işaretli {n['rec']} gün, sözleşmedeki {lo}-{hi} paylaşım için "
                 "Hamle'nin önerdiği seçim. Seçim, eleme ve ekleme sosyal medya ekibinde."),
           ("p", f"{n['days']} gün · ⭐ {n['rec']} önerilen · Instagram {n['ig']} · LinkedIn {n['li']} · ikisi birden "
                 f"{n['both']} · Hamle kontrolü {score}/100")]
    if notes(plan, findings):
        out += [("h1", "Ekibin bakması gerekenler")] + [("note", f"{t.upper()}: {x}") for t, x in notes(plan, findings)]
    out += [("h1", "Strateji ve içerik sütunları")] + [("li", x) for x in plan["strategy"]] + [("p", cp.STRATEGY_NOTE)]
    out += [("kv", (p["name"], p["why"])) for p in plan["pillars"]]
    specials = [f"{cp._dm(s['date'])} {s['name']}" for s in material.get("special_days") or []]
    if specials:
        out.append(("p", "Özel günler: " + " · ".join(specials)))
    out.append(("p", "Format dağılımı: " + n["formats"]))
    for title, lines in extras(plan, material):
        out += [("h1", title)] + [("li", x) for x in lines]
    out.append(("h1", "Takvim"))
    for r in plan["days"]:
        if r.get("missing"):
            out.append(("li", f"{cp._day(r)} · öneri yok"))
            continue
        out.append(("li", f"{'⭐ ' if r.get('recommended') else ''}{cp._day(r)} · "
                          f"{cp.FORMAT_LABELS.get(r['format'], r['format'])} · {cp.platform_label(r['platform'])} · "
                          f"{r['pillar']}" + (f" · Seri: {r['series']}" if r.get("series") else "") + f" · {r['topic']}"
                    + (f" · Özel gün: {r['special_day']}" if r.get("special_day") else "")))
    out.append(("h1", "Günlerin detayı"))
    for r in plan["days"]:
        if r.get("missing") or not r.get("caption"):
            continue
        out.append(("day", f"{'⭐ ' if r.get('recommended') else ''}{cp._day(r)} · {cp.platform_label(r['platform'])} · "
                           f"{cp.FORMAT_LABELS.get(r['format'], r['format'])} · {r['topic']}"))
        out += [("kv", (k, v)) for k, v, _ in detail_fields(r)]
    out.append(("p", "Hamle AI · Gamechangers iç taslak · Kaynak: marka hafızası (son 90 gün), Basecamp, özel günler "
                     "takvimi, Platform Kural Defteri."))
    return out


# ------------------------------------------------------------------ PDF (the look Ahmet chose on 17 Oct: the first plan's page)
CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm;
  @bottom-right { content: "Hamle AI · " counter(page) "/" counter(pages); font: 7.5pt sans-serif; color: #8a8f98; } }
html { background: #ffffff; }
body { font-family: "DejaVu Sans", "Liberation Sans", sans-serif; font-size: 8.8pt; line-height: 1.45; color: #1d1f22;
  orphans: 3; widows: 3; }
p, li { orphans: 3; widows: 3; }
h1, h2, h3 { break-after: avoid; page-break-after: avoid; margin: 0; }
.eyebrow { font-size: 7pt; letter-spacing: .12em; text-transform: uppercase; color: #c2620a; font-weight: bold; }
h1 { font-size: 19pt; line-height: 1.15; margin: 3pt 0 4pt; }
h2 { font-size: 12pt; margin: 16pt 0 7pt; }
.lede { color: #5d6168; margin: 0 0 8pt; }
.stats { width: 100%; border-collapse: separate; border-spacing: 5pt 0; margin: 0 -5pt 6pt; }
.stats td { border: .6pt solid #e2e0da; border-radius: 6pt; padding: 6pt 8pt; vertical-align: top; }
.stats b { display: block; font-size: 13pt; } .stats span { color: #5d6168; font-size: 7.5pt; }
.stats .score b { color: #2f7d4f; }
.notes { background: #fff2dc; border-radius: 8pt; padding: 8pt 11pt 9pt; margin-top: 10pt; break-inside: avoid; }
.notes h2 { margin: 0 0 5pt; }
.note { margin: 4pt 0; break-inside: avoid; page-break-inside: avoid; }
.tag { display: inline-block; font-size: 6.5pt; font-weight: bold; letter-spacing: .06em; text-transform: uppercase;
  border: .6pt solid #a15c00; color: #a15c00; border-radius: 3pt; padding: 0 4pt; margin-right: 5pt; }
.tag.bilgi { color: #2f7d4f; border-color: #2f7d4f; }
.tag.duzeltme, .tag.duzelt { color: #b3261e; border-color: #b3261e; }
.cols { width: 100%; border-collapse: collapse; }
.cols > tbody > tr > td { width: 50%; vertical-align: top; padding: 0 8pt 0 0; }
.cols > tbody > tr > td + td { padding: 0 0 0 8pt; }
ul.plain { margin: 0; padding-left: 11pt; } ul.plain li { margin: 0 0 4pt; break-inside: avoid; }
.pillar { margin: 0 0 6pt; break-inside: avoid; } .pillar b { display: block; } .pillar span { color: #5d6168; }
.small { color: #5d6168; font-size: 7.8pt; }
.extra li { margin: 0 0 3pt; white-space: pre-wrap; break-inside: avoid; }
table.cal { width: 100%; border-collapse: collapse; border: .6pt solid #e2e0da; }
table.cal thead { display: table-header-group; }
table.cal th { text-align: left; font-size: 6.8pt; text-transform: uppercase; letter-spacing: .06em; color: #8a8f98;
  padding: 4pt 6pt; border-bottom: .6pt solid #e2e0da; }
table.cal tr { break-inside: avoid; page-break-inside: avoid; }
table.cal td { padding: 5pt 6pt; border-top: .6pt solid #e2e0da; vertical-align: top; }
td.d { font-family: "DejaVu Sans Mono", monospace; font-size: 7.8pt; white-space: nowrap; }
td.s { width: 10pt; text-align: center; } td.f { white-space: nowrap; color: #5d6168; }
.pl { display: block; font-size: 7pt; color: #c2620a; font-weight: bold; }
.sd { display: inline-block; margin-top: 2pt; font-size: 7pt; background: #fbeedd; color: #c2620a; border-radius: 3pt; padding: 0 4pt; }
.chip { display: inline-block; font-size: 6.8pt; font-weight: bold; border-radius: 3pt; padding: 0 4pt; margin: 0 2pt 2pt 0; white-space: nowrap; }
.chip.ig { background: #f9e6ef; color: #b4326f; } .chip.li { background: #e3edf7; color: #1f5f99; }
/* a day may continue on the next page, but only between whole fields: no line, field or box is ever split */
.card { border: .6pt solid #e2e0da; border-radius: 7pt; margin: 0 0 7pt; }
.card .head { padding: 6pt 9pt; border-bottom: .6pt solid #e2e0da; break-after: avoid; page-break-after: avoid; break-inside: avoid; }
.card .head .d { font-family: "DejaVu Sans Mono", monospace; font-size: 8pt; margin-right: 4pt; }
.card .head .f { color: #5d6168; margin-left: 2pt; }
.card .head .tt { display: block; font-weight: bold; margin-top: 2pt; }
.card .body { padding: 6pt 9pt 7pt; }
.kv { margin: 0 0 4pt; break-inside: avoid; page-break-inside: avoid; }
.k { display: block; font-size: 6.5pt; text-transform: uppercase; letter-spacing: .06em; color: #8a8f98; font-weight: bold; }
.v { white-space: pre-wrap; }
.box { break-inside: avoid; page-break-inside: avoid; border-left: 2.4pt solid #b4326f; background: #f6f5f2; border-radius: 4pt; padding: 5pt 8pt; margin: 2pt 0 6pt; }
.box.li { border-left-color: #1f5f99; } .box.brief { border-left-color: #c2620a; background: #fbeedd; }
.foot { color: #5d6168; font-size: 7.5pt; border-top: .6pt solid #e2e0da; padding-top: 6pt; margin-top: 10pt; }
"""


def _chips(p):
    names = {"instagram": ["Instagram"], "linkedin": ["LinkedIn"], cp.BOTH: ["Instagram", "LinkedIn"]}.get(
        p, [cp.platform_label(p)])
    return "".join(f"<span class='chip {'li' if n == 'LinkedIn' else 'ig'}'>{n}</span>" for n in names)


def _tagclass(t):
    return unicodedata.normalize("NFKD", t.casefold().replace("ı", "i")).encode("ascii", "ignore").decode()


def plan_html(name, first, plan, findings, score, material):
    e = lambda s: html.escape(str(s), quote=False)
    lo, hi = material["brand"]["posts_per_month"]
    n = counts(plan)
    out = ["<div class='eyebrow'>Hamle AI · İç taslak · Müşteriye gönderilmez</div>",
           f"<h1>{e(name)} · {e(cp.month_label(first))} İçerik Planı</h1>",
           f"<p class='lede'>Ayın her günü için bir öneri var. ⭐ işaretli {n['rec']} gün, sözleşmedeki {lo}-{hi} "
           "paylaşım için Hamle'nin önerdiği seçim. Seçim, eleme ve ekleme sosyal medya ekibinde.</p>",
           "<table class='stats'><tr>"
           f"<td><b>{n['days']}</b><span>gün, her güne bir fikir</span></td>"
           f"<td><b>{n['rec']}</b><span>⭐ önerilen paylaşım</span></td>"
           f"<td><b>{n['ig']} · {n['li']} · {n['both']}</b><span>Instagram · LinkedIn · ikisi birden</span></td>"
           f"<td class='score'><b>{score}/100</b><span>Hamle'nin kendi kontrolü</span></td></tr></table>"]
    ns = notes(plan, findings)
    if ns:
        out.append("<div class='notes'><h2>Ekibin bakması gerekenler</h2>" + "".join(
            f"<div class='note'><span class='tag {_tagclass(t)}'>{e(t)}</span>{e(x)}</div>" for t, x in ns) + "</div>")
    specials = " · ".join(f"{cp._dm(s['date'])} {e(s['name'])}" for s in material.get("special_days") or [])
    out.append("<h2>Strateji ve içerik sütunları</h2><table class='cols'><tr><td><ul class='plain'>"
               + "".join(f"<li>{e(x)}</li>" for x in plan["strategy"]) + f"</ul><p class='small'>{e(cp.STRATEGY_NOTE)}</p>"
               "</td><td>" + "".join(f"<div class='pillar'><b>{e(p['name'])}</b><span>{e(p['why'])}</span></div>"
                                     for p in plan["pillars"])
               + "<p class='small'>" + (f"Özel günler: {specials}<br>" if specials else "")
               + f"Format dağılımı: {e(n['formats'])}</p></td></tr></table>")
    for title, lines in extras(plan, material):
        out.append(f"<h2>{e(title)}</h2><ul class='plain extra'>" + "".join(f"<li>{e(x)}</li>" for x in lines) + "</ul>")
    rows = []
    for r in plan["days"]:
        if r.get("missing"):
            rows.append(f"<tr><td class='d'>{e(cp._day(r))}</td><td class='s'></td><td></td><td class='f'></td>"
                        "<td>öneri yok</td></tr>")
            continue
        rows.append(f"<tr><td class='d'>{e(cp._day(r))}</td><td class='s'>{'⭐' if r.get('recommended') else ''}</td>"
                    f"<td>{_chips(r['platform'])}</td><td class='f'>{e(cp.FORMAT_LABELS.get(r['format'], r['format']))}</td>"
                    f"<td><span class='pl'>{e(r['pillar'])}" + (f" · Seri: {e(r['series'])}" if r.get("series") else "")
                    + f"</span>{e(r['topic'])}"
                    + (f"<br><span class='sd'>Özel gün: {e(r['special_day'])}</span>" if r.get("special_day") else "")
                    + "</td></tr>")
    out.append("<h2>Takvim</h2><table class='cal'><thead><tr><th>Gün</th><th></th><th>Mecra</th><th>Format</th>"
               "<th>Sütun ve konu</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")
    out.append("<h2>Günlerin detayı</h2>")
    for r in plan["days"]:
        if r.get("missing") or not r.get("caption"):
            continue
        body = []
        for k, v, kind in detail_fields(r):
            if kind == "kv":
                body.append(f"<div class='kv'><span class='k'>{e(k)}</span><div class='v'>{e(v)}</div></div>")
            else:
                body.append(f"<div class='box {kind}'><span class='k'>{e(k)}</span><div class='v'>{e(v)}</div></div>")
        out.append(f"<div class='card' id='g{r['date'][8:]}'><div class='head'><span class='d'>{e(cp._day(r))}</span>"
                   + ("⭐ " if r.get("recommended") else "") + _chips(r["platform"])
                   + f"<span class='f'>{e(cp.FORMAT_LABELS.get(r['format'], r['format']))}</span>"
                   f"<span class='tt'>{e(r['topic'])}</span></div><div class='body'>{''.join(body)}</div></div>")
    out.append("<div class='foot'>Hamle AI · Gamechangers iç taslak · Kaynak: marka hafızası (son 90 gün), Basecamp, "
               "özel günler takvimi, Platform Kural Defteri.</div>")
    return "<!doctype html><html lang='tr'><head><meta charset='utf-8'></head><body>" + "".join(out) + "</body></html>"


def _page_texts(document):
    """Text of each laid-out page (WeasyPrint box tree), for the completeness check."""
    def walk(box):
        text = getattr(box, "text", None)
        if isinstance(text, str):
            yield text
        for child in getattr(box, "children", None) or []:
            yield from walk(child)
    return [" ".join(walk(page._page_box)) for page in document.pages]


def complete(pages, plan):
    """Every day with a detail is on some page, and the last one is printed whole (its last field ends the text)."""
    text = " ".join(pages)
    flat = re.sub(r"\s+", "", text)
    days = [r for r in plan["days"] if not r.get("missing") and r.get("caption")]
    if not all(cp._day(r)[:5] in text for r in days):
        return False
    if days:
        last = [v for _, v, _ in detail_fields(days[-1])][-1]
        if re.sub(r"\s+", "", last)[-60:] not in flat:
            return False
    return True


def plan_pdf(name, first, plan, findings, score, material):
    try:
        import weasyprint
        document = weasyprint.HTML(string=plan_html(name, first, plan, findings, score, material)).render(
            stylesheets=[weasyprint.CSS(string=CSS)])
        pages = _page_texts(document)
        data = document.write_pdf()
    except Exception as exc:
        raise PlanFileError("plan_pdf_failed:" + type(exc).__name__) from None
    if not complete(pages, plan):
        raise PlanFileError("plan_pdf_incomplete")
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
    return [(file_name(name, first, "pdf"), plan_pdf(name, first, plan, findings, score, material), PDF_MIME),
            (file_name(name, first, "docx"), plan_docx(outline(name, first, plan, findings, score, material)), DOCX_MIME)]


def main(argv=None):
    """Local: python3 v11_content_plan_files.py report.json|approved.json OUT_DIR -> the plan as PDF + DOCX."""
    import json
    import sys
    from pathlib import Path
    args = argv if argv is not None else sys.argv[1:]
    rep = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    first = date.fromisoformat(rep["month"] + "-01")
    if "plan_data" in rep:
        p = rep["plan_data"]
        findings, score = [tuple(x) for x in p["findings"]], p["score"]
    else:  # an approved plan file (content-plan-approved-<brand>-<YYYYMM>.json): checked as the write will check it
        p = rep
        findings, score = cp.check(p["plan"], p["material"], first)
    out = Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    for n, data, _ in plan_files(p["name"], first, p["plan"], findings, score, p["material"]):
        (out / n).write_bytes(data)
        print(out / n, len(data))


if __name__ == "__main__":
    main()
