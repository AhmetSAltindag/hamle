"""Dijital Ayak İzi Raporu as an e-mail: the "Neon Kontrol Odası" design the owner picked (2026-10-16).

Mail-client safe: tables only, inline styles, max 680 px, no script, no SVG, no web fonts; a phone stacks the
two-column rows. Every number on it is either measured by Hamle (v11_footprint) or written by the strategist with
its source marks; nothing is drawn that the data does not carry. Kept well under Gmail's ~102 KB clipping limit.
"""
import copy
import re
from datetime import date
from html import escape

TB = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'
FA = "font-family:Arial,Helvetica,sans-serif;"
FM = "font-family:'Courier New',Courier,monospace;"
PAGE, BG, TILE, LINE = "#05081A", "#0B1026", "#111838", "#23305E"
TEXT, MUTED, WHITE, SOFT = "#E8ECFF", "#8E99C9", "#FFFFFF", "#C9D0F0"
YELLOW, PINK, CYAN, TRACK = "#EBE656", "#FF4FA3", "#5AD8FF", "#26305A"
RAG = {"green": ("#2BD47D", "#0F1F33", "İYİ"), "amber": ("#F5B53D", "#1F1E2E", "DİKKAT"),
       "red": ("#FF4D6A", "#231431", "KRİTİK"), None: (MUTED, TILE, "")}
MIX = (YELLOW, PINK, CYAN, MUTED)
SHARE_BAR = {"high": (85, "YÜKSEK PAY"), "medium": (55, "ORTA PAY"), "low": (30, "DÜŞÜK PAY")}
MONTHS = ("Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara")
FORMATS = {"reels": "Reels", "carousel": "Karusel", "carousel_album": "Karusel", "image": "Görsel", "post": "Gönderi",
           "video": "Video", "short": "Shorts", "story": "Hikâye", "other": "Diğer"}
PLATFORM_ORDER = ("instagram", "linkedin", "youtube", "tiktok", "x", "facebook")
LIMIT = 98_000  # bytes; Gmail clips a message body at ~102 KB
GROUPS = (("KANAL PANOSU", ("channels", "balance")),
          ("DİJİTAL VARLIK DEĞERLENDİRMESİ", ("message", "algorithm", "web", "google")),
          ("HEDEF KİTLE TAHMİNİ", ("audience",)), ("RAKİP KIYASI", ("competitors",)),
          ("SEKTÖR DİNAMİKLERİ", ("sector",)), ("KURUM, FIRSATLAR VE RİSKLER", ("company", "risks")))


def e(t):
    return escape(str(t if t is not None else ""), quote=True)


def txt(s, size=14, color=TEXT, w="normal", lh=None, extra="", font=FA):
    return (f'<div style="{font}font-size:{size}px;line-height:{lh or int(size * 1.5)}px;color:{color};'
            f'font-weight:{w};{extra}">{s}</div>')


def fill(w, h, c):
    wa = f' width="{w}%"' if w is not None else ""
    return f'<td{wa} height="{h}" bgcolor="{c}" style="font-size:1px;line-height:1px">&nbsp;</td>'


def bar(p, fg, bg=TRACK, h=8):
    p = max(0, min(100, int(round(p))))
    cells = fill(None, h, fg) if p >= 100 else fill(max(p, 1), h, fg) + fill(None, h, bg)
    return f'<table {TB} width="100%"><tr>{cells}</tr></table>'


def label(text, color, size=10):
    return (f'<span style="color:{color};font-size:13px">&#9679;</span><span style="{FA}font-size:{size}px;'
            f'font-weight:bold;letter-spacing:1px;color:{color}">&nbsp;{text}</span>')


def followers(n):
    if not isinstance(n, int):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".", ",")
    if n >= 1000:
        return f"{n / 1000:.1f}B".replace(".", ",")
    return str(n)


def day(iso):
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None
    return d


def human_date(iso):
    d = day(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}" if d else "—"


def channel_rag(ch, today):
    last, now = day(ch.get("last_post") or ""), day(today)
    if not last or not now:
        return "red"
    age = (now - last).days
    return "green" if age <= 14 else "amber" if age <= 45 else "red"


