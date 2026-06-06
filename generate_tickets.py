"""
Event QR Ticket Generator
Generates 200 unique QR-coded entry tickets as a printable PDF.
Each ticket has a unique ID, QR code, and serial number.
Layout: 4 tickets per page (2x2 grid), A4 paper.
"""

import os
import uuid
import hashlib
import qrcode
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


# ── Configuration ──────────────────────────────────────────────
EVENT_NAME = "YOUR EVENT NAME"
EVENT_DATE = "Date: TBD"
EVENT_VENUE = "Venue: TBD"
TOTAL_TICKETS = 200
TICKETS_PER_PAGE = 4  # 2x2 grid
OUTPUT_DIR = "output"
PDF_FILENAME = "event_tickets.pdf"
MANIFEST_FILENAME = "ticket_manifest.csv"

# Colors
PRIMARY = HexColor("#1a1a2e")
ACCENT = HexColor("#e94560")
LIGHT_BG = HexColor("#f5f5f5")
WHITE = HexColor("#ffffff")
DARK_TEXT = HexColor("#1a1a2e")
SUBTLE_TEXT = HexColor("#6b7280")


def generate_ticket_id(serial: int) -> str:
    """Generate a unique, short ticket ID from serial + random salt."""
    salt = uuid.uuid4().hex[:8]
    raw = f"TICKET-{serial:04d}-{salt}"
    short_hash = hashlib.sha256(raw.encode()).hexdigest()[:10].upper()
    return f"EVT-{serial:03d}-{short_hash}"


def make_qr_image(data: str, box_size: int = 6) -> ImageReader:
    """Create a QR code image and return it as a ReportLab ImageReader."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a2e", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def draw_ticket(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                serial: int, ticket_id: str):
    """Draw a single ticket at position (x, y) with dimensions (w, h)."""

    margin = 6 * mm
    inner_x = x + margin
    inner_y = y + margin
    inner_w = w - 2 * margin
    inner_h = h - 2 * margin

    # Outer card border with rounded rectangle
    c.setStrokeColor(HexColor("#d1d5db"))
    c.setLineWidth(0.5)
    c.setDash(3, 3)
    c.roundRect(x + 2 * mm, y + 2 * mm, w - 4 * mm, h - 4 * mm, 8)
    c.setDash()

    # Top accent bar
    bar_height = 18 * mm
    c.setFillColor(PRIMARY)
    c.roundRect(inner_x, inner_y + inner_h - bar_height, inner_w, bar_height, 6)
    c.rect(inner_x, inner_y + inner_h - bar_height, inner_w, bar_height / 2, fill=1, stroke=0)

    # Event name
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(inner_x + inner_w / 2, inner_y + inner_h - 12 * mm, EVENT_NAME)

    # Event details
    c.setFillColor(SUBTLE_TEXT)
    c.setFont("Helvetica", 8)
    details_y = inner_y + inner_h - bar_height - 8 * mm
    c.drawCentredString(inner_x + inner_w / 2, details_y + 3 * mm, EVENT_DATE)
    c.drawCentredString(inner_x + inner_w / 2, details_y - 1 * mm, EVENT_VENUE)

    # QR Code (centered)
    qr_size = 32 * mm
    qr_img = make_qr_image(ticket_id)
    qr_x = inner_x + (inner_w - qr_size) / 2
    qr_y = inner_y + 22 * mm
    c.drawImage(qr_img, qr_x, qr_y, qr_size, qr_size)

    # Ticket ID below QR
    c.setFillColor(DARK_TEXT)
    c.setFont("Courier-Bold", 8)
    c.drawCentredString(inner_x + inner_w / 2, inner_y + 16 * mm, ticket_id)

    # Serial number badge
    c.setFillColor(ACCENT)
    badge_w = 20 * mm
    badge_h = 7 * mm
    badge_x = inner_x + (inner_w - badge_w) / 2
    badge_y = inner_y + 6 * mm
    c.roundRect(badge_x, badge_y, badge_w, badge_h, 3, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(inner_x + inner_w / 2, badge_y + 2 * mm, f"#{serial:03d}")

    # Footer
    c.setFillColor(SUBTLE_TEXT)
    c.setFont("Helvetica", 5.5)
    c.drawCentredString(inner_x + inner_w / 2, inner_y + 2 * mm, "Present this ticket at entry. One person per ticket.")


def generate_pdf():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    pdf_path = os.path.join(OUTPUT_DIR, PDF_FILENAME)
    manifest_path = os.path.join(OUTPUT_DIR, MANIFEST_FILENAME)

    page_w, page_h = A4
    cols, rows = 2, 2
    ticket_w = page_w / cols
    ticket_h = page_h / rows

    c = canvas.Canvas(pdf_path, pagesize=A4)
    c.setTitle(f"{EVENT_NAME} - Entry Tickets")
    c.setAuthor("Event QR Ticket Generator")

    tickets = []
    for i in range(1, TOTAL_TICKETS + 1):
        ticket_id = generate_ticket_id(i)
        tickets.append((i, ticket_id))

    manifest_lines = ["serial,ticket_id"]
    page_ticket_idx = 0

    for serial, ticket_id in tickets:
        col = page_ticket_idx % cols
        row = 1 - (page_ticket_idx // cols)

        x = col * ticket_w
        y = row * ticket_h

        draw_ticket(c, x, y, ticket_w, ticket_h, serial, ticket_id)
        manifest_lines.append(f"{serial:03d},{ticket_id}")

        page_ticket_idx += 1
        if page_ticket_idx >= TICKETS_PER_PAGE:
            c.showPage()
            page_ticket_idx = 0

    if page_ticket_idx > 0:
        c.showPage()

    c.save()

    with open(manifest_path, "w") as f:
        f.write("\n".join(manifest_lines) + "\n")

    total_pages = (TOTAL_TICKETS + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE
    print(f"\n{'='*50}")
    print(f"  TICKETS GENERATED SUCCESSFULLY")
    print(f"{'='*50}")
    print(f"  Total tickets : {TOTAL_TICKETS}")
    print(f"  Total pages   : {total_pages}")
    print(f"  PDF file      : {pdf_path}")
    print(f"  Manifest CSV  : {manifest_path}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    generate_pdf()
