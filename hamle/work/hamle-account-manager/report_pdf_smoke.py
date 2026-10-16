"""Linux image build gate: WeasyPrint and its system libraries (Pango) turn a dark A4 page into a PDF."""
import weasyprint


def smoke():
    data = weasyprint.HTML(string='<table style="background:#05081A"><tr><td style="color:#EBE656;'
                                  'font-family:Arial">Hamle AI · Dijital Ayak İzi 58/100 çğıöşü</td></tr></table>'
                           ).write_pdf(stylesheets=[weasyprint.CSS(string="@page{size:A4;margin:0}")])
    if not data.startswith(b"%PDF-") or len(data) < 1000:
        raise ValueError("report_pdf_smoke_failed")
    return len(data)


if __name__ == "__main__":
    print({"status": "report_pdf_smoke_ok", "bytes": smoke()})