def spark(per_month):
    keys = sorted(per_month)[-6:]
    vals = [int(per_month[k] or 0) for k in keys]
    top = max(vals or [0]) or 1
    cols, nums = [], []
    for v in vals:
        h = max(2, round(v / top * 30)) if v else 2
        cols.append(f'<td valign="bottom" height="30" style="padding:0 2px"><table {TB} width="13"><tr>'
                    f'{fill(None, h, YELLOW if v else TRACK)}</tr></table></td>')
        nums.append(f'<td align="center" style="font-size:9px;line-height:12px;color:{MUTED};padding-top:3px">{v}</td>')
    first = keys[0] if keys else ""
    span = (f"{MONTHS[int(first[5:7]) - 1]} → {MONTHS[int(keys[-1][5:7]) - 1]}" if len(first) == 7 else "")
    return f'<table {TB} style="{FA}"><tr>{"".join(cols)}</tr><tr>{"".join(nums)}</tr></table>', span


def channel_tile(platform, ch, today, link_style):
    name = {"instagram": "Instagram", "linkedin": "LinkedIn", "youtube": "YouTube", "tiktok": "TikTok", "x": "X",
            "facebook": "Facebook"}.get(platform, platform)
    url = ch.get("url")
    if not ch.get("measured"):
        body = (f'<table {TB} width="100%"><tr><td>{txt(e(name), 16, WHITE, "bold")}</td>'
                f'<td align="right">{label("ÖLÇÜLEMEDİ", MUTED)}</td></tr></table>'
                + txt("Resmi okuma yolu yok; değerlendirmesi araştırma notlarında.", 12, MUTED, extra="padding:8px 0")
                + (txt(f'<a {link_style} href="{e(url)}">Profili aç &rarr;</a>', 12) if url else ""))
        return body
    rag = channel_rag(ch, today)
    color = RAG[rag][0]
    chart, span = spark(ch.get("per_month") or {})
    stat = lambda k, v, c=TEXT: (f'<tr><td style="{FA}font-size:12px;line-height:20px;color:{MUTED}">{k}</td>'  # noqa: E731
                                 f'<td align="right" style="{FA}font-size:12px;line-height:20px;font-weight:bold;'
                                 f'color:{c}">{v}</td></tr>')
    gap = ch.get("longest_gap_days")
    avg = ch.get("avg_per_month")
    mix = sorted((ch.get("formats") or {}).items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in mix) or 1
    mix_bar = "".join(fill(max(1, round(v / total * 100)), 8, MIX[min(i, 3)]) for i, (_, v) in enumerate(mix[:4]))
    legend = " &nbsp; ".join(f'<span style="color:{MIX[min(i, 3)]}">&#9632;</span>&nbsp;{e(FORMATS.get(k, k))} '
                             f'%{round(v / total * 100)}' for i, (k, v) in enumerate(mix[:4]))
    title = f'<a {link_style} href="{e(url)}">{e(name)}</a>' if url else e(name)
    return (f'<table {TB} width="100%"><tr><td>{txt(title, 16, WHITE, "bold")}</td>'
            f'<td align="right">{label(RAG[rag][2], color)}</td></tr>'
            f'<tr><td valign="bottom" style="padding:6px 0 10px">{txt(followers(ch.get("followers")), 28, YELLOW, "bold", 32)}'
            f'{txt("takipçi" + (" · " + span if span else ""), 11, MUTED)}</td>'
            f'<td align="right" valign="bottom" style="padding:6px 0 10px">{chart}</td></tr>'
            f'<tr><td colspan="2"><table {TB} width="100%">'
            + stat("Paylaşım/ay (6 ay ort.)", e(str(avg).replace(".", ",")) if avg is not None else "—")
            + stat("Son paylaşım", human_date(ch.get("last_post")))
            + stat("En uzun sessizlik", f"{gap} gün" if isinstance(gap, int) else "—",
                   RAG["red"][0] if isinstance(gap, int) and gap > 45 else
                   RAG["amber"][0] if isinstance(gap, int) and gap > 14 else TEXT)
            + (stat("Etkileşim oranı", f"%{str(ch['engagement_rate_pct']).replace('.', ',')}")
               if ch.get("engagement_rate_pct") is not None else "")
            + "</table></td></tr>"
            + (f'<tr><td colspan="2" style="padding-top:10px">{txt("Format karışımı", 11, MUTED, extra="padding-bottom:4px")}'
               f'<table {TB} width="100%"><tr>{mix_bar}</tr></table>'
               f'{txt(legend, 11, MUTED, extra="padding-top:6px")}</td></tr>' if mix else "")
            + "</table>")


