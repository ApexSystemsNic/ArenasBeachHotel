"""Arenas Beach Hotel — executive Restaurant POS + Front Desk PMS + Public
Reservations + Inventory + Admin dashboard (V13).
Single Flask app, single PostgreSQL database, session-based RBAC.
"""
import base64
import hmac
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_file, Response
from werkzeug.security import check_password_hash

import database as db
import report_export
import delivery
import scheduler
import realtime
import security
import printing
import backup
import doc_export
import hotel_policies

app = Flask(__name__)
security.init_app(app)
db.init_db()


# --------------------------------------------------------------- RBAC -----

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Se requiere autenticación'}), 401
        # Maintenance staff only reach their own section (routes marked
        # @maintenance_allowed) — no restaurant, guests or sales data.
        if session.get('role') == 'maintenance' and not getattr(view, '_maintenance_ok', False):
            return jsonify({'error': 'Su usuario solo tiene acceso a Mantenimiento'}), 403
        return view(*args, **kwargs)
    return wrapped


def maintenance_allowed(view):
    """Marks a @login_required route as usable by the maintenance role too.
    Goes below @login_required."""
    view._maintenance_ok = True
    return view


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Se requiere autenticación'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': 'Se requiere acceso de administrador'}), 403
        return view(*args, **kwargs)
    return wrapped


def front_desk_access_required(view):
    """Front Desk PMS actions: administrators and front-desk staff."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Se requiere autenticación'}), 401
        if session.get('role') not in ('admin', 'front_desk'):
            return jsonify({'error': 'Se requiere acceso de Recepción'}), 403
        return view(*args, **kwargs)
    return wrapped


def current_user():
    return {
        'id': session.get('user_id'),
        'username': session.get('username'),
        'full_name': session.get('full_name'),
        'role': session.get('role'),
    }


@app.before_request
def drop_deleted_user_session():
    """A deleted staff account loses access right away, not when its session expires."""
    if 'user_id' in session:
        user = db.get_user_by_id(session['user_id'])
        if not user or not user['is_active']:
            session.clear()


# ---------------------------------------------------------------- Pages ----

@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('index.html', exchange_rate=EXCHANGE_RATE)


@app.route('/booking')
def booking_page():
    """Public reservation portal — no login required."""
    return render_template('booking.html')


# Printed on every guest document, as on the property's paper forms.
HOTEL_LEGAL_NAME = 'INVERSIONES TURISTICAS LAS ISLAS DEL MAIZ S.A.'
HOTEL_ADDRESS = 'Southwest Bay, Corn Island'
# Meal plan code ("Rég/Ralo") when a stay has none — every rate includes
# breakfast (DESI = desayuno incluido).
DEFAULT_MEAL_PLAN = 'DESI'
EXCHANGE_RATE = float(os.environ.get('HOTEL_EXCHANGE_RATE', '36.62'))   # C$ per US$


def _doc_date(value):
    """'YYYY-MM-DD[ HH:MM:SS]' -> 'DD/MM/YYYY' ('' when missing)."""
    return datetime.strptime(value[:10], '%Y-%m-%d').strftime('%d/%m/%Y') if value else ''


def _stay_line(g):
    """One room of a confirmation / rooming list."""
    checkin = datetime.strptime(g['checkin_date'], '%Y-%m-%d')
    checkout = datetime.strptime(g['checkout_date'], '%Y-%m-%d')
    nights = max((checkout - checkin).days, 0)
    pax = None
    if g.get('adults') is not None:
        pax = g['adults'] + (g.get('children') or 0)
    subtotal = g.get('room_subtotal') or 0
    return {
        'guest': g,
        'room_type_label': db.ROOM_TYPE_LABELS.get(g['room_type'], g['room_type']),
        'checkin_display': checkin.strftime('%d/%m/%Y'),
        'checkout_display': checkout.strftime('%d/%m/%Y'),
        'nights': nights,
        'pax': pax,
        'plan': g.get('rate_code') or DEFAULT_MEAL_PLAN,
        'rate': round(subtotal / nights, 2) if nights else (g.get('nightly_rate') or 0),
        'subtotal': subtotal,
        'tax': g.get('tax_amount') or 0,
        'total': g.get('total_amount') or 0,
    }


def _guest_doc_context(guest_id):
    guest = db.get_guest(guest_id)
    if not guest:
        return None
    checkin = datetime.strptime(guest['checkin_date'], '%Y-%m-%d')
    checkout = datetime.strptime(guest['checkout_date'], '%Y-%m-%d')
    nights = max((checkout - checkin).days, 0)
    status_labels = {'reserved': 'Confirmada', 'checked_in': 'En Casa',
                      'checked_out': 'Salida Registrada', 'cancelled': 'Cancelada'}
    breakdown = json.loads(guest['rate_breakdown']) if guest.get('rate_breakdown') else []
    # Consecutive nights at the same season / day type / rate print as one
    # line ("12/10 – 14/10 · 3 × C$x"), so a long stay still fits one page.
    rate_groups = []
    for n in breakdown:
        label = f"{db.SEASON_LABELS_ES[n['season']]} · {db.DAY_TYPE_LABELS_ES[n['day_type']]}"
        day = datetime.strptime(n['date'], '%Y-%m-%d').strftime('%d/%m')
        last = rate_groups[-1] if rate_groups else None
        if last and last['label'] == label and last['rate'] == n['rate']:
            last['to'] = day
            last['nights'] += 1
            last['amount'] += n['rate']
        else:
            rate_groups.append({'from': day, 'to': day, 'label': label, 'rate': n['rate'], 'nights': 1, 'amount': n['rate']})
    occupancy_code = ''
    total_pax = None
    if guest.get('adults') is not None:
        children = guest.get('children') or 0
        total_pax = guest['adults'] + children
        occupancy_code = f"{guest['adults']}A{children}N {total_pax}PAX"
    now = datetime.now()
    reserved_by = db.get_user_by_id(guest['created_by']) if guest.get('created_by') else None
    lines = [_stay_line(g) for g in db.get_booking_group(guest)]
    return {
        'guest': guest,
        'is_agency': bool(guest.get('agency_id') or guest.get('agency')),
        'lines': lines,
        'group_subtotal': round(sum(l['subtotal'] for l in lines), 2),
        'group_tax': round(sum(l['tax'] for l in lines), 2),
        'group_total': round(sum(l['total'] for l in lines), 2),
        'group_checkin_display': min(lines, key=lambda l: l['guest']['checkin_date'])['checkin_display'],
        'group_checkout_display': max(lines, key=lambda l: l['guest']['checkout_date'])['checkout_display'],
        'document_number': min(l['guest']['id'] for l in lines),
        'meal_plan': guest.get('rate_code') or DEFAULT_MEAL_PLAN,
        'created_display': _doc_date(guest.get('created_at')) or now.strftime('%d/%m/%Y'),
        'reserved_by': (reserved_by.get('full_name') or reserved_by.get('username')) if reserved_by else '',
        'exchange_rate': EXCHANGE_RATE,
        'policies': hotel_policies,
        'hotel_legal_name': HOTEL_LEGAL_NAME,
        'hotel_address': HOTEL_ADDRESS,
        'nights': nights,
        'room_type_label': db.ROOM_TYPE_LABELS.get(guest['room_type'], guest['room_type']),
        'status_label': status_labels.get(guest['status'], guest['status']),
        'checkin_display': checkin.strftime('%d/%m/%Y'),
        'checkout_display': checkout.strftime('%d/%m/%Y'),
        'issued_display': now.strftime('%d/%m/%Y %I:%M %p'),
        'issued_date_display': now.strftime('%d/%m/%Y'),
        'occupancy_code': occupancy_code,
        'total_pax': total_pax,
        'rate_groups': rate_groups,
        'rate_type_label': db.RATE_TYPE_LABELS_ES.get(guest.get('rate_type'), ''),
        'hotel_email': 'info@arenasbeachhotel.com',
        'hotel_web': 'www.arenasbeachhotel.com',
        'hotel_phone': os.environ.get('HOTEL_PHONE', '(505) 8851 8046'),
    }


@app.route('/guests/<int:guest_id>/confirmation')
def guest_confirmation_page(guest_id):
    if 'user_id' not in session or session.get('role') not in ('admin', 'front_desk'):
        return redirect(url_for('login_page'))
    ctx = _guest_doc_context(guest_id)
    if not ctx:
        return 'Guest not found', 404
    # Agency stays print as the agency's rooming list; guests who booked
    # directly get the hotel reservation confirmation.
    template = 'rooming_list.html' if ctx['is_agency'] else 'guest_confirmation.html'
    return render_template(template, **ctx)


@app.route('/guests/<int:guest_id>/confirmation.docx')
def guest_confirmation_docx(guest_id):
    """The same confirmation (agency rooming list or direct guest) as a Word file."""
    if 'user_id' not in session or session.get('role') not in ('admin', 'front_desk'):
        return redirect(url_for('login_page'))
    ctx = _guest_doc_context(guest_id)
    if not ctx:
        return 'Guest not found', 404
    kind = 'rooming_list' if ctx['is_agency'] else 'confirmacion'
    return send_file(doc_export.build_confirmation_docx(ctx), as_attachment=True,
                     download_name=f"{kind}_{ctx['document_number']:05d}.docx",
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


@app.route('/guests/<int:guest_id>/registration-card')
def registration_card_page(guest_id):
    if 'user_id' not in session or session.get('role') not in ('admin', 'front_desk'):
        return redirect(url_for('login_page'))
    ctx = _guest_doc_context(guest_id)
    if not ctx:
        return 'Guest not found', 404
    return render_template('registration_card.html', **ctx)


# ------------------------------------------------------------ Auth API ----

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(force=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    blocked = security.check_login_allowed(username)
    if blocked:
        return blocked

    user = db.get_user_by_username(username)
    if not user or not check_password_hash(user['password_hash'], password):
        security.record_login_failure(username)
        return jsonify({'error': 'Usuario o contraseña inválidos'}), 401
    security.record_login_success(username)

    session.clear()
    session.permanent = True   # expires after PERMANENT_SESSION_LIFETIME of inactivity
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['full_name'] = user['full_name']
    session['role'] = user['role']

    return jsonify(current_user())


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'logged_out'})


@app.route('/api/me', methods=['GET'])
def api_me():
    if 'user_id' not in session:
        return jsonify({'error': 'No autenticado'}), 401
    return jsonify(current_user())


# --------------------------------------------------------- Staff (Admin) --

@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    return jsonify(db.list_users())


@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    data = request.get_json(force=True) or {}
    required = ['username', 'full_name', 'password']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        user = db.create_user(
            username=data['username'].strip(),
            full_name=data['full_name'].strip(),
            password=data['password'],
            role=data.get('role', 'server'),
            created_by_user_id=session['user_id'],
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(user), 201


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_delete_user(user_id):
    try:
        result = db.delete_user(user_id, requesting_user_id=session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


@app.route('/api/activity-log', methods=['GET'])
@admin_required
def api_activity_log():
    return jsonify(db.list_activity_log(
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 20, type=int),
    ))


# ---------------------------------------------------------------- Menu ----

@app.route('/api/menu', methods=['GET'])
@login_required
def api_list_menu():
    active_only = request.args.get('active_only') == 'true'
    return jsonify(db.list_menu_items(active_only=active_only))


@app.route('/api/menu', methods=['POST'])
@admin_required
def api_create_menu_item():
    data = request.get_json(force=True) or {}
    required = ['name', 'price']
    missing = [f for f in required if data.get(f) in (None, '')]
    if not data.get('section') and not data.get('category'):
        missing.append('section')
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        item = db.create_menu_item(
            name=data['name'], category=data.get('category'),
            price=float(data['price']), portion_size=data.get('portion_size', ''),
            section=data.get('section'), has_sides=data.get('has_sides'), asks_doneness=data.get('asks_doneness'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item), 201


@app.route('/api/menu/<int:item_id>', methods=['PUT'])
@admin_required
def api_update_menu_item(item_id):
    data = request.get_json(force=True) or {}
    try:
        item = db.update_menu_item(
            item_id,
            name=data.get('name'),
            category=data.get('category'),
            price=float(data['price']) if data.get('price') is not None else None,
            portion_size=data.get('portion_size'),
            section=data.get('section'), has_sides=data.get('has_sides'), asks_doneness=data.get('asks_doneness'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item)


@app.route('/api/menu/<int:item_id>/toggle', methods=['POST'])
@admin_required
def api_toggle_menu_item(item_id):
    try:
        item = db.toggle_menu_item(item_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item)


# ------------------------------------------------------------ Inventory ----

@app.route('/api/inventory', methods=['GET'])
@login_required
def api_list_inventory():
    return jsonify(db.list_inventory_items())


@app.route('/api/inventory', methods=['POST'])
@admin_required
def api_create_inventory_item():
    data = request.get_json(force=True) or {}
    required = ['item_name', 'unit_of_measure']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        item = db.create_inventory_item(
            item_name=data['item_name'], unit_of_measure=data['unit_of_measure'],
            current_stock=float(data.get('current_stock', 0)),
            low_stock_threshold=float(data.get('low_stock_threshold', 0)),
            bar_stock=float(data.get('bar_stock', 0)),
            unit_cost=float(data.get('unit_cost', 0)),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item), 201


@app.route('/api/inventory/<int:item_id>', methods=['PUT'])
@admin_required
def api_update_inventory_item(item_id):
    data = request.get_json(force=True) or {}
    try:
        item = db.update_inventory_item(
            item_id, item_name=data.get('item_name'), unit_of_measure=data.get('unit_of_measure'),
            low_stock_threshold=float(data['low_stock_threshold']) if data.get('low_stock_threshold') is not None else None,
            current_stock=float(data['current_stock']) if data.get('current_stock') is not None else None,
            bar_stock=float(data['bar_stock']) if data.get('bar_stock') is not None else None,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item)


@app.route('/api/inventory/<int:item_id>', methods=['DELETE'])
@admin_required
def api_delete_inventory_item(item_id):
    try:
        result = db.delete_inventory_item(item_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


IMPORT_MAX_BYTES = 5 * 1024 * 1024
IMPORT_NAME_HEADERS = ('articulo', 'producto', 'nombre', 'descripcion', 'item', 'insumo', 'material', 'detalle')
IMPORT_UNIT_HEADERS = ('unidad', 'medida', 'um', 'u/m', 'presentacion', 'unit')
# 'Saldo' is what the hotel's own inventory sheets call the quantity on hand.
IMPORT_QTY_HEADERS = ('saldo', 'cantidad', 'existencia', 'existencias', 'stock')


def _parse_quantity(value):
    """Spreadsheet quantity -> float; accepts numbers or text like '1,234.50'."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or '').strip().replace(' ', '')
    if not text:
        return 0.0
    if ',' in text and '.' in text:
        # Whichever separator comes last is the decimal one ('1,234.50' / '1.234,50').
        text = text.replace(',', '') if text.rfind('.') > text.rfind(',') else text.replace('.', '').replace(',', '.')
    elif ',' in text:
        text = text.replace(',', '.') if len(text.split(',')[-1]) != 3 else text.replace(',', '')
    try:
        return float(text)
    except ValueError:
        return 0.0


