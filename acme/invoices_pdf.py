"""Renders vendor invoice PDFs (attachments in the mailbox) from seed data."""
from __future__ import annotations

from decimal import Decimal

from fpdf import FPDF


def render_invoice_pdf(doc: dict) -> bytes:
    pdf = FPDF(format="Letter")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, doc["vendor_name"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, doc["vendor_address"], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    meta = [
        ("Invoice number", doc["number"]),
        ("Invoice date", doc["invoice_date"]),
        ("Payment due", doc["due_date"]),
        ("Bill to", "Acme Corp - Accounts Payable, 500 Industrial Pkwy, Portland, OR 97201"),
    ]
    if doc.get("supplier_no"):
        meta.insert(3, ("Your supplier no.", doc["supplier_no"]))
    if doc.get("po"):
        meta.insert(3, ("PO reference", doc["po"]))
    for label, value in meta:
        pdf.cell(45, 7, f"{label}:")
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(110, 8, "Description", border="B")
    pdf.cell(20, 8, "Qty", border="B", align="R")
    pdf.cell(0, 8, f"Amount ({doc['currency']})", border="B", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    for desc, qty, unit in doc["lines"]:
        line_total = Decimal(unit) * qty
        pdf.cell(110, 8, desc)
        pdf.cell(20, 8, str(qty), align="R")
        pdf.cell(0, 8, f"{line_total:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(130, 10, "TOTAL DUE", border="T")
    pdf.cell(0, 10, f"{doc['currency']} {Decimal(doc['total']):,.2f}", border="T", align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "Please pay by the due date to the bank account on file. Thank you for your business.")
    return bytes(pdf.output())