def tiles(measured, today, link_style):
    chans = measured.get("channels") or {}
    names = [p for p in PLATFORM_ORDER if p in chans] + [p for p in chans if p not in PLATFORM_ORDER]
    names.sort(key=lambda p: not chans[p].get("measured"))  # measured channels first, then the rest
    wrap = lambda s: (f'<table {TB} width="100%"><tr><td bgcolor="{TILE}" style="background:{TILE};'  # noqa: E731
                      f'border:1px solid {LINE};border-top:2px solid {CYAN};padding:14px">{s}</td></tr></table>')
    cells = [wrap(channel_tile(p, chans[p], today, link_style)) for p in names]
    if not cells:
        return txt("Sitede sosyal kanal bağlantısı bulunamadı.", 13, MUTED, extra="padding-bottom:12px")
    rows = []
    for i in range(0, len(cells), 2):
        a = f'<td class="col gap" width="50%" valign="top" style="padding:0 6px 12px 0">{cells[i]}</td>'
        b = (f'<td class="col gap" width="50%" valign="top" style="padding:0 0 12px 6px">{cells[i + 1]}</td>'
             if i + 1 < len(cells) else '<td class="col hm" width="50%">&nbsp;</td>')
        rows.append(f"<tr>{a}{b}</tr>")
    note = txt("Hamle'nin kendi ölçümü · son 6 ay · mini çubuklar aylık paylaşım sayısı.", 11, MUTED,
               extra="padding-bottom:12px")
    return note + f'<table {TB} width="100%">{"".join(rows)}</table>'


def block(title, sec, r):
    """One report section: a severity stripe, its title, paragraphs and bullets."""
    color, tint, word = RAG.get(sec.get("rag"), RAG[None])
    body = "".join(txt(r(p), 14, TEXT, extra="padding-top:6px") for p in sec.get("paragraphs") or [])
    bullets = sec.get("bullets") or []
    if bullets:
        body += f'<table {TB} width="100%" style="margin-top:6px">' + "".join(
            f'<tr><td width="18" valign="top" style="{FA}font-size:14px;line-height:21px;color:{PINK};'
            f'font-weight:bold">&rsaquo;</td><td style="{FA}font-size:13px;line-height:20px;color:{SOFT};'
            f'padding-bottom:4px">{r(b)}</td></tr>' for b in bullets) + "</table>"
    return (f'<table {TB} width="100%" style="margin-bottom:10px"><tr>'
            f'<td width="5" bgcolor="{color}" style="font-size:1px;line-height:1px">&nbsp;</td>'
            f'<td bgcolor="{tint}" style="background:{tint};padding:12px 14px">'
            f'<table {TB} width="100%"><tr><td>{txt(e(title), 15, WHITE, "bold")}</td>'
            f'<td align="right" width="84">{label(word, color) if word else ""}</td></tr></table>{body}</td></tr></table>')


def panel(n, title, body):
    return (f'<tr><td style="padding-top:10px"><table {TB} width="100%"><tr>'
            f'<td class="px" bgcolor="{BG}" style="background:{BG};border:1px solid {LINE};padding:22px 24px">'
            f'<div style="padding-bottom:12px;margin-bottom:14px;border-bottom:1px dashed {LINE}">'
            f'<span style="{FM}font-size:12px;color:{PINK};font-weight:bold">// {n:02d}</span>'
            f'<span style="{FA}font-size:13px;font-weight:bold;letter-spacing:3px;color:{YELLOW}">&nbsp;&nbsp;{title}</span>'
            f'</div>{body}</td></tr></table></td></tr>')


def clip(text, n):
    t = str(text or "")
    if len(t) <= n:
        return t
    t = t[:n].rsplit(" ", 1)[0]
    return re.sub(r"\[[^\]]*$", "", t).rstrip(" ,;:") + "…"


