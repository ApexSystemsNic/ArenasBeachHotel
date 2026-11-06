"""Word (.docx) version of the reservation confirmation — the same content as
the printed page (guest_confirmation.html / rooming_list.html), built from
the same context (app._guest_doc_context), so it can be edited or emailed."""
import io
import os

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

import hotel_policies

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'logo.png')
DARK = RGBColor(0x0F, 0x17, 0x2A)
MUTED = RGBColor(0x64, 0x74, 0x8B)
RED = RGBColor(0x99, 0x1B, 0x1B)


def _money(value):
    return f'C${value or 0:,.2f}'


def _para(doc_or_cell, text='', bold=False, size=10, color=None, align=None, italic=False, space_after=2):
    p = doc_or_cell.add_paragraph()
    if text:
        run = p.add_run(text)
        run.bold = bold
        run.italic = italic
        run.font.size = Pt(size)
        if color:
            run.font.color.rgb = color
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)
    return p


def _set_cell(cell, text, bold=False, size=9, align=None, color=None):
    cell.text = ''
    p = cell.paragraphs[0]
    run = p.add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = color
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(0)


def _label_value_table(doc, left_rows, right_rows):
    """Two blocks of label/value pairs side by side, like the page header."""
    n = max(len(left_rows), len(right_rows))
    t = doc.add_table(rows=n, cols=4)
    t.style = 'Table Grid'
    for i in range(n):
        for col, rows in ((0, left_rows), (2, right_rows)):
            if i < len(rows):
                label, value, strong = (tuple(rows[i]) + (False,))[:3]
                _set_cell(t.cell(i, col), label.upper(), size=8, color=MUTED)
                _set_cell(t.cell(i, col + 1), value, bold=True, size=9,
                          color=RED if strong else DARK,
                          align=WD_ALIGN_PARAGRAPH.RIGHT if col == 2 else None)
    return t


