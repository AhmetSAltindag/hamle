"""Linux image build gate: the monthly content plan renders as PDF (WeasyPrint) and DOCX (stdlib zip)."""
from datetime import date

import v11_content_plan_files as pf


def smoke():
    day = {"date": "2026-12-01", "pillar": "Şarj alışkanlığı", "topic": "Kış şarjı çğıöşü İÇ", "format": "karusel",
           "platform": "instagram", "special_day": None, "why": "Kış başlıyor.", "recommended": True,
           "hook": "Kışın şarj neden yavaşlar?", "caption": "Kısa cevap: biraz.", "linkedin_caption": None,
           "hashtags": ["#kis"], "cta": "Uygulamayı indir.", "visual_brief": "Karlı otopark.",
           "on_screen_text": "Kış şarjı 101", "size": "4:5", "success_metric": "Kaydetme sayısı"}
    plan = {"strategy": ["Çatı mesaj: şarj her yerde, kolay."], "last_month": [], "pillars": [], "brand_asks": [],
            "algorithm_moves": [], "shoots": [], "days": [day]}
    files = pf.plan_files("Hamle Smoke", date(2026, 12, 1), plan, [], 100,
                          {"brand": {"name": "Hamle Smoke", "platforms": ["instagram"], "posts_per_month": [18, 20]},
                           "special_days": [], "algorithm": []})
    (pdf_name, pdf, _), (docx_name, docx, _) = files
    if not (pdf_name.endswith(".pdf") and pdf.startswith(b"%PDF-") and len(pdf) > 1000):
        raise ValueError("content_plan_pdf_smoke_failed")
    if not (docx_name.endswith(".docx") and docx.startswith(b"PK")):
        raise ValueError("content_plan_docx_smoke_failed")
    return len(pdf), len(docx)


if __name__ == "__main__":
    print({"status": "content_plan_files_smoke_ok", "bytes": smoke()})