def shrink(written, sections, level):
    """A shorter copy for a report that would outgrow the mail limit; the full text stays in Basecamp."""
    w, secs = copy.deepcopy(written), copy.deepcopy(sections)
    if level >= 1:
        w["mail"] = ""
    keep_p, keep_b, n = {1: (2, 3, 900), 2: (1, 2, 600), 3: (1, 0, 280), 4: (1, 0, 160)}.get(level, (2, 6, 900))
    for _, _, sec in secs:
        sec["paragraphs"] = [clip(p, n) for p in (sec.get("paragraphs") or [])[:keep_p]]
        sec["bullets"] = [clip(b, 200) for b in (sec.get("bullets") or [])[:keep_b]]
    if level >= 2:
        for m in w.get("moves") or []:
            for f in ("why", "what", "measure", "first_30_days"):
                m[f] = clip(m.get(f), {2: 220, 3: 140}.get(level, 90))
        for key in ("plan_30", "plan_90"):
            for x in w.get(key) or []:
                x["what"] = clip(x.get("what"), 200 if level == 2 else 120)
        for x in w.get("competitor_rows") or []:
            x["them"] = [clip(t, 120) for t in x.get("them") or []]
        w["meeting_notes"] = (w.get("meeting_notes") or [])[:3]
    if level >= 4:
        w["competitor_rows"] = (w.get("competitor_rows") or [])[:3]
        for key in ("plan_30", "plan_90", "summary"):
            w[key] = (w.get(key) or [])[:3]
    if level >= 3:
        w["meeting_notes"] = []
        for s in w.get("audience_segments") or []:
            s["why"] = clip(s.get("why"), 140)
    return w, secs


def render(label_text, written, sources, measured, *, today, link, sections, refs, client=False, full=False,
           attached=False):
    """sections: [(key, title, section)] in report order; refs(text) -> escaped text with [n] source links.
    client: the company's version (no team notes, no Basecamp); full: never shortened (the PDF);
    attached: the mail carries the PDF. A mail body is shortened step by step until it fits the mail limit."""
    for level in range(1 if full else 5):
        w, secs = shrink(written, sections, level) if level else (written, sections)
        html = _render(label_text, w, sources, measured, today=today, link=link, sections=secs, refs=refs,
                       short=bool(level), client=client, attached=attached)
        if len(html.encode("utf-8")) <= LIMIT:
            break
    return html


