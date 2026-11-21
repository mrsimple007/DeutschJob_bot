"""Cover letter (Anschreiben) as a PDF, laid out like a German business letter (DIN 5008 style):
sender line, recipient, place + date on the right, bold subject, body."""
import os, re
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from config import TZ, log

# First regular+bold pair that exists is used. A Unicode TTF keeps every character;
# without one we fall back to built-in Helvetica (Latin-1: ä ö ü ß are fine).
FONT_CANDIDATES = [
    (os.getenv("LETTER_FONT", ""), os.getenv("LETTER_FONT_BOLD", "")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]
_TO_LATIN1 = str.maketrans({"„": '"', "“": '"', "”": '"', "‚": "'", "‘": "'", "’": "'", "–": "-",
                            "—": "-", "…": "...", "€": "EUR", "•": "-", "\u00a0": " ", "\u200b": ""})


def _setup_font(pdf: FPDF):
    """Returns (family, text cleaner)."""
    for reg, bold in FONT_CANDIDATES:
        if reg and bold and Path(reg).is_file() and Path(bold).is_file():
            try:
                pdf.add_font("Letter", "", reg)
                pdf.add_font("Letter", "B", bold)
                return "Letter", lambda s: s
            except Exception:
                log.warning("[letters] could not load font %s", reg, exc_info=True)
    return "Helvetica", lambda s: s.translate(_TO_LATIN1).encode("latin-1", "replace").decode("latin-1")


def safe_name(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s or "", flags=re.UNICODE).strip("_")[:60] or "Bewerbung"


def letter_pdf(profile: dict, job: dict, letter: dict) -> bytes:
    profile = profile or {}
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(25, 20, 20)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(letter.get("subject") or "Anschreiben")
    pdf.set_author(profile.get("full_name") or "")
    pdf.add_page()
    fam, t = _setup_font(pdf)
    width = pdf.w - pdf.l_margin - pdf.r_margin

    # sender
    pdf.set_font(fam, "B", 14)
    pdf.cell(width, 7, t(profile.get("full_name") or ""), new_x="LMARGIN", new_y="NEXT")
    contact = " · ".join(str(x) for x in (profile.get("location"), profile.get("phone"), profile.get("email")) if x)
    pdf.set_font(fam, "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(width, 5, t(contact) if fam == "Letter" else t(contact.replace("·", "|")),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, pdf.get_y() + 2, pdf.w - pdf.r_margin, pdf.get_y() + 2)

    # recipient (address window area)
    pdf.set_y(50)
    pdf.set_font(fam, "", 11)
    for line in (job.get("employer"), job.get("place")):
        if line and line != "?":
            pdf.cell(width, 5.5, t(str(line)), new_x="LMARGIN", new_y="NEXT")

    # place, date
    pdf.set_y(80)
    today = datetime.now(TZ).strftime("%d.%m.%Y")
    city = (profile.get("location") or "").split(",")[0].strip()
    pdf.cell(width, 5.5, t(f"{city}, {today}" if city else today), align="R", new_x="LMARGIN", new_y="NEXT")

    # subject + body
    pdf.ln(8)
    pdf.set_font(fam, "B", 11)
    pdf.multi_cell(width, 5.5, t(letter.get("subject") or ""), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_font(fam, "", 11)
    body = re.sub(r"\n{3,}", "\n\n", (letter.get("body") or "").strip())
    pdf.multi_cell(width, 5.5, t(body), align="L", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
