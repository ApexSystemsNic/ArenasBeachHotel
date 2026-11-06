"""Generates the periodic (default: every 15 days) Excel report of guest
checkouts and restaurant payments, so the owner can review month-by-month
whether the numbers look right.
"""
import io
import os
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import database as db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')

HEADER_FILL = PatternFill(start_color='0F172A', end_color='0F172A', fill_type='solid')
HEADER_FONT = Font(color='FFFFFF', bold=True)
CURRENCY_FMT = '"C$"#,##0.00'
PAYMENT_METHOD_LABELS_ES = {'cash': 'Efectivo', 'card': 'Tarjeta', 'room_charge': 'Cargo a Habitación',
                            'house': 'Cuenta de la Casa (cortesía)'}

LOW_FILL = PatternFill(start_color='FEE2E2', end_color='FEE2E2', fill_type='solid')
ZEBRA_FILL = PatternFill(start_color='F8FAFC', end_color='F8FAFC', fill_type='solid')
THIN_SIDE = Side(style='thin', color='E2E8F0')
THIN_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)
CENTER = Alignment(horizontal='center')


def _write_header(ws, headers):
    ws.append(headers)
    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal='left')
        cell.border = THIN_BORDER
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 20


def _style_row(ws, row, num_cols, fill=None, center_cols=(), bold_cols=()):
    """Applies a thin grid border to every cell in a data row, plus optional
    fill/alignment/bold — used to give exported sheets a real table look
    instead of bare unstyled cells."""
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.border = THIN_BORDER
        if fill:
            cell.fill = fill
        if col in center_cols:
            cell.alignment = CENTER
        if col in bold_cols:
            cell.font = Font(bold=True)


def _autosize(ws, widths):
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _fmt_date(iso_date):
    """'YYYY-MM-DD' -> 'DD/MM/YYYY'."""
    if not iso_date:
        return ''
    try:
        return datetime.strptime(iso_date, '%Y-%m-%d').strftime('%d/%m/%Y')
    except ValueError:
        return iso_date