def _render(label_text, written, sources, measured, *, today, link, sections, refs, short, client, attached):
    link_style = f'style="color:{CYAN}"'
    r = lambda t: refs(t).replace("<a href=", f"<a {link_style} href=")  # noqa: E731
    by = {k: (title, sec) for k, title, sec in sections}
    sc = written.get("score") or {}
    total = int(sc.get("total") or 0)
    status, scolor = (("GÜÇLÜ", RAG["green"][0]) if total >= 75 else ("İYİ · GELİŞTİRİLEBİLİR", YELLOW) if total >= 60
                      else ("ORTA · GELİŞTİRİLEBİLİR", RAG["amber"][0]) if total >= 45
                      else ("ZAYIF · ACİL HAMLE GEREKLİ", RAG["red"][0]))
    sub = lambda v: YELLOW if v >= 60 else PINK if v < 50 else RAG["amber"][0]  # noqa: E731
    subs = "".join(
        f'<tr><td style="padding:7px 0 4px;{FA}font-size:13px;line-height:17px;color:{SOFT}">{e(p.get("name"))}</td>'
        f'<td width="40" align="right" style="padding:7px 0 4px;{FA}font-size:18px;line-height:18px;font-weight:bold;'
        f'color:{sub(int(p.get("score") or 0))}">{int(p.get("score") or 0)}</td></tr>'
        f'<tr><td colspan="2" style="padding-bottom:4px">{bar(int(p.get("score") or 0), sub(int(p.get("score") or 0)), "#1E2750", 6)}</td></tr>'
        for p in sc.get("parts") or [])
    ticks = "".join(fill(None, 4, YELLOW if i * 10 < total else TRACK)
                    + (f'<td width="3" style="font-size:1px;line-height:1px">&nbsp;</td>' if i < 9 else "")
                    for i in range(10))
    rows = [f'<tr><td><table {TB} width="100%"><tr>'
            f'<td bgcolor="{YELLOW}" style="background:{YELLOW};padding:10px 16px;{FA}font-size:12px;line-height:16px;'
            f'font-weight:bold;letter-spacing:2.5px;color:{BG}">HAMLE AI · DİJİTAL AYAK İZİ RAPORU</td>'
            f'<td width="96" align="center" bgcolor="{PINK}" style="background:{PINK};padding:10px 8px;{FA}font-size:11px;'
            f'line-height:16px;font-weight:bold;letter-spacing:2px;color:{BG}">{"ÖZEL RAPOR" if client else "EKİP İÇİ"}</td></tr></table></td></tr>',
            f'<tr><td style="padding-top:10px"><table {TB} width="100%"><tr>'
            f'<td class="px" bgcolor="{BG}" style="background:{BG};border:1px solid {LINE};border-top:3px solid {PINK};'
            f'padding:24px">'
            f'<div style="{FM}font-size:11px;line-height:16px;color:{MUTED};letter-spacing:1px">HEDEF &gt; '
            f'{e(label_text).upper()} · ÖLÇÜM {e(human_date(today)).upper()} · SON 6 AY</div>'
            f'{txt(e(label_text), 30, WHITE, "bold", 36, "padding:6px 0 18px")}'
            f'<table {TB} width="100%"><tr><td class="col gap" width="40%" valign="top" style="padding-right:18px">'
            f'<div style="{FM}font-size:11px;letter-spacing:2px;color:{PINK};font-weight:bold">DİJİTAL AYAK İZİ SKORU</div>'
            f'<div class="big" style="{FA}font-size:112px;line-height:108px;font-weight:bold;color:{YELLOW};'
            f'letter-spacing:-4px">{total}<span style="font-size:22px;letter-spacing:0;color:{MUTED}">/100</span></div>'
            f'<table {TB} width="100%" style="margin:8px 0"><tr>{ticks}</tr></table>'
            f'<div style="{FA}font-size:12px;font-weight:bold;letter-spacing:1.5px;color:{scolor}">&#9679; {status}</div></td>'
            f'<td class="col" width="60%" valign="top" style="border-left:1px solid {LINE};padding-left:18px">'
            f'<table {TB} width="100%">{subs}</table></td></tr></table>'
            + (f'<table {TB} width="100%" style="margin-top:18px"><tr><td bgcolor="#141C44" style="background:#141C44;'
               f'border-left:3px solid {YELLOW};padding:14px 16px"><div style="{FM}font-size:10px;letter-spacing:2px;'
               f'color:{YELLOW};font-weight:bold">ANA BULGU</div>'
               f'{txt(r(written.get("headline")), 17, WHITE, "bold", 24, "padding-top:4px")}</td></tr></table>'
               if written.get("headline") else "")
            + "</td></tr></table></td></tr>"]
    panels = []
    if written.get("summary"):
        panels.append(("YÖNETİCİ ÖZETİ", f'<table {TB} width="100%">' + "".join(
            f'<tr><td width="44" valign="top" style="padding:12px 0;{FA}font-size:26px;line-height:28px;'
            f'font-weight:bold;color:{YELLOW}">0{i}</td><td valign="top" style="padding:12px 0;'
            f'{"border-top:1px solid " + LINE if i > 1 else ""}">{txt(r(x), 15, TEXT)}</td></tr>'
            for i, x in enumerate(written["summary"], 1)) + "</table>"))
    for title, keys in GROUPS:
        body = "".join(block(by[k][0], by[k][1], r) for k in keys if k in by)
        if keys[0] == "channels":
            body = tiles(measured, today, link_style) + body
        elif keys[0] == "audience" and written.get("audience_segments"):
            segs = ""
            for i, s in enumerate(written["audience_segments"]):
                width, word = SHARE_BAR.get(s.get("share"), (30, ""))
                segs += (f'<tr><td style="padding:8px 0"><table {TB} width="100%"><tr>'
                         f'<td>{txt(e(s.get("name")), 15, WHITE, "bold")}</td>'
                         f'<td align="right" style="{FA}font-size:11px;font-weight:bold;letter-spacing:1px;'
                         f'color:{MIX[i % 3]}">{word}</td></tr>'
                         f'<tr><td colspan="2" style="padding:6px 0">{bar(width, MIX[i % 3], TRACK, 10)}</td></tr>'
                         f'<tr><td colspan="2">{txt(r(s.get("why")), 13, MUTED)}</td></tr></table></td></tr>')
            body = (f'<table {TB} width="100%" style="margin-bottom:12px">{segs}</table>'
                    + txt("Tahmin: kitle payları kamuya açık sinyallerden çıkarıldı, ölçüm değildir.", 11, MUTED,
                          extra="padding-bottom:12px") + body)
        elif keys[0] == "competitors" and written.get("competitor_rows"):
            cmp = ""
            for x in written["competitor_rows"]:
                them = "".join(f'<tr><td style="padding:8px 10px;border-top:1px solid {LINE};{FA}font-size:13px;'
                               f'line-height:19px;color:{SOFT}">{r(t)}</td></tr>' for t in x.get("them") or [])
                cmp += (f'<tr><td style="padding:0 0 12px"><div style="{FM}font-size:11px;font-weight:bold;'
                        f'letter-spacing:1px;color:{PINK};padding-bottom:6px">{e(x.get("metric")).upper()}</div>'
                        f'<table {TB} width="100%" bgcolor="{TILE}" style="background:{TILE};border:1px solid {LINE}">'
                        f'<tr><td bgcolor="#1C2552" style="background:#1C2552;border-left:4px solid {YELLOW};'
                        f'padding:8px 10px;{FA}font-size:14px;line-height:20px;font-weight:bold;color:{WHITE}">'
                        f'<span style="color:{YELLOW}">SİZ&nbsp;&rsaquo;</span> {r(x.get("us"))}</td></tr>{them}'
                        f'</table></td></tr>')
            body = f'<table {TB} width="100%">{cmp}</table>' + body
        if body:
            panels.append((title, body))
    moves, p30, p90 = written.get("moves") or [], written.get("plan_30") or [], written.get("plan_90") or []
    if moves or p30 or p90:
        body = "".join(
            f'<table {TB} width="100%" style="margin-bottom:10px"><tr><td bgcolor="#1E1335" style="background:#1E1335;'
            f'border-left:3px solid {PINK};padding:12px 14px"><span style="{FM}font-size:11px;font-weight:bold;'
            f'color:{PINK}">HAMLE {i} &gt;</span><span style="{FA}font-size:15px;font-weight:bold;color:{WHITE}"> '
            f'{e(m.get("title"))}</span>'
            + "".join(txt(f'<strong style="color:{SOFT}">{k}:</strong> {r(m.get(f))}', 13, MUTED, extra="padding-top:4px")
                      for k, f in (("Neden", "why"), ("Ne yaparız", "what"), ("Nasıl ölçeriz", "measure"),
                                   ("İlk 30 gün", "first_30_days")) if m.get(f))
            + "</td></tr></table>" for i, m in enumerate(moves, 1))
        col = lambda head, sub, items, accent: (  # noqa: E731
            f'<table {TB} width="100%"><tr><td bgcolor="{TILE}" style="background:{TILE};border:1px solid {LINE};'
            f'padding:16px">{txt(sub, 11, accent, "bold", extra="letter-spacing:2px")}'
            f'{txt(head, 22, TEXT, "bold", 28, "padding:2px 0 6px")}<table {TB} width="100%">'
            + "".join(f'<tr><td width="22" valign="top" style="padding:8px 0;{FA}font-size:14px;line-height:20px;'
                      f'color:{accent};font-weight:bold">&rarr;</td><td style="padding:8px 0">'
                      f'{txt(e(x.get("title")), 14, TEXT, "bold")}{txt(r(x.get("what")), 13, SOFT)}'
                      f'{txt("Hedef: " + r(x.get("target")), 12, MUTED)}</td></tr>' for x in items)
            + "</table></td></tr></table>")
        if p30 or p90:
            cells = [c for c in (col("İlk 30 gün", "KISA VADE", p30, YELLOW) if p30 else "",
                                 col("90 günde", "ORTA VADE", p90, PINK) if p90 else "") if c]
            if len(cells) == 2:
                body += (f'<table {TB} width="100%" style="margin-top:6px"><tr>'
                         f'<td class="col gap" width="50%" valign="top" style="padding-right:6px">{cells[0]}</td>'
                         f'<td class="col gap" width="50%" valign="top" style="padding-left:6px">{cells[1]}</td></tr></table>')
            else:
                body += cells[0]
        panels.append(("BİZ YÖNETSEYDİK", body))
    if written.get("closing_summary"):
        panels.append(("SON SÖZ · YÖNETİCİ ÖZETİ",
                       f'<table {TB} width="100%"><tr><td bgcolor="#141C44" style="background:#141C44;'
                       f'border-left:3px solid {YELLOW};padding:16px 18px">'
                       f'{txt(r(written["closing_summary"]), 16, WHITE, "bold", 25)}</td></tr></table>'))
    team = ""
    if written.get("meeting_notes"):
        team += txt("Görüşme notu", 13, YELLOW, "bold", extra="letter-spacing:1px;padding-bottom:4px") + "".join(
            txt(f'<span style="color:{PINK}">&rsaquo;</span> {r(x)}', 13, SOFT, extra="padding-bottom:4px")
            for x in written["meeting_notes"])
    mail = str(written.get("mail") or "").strip()
    if mail:
        team += (txt("Kuruma gidecek mail taslağı", 13, YELLOW, "bold", extra="letter-spacing:1px;padding:12px 0 6px")
                 + f'<table {TB} width="100%"><tr><td bgcolor="{TILE}" style="background:{TILE};border:1px dashed {LINE};'
                 f'padding:14px 16px;{FA}font-size:13px;line-height:20px;color:{SOFT}">'
                 + "<br>".join(e(x) for x in mail.split("\n")) + "</td></tr></table>")
    if team and not client:
        panels.append(("YALNIZ EKİP İÇİ", team))
    src = "".join(f'<tr><td width="28" valign="top" style="{FA}font-size:11px;line-height:17px;color:{PINK};'
                  f'font-weight:bold">[{i}]</td><td style="{FA}font-size:11px;line-height:17px;word-break:break-all">'
                  f'<a style="color:{MUTED};text-decoration:none" href="{e(u)}">{e(u)}</a></td></tr>'
                  for i, u in enumerate(sources, 1))
    button = (f'<table {TB} align="center" style="margin-top:20px"><tr><td align="center" bgcolor="{YELLOW}" '
              f'style="background:{YELLOW}"><a href="{e(link)}" target="_blank" style="display:inline-block;'
              f'padding:14px 30px;{FA}font-size:15px;font-weight:bold;color:{BG};text-decoration:none">'
              f'Basecamp&#x27;te aç &rarr;</a></td></tr></table>'
              + txt("Kuruma göndermek için to-do'ya \"Hamle gönder\" yazın.", 12, MUTED, extra="text-align:center;padding-top:8px"))
    if short:
        button += txt("Bu mail kısaltılmış özettir; raporun tamamı " + ("ekteki PDF'te, " if attached else "")
                      + "kuruma gidecek mail taslağı Basecamp'teki to-do'da.", 12, YELLOW,
                      extra="text-align:center;padding-top:8px")
    if client:
        button = ""
    unmeasured = [p for p, c in (measured.get("channels") or {}).items() if not c.get("measured")]
    note = txt("[Ö] Hamle ölçümü. Resmi arayüzle ölçülemeyen kanallar kısmi değerlendirilmiştir." if unmeasured
               else "[Ö] Hamle ölçümü.", 11, MUTED, extra="padding-top:10px")
    panels.append(("KAYNAKLAR", (f'<table {TB} width="100%">{src}</table>' if src else "") + note + button))
    rows += [panel(i, t, b) for i, (t, b) in enumerate(panels, 1)]
    rows.append(f'<tr><td style="padding:22px 16px 6px" align="center">'
                f'<div style="{FA}font-size:12px;line-height:18px;font-weight:bold;letter-spacing:2px;color:{YELLOW}">'
                f'HAMLE AI <span style="color:{PINK}">by</span> GAMECHANGERS</div>'
                f'<div style="{FM}font-size:11px;line-height:18px;color:{MUTED}">'
                + (f"Bu rapor {e(label_text)} için hazırlanmıştır." if client
                   else "Bu rapor yalnız ekip içindir; kuruma kendiliğinden gitmez.") + '</div></td></tr>')
    pre = e(f"{label_text} · Dijital Ayak İzi Skoru {total}/100")
    return (f'<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="x-apple-disable-message-reformatting"><meta name="color-scheme" content="dark">'
            f'<meta name="supported-color-schemes" content="dark"><title>Hamle AI Dijital Ayak İzi Raporu</title>'
            f'<style>@media only screen and (max-width:620px){{.col{{display:block!important;width:100%!important;'
            f'max-width:100%!important;border-left:0!important;padding-left:0!important}}'
            f'.px{{padding-left:16px!important;padding-right:16px!important}}'
            f'.gap{{padding-left:0!important;padding-right:0!important;padding-bottom:12px!important}}'
            f'.big{{font-size:84px!important;line-height:84px!important}}.hm{{display:none!important}}}}</style></head>'
            f'<body style="margin:0;padding:0;background:{PAGE}" bgcolor="{PAGE}">'
            f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{pre}</div>'
            f'<table {TB} width="100%" bgcolor="{PAGE}" style="background:{PAGE}"><tr>'
            f'<td align="center" style="padding:20px 8px">'
            f'<!--[if mso]><table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->'
            f'<table {TB} width="100%" style="max-width:680px;width:100%">{"".join(rows)}</table>'
            f'<!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>')