def _normalize_header(value):
    import unicodedata
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(text.split())


def _read_spreadsheet_sheets(filename, data):
    """Return (sheets, active_index): every sheet of the upload as a list of
    row tuples (a CSV is a single sheet)."""
    if filename.endswith('.csv'):
        import csv, io
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1252')
        delimiter = ';' if text.count(';') > text.count(',') else ','
        return [[tuple(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]], 0
    import io, openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = [[tuple(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    active_index = wb.worksheets.index(wb.active)
    wb.close()
    return sheets, active_index


def _is_text(value):
    return isinstance(value, str) and value.strip() and not value.strip().replace('.', '', 1).isdigit()


def _extract_import_items(rows):
    """Find the item-name column (plus unit and quantity columns if any) by
    header, e.g. 'Artículo'/'Descripción', 'Unidad', 'Saldo'. A header row
    with 'Saldo' counts even when the name column has no title of its own
    (the hotel's weekly bodega sheets) — the names are then the first column
    holding text. Rows with a blank quantity are section titles ('CERVEZAS')
    and are skipped. Returns (items, found_saldo_column)."""
    header_idx, name_col, unit_col, qty_col = None, None, None, None
    found_saldo = False
    for idx, row in enumerate(rows[:15]):
        headers = [_normalize_header(c) for c in row]
        for col, h in enumerate(headers):
            if name_col is None and any(k in h for k in IMPORT_NAME_HEADERS):
                name_col = col
            elif unit_col is None and h and (h in IMPORT_UNIT_HEADERS or h.startswith('unidad')):
                unit_col = col
        qty_matches = {h: col for col, h in enumerate(headers) if h in IMPORT_QTY_HEADERS or h.startswith('saldo')}
        qty_col = next((qty_matches[h] for h in sorted(qty_matches, key=lambda h: not h.startswith('saldo'))), None)
        found_saldo = any(h.startswith('saldo') for h in qty_matches)
        if name_col is not None or found_saldo:
            header_idx = idx
            break
        name_col = unit_col = qty_col = None
    if header_idx is not None and name_col is None:
        sample = rows[header_idx + 1:header_idx + 31]
        width = max((len(r) for r in sample), default=0)
        name_col = next((col for col in range(width) if col != qty_col
                         and sum(1 for r in sample if col < len(r) and _is_text(r[col])) >= 3), None)
    if name_col is None:
        for row in rows:
            for col, c in enumerate(row):
                if isinstance(c, str) and c.strip() and not c.strip().replace('.', '', 1).isdigit():
                    name_col = col
                    break
            if name_col is not None:
                break
    if name_col is None:
        return [], False
    items = []
    for row in rows[(header_idx + 1) if header_idx is not None else 0:]:
        raw = row[name_col] if name_col < len(row) else None
        if raw is None or isinstance(raw, (int, float)):
            continue
        name = ' '.join(str(raw).split())[:100]
        if not name or name.lower() in ('total', 'totales', 'subtotal'):
            continue
        qty_cell = row[qty_col] if qty_col is not None and qty_col < len(row) else None
        if qty_col is not None and qty_cell in (None, ''):
            continue
        unit = row[unit_col] if unit_col is not None and unit_col < len(row) else None
        unit = ' '.join(str(unit).split())[:20] if unit not in (None, '') else None
        items.append((name, unit, _parse_quantity(qty_cell)))
    return items, found_saldo


@app.route('/api/inventory/import', methods=['POST'])
@admin_required
def api_import_inventory():
    if (request.content_length or 0) > IMPORT_MAX_BYTES:
        return jsonify({'error': 'El archivo es muy grande (máximo 5 MB)'}), 400
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'Seleccione un archivo Excel (.xlsx) o CSV'}), 400
    filename = upload.filename.lower()
    if not filename.endswith(('.xlsx', '.csv')):
        return jsonify({'error': 'Formato no soportado. Guarde el archivo como Excel (.xlsx) o CSV.'}), 400
    try:
        sheets, active_index = _read_spreadsheet_sheets(filename, upload.read())
    except Exception:
        return jsonify({'error': 'No se pudo leer el archivo. Verifique que sea un Excel (.xlsx) válido.'}), 400
    parsed = [_extract_import_items(rows) for rows in sheets]
    # Workbooks with a 'Saldo' column (one tab per bodega) import every such
    # tab; any other file just its active sheet.
    if any(found_saldo for _, found_saldo in parsed):
        chosen = [items for items, found_saldo in parsed if found_saldo]
    else:
        chosen = [parsed[active_index][0]]
    items, seen = [], set()
    for sheet_items in chosen:
        for item in sheet_items:
            # First appearance wins; later repeats (another tab, or a
            # per-area section further down) are not added on top.
            if item[0].lower() not in seen:
                seen.add(item[0].lower())
                items.append(item)
    if not items:
        return jsonify({'error': 'No se encontraron artículos en el archivo'}), 400
    return jsonify(db.import_inventory_items(items))


@app.route('/api/inventory/<int:item_id>/adjust', methods=['POST'])
@admin_required
def api_adjust_inventory_item(item_id):
    data = request.get_json(force=True) or {}
    if data.get('quantity_change') is None:
        return jsonify({'error': 'quantity_change es obligatorio'}), 400
    try:
        item = db.adjust_inventory_stock(
            item_id, quantity_change=float(data['quantity_change']),
            reason=data.get('reason', 'manual_adjustment'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item)


# ------------------------------------------------- Inventory & Requisition --

@app.route('/api/inventory/receive', methods=['POST'])
@front_desk_access_required
def api_receive_inventory():
    """Logs an incoming shipment into the main storeroom."""
    data = request.get_json(force=True) or {}
    if not data.get('inventory_item_id') or data.get('quantity') is None:
        return jsonify({'error': 'inventory_item_id y quantity son obligatorios'}), 400
    try:
        receipt = db.receive_inventory(
            int(data['inventory_item_id']), float(data['quantity']), session['user_id'],
            supplier=data.get('supplier'), notes=data.get('notes'),
            unit_cost=float(data['unit_cost']) if data.get('unit_cost') not in (None, '') else None,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(receipt), 201


@app.route('/api/inventory/receipts', methods=['GET'])
@front_desk_access_required
def api_list_inventory_receipts():
    return jsonify(db.list_inventory_receipts())


@app.route('/api/inventory/transfers', methods=['POST'])
@login_required
def api_request_inventory_transfer():
    """Any staff member can request stock move from the storeroom to the
    bar/restaurant (or a direct cleaning-supplies issue)."""
    data = request.get_json(force=True) or {}
    required = ['inventory_item_id', 'quantity', 'destination']
    missing = [f for f in required if data.get(f) in (None, '')]
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        transfer = db.request_inventory_transfer(
            int(data['inventory_item_id']), float(data['quantity']), data['destination'],
            session['user_id'], notes=data.get('notes'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'inventory_transfer_requested'})
    return jsonify(transfer), 201


@app.route('/api/inventory/transfers', methods=['GET'])
@front_desk_access_required
def api_list_inventory_transfers():
    return jsonify(db.list_inventory_transfers())


@app.route('/api/inventory/transfers/pending', methods=['GET'])
@front_desk_access_required
def api_list_pending_inventory_transfers():
    return jsonify(db.list_pending_inventory_transfers())


@app.route('/api/inventory/transfers/<int:transfer_id>/decide', methods=['POST'])
@front_desk_access_required
def api_decide_inventory_transfer(transfer_id):
    """Authorizes or rejects a pending bar/restaurant transfer request."""
    data = request.get_json(force=True) or {}
    if data.get('approve') is None:
        return jsonify({'error': 'approve (true/false) es obligatorio'}), 400
    try:
        transfer = db.decide_inventory_transfer(
            transfer_id, session['user_id'], bool(data['approve']), note=data.get('note'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'inventory_transfer_decided'})
    return jsonify(transfer)


@app.route('/api/inventory/writeoff', methods=['POST'])
@front_desk_access_required
def api_write_off_inventory():
    """Records shrinkage (expired/spoiled/broken) as its own Kardex line."""
    data = request.get_json(force=True) or {}
    if not data.get('inventory_item_id') or data.get('quantity') is None or not data.get('pool'):
        return jsonify({'error': 'inventory_item_id, quantity y pool son obligatorios'}), 400
    try:
        item = db.write_off_inventory(
            int(data['inventory_item_id']), float(data['quantity']), data['pool'],
            session['user_id'], reason_note=data.get('reason_note'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item), 201


@app.route('/api/inventory/<int:item_id>/kardex', methods=['GET'])
@admin_required
def api_inventory_kardex(item_id):
    """Full costed movement history for one item — quantities and the
    weighted-average valuation behind them, over an optional date range."""
    try:
        kardex = db.get_inventory_kardex(
            item_id, start_date=request.args.get('start_date'), end_date=request.args.get('end_date'),
            pool=request.args.get('pool'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(kardex)


# -------------------------------------------------------------- Recipes ----

@app.route('/api/recipes', methods=['GET'])
@admin_required
def api_list_recipes():
    return jsonify(db.list_all_recipes())


@app.route('/api/menu/<int:menu_item_id>/recipe', methods=['GET'])
@admin_required
def api_get_recipe(menu_item_id):
    return jsonify(db.get_recipe_for_menu_item(menu_item_id))


@app.route('/api/menu/<int:menu_item_id>/recipe', methods=['POST'])
@admin_required
def api_add_recipe_line(menu_item_id):
    data = request.get_json(force=True) or {}
    if not data.get('inventory_item_id') or data.get('quantity_required') is None:
        return jsonify({'error': 'inventory_item_id y quantity_required son obligatorios'}), 400
    try:
        line = db.add_recipe_line(
            menu_item_id, inventory_item_id=int(data['inventory_item_id']),
            quantity_required=float(data['quantity_required']),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(line), 201


@app.route('/api/recipes/<int:recipe_id>', methods=['DELETE'])
@admin_required
def api_delete_recipe_line(recipe_id):
    try:
        result = db.delete_recipe_line(recipe_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


# -------------------------------------------------------------- Guests ----

@app.route('/api/guests', methods=['GET'])
@front_desk_access_required
def api_list_guests():
    status = request.args.get('status')
    return jsonify(db.list_guests(status=status))


@app.route('/api/guests/active-lite', methods=['GET'])
@login_required
def api_list_guests_lite():
    """Minimal active-guest list (name/room only) so servers can attach
    Charge-to-Room orders without seeing folio balances or full guest data."""
    guests = db.list_guests(status='checked_in')
    lite = [{'id': g['id'], 'name': g['name'], 'room_number': g['room_number'], 'room_type': g['room_type']}
            for g in guests]
    return jsonify(lite)


def _registration_card_fields(data):
    """The extra fields the registration card can show (agency, rate code,
    occupancy, full address) — all optional, matching the property's paper
    form where most of these are often left blank."""
    fields = {
        'rate_code': data.get('rate_code'),
        'country': data.get('country'), 'city': data.get('city'),
        'postal_code': data.get('postal_code'), 'booking_reference': data.get('booking_reference'),
    }
    for key in ('adults', 'children'):
        if data.get(key) not in (None, ''):
            try:
                fields[key] = int(data[key])
            except (TypeError, ValueError):
                pass
    return fields


def _pricing_args(data):
    """Agency, rate type (rack / net / manual) and tax exemption from a
    booking form. A manual rate is a discount tool for special guests /
    family — only an admin can apply one."""
    rate_type = data.get('rate_type') or 'rack'
    if rate_type not in db.RATE_TYPES:
        raise ValueError('Tipo de tarifa inválido')
    nightly_rate = None
    if rate_type == 'manual' or data.get('nightly_rate') not in (None, ''):
        if session.get('role') != 'admin':
            raise ValueError('Solo un administrador puede ingresar una tarifa manual')
        try:
            nightly_rate = float(data['nightly_rate'])
        except (TypeError, ValueError, KeyError):
            raise ValueError('Ingrese la tarifa manual por noche')
        rate_type = 'manual'
    agency_id = data.get('agency_id') or None
    if agency_id is not None:
        try:
            agency_id = int(agency_id)
        except (TypeError, ValueError):
            raise ValueError('Agencia inválida')
    # The agency can also be typed by hand ("JP TRAVEL"): a registered one is
    # matched by name; anything else is kept as typed, with no commission.
    agency_name = ' '.join((data.get('agency_name') or '').split())[:60] or None
    if agency_id is None and agency_name:
        known = db.find_agency_by_name(agency_name)
        if known:
            agency_id, agency_name = known['id'], None
    if agency_name and rate_type == 'net':
        raise ValueError(f'«{agency_name}» no está registrada y no tiene tarifa neta. Use Rack o regístrela en Agencias.')
    return {'rate_type': rate_type, 'agency_id': agency_id, 'nightly_rate': nightly_rate,
            'tax_exempt': bool(data.get('tax_exempt')), 'agency_name': agency_name}


@app.route('/api/guests/checkin', methods=['POST'])
@front_desk_access_required
def api_checkin():
    data = request.get_json(force=True) or {}
    required = ['name', 'room_number', 'room_type', 'checkin_date', 'checkout_date']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        guest = db.check_in_guest(
            name=data['name'], room_number=data['room_number'], room_type=data['room_type'],
            checkin_date=data['checkin_date'], checkout_date=data['checkout_date'],
            observations=data.get('observations'),
            id_number=data.get('id_number'), nationality=data.get('nationality'), address=data.get('address'),
            **_pricing_args(data), **_registration_card_fields(data), created_by=session['user_id'],
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'checkin'})
    return jsonify(guest), 201


@app.route('/api/guests/<int:guest_id>/details', methods=['PUT'])
@front_desk_access_required
def api_update_guest_details(guest_id):
    data = request.get_json(force=True) or {}
    try:
        guest = db.update_guest_details(
            guest_id, observations=data.get('observations'), id_number=data.get('id_number'),
            nationality=data.get('nationality'), address=data.get('address'),
            **_registration_card_fields(data),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(guest)


@app.route('/api/guests/<int:guest_id>/room', methods=['PUT'])
@front_desk_access_required
def api_change_guest_room(guest_id):
    data = request.get_json(force=True) or {}
    if not data.get('room_number'):
        return jsonify({'error': 'room_number es obligatorio'}), 400
    try:
        guest = db.change_guest_room(
            guest_id, new_room_number=data['room_number'],
            changed_by_user_id=session['user_id'], reason=data.get('reason'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'room_changed'})
    return jsonify(guest)


@app.route('/api/guests/<int:guest_id>/folio', methods=['GET'])
@front_desk_access_required
def api_guest_folio(guest_id):
    try:
        folio = db.get_guest_folio(guest_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(folio)


@app.route('/api/guests/<int:guest_id>/rate', methods=['PUT'])
@admin_required
def api_adjust_guest_rate(guest_id):
    data = request.get_json(force=True) or {}
    if data.get('nightly_rate') is None:
        return jsonify({'error': 'nightly_rate es obligatorio'}), 400
    try:
        guest = db.adjust_guest_rate(
            guest_id, new_rate=float(data['nightly_rate']),
            admin_user_id=session['user_id'], reason=data.get('reason'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(guest)


@app.route('/api/guests/<int:guest_id>/pricing', methods=['PUT'])
@front_desk_access_required
def api_update_guest_pricing(guest_id):
    """Change a reservation's / stay's agency, rate type or tax exemption."""
    data = request.get_json(force=True) or {}
    try:
        if data.get('rate_type') == 'manual' and data.get('nightly_rate') in (None, ''):
            # Keeping a stay's existing manual rate (e.g. only toggling the
            # tax exemption) needs no admin; setting a new one does.
            current = db.get_guest(guest_id)
            if not current or current['rate_type'] != 'manual':
                raise ValueError('Para una tarifa manual use «Ajustar Tarifa» (administrador)')
            data = {**data, 'rate_type': 'rack'}
            args = {**_pricing_args(data), 'rate_type': 'manual'}
        else:
            args = _pricing_args(data)
        guest = db.update_guest_pricing(
            guest_id, session['user_id'], rate_type=args['rate_type'],
            agency_id=args['agency_id'], manual_rate=args['nightly_rate'], tax_exempt=args['tax_exempt'],
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(guest)


@app.route('/api/rates/quote', methods=['POST'])
@front_desk_access_required
def api_rate_quote():
    """Live price of a stay for the booking form, before it is saved."""
    data = request.get_json(force=True) or {}
    try:
        args = _pricing_args(data)
        quote = db.quote_stay(data.get('room_type'), data.get('checkin_date'), data.get('checkout_date'),
                              args['rate_type'], args['agency_id'], args['nightly_rate'], args['tax_exempt'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(quote)


@app.route('/api/guests/<int:guest_id>/checkout', methods=['POST'])
@front_desk_access_required
def api_checkout(guest_id):
    try:
        folio = db.checkout_guest(guest_id, checked_out_by_user_id=session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'checkout'})
    return jsonify(folio)


# ----------------------------------------------------------------- Rooms ----

@app.route('/api/rooms', methods=['GET'])
@front_desk_access_required
def api_list_rooms():
    return jsonify(db.list_rooms())


@app.route('/api/rooms/available', methods=['GET'])
@front_desk_access_required
def api_available_rooms():
    room_type = request.args.get('room_type')
    if room_type not in db.ROOM_TYPES:
        return jsonify({'error': f'room_type must be one of {db.ROOM_TYPES}'}), 400
    return jsonify(db.list_available_rooms(room_type))


@app.route('/api/rooms/inventory', methods=['GET'])
@front_desk_access_required
def api_room_inventory():
    return jsonify(db.get_room_inventory_status())


@app.route('/api/rooms/<room_number>/housekeeping', methods=['PUT'])
@front_desk_access_required
def api_set_room_housekeeping(room_number):
    data = request.get_json(force=True) or {}
    status = data.get('status')
    if status not in db.HOUSEKEEPING_STATUSES:
        return jsonify({'error': f'status debe ser uno de {db.HOUSEKEEPING_STATUSES}'}), 400
    try:
        room = db.set_room_housekeeping_status(
            room_number, status, session['user_id'], notes=(data.get('notes') or '').strip() or None,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'housekeeping'})
    return jsonify(room)


@app.route('/api/rooms/timeline', methods=['GET'])
@front_desk_access_required
def api_room_timeline():
    start_date = request.args.get('start_date')
    if not start_date:
        return jsonify({'error': 'start_date es obligatorio'}), 400
    try:
        days = int(request.args.get('days', 14))
    except ValueError:
        return jsonify({'error': 'days debe ser un número entero'}), 400
    return jsonify(db.get_room_timeline(start_date, days))


# --------------------------------------------------------- Reservations ----

@app.route('/api/reservations', methods=['GET'])
@front_desk_access_required
def api_list_reservations():
    return jsonify(db.list_reservations())


@app.route('/api/reservations', methods=['POST'])
@front_desk_access_required
def api_create_reservation():
    """Front desk / admin booking a future stay directly (the 'Reserve'
    button) — as opposed to a walk-in's immediate Check In."""
    data = request.get_json(force=True) or {}
    required = ['name', 'room_type', 'checkin_date', 'checkout_date']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        guest = db.create_reservation(
            name=data['name'], room_type=data['room_type'],
            checkin_date=data['checkin_date'], checkout_date=data['checkout_date'],
            email=data.get('email'), phone=data.get('phone'), room_number=data.get('room_number'),
            observations=data.get('observations'), source='walk_in', **_pricing_args(data),
            **_registration_card_fields(data), created_by=session['user_id'],
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'reservation_created'})
    return jsonify(guest), 201


@app.route('/api/reservations/<int:guest_id>/checkin', methods=['POST'])
@front_desk_access_required
def api_checkin_reservation(guest_id):
    data = request.get_json(silent=True) or {}
    try:
        guest = db.checkin_reservation(
            guest_id, room_number=data.get('room_number'), id_number=data.get('id_number'),
            nationality=data.get('nationality'), address=data.get('address'),
            observations=data.get('observations'),
            **_registration_card_fields(data),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'reservation_checkin'})
    return jsonify(guest)


@app.route('/api/reservations/<int:guest_id>', methods=['DELETE'])
@front_desk_access_required
def api_cancel_reservation(guest_id):
    data = request.get_json(silent=True) or {}
    try:
        result = db.cancel_reservation(guest_id, cancelled_by_user_id=session['user_id'], reason=data.get('reason'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'reservation_cancelled'})
    return jsonify(result)


# ------------------------------------------------ Rates, seasons, agencies --

@app.route('/api/pricing', methods=['GET'])
@front_desk_access_required
def api_pricing():
    """Everything the rate screens need: tax/weekend settings, the rate grid,
    high-season periods and the labels."""
    return jsonify({
        'settings': db.get_pricing_settings(),
        'rates': db.get_room_rates(),
        'high_seasons': db.list_high_seasons(),
        'room_types': db.ROOM_TYPES, 'room_labels': db.ROOM_TYPE_LABELS,
        'seasons': db.SEASON_LABELS_ES, 'day_types': db.DAY_TYPE_LABELS_ES,
        'rate_types': db.RATE_TYPE_LABELS_ES, 'commission_choices': db.COMMISSION_CHOICES,
    })


@app.route('/api/pricing/settings', methods=['PUT'])
@admin_required
def api_save_pricing_settings():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.save_pricing_settings(data.get('tax_rate'), data.get('weekend_nights') or [], session['user_id']))
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/pricing/rates', methods=['PUT'])
@admin_required
def api_save_room_rates():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.save_room_rates(data.get('rates') or [], session['user_id']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/pricing/seasons', methods=['POST'])
@admin_required
def api_add_high_season():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.add_high_season(data.get('name'), data.get('start_date'), data.get('end_date'),
                                          session['user_id'])), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/pricing/seasons/<int:period_id>', methods=['DELETE'])
@admin_required
def api_delete_high_season(period_id):
    try:
        return jsonify(db.delete_high_season(period_id, session['user_id']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 404


@app.route('/api/agencies', methods=['GET'])
@front_desk_access_required
def api_list_agencies():
    # Front desk picks from active agencies; the admin manages all of them.
    active_only = session.get('role') != 'admin' or request.args.get('active') == '1'
    return jsonify(db.list_agencies(active_only=active_only))


@app.route('/api/agencies', methods=['POST'])
@admin_required
def api_create_agency():
    try:
        return jsonify(db.save_agency(request.get_json(force=True) or {}, session['user_id'])), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/agencies/<int:agency_id>', methods=['PUT'])
@admin_required
def api_update_agency(agency_id):
    try:
        return jsonify(db.save_agency(request.get_json(force=True) or {}, session['user_id'], agency_id=agency_id))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ----------------------------------------------- Front desk operating lists --

def _room_list_args():
    return (request.args.get('kind', 'arrivals'), request.args.get('date') or datetime.now().date().isoformat(),
            request.args.get('agency_id') or None)


@app.route('/api/reports/room-list', methods=['GET'])
@front_desk_access_required
def api_room_list():
    """Llegadas / En Casa / Salidas for one day, optionally one agency."""
    try:
        return jsonify(db.room_list(*_room_list_args()))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/print/room-list')
def print_room_list():
    if 'user_id' not in session or session.get('role') not in ('admin', 'front_desk'):
        return redirect(url_for('login_page'))
    kind, day, agency_id = _room_list_args()
    try:
        data = db.room_list(kind, day, agency_id)
    except ValueError as e:
        return str(e), 400
    agency = db.get_agency(int(agency_id)) if agency_id else None
    return render_template('room_list.html', **data, agency=agency,
                           room_labels=db.ROOM_TYPE_LABELS, rate_labels=db.RATE_TYPE_LABELS_ES,
                           date_display=datetime.strptime(data['date'], '%Y-%m-%d').strftime('%d/%m/%Y'),
                           issued_display=datetime.now().strftime('%d/%m/%Y %I:%M %p'))


@app.route('/export/room-list.xlsx')
def export_room_list():
    if 'user_id' not in session or session.get('role') not in ('admin', 'front_desk'):
        return redirect(url_for('login_page'))
    kind, day, agency_id = _room_list_args()
    try:
        data = db.room_list(kind, day, agency_id)
    except ValueError as e:
        return str(e), 400
    agency = db.get_agency(int(agency_id)) if agency_id else None
    buf = report_export.build_room_list_workbook(data, db.ROOM_TYPE_LABELS, db.RATE_TYPE_LABELS_ES,
                                                 agency['name'] if agency else None)
    names = {'arrivals': 'llegadas', 'in_house': 'en_casa', 'departures': 'salidas'}
    return send_file(buf, as_attachment=True, download_name=f"{names[kind]}_{data['date']}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ------------------------------------------------------- Cashier shifts ----

@app.route('/api/shifts/current', methods=['GET'])
@front_desk_access_required   # the cashier at Recepción runs the shift
def api_current_shift():
    """The user's open shift with its close so far, or null."""
    shift = db.get_open_shift(session['user_id'])
    return jsonify(db.get_shift(shift['id']) if shift else None)


@app.route('/api/shifts/open', methods=['POST'])
@front_desk_access_required   # the cashier at Recepción runs the shift
def api_open_shift():
    data = request.get_json(silent=True) or {}
    try:
        shift = db.open_shift(session['user_id'], data.get('opening_cash'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(db.get_shift(shift['id'])), 201


@app.route('/api/shifts/close', methods=['POST'])
@front_desk_access_required   # the cashier at Recepción runs the shift
def api_close_shift():
    data = request.get_json(silent=True) or {}
    try:
        shift = db.close_shift(session['user_id'], data.get('counted_cash'), data.get('notes'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(shift)


@app.route('/api/shifts', methods=['GET'])
@login_required
def api_list_shifts():
    """Recent closes: an admin sees everyone's, anyone else only their own."""
    limit = min(request.args.get('limit', 30, type=int) or 30, 200)
    return jsonify(db.list_shifts(None if session.get('role') == 'admin' else session['user_id'], limit))


@app.route('/api/shifts/<int:shift_id>/history', methods=['GET'])
@admin_required
def api_shift_history(shift_id):
    """Históricos de Turno: every report section of one shift."""
    data = db.shift_history(shift_id)
    if not data:
        return jsonify({'error': 'Turno no encontrado'}), 404
    return jsonify(data)


@app.route('/api/history/general', methods=['GET'])
@admin_required
def api_general_history():
    """Históricos de Turno, general: every cashier together for ?from=&to= (YYYY-MM-DD)."""
    try:
        return jsonify(db.general_history(request.args.get('from'), request.args.get('to')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/export/history/general/<key>.xlsx')
def export_general_history(key):
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    try:
        data = db.general_history(request.args.get('from'), request.args.get('to'))
    except ValueError as e:
        return str(e), 400
    sections = data['sections'] if key == 'todo' else [s for s in data['sections'] if s['key'] == key]
    if not sections:
        return 'Reporte no encontrado', 404
    g = data['general']
    period = g['from'] if g['from'] == g['to'] else f"{g['from']}_al_{g['to']}"
    buf = report_export.build_shift_history_workbook(data, sections)
    return send_file(buf, as_attachment=True, download_name=f'historico_general_{period}_{key}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/export/shift/<int:shift_id>/<key>.xlsx')
def export_shift_history(shift_id, key):
    """One section of a shift's history as Excel ('todo': every section, one sheet each)."""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    data = db.shift_history(shift_id)
    if not data:
        return 'Turno no encontrado', 404
    sections = data['sections'] if key == 'todo' else [s for s in data['sections'] if s['key'] == key]
    if not sections:
        return 'Reporte no encontrado', 404
    buf = report_export.build_shift_history_workbook(data, sections)
    return send_file(buf, as_attachment=True, download_name=f'turno_{shift_id}_{key}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/print/shift/<int:shift_id>')
def print_shift(shift_id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    shift = db.get_shift(shift_id)
    if not shift or (shift['user_id'] != session['user_id'] and session.get('role') != 'admin'):
        return 'Turno no encontrado', 404
    fmt = lambda v: datetime.strptime(v, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %I:%M %p') if v else ''
    return render_template('shift_close.html', shift=shift, fmt=fmt,
                           issued_display=datetime.now().strftime('%d/%m/%Y %I:%M %p'))


# ----------------------------------------------------------- Maintenance ----
# One tab for everything maintenance. Anyone may report a breakdown (waiters
# see only their own reports); maintenance, front desk and admins work the
# breakdowns and the maintenance storeroom; using material from it always
# needs an administrator's approval.

MAINTENANCE_STAFF = ('admin', 'front_desk', 'maintenance')


def _maintenance_staff_only():
    if session.get('role') not in MAINTENANCE_STAFF:
        return jsonify({'error': 'Solo Mantenimiento, Recepción o Administración'}), 403
    return None


@app.route('/api/maintenance/meta', methods=['GET'])
@login_required
@maintenance_allowed
def api_maintenance_meta():
    role = session.get('role')
    rooms = sorted({r for rs in db.ROOM_NUMBERS_BY_TYPE.values() for r in rs}, key=lambda r: int(r))
    return jsonify({
        'areas': db.MAINTENANCE_AREAS, 'rooms': rooms,
        'priorities': db.INCIDENT_PRIORITY_LABELS_ES, 'statuses': db.INCIDENT_STATUS_LABELS_ES,
        'can_work': role in MAINTENANCE_STAFF,
        'can_approve_usage': role == 'admin',
        'can_approve_transfers': role in ('admin', 'front_desk'),
        'can_take_room_out': role in MAINTENANCE_STAFF,
        'can_manage_items': role == 'admin',
        'can_export': role in ('admin', 'front_desk'),
    })


@app.route('/api/maintenance/incidents', methods=['GET'])
@login_required
@maintenance_allowed
def api_list_incidents():
    args = request.args
    # Waiters only see what they reported themselves.
    mine = session.get('role') not in MAINTENANCE_STAFF
    return jsonify(db.list_incidents(
        status=args.get('status') or None, reported_by=session['user_id'] if mine else None,
        room_number=args.get('room') or None, area=args.get('area') or None,
        start_date=args.get('start') or None, end_date=args.get('end') or None))


@app.route('/api/maintenance/incidents', methods=['POST'])
@login_required
@maintenance_allowed
def api_report_incident():
    if (request.content_length or 0) > 4_000_000:
        return jsonify({'error': 'La foto es demasiado grande'}), 400
    data = request.get_json(force=True) or {}
    photo_mime = photo_data = None
    photo = data.get('photo') or ''
    if photo:
        m = re.match(r'^data:(image/[a-z]+);base64,([A-Za-z0-9+/=]+)$', photo)
        if not m:
            return jsonify({'error': 'Foto inválida'}), 400
        photo_mime, photo_data = m.group(1), m.group(2)
    try:
        incident = db.report_incident(
            session['user_id'], data.get('area'), data.get('description'), data.get('priority') or 'medium',
            room_number=data.get('room_number'), photo_mime=photo_mime, photo_data=photo_data,
            mark_out_of_order=bool(data.get('mark_out_of_order')) and session.get('role') in MAINTENANCE_STAFF)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if incident['room_out_of_order']:
        realtime.broadcast('availability_changed', {'reason': 'housekeeping'})
    return jsonify(incident), 201


@app.route('/api/maintenance/incidents/<int:incident_id>/start', methods=['POST'])
@login_required
@maintenance_allowed
def api_start_incident(incident_id):
    denied = _maintenance_staff_only()
    if denied:
        return denied
    try:
        return jsonify(db.start_incident(incident_id, session['user_id']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/maintenance/incidents/<int:incident_id>/resolve', methods=['POST'])
@login_required
@maintenance_allowed
def api_resolve_incident(incident_id):
    denied = _maintenance_staff_only()
    if denied:
        return denied
    data = request.get_json(force=True) or {}
    try:
        incident = db.resolve_incident(incident_id, session['user_id'], data.get('resolution_note'),
                                       materials=data.get('materials') or [],
                                       return_to_service=bool(data.get('return_to_service')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'housekeeping'})
    return jsonify(incident)


@app.route('/api/maintenance/photos/<int:photo_id>', methods=['GET'])
@login_required
@maintenance_allowed
def api_incident_photo(photo_id):
    photo = db.get_incident_photo(photo_id)
    if not photo or (session.get('role') not in MAINTENANCE_STAFF and photo['reported_by'] != session['user_id']):
        return 'Foto no encontrada', 404
    return Response(base64.b64decode(photo['data']), mimetype=photo['mime'],
                    headers={'Cache-Control': 'private, max-age=86400'})


@app.route('/api/maintenance/items', methods=['GET'])
@login_required
@maintenance_allowed
def api_maintenance_items():
    denied = _maintenance_staff_only()
    if denied:
        return denied
    return jsonify(db.list_maintenance_items())


@app.route('/api/maintenance/items', methods=['POST'])
@admin_required
def api_create_maintenance_item():
    data = request.get_json(force=True) or {}
    try:
        item = db.create_maintenance_item(data.get('item_name'), data.get('unit_of_measure'),
                                          data.get('maintenance_threshold'), data.get('initial_stock'),
                                          session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(item), 201


IMPORT_MIN_HEADERS = ('minimo', 'min', 'stock minimo', 'existencia minima', 'cantidad minima', 'punto de reorden')


def _maintenance_minimums(rows):
    """{item name (lowercase): minimum} from an optional 'Mínimo' column."""
    for idx, row in enumerate(rows[:15]):
        headers = [_normalize_header(c) for c in row]
        min_col = next((c for c, h in enumerate(headers) if h in IMPORT_MIN_HEADERS or h.startswith('minimo')), None)
        if min_col is None:
            continue
        name_col = next((c for c, h in enumerate(headers) if any(k in h for k in IMPORT_NAME_HEADERS)), None)
        if name_col is None:
            return {}
        out = {}
        for r in rows[idx + 1:]:
            name = r[name_col] if name_col < len(r) else None
            value = r[min_col] if min_col < len(r) else None
            if isinstance(name, str) and name.strip() and value not in (None, ''):
                out[' '.join(name.split())[:100].lower()] = _parse_quantity(value)
        return out
    return {}


@app.route('/api/maintenance/items/import', methods=['POST'])
@admin_required
def api_import_maintenance_items():
    """Excel/CSV with Artículo, Unidad, Cantidad (or Saldo) and an optional
    Mínimo column, loaded straight into the maintenance storeroom."""
    if (request.content_length or 0) > IMPORT_MAX_BYTES:
        return jsonify({'error': 'El archivo es muy grande (máximo 5 MB)'}), 400
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'Seleccione un archivo Excel (.xlsx) o CSV'}), 400
    filename = upload.filename.lower()
    if not filename.endswith(('.xlsx', '.csv')):
        return jsonify({'error': 'Formato no soportado. Guarde el archivo como Excel (.xlsx) o CSV.'}), 400
    try:
        sheets, active_index = _read_spreadsheet_sheets(filename, upload.read())
    except Exception:
        return jsonify({'error': 'No se pudo leer el archivo. Verifique que sea un Excel (.xlsx) válido.'}), 400
    parsed = [_extract_import_items(rows) for rows in sheets]
    chosen = [i for i, (_, saldo) in enumerate(parsed) if saldo] or [active_index]
    items, seen = [], set()
    for i in chosen:
        minimums = _maintenance_minimums(sheets[i])
        for name, unit, qty in parsed[i][0]:
            if name.lower() not in seen:
                seen.add(name.lower())
                items.append((name, unit, qty, minimums.get(name.lower())))
    if not items:
        return jsonify({'error': 'No se encontraron artículos en el archivo'}), 400
    return jsonify(db.import_maintenance_items(items, session['user_id']))


@app.route('/api/maintenance/items/<int:item_id>/threshold', methods=['PUT'])
@admin_required
def api_set_maintenance_threshold(item_id):
    data = request.get_json(force=True) or {}
    try:
        db.set_maintenance_threshold(item_id, data.get('maintenance_threshold'), session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True})


@app.route('/api/maintenance/transfer-requests', methods=['GET'])
@login_required
@maintenance_allowed
def api_maintenance_transfer_requests():
    denied = _maintenance_staff_only()
    if denied:
        return denied
    return jsonify(db.list_maintenance_transfer_requests())


@app.route('/api/maintenance/transfer-requests', methods=['POST'])
@login_required
@maintenance_allowed
def api_request_maintenance_transfer():
    """Ask the main storeroom for material; front desk or an admin authorizes it."""
    denied = _maintenance_staff_only()
    if denied:
        return denied
    data = request.get_json(force=True) or {}
    try:
        transfer = db.request_inventory_transfer(int(data.get('inventory_item_id') or 0), float(data.get('quantity') or 0),
                                                 'maintenance', session['user_id'], notes=data.get('notes'))
    except (TypeError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'inventory_transfer_requested'})
    return jsonify(transfer), 201


@app.route('/api/maintenance/usage', methods=['GET'])
@login_required
@maintenance_allowed
def api_maintenance_usage():
    denied = _maintenance_staff_only()
    if denied:
        return denied
    return jsonify(db.list_maintenance_usage(status=request.args.get('status') or None))


@app.route('/api/maintenance/usage', methods=['POST'])
@login_required
@maintenance_allowed
def api_request_maintenance_usage():
    denied = _maintenance_staff_only()
    if denied:
        return denied
    data = request.get_json(force=True) or {}
    try:
        usage = db.request_maintenance_usage(session['user_id'], data.get('inventory_item_id'), data.get('quantity'),
                                             data.get('area'), room_number=data.get('room_number'),
                                             incident_id=data.get('incident_id'), notes=data.get('notes'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(usage), 201


@app.route('/api/maintenance/usage/<int:usage_id>/decide', methods=['POST'])
@admin_required
def api_decide_maintenance_usage(usage_id):
    """Only an administrator lets material leave the maintenance storeroom."""
    data = request.get_json(force=True) or {}
    if data.get('approve') is None:
        return jsonify({'error': 'approve (true/false) es obligatorio'}), 400
    try:
        return jsonify(db.decide_maintenance_usage(usage_id, session['user_id'], bool(data['approve']),
                                                   note=data.get('note')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/maintenance/alerts', methods=['GET'])
@login_required
@maintenance_allowed
def api_maintenance_alerts():
    """Badge counts for everyone who works maintenance; the urgent-breakdown
    pop-up is for front desk and admins."""
    denied = _maintenance_staff_only()
    if denied:
        return denied
    return jsonify(db.maintenance_alerts())


@app.route('/api/maintenance/export', methods=['GET'])
@front_desk_access_required
def api_maintenance_export():
    args = request.args
    incidents = db.list_incidents(status=args.get('status') or None, room_number=args.get('room') or None,
                                  area=args.get('area') or None, start_date=args.get('start') or None,
                                  end_date=args.get('end') or None, limit=100000)
    buf = report_export.build_maintenance_workbook(incidents, db.list_maintenance_items(),
                                                   db.list_maintenance_usage(limit=100000))
    return send_file(buf, as_attachment=True, download_name=f"mantenimiento_{datetime.now():%Y-%m-%d}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ---------------------------------------------------------- Room Blocks ----

@app.route('/api/room-blocks', methods=['GET'])
@front_desk_access_required
def api_list_room_blocks():
    active_only = request.args.get('active') == '1'
    return jsonify(db.list_room_blocks(active_only=active_only))


@app.route('/api/room-blocks', methods=['POST'])
@front_desk_access_required
def api_block_room():
    data = request.get_json(force=True) or {}
    required = ['room_number', 'reason', 'start_date']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f"Faltan campos: {', '.join(missing)}"}), 400
    try:
        block = db.block_room(
            room_number=data['room_number'], reason=data['reason'], start_date=data['start_date'],
            end_date=data.get('end_date') or None, blocked_by_user_id=session['user_id'],
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'room_blocked'})
    return jsonify(block), 201


@app.route('/api/room-blocks/<int:block_id>', methods=['DELETE'])
@front_desk_access_required
def api_unblock_room(block_id):
    try:
        result = db.unblock_room(block_id, unblocked_by_user_id=session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'room_unblocked'})
    return jsonify(result)


# ------------------------------------------------- Public Reservation API --

@app.route('/api/public/room-types', methods=['GET'])
def api_public_room_types():
    return jsonify({
        'rates': db.base_rates(),
        'labels': db.ROOM_TYPE_LABELS,
        'room_counts': {rt: db.room_type_capacity(rt) for rt in db.ROOM_TYPES},
    })


@app.route('/api/public/quote', methods=['GET'])
def api_public_quote():
    """What a guest booking online will pay: Rack rate per night plus tax."""
    try:
        quote = db.quote_stay(request.args.get('room_type'), request.args.get('checkin_date'),
                              request.args.get('checkout_date'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({k: quote[k] for k in ('night_count', 'average_rate', 'subtotal', 'tax_rate', 'tax', 'total')})


@app.route('/api/public/availability', methods=['GET'])
def api_public_availability():
    try:
        year = int(request.args.get('year'))
        month = int(request.args.get('month'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Los parámetros year y month son obligatorios'}), 400
    if not (1 <= month <= 12):
        return jsonify({'error': 'month debe estar entre 1 y 12'}), 400
    return jsonify(db.get_month_availability(year, month))


EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
PUBLIC_MAX_NIGHTS = 60
PUBLIC_MAX_DAYS_AHEAD = 730


def _validate_public_booking(data):
    """Rules for bookings made by guests on the public site. Front desk uses
    db.create_reservation directly and may record past dates (e.g. a stay
    entered late); a guest online must book from today onward and leave a
    way for the hotel to reach them. Returns the cleaned fields."""
    name = ' '.join((data.get('name') or '').split())
    email = (data.get('email') or '').strip()
    phone = (data.get('phone') or '').strip()
    if not name:
        raise ValueError('Por favor ingrese su nombre.')
    if len(name) > 100:
        raise ValueError('El nombre es demasiado largo.')
    if not data.get('room_type'):
        raise ValueError('Seleccione el tipo de habitación.')
    try:
        checkin = datetime.strptime(data.get('checkin_date') or '', '%Y-%m-%d').date()
        checkout = datetime.strptime(data.get('checkout_date') or '', '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('Seleccione fechas de entrada y salida válidas.')
    today = datetime.now().date()
    if checkin < today:
        raise ValueError('La fecha de entrada no puede ser en el pasado.')
    if checkout <= checkin:
        raise ValueError('La salida debe ser después de la entrada.')
    if (checkout - checkin).days > PUBLIC_MAX_NIGHTS:
        raise ValueError(f'Las reservaciones en línea son de máximo {PUBLIC_MAX_NIGHTS} noches; para estadías más largas contáctenos.')
    if (checkin - today).days > PUBLIC_MAX_DAYS_AHEAD:
        raise ValueError('Solo se aceptan reservaciones hasta dos años por adelantado.')
    if email and not EMAIL_RE.match(email):
        raise ValueError('El correo electrónico no es válido (ejemplo: usted@ejemplo.com).')
    if phone and len(re.sub(r'\D', '', phone)) < 7:
        raise ValueError('El teléfono debe tener al menos 7 dígitos.')
    if not email and not phone:
        raise ValueError('Ingrese un correo electrónico o un teléfono para poder confirmar su reservación.')
    return {'name': name, 'room_type': data['room_type'], 'email': email or None, 'phone': phone or None,
            'checkin_date': checkin.isoformat(), 'checkout_date': checkout.isoformat()}


@app.route('/api/public/book', methods=['POST'])
def api_public_book():
    data = request.get_json(force=True) or {}
    # Validate before counting against the per-device limit, so a guest who
    # mistypes their email a few times isn't locked out for an hour.
    try:
        clean = _validate_public_booking(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    blocked = security.check_public_booking_allowed()
    if blocked:
        return blocked
    try:
        guest = db.create_reservation(**clean)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('availability_changed', {'reason': 'web_booking'})
    return jsonify(guest), 201


@app.route('/api/events/availability')
def sse_availability():
    """Both the public booking site and the front-desk dashboard subscribe
    here. Any occupancy-changing action anywhere pushes a message to every
    connected client within seconds — the two-way real-time sync."""
    q = realtime.subscribe()
    return Response(realtime.stream(q), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })


# --------------------------------------------------------------- Orders ----

@app.route('/api/orders', methods=['GET'])
@login_required
def api_list_orders():
    # Servers don't see cancelled orders (removed from their view entirely);
    # front desk and admin still see the full history.
    include_cancelled = session.get('role') != 'server'
    return jsonify(db.list_orders(include_cancelled=include_cancelled))


@app.route('/api/restaurant/tables', methods=['GET'])
@login_required
def api_restaurant_tables():
    return jsonify({
        'areas': db.RESTAURANT_AREAS,
        'area_labels': db.RESTAURANT_AREA_LABELS,
        'tables': db.get_restaurant_table_status(),
    })


@app.route('/api/restaurant/tables/<table_number>/position', methods=['PUT'])
@front_desk_access_required
def api_set_table_position(table_number):
    data = request.get_json(force=True) or {}
    if data.get('pos_x') is None or data.get('pos_y') is None:
        return jsonify({'error': 'pos_x y pos_y son obligatorios'}), 400
    try:
        result = db.set_table_position(table_number, data['pos_x'], data['pos_y'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


@app.route('/api/orders/<int:order_id>', methods=['GET'])
@login_required
def api_get_order(order_id):
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': 'Orden no encontrada'}), 404
    return jsonify(order)


@app.route('/api/orders', methods=['POST'])
@login_required
def api_start_order():
    """Opens a new ticket. No payment method yet — that's chosen at checkout."""
    data = request.get_json(force=True) or {}
    items = data.get('items') or []
    guest_type = data.get('guest_type')
    guest_id = data.get('guest_id')
    table_number = (data.get('table_number') or '').strip() or None
    sub_account = data.get('sub_account')

    try:
        order = db.start_order(
            server_id=session['user_id'], items=items, guest_type=guest_type,
            guest_id=guest_id, table_number=table_number, sub_account=sub_account,
            account_name=data.get('account_name'), guest_count=data.get('guest_count'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    printing.notify()
    realtime.broadcast('order_changed', {'reason': 'order_opened'})
    return jsonify(order), 201


@app.route('/api/owners/names', methods=['GET'])
@login_required
def api_owner_names():
    """Owners' account names used before, offered as suggestions."""
    return jsonify(db.list_owner_account_names())


# Master account: the day's included breakfasts, each line tied to a room.
@app.route('/api/breakfast/rooms', methods=['GET'])
@login_required
def api_breakfast_rooms():
    return jsonify(db.checked_in_rooms())


@app.route('/api/breakfast', methods=['POST'])
@login_required
def api_add_breakfast():
    data = request.get_json(force=True) or {}
    try:
        result = db.add_breakfast(session['user_id'], data.get('room_number'), data.get('items') or [])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    printing.notify()
    return jsonify(result), 201


@app.route('/api/orders/<int:order_id>/items', methods=['POST'])
@login_required
def api_add_order_items(order_id):
    """'The client wants to order more' — append items to a still-open ticket."""
    data = request.get_json(force=True) or {}
    items = data.get('items') or []
    try:
        order = db.add_items_to_order(order_id, items)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(order), 201


@app.route('/api/orders/<int:order_id>/items/<int:item_id>/notes', methods=['PUT'])
@login_required
def api_set_order_item_notes(order_id, item_id):
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.set_order_item_notes(order_id, item_id, data.get('notes')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/menu/options', methods=['GET'])
@login_required
def api_menu_options():
    """Sections, sides and doneness choices for the menu screens."""
    return jsonify({'sections': db.list_menu_sections(), 'sides': db.SIDE_OPTIONS, 'doneness': db.DONENESS_OPTIONS,
                    'egg_doneness': db.EGG_OPTIONS})


@app.route('/api/menu/sections', methods=['POST'])
@admin_required
def api_create_menu_section():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.create_menu_section(data.get('label'), data.get('icon'), data.get('station') or 'kitchen',
                                              session['user_id'])), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/menu/sections/<key>', methods=['PUT'])
@admin_required
def api_update_menu_section(key):
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.update_menu_section(key, data.get('label'), data.get('icon'), data.get('station') or 'kitchen',
                                              session['user_id']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/menu/sections/<key>/move', methods=['POST'])
@admin_required
def api_move_menu_section(key):
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.move_menu_section(key, int(data.get('direction') or 0)))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/menu/sections/<key>', methods=['DELETE'])
@admin_required
def api_delete_menu_section(key):
    data = request.get_json(silent=True) or {}
    try:
        db.delete_menu_section(key, data.get('move_to'), session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True})


@app.route('/api/orders/<int:order_id>/items/<int:item_id>/included', methods=['PUT'])
@login_required
def api_set_order_item_included(order_id, item_id):
    """Toggle a hotel guest's line between included breakfast (C$0) and charged."""
    data = request.get_json(force=True) or {}
    try:
        order = db.set_order_item_included(order_id, item_id, bool(data.get('included')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(order)


@app.route('/api/orders/<int:order_id>/send-to-stations', methods=['POST'])
@login_required
def api_send_order_to_stations(order_id):
    """✓ Listo while editing an open order: print the added items now."""
    try:
        result = db.send_order_to_stations(order_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    printing.notify()
    return jsonify(result)


# ------------------------------------------------------ Customer receipt ----

@app.route('/print/receipt/<int:order_id>')
def print_receipt(order_id):
    """The printed bill (80 mm): FACTURA when paid (?copy=1 = COPIA), PRE-CUENTA while open."""
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    if session.get('role') == 'maintenance':
        return 'Sin acceso', 403
    data = db.receipt_view(order_id, session.get('full_name'), copy=request.args.get('copy') == '1')
    if not data:
        return 'Cuenta no encontrada', 404
    return render_template('receipt.html', **data)


@app.route('/api/orders/<int:order_id>/print-receipt', methods=['POST'])
@login_required
def api_print_receipt(order_id):
    """Sends the bill / pre-cuenta to the reception printer. {'queued': false}
    when there is none, and the screen opens the browser print page instead."""
    data = db.receipt_view(order_id, session.get('full_name'), copy=bool((request.get_json(silent=True) or {}).get('copy')))
    if not data:
        return jsonify({'error': 'Cuenta no encontrada'}), 404
    queued = db.queue_document_print(db.receipt_print_lines(data), order_id=order_id, reference=data['r']['number'])
    if queued:
        printing.notify()
    return jsonify({'queued': queued})


def _consumption_args(guest_id):
    return db.guest_consumption(guest_id, request.args.get('from') or None, request.args.get('to') or None,
                                request.args.get('meal') or None)


@app.route('/api/guests/<int:guest_id>/consumption', methods=['GET'])
@front_desk_access_required
def api_guest_consumption(guest_id):
    """A guest's running consumption (pre-cuenta) by day and meal, for any day or range of the stay."""
    try:
        return jsonify(_consumption_args(guest_id))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/print/guest-consumption/<int:guest_id>')
def print_guest_consumption(guest_id):
    if 'user_id' not in session or session.get('role') not in ('admin', 'front_desk'):
        return redirect(url_for('login_page'))
    try:
        data = _consumption_args(guest_id)
    except ValueError as e:
        return str(e), 400
    fmt = lambda d: datetime.strptime(d, '%Y-%m-%d').strftime('%d/%m/%Y')
    return render_template('guest_consumption.html', c=data, s=db.get_receipt_settings(), fmt=fmt,
                           printed=datetime.now().strftime('%d/%m/%Y %H:%M'))


@app.route('/api/guests/<int:guest_id>/consumption/print', methods=['POST'])
@front_desk_access_required
def api_print_guest_consumption(guest_id):
    try:
        data = _consumption_args(guest_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    lines = db.consumption_print_lines(data, db.get_receipt_settings()['receipt_business_name'])
    queued = db.queue_document_print(lines, reference=f"Hab. {data['guest']['room_number']}")
    if queued:
        printing.notify()
    return jsonify({'queued': queued})


@app.route('/api/receipt/settings', methods=['GET'])
@admin_required
def api_receipt_settings():
    return jsonify(db.get_receipt_settings())


@app.route('/api/receipt/settings', methods=['PUT'])
@admin_required
def api_save_receipt_settings():
    try:
        return jsonify(db.save_receipt_settings(request.get_json(force=True) or {}, session['user_id']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ------------------------------------------------- Kitchen/Bar Printing ----

@app.route('/api/printing/settings', methods=['GET'])
@admin_required
def api_printer_settings():
    return jsonify(db.get_printer_settings())


@app.route('/api/printing/settings', methods=['PUT'])
@admin_required
def api_save_printer_settings():
    data = request.get_json(force=True) or {}
    try:
        settings = db.save_printer_settings(
            enabled=bool(data.get('enabled')), kitchen_host=data.get('kitchen_host'),
            bar_host=data.get('bar_host'), port=data.get('port', 9100), reception_host=data.get('reception_host'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(settings)


@app.route('/api/printing/test/<station>', methods=['POST'])
@admin_required
def api_printer_test(station):
    if station not in db.STATION_LABELS_ES:
        return jsonify({'error': 'Estación inválida'}), 400
    try:
        via = printing.print_test(station)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'via': via})


@app.route('/api/printing/jobs', methods=['GET'])
@admin_required
def api_print_jobs():
    return jsonify(db.list_print_jobs())


@app.route('/api/printing/status', methods=['GET'])
@login_required
def api_print_status():
    return jsonify(printing.status())


# ------------------------------------------------------------ Print agent ---
# The hotel's print agent (print_agent.py) when the app runs in the cloud.
# No login session: it authenticates with the shared PRINT_AGENT_TOKEN.

def print_agent_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        sent = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        if not printing.AGENT_MODE or not hmac.compare_digest(sent, printing.AGENT_TOKEN):
            return jsonify({'error': 'No autorizado'}), 401
        return view(*args, **kwargs)
    return wrapped


@app.route('/api/print-agent/poll', methods=['GET'])
@print_agent_required
def api_print_agent_poll():
    return jsonify(printing.agent_poll())


@app.route('/api/print-agent/jobs/<int:job_id>', methods=['POST'])
@print_agent_required
def api_print_agent_report(job_id):
    data = request.get_json(silent=True) or {}
    printing.agent_report(job_id, bool(data.get('ok')), (data.get('error') or '')[:300] or None)
    return jsonify({'ok': True})


@app.route('/api/printing/jobs/<int:job_id>/reprint', methods=['POST'])
@login_required
def api_reprint_job(job_id):
    try:
        db.requeue_print_job(job_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    printing.notify()
    return jsonify({'ok': True})


@app.route('/api/printing/retry-failed', methods=['POST'])
@login_required
def api_retry_failed_prints():
    count = db.requeue_failed_print_jobs()
    printing.notify()
    return jsonify({'requeued': count})


@app.route('/api/orders/<int:order_id>/account-name', methods=['PUT'])
@login_required
def api_set_order_account_name(order_id):
    data = request.get_json(force=True) or {}
    try:
        result = db.set_order_account_name(order_id, data.get('account_name'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


@app.route('/api/orders/<int:order_id>/items/<int:item_id>', methods=['DELETE'])
@login_required
def api_delete_order_item(order_id, item_id):
    """'What if the client wanted less?' — remove one line from an open ticket."""
    try:
        order = db.delete_order_item(order_id, item_id, deleted_by_user_id=session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(order)


@app.route('/api/orders/<int:order_id>/items/<int:item_id>', methods=['PUT'])
@login_required
def api_update_order_item(order_id, item_id):
    """'Actually make that two' — adjust one line's quantity in place."""
    data = request.get_json(force=True) or {}
    if data.get('quantity') is None:
        return jsonify({'error': 'quantity es obligatorio'}), 400
    try:
        quantity = int(data['quantity'])
    except (TypeError, ValueError):
        return jsonify({'error': 'quantity debe ser un número entero'}), 400
    try:
        order = db.update_order_item_quantity(order_id, item_id, quantity, changed_by_user_id=session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(order)


@app.route('/api/orders/<int:order_id>/checkout', methods=['POST'])
@front_desk_access_required   # only the cashier charges; waiters print the pre-cuenta
def api_checkout_order(order_id):
    """The guest is ready to pay — pick cash/card/room charge and close the tab."""
    data = request.get_json(force=True) or {}
    payment_method = data.get('payment_method')
    try:
        order = db.close_order(order_id, payment_method=payment_method, closed_by=session['user_id'],
                               amount_tendered=data.get('amount_tendered'), payments=data.get('payments'),
                               reference=data.get('reference'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    printing.notify()
    realtime.broadcast('order_changed', {'reason': 'room_charge_closed' if order['payment_method'] == 'room_charge'
                                         else 'order_closed'})
    return jsonify(order)


@app.route('/api/orders/<int:order_id>/move', methods=['POST'])
@login_required
def api_move_order(order_id):
    """Move one open account to another table, or merge it into another open account."""
    data = request.get_json(force=True) or {}
    try:
        if data.get('target_order_id'):
            order = db.merge_orders(order_id, int(data['target_order_id']), merged_by_user_id=session['user_id'])
        else:
            order = db.move_order_to_table(order_id, data.get('target_table'), moved_by_user_id=session['user_id'])
    except (TypeError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('order_changed', {'reason': 'order_moved'})
    return jsonify(order)


@app.route('/api/restaurant/tables/<table_number>/move', methods=['POST'])
@login_required
def api_move_table(table_number):
    data = request.get_json(force=True) or {}
    try:
        result = db.move_table(table_number, (data.get('target_table') or '').strip(),
                               moved_by_user_id=session['user_id'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('order_changed', {'reason': 'table_moved'})
    return jsonify(result)


@app.route('/api/orders/transfer-server', methods=['POST'])
@login_required
def api_transfer_server():
    """Hand open accounts to another server: by order_ids, a whole table, or
    every open account of from_server_id (shift change)."""
    data = request.get_json(force=True) or {}
    if not data.get('server_id'):
        return jsonify({'error': 'Seleccione el mesero que recibe'}), 400
    try:
        result = db.transfer_orders_to_server(
            int(data['server_id']), changed_by_user_id=session['user_id'],
            order_ids=data.get('order_ids'), table_number=data.get('table_number'),
            from_server_id=data.get('from_server_id'),
        )
    except (TypeError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('order_changed', {'reason': 'server_transferred'})
    return jsonify(result)


@app.route('/api/staff/servers', methods=['GET'])
@login_required
def api_transfer_staff():
    return jsonify(db.list_staff_for_transfer())


def _item_sales_args():
    today = datetime.now().strftime('%Y-%m-%d')
    return dict(
        start_date=request.args.get('start') or today,
        end_date=request.args.get('end') or today,
        include_open=request.args.get('include_open') == 'true',
        server_id=request.args.get('server_id', type=int),
    )


@app.route('/api/sales/items', methods=['GET'])
@front_desk_access_required
def api_item_sales():
    return jsonify(db.get_item_sales(**_item_sales_args()))


def _courtesy_report():
    today = datetime.now().strftime('%Y-%m-%d')
    return db.courtesy_report(request.args.get('kind', 'breakfast'),
                              request.args.get('start') or today, request.args.get('end') or today)


@app.route('/api/courtesies', methods=['GET'])
@front_desk_access_required
def api_courtesies():
    """Summary of a master account (breakfasts or owners) over a date range."""
    try:
        return jsonify(_courtesy_report())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/courtesies/export', methods=['GET'])
@front_desk_access_required
def api_courtesies_export():
    try:
        data = _courtesy_report()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    prefix = 'desayunos_incluidos' if data['kind'] == 'breakfast' else 'propietarios'
    return send_file(report_export.build_courtesy_workbook(data), as_attachment=True,
                     download_name=f"{prefix}_{data['start_date']}_{data['end_date']}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/sales/items/export', methods=['GET'])
@front_desk_access_required
def api_item_sales_export():
    data = db.get_item_sales(**_item_sales_args())
    buf = report_export.build_item_sales_workbook(data)
    name = f"ventas_articulos_{data['start_date']}_{data['end_date']}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/sales/daily/export', methods=['GET'])
@front_desk_access_required
def api_daily_sales_export():
    """'Artículos vendidos el <día>': one day's units per item, Matutino / Vespertino / Total."""
    try:
        data = db.get_daily_item_sales(request.args.get('date'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return send_file(report_export.build_daily_sales_workbook(data), as_attachment=True,
                     download_name=f"Ventas_{data['file_label']}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/orders/<int:order_id>/request-cancel', methods=['POST'])
@login_required
def api_request_cancel_order(order_id):
    data = request.get_json(silent=True) or {}
    try:
        order = db.request_order_cancellation(order_id, requested_by_user_id=session['user_id'],
                                               reason=data.get('reason'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('order_changed', {'reason': 'cancel_requested'})
    return jsonify(order)


@app.route('/api/orders/pending-cancellations', methods=['GET'])
@front_desk_access_required
def api_pending_cancellations():
    return jsonify(db.list_pending_cancellations())


@app.route('/api/orders/<int:order_id>/decide-cancel', methods=['POST'])
@front_desk_access_required
def api_decide_cancel_order(order_id):
    data = request.get_json(force=True) or {}
    if data.get('approve') is None:
        return jsonify({'error': 'approve (true/false) es obligatorio'}), 400
    try:
        order = db.decide_order_cancellation(
            order_id, approver_user_id=session['user_id'],
            approve=bool(data['approve']), note=data.get('note'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    realtime.broadcast('order_changed', {'reason': 'cancel_decided'})
    return jsonify(order)


# ----------------------------------------------------------- Analytics ----

@app.route('/api/analytics/kpis', methods=['GET'])
@admin_required
def api_dashboard_kpis():
    return jsonify(db.get_dashboard_kpis())


@app.route('/api/analytics/revenue-trend', methods=['GET'])
@admin_required
def api_revenue_trend():
    days = int(request.args.get('days', 8))
    return jsonify(db.get_revenue_trend(days))


@app.route('/api/analytics/servers', methods=['GET'])
@admin_required
def api_server_analytics():
    return jsonify(db.server_performance())


@app.route('/api/analytics/summary', methods=['GET'])
@admin_required
def api_revenue_summary():
    return jsonify(db.revenue_summary())


@app.route('/api/rooms/rates', methods=['GET'])
@login_required
def api_room_rates():
    return jsonify({
        'rates': db.base_rates(),
        'labels': db.ROOM_TYPE_LABELS,
    })


@app.route('/api/config', methods=['GET'])
@login_required
def api_config():
    return jsonify({'walk_in_credit': db.WALK_IN_CREDIT})


# --------------------------------------------------------- Periodic reports

@app.route('/api/reports/config', methods=['GET'])
@admin_required
def api_reports_config():
    return jsonify({
        'schedule_days': scheduler.REPORT_SCHEDULE_DAYS,
        'next_run_at': scheduler.next_run_time('periodic_checkout_report'),
        'inventory_schedule_days': scheduler.INVENTORY_REPORT_SCHEDULE_DAYS,
        'inventory_next_run_at': scheduler.next_run_time('periodic_inventory_report'),
        'cloud_folder_configured': delivery.cloud_folder_is_configured(),
        'cloud_folder_path': delivery.cloud_folder_path(),
        'email_configured': delivery.email_is_configured(),
    })


@app.route('/api/reports', methods=['GET'])
@admin_required
def api_reports_list():
    report_type = request.args.get('type')
    return jsonify(db.list_report_runs(report_type=report_type))


@app.route('/api/reports/generate', methods=['POST'])
@admin_required
def api_reports_generate():
    data = request.get_json(silent=True) or {}
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    try:
        run = scheduler.run_periodic_report(
            triggered_by=f"manual ({session.get('username')})",
            start_date=start_date, end_date=end_date,
        )
    except Exception:
        app.logger.exception('Report generation failed')
        return jsonify({'error': 'Error al generar el reporte. Revise el registro del servidor.'}), 500
    return jsonify(run), 201


@app.route('/api/reports/inventory/generate', methods=['POST'])
@admin_required
def api_inventory_report_generate():
    data = request.get_json(silent=True) or {}
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    try:
        run = scheduler.run_inventory_report(
            triggered_by=f"manual ({session.get('username')})",
            start_date=start_date, end_date=end_date,
        )
    except Exception:
        app.logger.exception('Report generation failed')
        return jsonify({'error': 'Error al generar el reporte. Revise el registro del servidor.'}), 500
    return jsonify(run), 201


@app.route('/api/reports/<int:run_id>/download', methods=['GET'])
@admin_required
def api_reports_download(run_id):
    run = db.get_report_run(run_id)
    if not run or not os.path.exists(run['file_path']):
        return jsonify({'error': 'Archivo de reporte no encontrado'}), 404
    return send_file(run['file_path'], as_attachment=True, download_name=run['file_name'])


# --------------------------------------------------------------- Backups ---

@app.route('/api/backups/config', methods=['GET'])
@admin_required
def api_backups_config():
    return jsonify({
        'incremental_days': scheduler.INCREMENTAL_BACKUP_DAYS,
        'incremental_next_run_at': scheduler.next_run_time('incremental_backup'),
        'full_days': scheduler.FULL_BACKUP_DAYS,
        'full_next_run_at': scheduler.next_run_time('full_backup'),
        'cloud_folder_configured': delivery.cloud_folder_is_configured(),
        'cloud_folder_path': delivery.cloud_folder_path(),
    })


@app.route('/api/backups', methods=['GET'])
@admin_required
def api_list_backups():
    kind = request.args.get('kind')
    return jsonify(db.list_backup_runs(kind=kind))


@app.route('/api/backups/run', methods=['POST'])
@admin_required
def api_run_backup():
    data = request.get_json(silent=True) or {}
    kind = data.get('kind', 'incremental')
    if kind not in ('incremental', 'full'):
        return jsonify({'error': "kind must be 'incremental' or 'full'"}), 400
    try:
        run_fn = scheduler.run_incremental_backup if kind == 'incremental' else scheduler.run_full_backup
        result = run_fn(triggered_by=f"manual ({session.get('username')})")
    except Exception:
        app.logger.exception('Manual backup failed')
        return jsonify({'error': 'Error al crear el respaldo. Revise el registro del servidor.'}), 500
    return jsonify(result), 201


# Restoring is two steps: preview (upload a backup file or pick a saved one; it is
# checked and summarized, nothing changes yet) then restore (confirmed by
# typing RESTAURAR). Both work on a private copy in backups/uploads, so a
# saved backup can't be pruned out from under a restore in progress.
RESTORE_MAX_BYTES = 500 * 1024 * 1024
RESTORE_TOKEN_RE = re.compile(r'^[0-9a-f]{32}$')


def _restore_path(token):
    return os.path.join(backup.UPLOADS_DIR, f'{token}{backup.BACKUP_EXTENSION}')


@app.route('/api/backups/restore/preview', methods=['POST'])
@admin_required
def api_restore_preview():
    os.makedirs(backup.UPLOADS_DIR, exist_ok=True)
    token = uuid.uuid4().hex
    path = _restore_path(token)
    upload = request.files.get('file')
    if upload:
        if (request.content_length or 0) > RESTORE_MAX_BYTES:
            return jsonify({'error': 'El archivo es muy grande (máximo 500 MB)'}), 400
        upload.save(path)
        source_name = upload.filename
    else:
        run = db.get_backup_run((request.get_json(silent=True) or {}).get('backup_id'))
        if not run or not os.path.exists(run['file_path']):
            return jsonify({'error': 'Seleccione un archivo o un respaldo de la lista'}), 400
        shutil.copyfile(run['file_path'], path)
        source_name = run['file_name']
    try:
        summary = backup.inspect_backup(path)
    except ValueError as e:
        os.remove(path)
        return jsonify({'error': str(e)}), 400
    return jsonify({'token': token, 'source_name': source_name, 'summary': summary})


@app.route('/api/backups/restore', methods=['POST'])
@admin_required
def api_restore():
    data = request.get_json(silent=True) or {}
    token = data.get('token') or ''
    if (data.get('confirm') or '').strip().upper() != 'RESTAURAR':
        return jsonify({'error': 'Escriba RESTAURAR para confirmar'}), 400
    if not RESTORE_TOKEN_RE.match(token) or not os.path.exists(_restore_path(token)):
        return jsonify({'error': 'La vista previa expiró; vuelva a seleccionar el archivo'}), 400
    path = _restore_path(token)
    try:
        result = backup.restore_from_file(path, restored_by_name=session.get('full_name') or session.get('username'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        app.logger.exception('Restore failed')
        return jsonify({'error': 'No se pudo restaurar. Revise el registro del servidor.'}), 500
    finally:
        if os.path.exists(path):
            os.remove(path)
    return jsonify(result)


@app.route('/api/backups/<int:backup_id>/download', methods=['GET'])
@admin_required
def api_download_backup(backup_id):
    run = db.get_backup_run(backup_id)
    if not run or not os.path.exists(run['file_path']):
        return jsonify({'error': 'Archivo de respaldo no encontrado'}), 404
    return send_file(run['file_path'], as_attachment=True, download_name=run['file_name'])


if __name__ == '__main__':
    # Off unless explicitly requested: the debugger lets anyone who can reach
    # the page run code on this PC, and the app listens on the whole network.
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    weak = db.users_with_factory_passwords()
    if weak:
        print(f"\n*** AVISO: estas cuentas usan la contraseña de fábrica: {', '.join(weak)}\n"
              f"*** Cámbielas con:  py set_password.py <usuario>\n")
    if not debug_mode or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        scheduler.init_scheduler()
        printing.start_worker()
    # PORT is set by Railway; on the hotel PC it defaults to 8000.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), debug=debug_mode, threaded=True)