def _fmt_datetime(sql_dt):
    """'YYYY-MM-DD HH:MM:SS' -> 'DD/MM/YYYY hh:mm AM/PM'."""
    if not sql_dt:
        return ''
    try:
        dt = datetime.strptime(sql_dt, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            dt = datetime.strptime(sql_dt, '%Y-%m-%d %H:%M')
        except ValueError:
            return sql_dt
    return dt.strftime('%d/%m/%Y %I:%M %p')


def generate_report(start_date, end_date):
    """start_date/end_date are 'YYYY-MM-DD' strings, inclusive."""
    os.makedirs(REPORTS_DIR, exist_ok=True)

    checkouts = db.list_checkouts_in_range(start_date, end_date)
    orders = db.list_orders_in_range(start_date, end_date)
    cancelled_orders = db.list_cancelled_orders_in_range(start_date, end_date)

    wb = Workbook()

    # ---- Summary sheet ----
    ws = wb.active
    ws.title = 'Resumen'
    ws.append(['Arenas Beach Hotel — Reporte Periódico'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f'Período: {_fmt_date(start_date)} a {_fmt_date(end_date)}'])
    ws.append([f'Generado: {datetime.now().strftime("%d/%m/%Y %I:%M %p")}'])
    ws.append([])

    # Room revenue is the subtotal before tax; the tax collected is reported apart.
    total_room_revenue = sum(c['room_subtotal'] or 0 for c in checkouts)
    total_room_tax = sum(c['tax_amount'] or 0 for c in checkouts)

    by_method = {'cash': 0.0, 'card': 0.0, 'room_charge': 0.0}
    # Master accounts collect nothing; report what they were worth instead.
    courtesy_value = {'breakfast': 0.0, 'owners': 0.0}
    for o in orders:
        if o['payment_method'] == 'house':
            courtesy_value[o['guest_type']] = courtesy_value.get(o['guest_type'], 0.0) + o['subtotal']
            continue
        by_method[o['payment_method']] = by_method.get(o['payment_method'], 0.0) + o['amount_collected']
    total_restaurant_revenue = sum(by_method.values())

    summary_rows = [
        ('Huéspedes con Salida Registrada', len(checkouts)),
        ('Ingresos Totales de Habitaciones (sin impuesto)', round(total_room_revenue, 2)),
        ('Impuesto Cobrado en Habitaciones', round(total_room_tax, 2)),
        ('Órdenes Totales de Restaurante', len(orders)),
        ('Órdenes Canceladas (ver hoja)', len(cancelled_orders)),
        ('Ingresos de Restaurante — Efectivo', round(by_method['cash'], 2)),
        ('Ingresos de Restaurante — Tarjeta', round(by_method['card'], 2)),
        ('Ingresos de Restaurante — Cargado a Habitación', round(by_method['room_charge'], 2)),
        ('Ingresos Totales de Restaurante', round(total_restaurant_revenue, 2)),
        ('Gran Total sin impuesto (Habitaciones + Restaurante)', round(total_room_revenue + total_restaurant_revenue, 2)),
        ('Cortesías — Desayunos Incluidos (valor, no cobrado)', round(courtesy_value['breakfast'], 2)),
        ('Cortesías — Propietarios (valor, no cobrado)', round(courtesy_value['owners'], 2)),
    ]
    ws.append(['Métrica', 'Valor'])
    for col_idx in (1, 2):
        cell = ws.cell(row=ws.max_row, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    for label, value in summary_rows:
        ws.append([label, value])
        if isinstance(value, float):
            ws.cell(row=ws.max_row, column=2).number_format = CURRENCY_FMT
    _autosize(ws, [32, 18])

    # ---- Checkouts sheet ----
    ws2 = wb.create_sheet('Salidas')
    headers = ['Huésped', 'Habitación', 'Tipo de Habitación', 'Agencia', 'Tarifa', 'Entrada', 'Salida (Planeada)',
               'Salida Registrada', 'Tarifa Promedio', 'Noches', 'Subtotal', 'Impuesto', 'Total', 'Comisión Agencia']
    _write_header(ws2, headers)
    for c in checkouts:
        nights = max((datetime.strptime(c['checkout_date'], '%Y-%m-%d').date()
                      - datetime.strptime(c['checkin_date'], '%Y-%m-%d').date()).days, 1)
        ws2.append([
            c['name'], c['room_number'], db.ROOM_TYPE_LABELS.get(c['room_type'], c['room_type']),
            c['agency'] or 'Directo', db.RATE_TYPE_LABELS_ES.get(c['rate_type'], ''),
            _fmt_date(c['checkin_date']), _fmt_date(c['checkout_date']), _fmt_datetime(c['checked_out_at']),
            c['nightly_rate'], nights, c['room_subtotal'] or 0,
            'Exonerado' if c['tax_exempt'] else (c['tax_amount'] or 0), c['total_amount'] or 0, c['commission_amount'] or 0,
        ])
        for col in (9, 11, 12, 13, 14):
            ws2.cell(row=ws2.max_row, column=col).number_format = CURRENCY_FMT
    _autosize(ws2, [22, 10, 18, 18, 14, 14, 20, 20, 14, 8, 14, 12, 14, 14])

    # ---- Payments / Orders sheet ----
    ws3 = wb.create_sheet('Pagos de Restaurante')
    headers = ['Recibo N.º', 'Hora de Ingreso', 'Hora de Cierre', 'Mesero', 'Tipo de Cliente', 'Mesa/Habitación',
               'Subtotal', 'Crédito Aplicado', 'Saldo Pendiente', 'Método de Pago', 'Monto Cobrado']
    _write_header(ws3, headers)
    for o in orders:
        target = f"Mesa {o['table_number']}" if o['table_number'] else ''
        if o['guest_id']:
            guest = db.get_guest(o['guest_id'])
            who = f"{guest['name']} (Habitación {guest['room_number']})" if guest else f"Huésped #{o['guest_id']}"
            target = f"{who} · {target}" if target else who
        target = target or '—'
        ws3.append([
            o['receipt_number'], _fmt_datetime(o.get('created_at') or o['timestamp']), _fmt_datetime(o['closed_at']),
            o['server_name'],
            {'hotel_guest': 'Huésped del Hotel', 'walk_in': 'Pase de Día'}.get(o['guest_type'], 'Cliente Externo'),
            target, o['subtotal'], o['credit_applied'], o['balance_due'],
            PAYMENT_METHOD_LABELS_ES.get(o['payment_method'], o['payment_method']), o['amount_collected'],
        ])
        for col in (7, 8, 9, 11):
            ws3.cell(row=ws3.max_row, column=col).number_format = CURRENCY_FMT
    _autosize(ws3, [16, 20, 20, 18, 12, 28, 12, 14, 12, 16, 16])

    # ---- Cancelled Orders sheet (transparency for admins/front desk/owners) ----
    ws4 = wb.create_sheet('Órdenes Canceladas')
    headers4 = ['Recibo N.º', 'Mesero', 'Mesa/Huésped', 'Subtotal', 'Solicitado Por', 'Motivo',
                'Autorizado Por', 'Nota de Decisión', 'Cancelada El']
    _write_header(ws4, headers4)
    for o in cancelled_orders:
        if o['table_number']:
            target = f"Mesa {o['table_number']}"
        elif o['guest_id']:
            guest = db.get_guest(o['guest_id'])
            target = f"{guest['name']} (Habitación {guest['room_number']})" if guest else f"Huésped #{o['guest_id']}"
        else:
            target = '—'
        ws4.append([
            o['receipt_number'], o['server_name'], target, o['subtotal'],
            o['cancel_requested_by_name'], o['cancel_reason'] or '',
            o['cancelled_by_name'], o['cancel_decision_note'] or '',
            _fmt_datetime(o['cancelled_at']),
        ])
        ws4.cell(row=ws4.max_row, column=4).number_format = CURRENCY_FMT
    _autosize(ws4, [16, 18, 22, 12, 18, 26, 18, 26, 20])

    file_name = f"arenas_beach_report_{start_date}_to_{end_date}.xlsx"
    file_path = os.path.join(REPORTS_DIR, file_name)
    wb.save(file_path)

    return {
        'file_name': file_name,
        'file_path': file_path,
        'checkouts_count': len(checkouts),
        'orders_count': len(orders),
        'total_room_revenue': round(total_room_revenue, 2),
        'total_restaurant_revenue': round(total_restaurant_revenue, 2),
    }


def generate_inventory_report(start_date, end_date):
    """Weekly (default) stock-consumption report: how much of each inventory
    item was depleted by orders in the period, plus current stock levels."""
    os.makedirs(REPORTS_DIR, exist_ok=True)

    transactions = db.list_inventory_transactions_in_range(start_date, end_date)
    current_items = db.list_inventory_items()

    consumption = {}
    for t in transactions:
        if t['quantity_change'] < 0:
            key = (t['item_name'], t['unit_of_measure'])
            consumption[key] = consumption.get(key, 0.0) + abs(t['quantity_change'])

    wb = Workbook()

    ws = wb.active
    ws.title = 'Resumen'
    ws.append(['Arenas Beach Hotel — Reporte Semanal de Inventario'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f'Período: {_fmt_date(start_date)} a {_fmt_date(end_date)}'])
    ws.append([f'Generado: {datetime.now().strftime("%d/%m/%Y %I:%M %p")}'])
    ws.append([])
    ws.append(['Métrica', 'Valor'])
    for col_idx in (1, 2):
        cell = ws.cell(row=ws.max_row, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = THIN_BORDER
    low_stock_count = sum(1 for i in current_items if i['is_low'])
    summary_rows = [
        ('Artículos Consumidos en este Período', len(consumption)),
        ('Bajo Inventario — Bodega Principal', low_stock_count),
    ]
    for idx, (label, value) in enumerate(summary_rows, start=1):
        ws.append([label, value])
        _style_row(ws, ws.max_row, 2, fill=ZEBRA_FILL if idx % 2 == 0 else None, center_cols=(2,))
    _autosize(ws, [38, 18])

    ws2 = wb.create_sheet('Consumo')
    _write_header(ws2, ['Artículo', 'Unidad', 'Cantidad Consumida'])
    for idx, ((item_name, unit), qty) in enumerate(sorted(consumption.items()), start=1):
        ws2.append([item_name, unit, round(qty, 3)])
        row = ws2.max_row
        _style_row(ws2, row, 3, fill=ZEBRA_FILL if idx % 2 == 0 else None, center_cols=(3,), bold_cols=(1,))
    _autosize(ws2, [28, 12, 20])

    # Three separate warehouses, matching the Existencias por Ubicación view
    # in the app: Bodega Principal is the receiving buffer everything lands
    # in first; Bar and Restaurante are the two ready-to-use pools that never
    # share a number (drinks vs. food). A pool shows '—' for an item that
    # isn't used by any recipe on that side, same as the on-screen table.
    ws3 = wb.create_sheet('Niveles de Inventario Actual')
    headers3 = ['Artículo', 'Unidad', 'Bodega Principal', 'Bar', 'Restaurante', 'Umbral de Alerta', 'Estado']
    _write_header(ws3, headers3)
    for idx, i in enumerate(current_items, start=1):
        estado = 'BAJO (Bodega Principal)' if i['is_low'] else 'BIEN'
        ws3.append([
            i['item_name'], i['unit_of_measure'], round(i['storeroom_stock'], 3),
            round(i['bar_stock'], 3) if i['bar_relevant'] else '—',
            round(i['current_stock'], 3) if i['restaurant_relevant'] else '—',
            i['low_stock_threshold'], estado,
        ])
        row = ws3.max_row
        fill = LOW_FILL if i['is_low'] else (ZEBRA_FILL if idx % 2 == 0 else None)
        _style_row(ws3, row, 7, fill=fill, center_cols=(3, 4, 5, 6), bold_cols=(1,))
        ws3.cell(row=row, column=7).font = Font(bold=True, color='B91C1C' if i['is_low'] else '15803D')
    ws3.auto_filter.ref = f"A1:G{ws3.max_row}"
    _autosize(ws3, [26, 10, 16, 12, 14, 16, 24])

    file_name = f"arenas_beach_inventory_report_{start_date}_to_{end_date}.xlsx"
    file_path = os.path.join(REPORTS_DIR, file_name)
    wb.save(file_path)

    return {
        'file_name': file_name,
        'file_path': file_path,
        'items_consumed': len(consumption),
        'low_stock_count': low_stock_count,
    }


def build_courtesy_workbook(data):
    """Master-account courtesies (breakfasts or owners) as an in-memory Excel
    file: a summary sheet (per room and day, or per owners' account) and a
    line-by-line detail sheet. Values are menu prices — none of it was charged."""
    wb = Workbook()
    breakfast = data['kind'] == 'breakfast'
    period = (_fmt_date(data['start_date']) if data['start_date'] == data['end_date']
              else f"{_fmt_date(data['start_date'])} al {_fmt_date(data['end_date'])}")
    header_lines = [
        f'Periodo: {period}',
        f"Emisión: {data['generated_at']}",
        'Cortesía — valor a precio del menú, no cobrado (Cuenta de la Casa)',
    ]

    def write_sheet(ws, title, headers, rows, total_row, widths, currency_cols=()):
        ws.append([title])
        ws.cell(row=1, column=1).font = Font(bold=True, size=14)
        for line in header_lines:
            ws.append([line])
        ws.append([])
        start = ws.max_row + 1
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=start, column=col_idx)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.border = THIN_BORDER
        for i, row in enumerate(rows):
            ws.append(row)
            _style_row(ws, ws.max_row, len(headers), fill=ZEBRA_FILL if i % 2 else None)
        ws.append(total_row)
        _style_row(ws, ws.max_row, len(headers), bold_cols=range(1, len(headers) + 1))
        for col in currency_cols:
            for r in range(start + 1, ws.max_row + 1):
                ws.cell(row=r, column=col).number_format = CURRENCY_FMT
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        _autosize(ws, widths)

    items_text = lambda g: ', '.join(f"{i['quantity']}× {i['item_name']}" for i in g['items'])
    status_text = lambda s: 'Abierta' if s == 'open' else 'Cerrada'
    if breakfast:
        write_sheet(wb.active, 'Desayunos Incluidos — por Habitación',
                    ['Fecha', 'Habitación', 'Huésped', 'Artículos', 'Cant', 'Valor'],
                    [[_fmt_date(g['day']), g['room_number'], g['guest_name'], items_text(g), g['quantity'], g['value']]
                     for g in data['groups']],
                    ['', '', f"{data['rooms']} habitación(es)", 'Total', data['total_quantity'], data['total_value']],
                    [12, 12, 24, 60, 8, 14], currency_cols=(6,))
        wb.active.title = 'Por Habitación'
        write_sheet(wb.create_sheet('Detalle'), 'Desayunos Incluidos — Detalle',
                    ['Fecha', 'Habitación', 'Huésped', 'Artículo', 'Cant', 'Precio', 'Valor'],
                    [[_fmt_date(l['day']), l['room_number'], l['guest_name'], l['item_name'], l['quantity'], l['unit_price'], l['value']]
                     for l in data['lines']],
                    ['', '', '', 'Total', data['total_quantity'], '', data['total_value']],
                    [12, 12, 24, 40, 8, 14, 14], currency_cols=(6, 7))
    else:
        write_sheet(wb.active, 'Propietarios — por Cuenta',
                    ['Fecha', 'Cuenta', 'Estado', 'Mesero', 'Artículos', 'Cant', 'Valor'],
                    [[_fmt_date(g['day']), g['account_name'], status_text(g['status']), g['server_name'], items_text(g),
                      g['quantity'], g['value']] for g in data['groups']],
                    ['', f"{data['accounts']} cuenta(s)", '', '', 'Total', data['total_quantity'], data['total_value']],
                    [12, 24, 10, 20, 60, 8, 14], currency_cols=(7,))
        wb.active.title = 'Por Cuenta'
        write_sheet(wb.create_sheet('Detalle'), 'Propietarios — Detalle',
                    ['Fecha', 'Cuenta', 'Artículo', 'Cant', 'Precio', 'Valor'],
                    [[_fmt_date(l['day']), l['account_name'], l['item_name'], l['quantity'], l['unit_price'], l['value']]
                     for l in data['lines']],
                    ['', '', 'Total', data['total_quantity'], '', data['total_value']],
                    [12, 24, 40, 8, 14, 14], currency_cols=(5, 6))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_item_sales_workbook(data):
    """'Ventas por Artículos' close as an in-memory Excel file: one sheet with
    the quantity sold per item (like the cashier's printout) and one with
    what those sales came to in C$."""
    wb = Workbook()
    period = (_fmt_date(data['start_date']) if data['start_date'] == data['end_date']
              else f"{_fmt_date(data['start_date'])} al {_fmt_date(data['end_date'])}")
    header_lines = [
        'ARENAS BOAT BAR AND RESTAURANT',
        f"Emisión: {data['generated_at']}",
        f'Periodo: {period}',
        f"Apertura: {data['first_opened'] or '—'}",
        f"Mesero: {data['server_name'] or 'Todos'}",
        f"Cuentas: {data['order_count']}"
        + (f" (incluye {data['open_order_count']} abiertas)" if data['include_open'] else ' (solo cerradas)'),
    ]

    def write_sheet(ws, title, headers, row_fn, total_row, widths, currency_cols=()):
        ws.append([title])
        ws.cell(row=1, column=1).font = Font(bold=True, size=14)
        for line in header_lines:
            ws.append([line])
        ws.append([])
        start = ws.max_row + 1
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=start, column=col_idx)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.border = THIN_BORDER
        for i, item in enumerate(data['items']):
            ws.append(row_fn(item))
            _style_row(ws, ws.max_row, len(headers), fill=ZEBRA_FILL if i % 2 else None)
        ws.append(total_row)
        _style_row(ws, ws.max_row, len(headers), bold_cols=range(1, len(headers) + 1))
        for col in currency_cols:
            for r in range(start + 1, ws.max_row + 1):
                ws.cell(row=r, column=col).number_format = CURRENCY_FMT
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        _autosize(ws, widths)

    write_sheet(wb.active, 'Ventas por Artículos (Cantidad)', ['Código', 'Artículo', 'Cant'],
                lambda i: [i['code'], i['item_name'], i['quantity']],
                ['', 'Total', data['total_quantity']], [10, 48, 10])
    wb.active.title = 'Artículos'
    write_sheet(wb.create_sheet('Ventas'), 'Ventas por Artículos (C$)',
                ['Código', 'Artículo', 'Cant', 'Precio', 'Total'],
                lambda i: [i['code'], i['item_name'], i['quantity'], i['unit_price'], i['total']],
                ['', 'Total', data['total_quantity'], '', data['total_amount']],
                [10, 48, 10, 14, 16], currency_cols=(4, 5))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


ROOM_LIST_STATUS_ES = {'reserved': 'Confirmada', 'checked_in': 'En Casa', 'checked_out': 'Salió'}


def build_room_list_workbook(data, room_labels, rate_labels, agency_name=None):
    """The front-desk operating list (Llegadas / En Casa / Salidas) for one
    day as an in-memory Excel file — the same columns as the printed list."""
    wb = Workbook()
    ws = wb.active
    ws.title = data['title'][:31]
    ws.append([f"{data['title']} — {_fmt_date(data['date'])}"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append([f"Agencia: {agency_name}" if agency_name else 'Todas las agencias'])
    ws.append([f"Emisión: {datetime.now().strftime('%d/%m/%Y %I:%M %p')}"])
    ws.append([])
    headers = ['Hab.', 'Huésped', 'Tipo', 'Agencia', 'Entrada', 'Salida', 'Pax', 'Tarifa',
               'Subtotal', 'Impuesto', 'Total', 'Estado', 'Observaciones']
    start = ws.max_row + 1
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=start, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = THIN_BORDER
    ws.freeze_panes = ws.cell(row=start + 1, column=1)
    for g in data['guests']:
        ws.append([
            g['room_number'] or '', g['name'], room_labels.get(g['room_type'], g['room_type']),
            g['agency'] or 'Directo', _fmt_date(g['checkin_date']), _fmt_date(g['checkout_date']),
            ((g['adults'] or 0) + (g['children'] or 0)) or None, rate_labels.get(g['rate_type'], ''),
            g['room_subtotal'] or 0, 0 if g['tax_exempt'] else (g['tax_amount'] or 0), g['total_amount'] or 0,
            ROOM_LIST_STATUS_ES.get(g['status'], g['status']), g['observations'] or '',
        ])
        _style_row(ws, ws.max_row, len(headers), center_cols=(1, 5, 6, 7))
    t = data['totals']
    ws.append(['Totales', f"{t['count']} habitación(es)", '', '', '', '', t['pax'] or None, '',
               t['subtotal'], t['tax'], t['total'], '', ''])
    _style_row(ws, ws.max_row, len(headers), bold_cols=range(1, len(headers) + 1))
    for row in ws.iter_rows(min_row=start + 1, min_col=9, max_col=11):
        for cell in row:
            cell.number_format = '"C$"#,##0.00'
    _autosize(ws, [7, 28, 20, 20, 12, 12, 6, 14, 14, 13, 14, 12, 40])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_maintenance_workbook(incidents, items, usage):
    """Maintenance history as Excel: breakdowns (by room / area), the
    maintenance storeroom with low-stock flags, and every material use."""
    wb = Workbook()

    def sheet(ws, title, headers, rows, widths, center=(), money=()):
        ws.title = title
        _write_header(ws, headers)
        for r in rows:
            ws.append(r)
            _style_row(ws, ws.max_row, len(headers), center_cols=center)
            for col in money:
                ws.cell(row=ws.max_row, column=col).number_format = CURRENCY_FMT
        _autosize(ws, widths)

    sheet(wb.active, 'Averías',
          ['N.º', 'Reportada', 'Área / Habitación', 'Descripción', 'Prioridad', 'Estado', 'Reportó',
           'En proceso', 'Resuelta', 'Resolvió', 'Qué se hizo', 'Materiales', 'Fuera de servicio'],
          [[i['id'], _fmt_datetime(i['reported_at']), i['target'], i['description'], i['priority_label'],
            i['status_label'], i['reported_by_name'], _fmt_datetime(i['started_at']), _fmt_datetime(i['resolved_at']),
            i['resolved_by_name'] or '', i['resolution_note'] or '',
            ', '.join(f"{m['quantity']:g} {m['unit_of_measure']} {m['item_name']} ({m['status']})" for m in i['materials']),
            'Sí' if i['room_out_of_order'] else ''] for i in incidents],
          [7, 18, 18, 40, 10, 12, 18, 18, 18, 18, 36, 36, 10], center=(1, 5, 6, 13))
    sheet(wb.create_sheet(), 'Bodega de Mantenimiento',
          ['Artículo', 'Unidad', 'En Mantenimiento', 'Mínimo', 'Estado', 'En Bodega Principal', 'Costo prom.'],
          [[i['item_name'], i['unit_of_measure'], i['maintenance_stock'], i['maintenance_threshold'],
            'BAJO' if i['is_low_maintenance'] else 'OK', i['storeroom_stock'], i['unit_cost']] for i in items],
          [30, 10, 16, 10, 10, 18, 12], center=(5,), money=(7,))
    sheet(wb.create_sheet(), 'Consumos',
          ['Solicitado', 'Artículo', 'Cantidad', 'Unidad', 'Dónde', 'Avería', 'Notas', 'Solicitó', 'Estado',
           'Decidió', 'Decidido'],
          [[_fmt_datetime(u['requested_at']), u['item_name'], u['quantity'], u['unit_of_measure'], u['target'],
            f"#{u['incident_id']}" if u['incident_id'] else '', u['notes'] or '', u['requested_by_name'],
            u['status_label'], u['decided_by_name'] or '', _fmt_datetime(u['decided_at'])] for u in usage],
          [18, 30, 10, 10, 18, 8, 30, 18, 12, 18, 18], center=(3, 6, 9))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_shift_history_workbook(data, sections):
    """'Históricos de Turno' as Excel: one sheet per section, each with the
    shift's header and the full table."""
    wb = Workbook()
    wb.remove(wb.active)
    header_lines = ['ARENAS BOAT BAR AND RESTAURANT', *data['header_lines'],
                    f"Emisión: {_fmt_datetime(data['generated_at'])}"]
    used = set()
    for sec in sections:
        name = ''.join(ch for ch in sec['title'] if ch not in '[]:*?/\\')[:31]
        while name in used:
            name = name[:29] + str(len(used))
        used.add(name)
        ws = wb.create_sheet(name)
        ws.append([sec['title']])
        ws.cell(row=1, column=1).font = Font(bold=True, size=14)
        for line in header_lines:
            ws.append([line])
        ws.append([sec['description']])
        ws.cell(row=ws.max_row, column=1).font = Font(italic=True, color='64748B')
        ws.append([])
        cols = sec['columns']
        start = ws.max_row + 1
        ws.append([c[0] for c in cols])
        for i in range(1, len(cols) + 1):
            cell = ws.cell(row=start, column=i)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.border = THIN_BORDER
        if not sec['rows']:
            ws.append(['Sin movimientos.'])
        for n, row in enumerate(sec['rows']):
            ws.append([_fmt_datetime(v) if cols[i][1] == 'datetime' else v for i, v in enumerate(row)])
            _style_row(ws, ws.max_row, len(cols), fill=ZEBRA_FILL if n % 2 else None)
        if sec.get('total'):
            ws.append(sec['total'])
            _style_row(ws, ws.max_row, len(cols), bold_cols=range(1, len(cols) + 1))
        for i, (_, kind) in enumerate(cols, start=1):
            if kind == 'money':
                for r in range(start + 1, ws.max_row + 1):
                    ws.cell(row=r, column=i).number_format = CURRENCY_FMT
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        widths = []
        for i, (label, kind) in enumerate(cols):
            longest = max([len(str(label))] + [len(str(r[i])) for r in sec['rows'] if r[i] is not None])
            widths.append(min(max(longest + 2, 14), 60))
        if len(cols) <= 2:
            widths[0] = max(widths[0], 34)
        _autosize(ws, widths)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_daily_sales_workbook(data):
    """'Artículos vendidos el <día>' as the cashier's printout: title, summary,
    legend, then Cód · Artículo · M · V · T in two tables side by side."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Ventas del día'
    head_fill = PatternFill(start_color='334155', end_color='334155', fill_type='solid')
    zebra = PatternFill(start_color='F1F5F9', end_color='F1F5F9', fill_type='solid')
    side = Side(style='thin', color='CBD5E1')
    border = Border(left=side, right=side, top=side, bottom=side)
    ws['A1'] = data['title']
    ws['A1'].font = Font(name='Arial', size=16, bold=True)
    ws['A2'] = data['business']
    ws['A2'].font = Font(name='Arial', size=9)
    ws['A3'] = data['summary']
    ws['A3'].font = Font(name='Arial', size=9, bold=True)
    ws['A4'] = data['legend']
    ws['A4'].font = Font(name='Arial', size=9, color='334155')
    items = data['items']
    half = (len(items) + 1) // 2
    blocks = [(1, items[:half]), (7, items[half:])]          # columns A–E and G–K
    header_row = 6
    for col0, rows in blocks:
        for j, label in enumerate(['Cód.', 'Artículo', 'M', 'V', 'T']):
            cell = ws.cell(row=header_row, column=col0 + j, value=label)
            cell.fill = head_fill
            cell.font = Font(name='Arial', size=9, bold=True, color='FFFFFF')
            cell.border = border
            cell.alignment = Alignment(horizontal='left' if j < 2 else 'center', vertical='center')
        for i, it in enumerate(rows):
            r = header_row + 1 + i
            values = [it['code'], it['name'], it['m'], it['v'], it['t']]
            for j, v in enumerate(values):
                cell = ws.cell(row=r, column=col0 + j, value=v)
                cell.font = Font(name='Arial', size=9, bold=(j == 4))
                cell.border = border
                cell.alignment = Alignment(horizontal='left' if j < 2 else 'center', vertical='center', wrap_text=(j == 1))
                if i % 2:
                    cell.fill = zebra
            ws.cell(row=r, column=col0).number_format = '@'
    for col, width in zip('ABCDEFGHIJK', [7, 40, 4.5, 4.5, 4.5, 2.5, 7, 40, 4.5, 4.5, 4.5]):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f'{header_row}:{header_row}'
    ws.page_margins.left = ws.page_margins.right = 0.4
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
