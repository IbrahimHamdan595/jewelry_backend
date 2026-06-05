"""PDF export via WeasyPrint — RTL/Arabic-aware.

Jinja-less: small f-string HTML builders. `render_pdf` turns an HTML string into
PDF bytes; `pdf_response` wraps it in a download response. `document(...)` is the
shared shell: it sets `dir`/`lang` and a font stack led by **Noto Naskh Arabic**
(shipped via the Dockerfile's fonts-noto-core) so Arabic *shapes* and *right-aligns*
through Pango/HarfBuzz instead of rendering as boxes or running left-to-right.

Numeric cells force `direction: ltr` so "1,234.56" never reorders next to signs in
an RTL context, while still sitting on the page's end edge.
"""
import io
from html import escape

from fastapi.responses import StreamingResponse

PDF_MEDIA_TYPE = "application/pdf"

_BASE_CSS = """
@page { size: A4; margin: 1.6cm 1.4cm; }
* { box-sizing: border-box; }
body { font-family: 'Noto Naskh Arabic', 'Noto Sans', sans-serif;
       color: #1f2937; font-size: 12px; line-height: 1.5; }
h1 { font-size: 20px; margin: 0; color: #111827; }
.brand { display: flex; justify-content: space-between; align-items: flex-end;
         border-bottom: 2px solid #caa86a; padding-bottom: 10px; margin-bottom: 18px; }
.brand .name { font-size: 18px; font-weight: 700; color: #8a6d2f; }
.muted { color: #6b7280; font-size: 11px; }
.meta { margin-bottom: 14px; }
.meta div { margin: 2px 0; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { padding: 7px 9px; border-bottom: 1px solid #e5e7eb; text-align: start; vertical-align: top; }
th { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: #6b7280; }
td.num, th.num { text-align: right; direction: ltr; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.total td { font-weight: 700; border-top: 2px solid #111827; border-bottom: none; }
tr.section td { background: #f9fafb; font-size: 10px; text-transform: uppercase;
                letter-spacing: .06em; color: #6b7280; font-weight: 600; }
.summary { margin-top: 16px; }
.summary .row { display: flex; justify-content: space-between; padding: 4px 0; }
.summary .row.grand { font-weight: 700; border-top: 2px solid #111827; margin-top: 6px; padding-top: 8px; }
.amt { direction: ltr; font-variant-numeric: tabular-nums; }
"""


def document(*, title: str, lang: str, body: str, subtitle: str = "") -> str:
    """Wrap report `body` HTML in the branded, dir-aware shell."""
    rtl = lang == "ar"
    store = "فواز النمل" if rtl else "Fawaz El Namel"
    tagline = "مجوهرات ذهبية" if rtl else "Gold Jewellery"
    dir_attr = "rtl" if rtl else "ltr"
    return f"""<!DOCTYPE html>
<html dir="{dir_attr}" lang="{escape(lang)}">
<head><meta charset="utf-8"><style>{_BASE_CSS}</style></head>
<body>
  <div class="brand">
    <div><div class="name">{escape(store)}</div><div class="muted">{escape(tagline)}</div></div>
    <div><h1>{escape(title)}</h1><div class="muted">{escape(subtitle)}</div></div>
  </div>
  {body}
</body></html>"""


def table(headers: list[tuple[str, bool]], rows: list[list], *, total_row: list | None = None) -> str:
    """Build an HTML table. `headers` is a list of (label, is_numeric); each row is
    a list of cell values aligned to those columns. `total_row` is emphasised."""
    head = "".join(f'<th class="{"num" if num else ""}">{escape(str(h))}</th>' for h, num in headers)
    nums = [num for _, num in headers]

    def _row(cells, cls=""):
        tds = "".join(
            f'<td class="{"num" if nums[i] else ""}">{escape(str(c))}</td>'
            for i, c in enumerate(cells))
        return f'<tr class="{cls}">{tds}</tr>'

    body = "".join(_row(r) for r in rows)
    tail = _row(total_row, "total") if total_row is not None else ""
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}{tail}</tbody></table>"


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html).write_pdf()


def pdf_response(html: str, *, filename: str) -> StreamingResponse:
    body = render_pdf(html)
    return StreamingResponse(
        io.BytesIO(body), media_type=PDF_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'})