def build_confirmation_docx(ctx):
    guest = ctx['guest']
    is_agency = ctx['is_agency']
    doc = Document()
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Cm(1.5)
        section.left_margin = section.right_margin = Cm(1.8)
    doc.styles['Normal'].font.name = 'Calibri'
    doc.styles['Normal'].font.size = Pt(10)

    # Letterhead: logo left, hotel details right
    head = doc.add_table(rows=1, cols=2)
    if os.path.exists(LOGO_PATH):
        head.cell(0, 0).paragraphs[0].add_run().add_picture(LOGO_PATH, height=Cm(2.2))
    right = head.cell(0, 1)
    lines = [('ARENAS BEACH HOTEL', True, 13), (ctx['hotel_legal_name'], False, 9), (ctx['hotel_address'], False, 9)]
    if ctx['hotel_phone']:
        lines.append((f"Tel: {ctx['hotel_phone']}", True, 9))
    lines += [(f"E-mail: {ctx['hotel_email']}", False, 9), (f"Web: {ctx['hotel_web']}", False, 9)]
    for i, (text, bold, size) in enumerate(lines):
        p = right.paragraphs[0] if i == 0 else right.add_paragraph()
        run = p.add_run(text)
        run.bold, run.font.size = bold, Pt(size)
        run.italic = i == 0
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.paragraph_format.space_after = Pt(0)

    _para(doc, space_after=4)
    title = 'ROOMING LIST GUARANTEED' if is_agency else 'HOTEL RESERVATION GUARANTEED'
    p = _para(doc, title, bold=True, size=14, color=DARK, italic=True, space_after=0)
    run = p.add_run(f"     Date: {ctx['created_display']}")
    run.font.size = Pt(9)
    if guest['status'] == 'cancelled':
        _para(doc, 'RESERVACIÓN CANCELADA', bold=True, size=10, color=RED)

    number = f"{ctx['document_number']:05d}"
    if is_agency:
        left = [('Contract', (guest.get('agency') or '').upper()), ('Agency', 'AGENCIAS'),
                ('Reserved for', guest['name'].upper())]
        right_rows = [('Rooming No.', number, True), ('Exchange Rate', f"C$ {ctx['exchange_rate']:.2f}"),
                      ('Reserved by', ctx['reserved_by'] or '')]
    else:
        attention = ' · '.join(x for x in (guest.get('email'), guest.get('phone')) if x)
        left = [('Name', guest['name']), ('Attention', attention), ('Company', 'DIRECTOS')]
        right_rows = [('Reservation No.', number, True), ('Check-In', ctx['group_checkin_display']),
                      ('Check-Out', ctx['group_checkout_display'])]
    if guest.get('booking_reference'):
        right_rows.append(('Referencia', guest['booking_reference']))
    _label_value_table(doc, left, right_rows)
    _para(doc, space_after=4)

    # Rooms
    headers = ['Room', 'Type of Room', 'Check-In', 'Check-Out', 'Pax', 'Plan', 'Rate', 'Nights', 'Amount', 'Tax']
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = 'Table Grid'
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(headers):
        _set_cell(t.rows[0].cells[i], h, bold=True, size=8)
    for line in ctx['lines']:
        g = line['guest']
        values = [g.get('room_number') or '', line['room_type_label'].upper(), line['checkin_display'],
                  line['checkout_display'], line['pax'] or '', line['plan'], _money(line['rate']), line['nights'],
                  _money(line['subtotal']), _money(line['tax']) + (' (exon.)' if g.get('tax_exempt') else '')]
        cells = t.add_row().cells
        for i, v in enumerate(values):
            _set_cell(cells[i], v, size=8, align=WD_ALIGN_PARAGRAPH.RIGHT if i in (6, 8, 9) else None)
    if any(l['plan'] == 'DESI' for l in ctx['lines']):
        _para(doc, 'Plan DESI: DESAYUNO INCLUIDO', size=8, color=MUTED)

    # Totals
    totals = doc.add_table(rows=3, cols=2)
    totals.alignment = WD_TABLE_ALIGNMENT.RIGHT
    for i, (label, value) in enumerate((('Subtotal:', ctx['group_subtotal']), ('Taxes:', ctx['group_tax']),
                                        ('Balance:', ctx['group_total']))):
        _set_cell(totals.cell(i, 0), label, bold=True, size=10, align=WD_ALIGN_PARAGRAPH.RIGHT)
        _set_cell(totals.cell(i, 1), _money(value), bold=True, size=11 if i == 2 else 10,
                  align=WD_ALIGN_PARAGRAPH.RIGHT)

    _para(doc, 'NOTES:', bold=True, size=9, color=MUTED, space_after=0)
    _para(doc, guest.get('observations') or '', size=9)

    # Policies
    _para(doc, 'Tarifa incluye', bold=True, size=9, space_after=0)
    for line in hotel_policies.RATE_INCLUDES:
        _para(doc, line, size=8, space_after=0)
    _para(doc, 'HORARIOS RESTAURANTE Y BAR', bold=True, size=9, space_after=0)
    for line in hotel_policies.HOURS:
        _para(doc, line, size=8, space_after=0)
    for heading, items in (('POLÍTICAS GENERALES', hotel_policies.GENERAL),
                           ('POLÍTICAS DE RESERVACIÓN', hotel_policies.RESERVATION),
                           ('POLÍTICAS DE CAMBIOS Y CANCELACIÓN DE RESERVACIONES',
                            hotel_policies.cancellation(ctx['hotel_email']))):
        _para(doc, heading, bold=True, size=9, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=1)
        for n, item in enumerate(items, start=1):
            p = _para(doc, size=8, space_after=0)
            r = p.add_run(f'{n}. ')
            r.bold, r.font.size = True, Pt(8)
            r = p.add_run(item)
            r.font.size = Pt(8)
        if heading == 'POLÍTICAS GENERALES':
            _para(doc, hotel_policies.GENERAL_ACCEPTANCE, size=8)
    p = _para(doc, size=8)
    r = p.add_run('RESTRICCIONES: ')
    r.bold, r.font.size = True, Pt(8)
    r = p.add_run(hotel_policies.RESTRICTIONS)
    r.font.size = Pt(8)

    _para(doc, f"Emitido el {ctx['issued_display']}", size=7, color=MUTED)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
