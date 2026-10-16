"""The Dijital Ayak İzi Raporu as an A4 PDF (owner 2026-10-16: "ekte PDF"): the same Neon design, never shortened.

WeasyPrint turns the mail HTML into a PDF inside the job (no external service, no model call). Callers treat any
failure as "no PDF": the owner still gets the HTML mail; a mail that promises an attachment is not sent without it.
"""
import hashlib
import re
import unicodedata

PRINT_CSS = """
@page { size: A4; margin: 0; background: #05081A; }
html, body { background: #05081A; }
td:not([align]) { text-align: left; }
table table table { page-break-inside: avoid; }
"""
MAX_BYTES = 3 * 1024 * 1024


class ReportPdfError(Exception):
    pass


def report_pdf(html):
    """HTML -> PDF bytes (A4, dark page). Raises ReportPdfError."""
    try:
        import weasyprint
        data = weasyprint.HTML(string=html).write_pdf(stylesheets=[weasyprint.CSS(string=PRINT_CSS)],
                                                      presentational_hints=True)
    except Exception as exc:
        raise ReportPdfError("report_pdf_failed:" + type(exc).__name__) from None
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-") or not 0 < len(data) <= MAX_BYTES:
        raise ReportPdfError("report_pdf_invalid")
    return data


def pdf_name(label, today):
    """ASCII file name: Hamle-AI-Dijital-Ayak-Izi-Raporu-Acme-Yapi-2026-10-16.pdf"""
    folded = unicodedata.normalize("NFKD", str(label).replace("ı", "i").replace("İ", "I"))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folded.encode("ascii", "ignore").decode()).strip("-")[:60] or "Kurum"
    day = re.sub(r"[^0-9-]", "", str(today))[:10]
    return f"Hamle-AI-Dijital-Ayak-Izi-Raporu-{slug}-{day}.pdf"


def attachment(label, today, data):
    """The mail payload part: JSON-safe (base64) with its digest, checked again on readback."""
    import base64
    return {"filename": pdf_name(label, today), "content_type": "application/pdf",
            "data_b64": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest()}
