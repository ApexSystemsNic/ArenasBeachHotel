"""PostgreSQL schema, setup, seed data and helpers for the Arenas Beach Hotel
executive Restaurant POS + Front Desk PMS + Public Reservations + Inventory
dashboard (V13).
"""
import atexit
import functools
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta

import psycopg
from dotenv import load_dotenv
from psycopg.adapt import Dumper, Loader
from psycopg_pool import ConnectionPool
from werkzeug.security import generate_password_hash

# Settings (DATABASE_URL, SECRET_KEY, SMTP...) from a .env next to the app,
# for running on the hotel PC; real environment variables (Railway) win.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

MENU_CATEGORIES = ('Drinks', 'Food', 'Desserts')
# How the menu is organized for the waiter. The section decides the older
# category (bar vs. kitchen ticket, bar vs. restaurant stock): Bebidas are
# Drinks, Postres are Desserts, everything else is Food.
MENU_SECTIONS = ('entradas', 'platos_fuertes', 'pizzas', 'postres', 'bebidas', 'desayunos')
MENU_SECTION_LABELS_ES = {'entradas': 'Entradas', 'platos_fuertes': 'Platos fuertes', 'pizzas': 'Pizzas',
                          'postres': 'Postres', 'bebidas': 'Bebidas', 'desayunos': 'Desayunos'}
SECTION_CATEGORY = {'bebidas': 'Drinks', 'postres': 'Desserts'}
CATEGORY_DEFAULT_SECTION = {'Drinks': 'bebidas', 'Desserts': 'postres', 'Food': 'platos_fuertes'}
# A main course comes with two sides (they may repeat) or none.
SIDE_OPTIONS = ('Papas fritas', 'Puré de papas', 'Vegetales', 'Arroz', 'Tostones')
# How the meat is cooked, for dishes that ask for it.
DONENESS_OPTIONS = ('Azul', 'Rojo', 'Medio rojo', 'Término medio', 'Tres cuartos (3/4)', 'Bien cocido')
# asks_doneness: 0 = nothing to ask, 1 = meat doneness, 2 = how the eggs are done (breakfasts).
EGG_OPTIONS = ('Cocido', 'Entero duro', 'Entero medio', 'Entero suave', 'Revuelto con todo', 'Revuelto sin nada',
               'Hervidos (duros)', 'Poché (pasado por agua)')


def _doneness_kind(value):
    if value in (2, '2', 'eggs', 'huevos'):
        return 2
    return 1 if value else 0
MAX_LINE_NOTE = 150
ROLES = ('admin', 'server', 'front_desk', 'maintenance')
STAFF_CREATABLE_ROLES = ('server', 'front_desk', 'maintenance', 'admin')
ROLE_LABELS = {
    'admin': 'Administrador',
    'front_desk': 'Recepción',
    'server': 'Mesero',
    'maintenance': 'Mantenimiento',
}

ROOM_TYPES = ('comfort', 'bungalow_double', 'bungalow')
ROOM_TYPE_LABELS = {
    'comfort': 'Confort',
    'bungalow_double': 'Bungaló Unipersonal',
    'bungalow': 'Bungaló Queen',
}
# Nightly rates in Nicaraguan Cordobas (C$), converted from the prior USD
# rates at C$36.62/US$1.
NIGHTLY_RATES = {
    'comfort': 6005.68,
    'bungalow_double': 5033.05,
    'bungalow': 5033.05,
}
# The real room numbers at the property. This is the authoritative roster —
# capacity everywhere else is derived by counting these, not a fixed number.
ROOM_NUMBERS_BY_TYPE = {
    'bungalow': ['1', '4', '5', '8', '9', '12', '14', '17', '18', '21'],
    'bungalow_double': ['2', '3', '6', '7', '10', '11', '15', '16', '19', '20'],
    'comfort': [str(n) for n in range(24, 36)],
}
# Only used if the roster is ever completely empty and can't be reseeded.
ROOM_INVENTORY_COUNTS_FALLBACK = {
    'comfort': len(ROOM_NUMBERS_BY_TYPE['comfort']),
    'bungalow_double': len(ROOM_NUMBERS_BY_TYPE['bungalow_double']),
    'bungalow': len(ROOM_NUMBERS_BY_TYPE['bungalow']),
}

# The restaurant's real physical layout: 3 small ranchos with 9 tables each
# (numbered continuously, not restarting per rancho) plus 9 standalone
# gazebos (one table each). Used to populate the table-map views and the
# walk-in order form's table select — not a mutable DB table since these
# never change, just a fixed roster like the room numbers above.
RESTAURANT_AREAS = ['rancho_1', 'rancho_2', 'rancho_3', 'gazebos', 'sillas_playa', 'bar']
RESTAURANT_AREA_LABELS = {
    'rancho_1': 'Rancho 1',
    'rancho_2': 'Rancho 2',
    'rancho_3': 'Rancho 3',
    'gazebos': 'Gazebos',
    'sillas_playa': 'Sillas de Playa',
    'bar': 'Bar',
}
RESTAURANT_TABLES_BY_AREA = {
    'rancho_1': [str(n) for n in range(1, 10)],
    'rancho_2': [str(n) for n in range(10, 19)],
    'rancho_3': [str(n) for n in range(19, 28)],
    'gazebos': [f'Gazebo {n}' for n in range(1, 10)],
    # 27 beach loungers on the sand, served like tables: S1..S27.
    'sillas_playa': [f'S{n}' for n in range(1, 28)],
    # The boat bar: 10 stools at the counter (B1-B10, five per side), 3 seats
    # on the sand in front of it (F1-F3), 1 on the stage (E1) and 3 more
    # tables on the sand (A1-A3). New ones go at the end so existing
    # positions/defaults keep their index.
    'bar': [f'B{n}' for n in range(1, 11)] + ['F1', 'F2', 'F3', 'E1', 'A1', 'A2', 'A3'],
}

# A table can host up to this many separate, independently-closing tabs at
# once (e.g. a gazebo group that wants separate checks) instead of one shared
# order per table.
MAX_SUB_ACCOUNTS_PER_TABLE = 10

# hotel_guest = in-house guest; walk_in = Day Pass (consumable credit);
# visitor = outside customer who only eats/drinks, pays the plain bill.
GUEST_TYPES = ('hotel_guest', 'walk_in', 'visitor', 'owners')   # 'breakfast' is opened only by the system
# Master accounts: courtesies settled as 'house' (Cuenta de la Casa) — valued
# at menu price and depleting inventory, but never counted as revenue.
MASTER_ACCOUNT_TYPES = ('breakfast', 'owners')
PAYMENT_METHODS = ('cash', 'card', 'room_charge', 'house')
WALK_IN_CREDIT = 370.00  # rounded so day-pass cash payments need no small change

HOUSEKEEPING_STATUSES = ('clean', 'dirty', 'inspected', 'out_of_order')
HOUSEKEEPING_STATUS_LABELS_ES = {
    'clean': 'Limpia',
    'dirty': 'Sucia',
    'inspected': 'Inspeccionada',
    'out_of_order': 'Fuera de Servicio',
}
HOUSEKEEPING_BLOCK_REASON = 'Fuera de servicio (Housekeeping)'

GUEST_STATUSES = ('reserved', 'checked_in', 'checked_out', 'cancelled')
GUEST_SOURCES = ('web_booking', 'walk_in')

REPORT_TYPES = ('checkout_payment', 'inventory')
ORDER_STATUS_LABELS_ES = {'open': 'abierta', 'closed': 'cerrada', 'cancelled': 'cancelada'}


# ------------------------------------------------------------ Connection ----
# PostgreSQL, reached through DATABASE_URL (Railway sets it; locally it comes
# from .env). Queries throughout this module are written sqlite-style — '?'
# placeholders, cursor.lastrowid, row['col'] / row[0] — and the thin wrapper
# below translates them, so the SQL itself stays plain and portable.

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres@localhost:5432/arenas')
# Stored timestamps are local hotel time ('YYYY-MM-DD HH:MM:SS' text, like
# the Python side writes), so the session runs in the hotel's time zone.
HOTEL_TIMEZONE = os.environ.get('HOTEL_TIMEZONE', 'America/Managua')
# Python's clock (datetime.now(), used throughout) must read hotel time too:
# a cloud container runs in UTC. tzset() only exists on Linux/macOS — on the
# hotel's Windows PC the system clock is already local time.
if hasattr(time, 'tzset'):
    os.environ['TZ'] = HOTEL_TIMEZONE
    time.tzset()
DB_POOL_SIZE = int(os.environ.get('DB_POOL_SIZE', '10'))

# Any single writer at a time, like sqlite's BEGIN IMMEDIATE: write
# functions check something (room free? table free?) and then write, so two
# requests must not interleave between the check and the write.
_WRITE_LOCK_KEY = 7_202_601


class _UntypedIntDumper(Dumper):
    """Sends ints as 'unknown' so Postgres types them from context — a room
    or table number may arrive as int and be compared with a TEXT column,
    which sqlite accepted and Postgres would otherwise reject."""
    oid = 0

    def dump(self, obj):
        return str(int(obj)).encode()


class _UntypedBoolDumper(Dumper):
    """Flags are INTEGER 0/1 columns, as they were under sqlite."""
    oid = 0

    def dump(self, obj):
        return b'1' if obj else b'0'


class _IsoDateDumper(Dumper):
    """date/datetime params become the same 'YYYY-MM-DD[ HH:MM:SS]' text the
    columns hold (sqlite's adapter did this), not typed date values that
    Postgres would refuse to compare with TEXT."""
    oid = 0

    def dump(self, obj):
        return (obj.isoformat(sep=' ') if isinstance(obj, datetime) else obj.isoformat()).encode()


class _FloatNumericLoader(Loader):
    """NUMERIC results (ROUND, AVG...) as float, not Decimal, so JSON and
    arithmetic behave as they did with sqlite."""

    def load(self, data):
        return float(bytes(data).decode())


psycopg.adapters.register_dumper(int, _UntypedIntDumper)
psycopg.adapters.register_dumper(bool, _UntypedBoolDumper)
psycopg.adapters.register_dumper(date, _IsoDateDumper)
psycopg.adapters.register_dumper(datetime, _IsoDateDumper)
psycopg.adapters.register_loader('numeric', _FloatNumericLoader)


class _HotelTimeLoader(Loader):
    """TIMESTAMPTZ columns come back as the same 'YYYY-MM-DD HH:MM:SS' text
    every other timestamp uses. The session runs in the hotel's time zone, so
    Postgres already writes the value in hotel time."""
    def load(self, data):
        return bytes(data).decode()[:19]


psycopg.adapters.register_loader('timestamptz', _HotelTimeLoader)


class Row(dict):
    """A result row usable as row['col'], row[0] and dict(row), like
    sqlite3.Row. On duplicate column names the first one wins, as there."""
    __slots__ = ('_values',)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []

    def make_row(values):
        row = Row()
        for name, value in zip(names, values):
            if name not in row:
                dict.__setitem__(row, name, value)
        row._values = values
        return row
    return make_row


_NO_ID_TABLES = {'app_settings', 'table_layout', 'room_rates', 'menu_sections'}
_INSERT_TABLE = re.compile(r'^\s*INSERT\s+INTO\s+(\w+)', re.IGNORECASE)
_RETURNING = re.compile(r'\bRETURNING\b', re.IGNORECASE)


@functools.lru_cache(maxsize=2048)
def _translate(sql):
    """sqlite-style SQL -> psycopg: '?' placeholders outside string literals
    become %s and literal % is doubled. An INSERT into a table with an id
    gets RETURNING id so cursor.lastrowid works. Returns (sql, wants_id)."""
    out, in_string = [], False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == '%':
            out.append('%%')
        elif ch == '?' and not in_string:
            out.append('%s')
        else:
            out.append(ch)
    query = ''.join(out)
    m = _INSERT_TABLE.match(query)
    wants_id = bool(m) and m.group(1).lower() not in _NO_ID_TABLES and not _RETURNING.search(query)
    if wants_id:
        query = query.rstrip().rstrip(';') + ' RETURNING id'
    return query, wants_id


class _Cursor:
    def __init__(self, raw):
        self._raw = raw
        self.lastrowid = None

    def execute(self, sql, params=()):
        query, wants_id = _translate(sql)
        self._raw.execute(query, tuple(params))
        if wants_id:
            row = self._raw.fetchone()
            self.lastrowid = row[0] if row else None
        return self

    def executemany(self, sql, seq_of_params):
        query, _ = _translate(sql)
        self._raw.executemany(query, [tuple(p) for p in seq_of_params])
        return self

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    def __iter__(self):
        return iter(self._raw)

    @property
    def rowcount(self):
        return self._raw.rowcount

    @property
    def description(self):
        return self._raw.description


class _Connection:
    """A pooled connection with the sqlite3.Connection surface this module
    uses. close() hands it back to the pool (rolling back anything not
    committed, as closing a sqlite connection did)."""

    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return _Cursor(self._raw.cursor())

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq_of_params):
        return self.cursor().executemany(sql, seq_of_params)

    def executescript(self, sql):
        """Several ';'-separated statements, no parameters (schema setup)."""
        self._raw.execute(sql)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        raw, self._raw = self._raw, None
        if raw is None:
            return
        if raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raw.rollback()
        _get_pool().putconn(raw)

    def __del__(self):
        # A function that forgot close() must not leak a pool slot forever.
        try:
            self.close()
        except Exception:
            pass


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    DATABASE_URL, min_size=1, max_size=DB_POOL_SIZE, open=True,
                    kwargs={'options': f'-c timezone={HOTEL_TIMEZONE}'},
                    configure=lambda c: setattr(c, 'row_factory', _row_factory),
                )
                atexit.register(_pool.close)   # close cleanly before Python tears down threads
    return _pool


def get_connection(write=False):
    conn = _Connection(_get_pool().getconn())
    if write:
        # Take the write lock before the function's checks (room free? table
        # account free? transfer still pending?) so two simultaneous requests
        # can't both pass the same check and then both write. Released at
        # commit/rollback.
        conn.execute('SELECT pg_advisory_xact_lock(?)', (_WRITE_LOCK_KEY,))
    return conn


# ---------------------------------------------------------------- Schema ----
# Timestamps and dates stay TEXT ('YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD') in
# local hotel time — the format the whole app compares and slices as
# strings. now_local()/today_local() produce that format in SQL.

SCHEMA_SQL = '''
CREATE OR REPLACE FUNCTION now_local() RETURNS text LANGUAGE sql STABLE
    AS $$ SELECT to_char(LOCALTIMESTAMP, 'YYYY-MM-DD HH24:MI:SS') $$;
CREATE OR REPLACE FUNCTION today_local() RETURNS text LANGUAGE sql STABLE
    AS $$ SELECT to_char(LOCALTIMESTAMP, 'YYYY-MM-DD') $$;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'server', 'front_desk')),
    -- Staff referenced by orders or the activity log can't be hard deleted
    -- without breaking that history, so delete_user() deactivates them.
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('Drinks', 'Food', 'Desserts')),
    price DOUBLE PRECISION NOT NULL,
    portion_size TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS guests (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    room_number TEXT,
    room_type TEXT NOT NULL CHECK(room_type IN ('comfort', 'bungalow_double', 'bungalow')),
    status TEXT NOT NULL DEFAULT 'checked_in' CHECK(status IN ('reserved', 'checked_in', 'checked_out', 'cancelled')),
    checkin_date TEXT NOT NULL,
    checkout_date TEXT NOT NULL,
    nightly_rate DOUBLE PRECISION NOT NULL,
    checked_out_at TEXT,
    email TEXT,
    phone TEXT,
    source TEXT NOT NULL DEFAULT 'walk_in' CHECK(source IN ('web_booking', 'walk_in')),
    observations TEXT,
    id_number TEXT,
    nationality TEXT,
    address TEXT,
    cancelled_at TEXT,
    cancelled_by INTEGER REFERENCES users(id),
    -- Registration-card extras, optional like on the paper card.
    agency TEXT,
    rate_code TEXT,
    adults INTEGER,
    children INTEGER,
    country TEXT,
    city TEXT,
    postal_code TEXT,
    booking_reference TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    receipt_number TEXT NOT NULL UNIQUE,
    server_id INTEGER NOT NULL REFERENCES users(id),
    guest_id INTEGER REFERENCES guests(id),
    -- breakfast / owners are the master accounts, settled as 'house'.
    guest_type TEXT NOT NULL CHECK(guest_type IN ('hotel_guest', 'walk_in', 'visitor', 'breakfast', 'owners')),
    table_number TEXT,
    sub_account INTEGER NOT NULL DEFAULT 1,
    account_name TEXT,
    subtotal DOUBLE PRECISION NOT NULL DEFAULT 0,
    credit_applied DOUBLE PRECISION NOT NULL DEFAULT 0,
    balance_due DOUBLE PRECISION NOT NULL DEFAULT 0,
    payment_method TEXT CHECK(payment_method IN ('cash', 'card', 'room_charge', 'house')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed', 'cancelled')),
    cancel_requested_at TEXT,
    cancel_requested_by INTEGER REFERENCES users(id),
    cancel_reason TEXT,
    cancelled_at TEXT,
    cancelled_by INTEGER REFERENCES users(id),
    cancel_decision_note TEXT,
    timestamp TEXT NOT NULL DEFAULT now_local(),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS rate_adjustments (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    guest_id INTEGER NOT NULL REFERENCES guests(id),
    old_rate DOUBLE PRECISION NOT NULL,
    new_rate DOUBLE PRECISION NOT NULL,
    reason TEXT,
    admin_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT now_local()
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price DOUBLE PRECISION NOT NULL,
    -- How much of the line the kitchen/bar already has a ticket for.
    sent_quantity INTEGER NOT NULL DEFAULT 0,
    -- Desayunos account lines: the room served and its guest at that moment.
    room_number TEXT,
    guest_name TEXT
);

CREATE TABLE IF NOT EXISTS report_runs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    report_type TEXT NOT NULL DEFAULT 'checkout_payment' CHECK(report_type IN ('checkout_payment', 'inventory')),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    cloud_synced INTEGER NOT NULL DEFAULT 0,
    cloud_path TEXT,
    emailed INTEGER NOT NULL DEFAULT 0,
    email_error TEXT,
    triggered_by TEXT NOT NULL DEFAULT 'scheduled',
    generated_at TEXT NOT NULL DEFAULT now_local()
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('incremental', 'full')),
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL DEFAULT 0,
    cloud_synced INTEGER NOT NULL DEFAULT 0,
    cloud_path TEXT,
    triggered_by TEXT NOT NULL DEFAULT 'scheduled',
    created_at TEXT NOT NULL DEFAULT now_local()
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    user_name TEXT NOT NULL,
    user_role TEXT,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT now_local()
);

-- Three stock pools per item: storeroom_stock (the warehouse everything is
-- received into), current_stock (Restaurant, food) and bar_stock (Bar,
-- drinks). unit_cost is the weighted-average cost, changed only by receipts.
CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    item_name TEXT NOT NULL UNIQUE,
    unit_of_measure TEXT NOT NULL,
    current_stock DOUBLE PRECISION NOT NULL DEFAULT 0,
    low_stock_threshold DOUBLE PRECISION NOT NULL DEFAULT 0,
    storeroom_stock DOUBLE PRECISION NOT NULL DEFAULT 0,
    bar_stock DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id),
    inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    quantity_required DOUBLE PRECISION NOT NULL
);

-- The Kardex ledger: every stock movement with before/after quantity and
-- unit cost, so any item's history (and valuation) can be replayed.
CREATE TABLE IF NOT EXISTS inventory_transactions (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    order_id INTEGER REFERENCES orders(id),
    order_item_id INTEGER,
    quantity_change DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL DEFAULT 'depletion',
    pool TEXT NOT NULL DEFAULT 'restaurant',
    stock_before DOUBLE PRECISION,
    stock_after DOUBLE PRECISION,
    unit_cost_before DOUBLE PRECISION,
    unit_cost_after DOUBLE PRECISION,
    notes TEXT,
    timestamp TEXT NOT NULL DEFAULT now_local()
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    room_number TEXT NOT NULL UNIQUE,
    room_type TEXT NOT NULL CHECK(room_type IN ('comfort', 'bungalow_double', 'bungalow')),
    -- Housekeeping turnover status; 'out_of_order' also keeps a room_blocks
    -- entry so a room under repair can't be booked.
    housekeeping_status TEXT NOT NULL DEFAULT 'clean',
    housekeeping_notes TEXT,
    housekeeping_updated_at TEXT,
    housekeeping_updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS room_blocks (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    room_number TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    reason TEXT NOT NULL,
    blocked_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT now_local(),
    unblocked_at TEXT,
    unblocked_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS table_layout (
    table_number TEXT PRIMARY KEY,
    pos_x DOUBLE PRECISION NOT NULL,
    pos_y DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_receipts (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    quantity DOUBLE PRECISION NOT NULL,
    unit_cost DOUBLE PRECISION,
    supplier TEXT,
    notes TEXT,
    received_by INTEGER NOT NULL REFERENCES users(id),
    received_at TEXT NOT NULL DEFAULT now_local()
);

CREATE TABLE IF NOT EXISTS inventory_transfers (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    quantity DOUBLE PRECISION NOT NULL,
    destination TEXT NOT NULL CHECK(destination IN ('bar', 'restaurant', 'cleaning')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
    notes TEXT,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL DEFAULT now_local(),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Kitchen/bar order tickets ("comandas"), queued in the same transaction as
-- the order lines so a ticket is never lost, then sent by the print worker.
CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    station TEXT NOT NULL CHECK(station IN ('kitchen', 'bar')),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'printed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TEXT NOT NULL DEFAULT now_local(),
    created_at TEXT NOT NULL DEFAULT now_local(),
    printed_at TEXT
);

-- ---- Rates, seasons and agencies ------------------------------------------
-- Travel agencies that book for guests. Commission % per season (low/high)
-- and day type (week/weekend), applied when they book at the Rack rate.
CREATE TABLE IF NOT EXISTS agencies (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    notes TEXT,
    commission_low_week DOUBLE PRECISION NOT NULL DEFAULT 10,
    commission_low_weekend DOUBLE PRECISION NOT NULL DEFAULT 10,
    commission_high_week DOUBLE PRECISION NOT NULL DEFAULT 10,
    commission_high_weekend DOUBLE PRECISION NOT NULL DEFAULT 10,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT now_local()
);

-- Nightly price per room type, season and day type: Rack (general public)
-- and Net (agencies).
CREATE TABLE IF NOT EXISTS room_rates (
    room_type TEXT NOT NULL,
    season TEXT NOT NULL CHECK(season IN ('low', 'high')),
    day_type TEXT NOT NULL CHECK(day_type IN ('week', 'weekend')),
    rack DOUBLE PRECISION NOT NULL,
    net DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (room_type, season, day_type)
);

-- High-season date ranges (nights, inclusive); every other night is low season.
CREATE TABLE IF NOT EXISTS high_season_periods (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL
);

-- What each stay costs, fixed when it is booked or repriced: the room
-- subtotal over all nights, the tax (0 when exempt — embassies, diplomats),
-- the total, and the agency commission (informational: the guest pays total).
ALTER TABLE guests ADD COLUMN IF NOT EXISTS agency_id INTEGER REFERENCES agencies(id);
ALTER TABLE guests ADD COLUMN IF NOT EXISTS rate_type TEXT NOT NULL DEFAULT 'rack';
ALTER TABLE guests ADD COLUMN IF NOT EXISTS tax_exempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS tax_rate DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS room_subtotal DOUBLE PRECISION;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS tax_amount DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS total_amount DOUBLE PRECISION;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS commission_amount DOUBLE PRECISION NOT NULL DEFAULT 0;
-- The per-night prices behind room_subtotal (JSON), as quoted at the time.
ALTER TABLE guests ADD COLUMN IF NOT EXISTS rate_breakdown TEXT;
-- When and by whom the stay was booked (printed as "Hecha el" / "Reserved
-- by" on the confirmation). NULL for stays booked before this was recorded.
ALTER TABLE guests ADD COLUMN IF NOT EXISTS created_at TEXT;
ALTER TABLE guests ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id);

-- When each order entered the system, as a real point in time (shown in
-- hotel time, America/Managua). Existing orders take it from their local
-- 'timestamp' text; new ones get now().
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
UPDATE orders SET created_at = timestamp::timestamptz WHERE created_at IS NULL;
ALTER TABLE orders ALTER COLUMN created_at SET DEFAULT now();
-- Who settled the account (the cashier) — the basis of the shift close.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS closed_by INTEGER REFERENCES users(id);
-- The printed bill: its sequential invoice number (taken at payment) and the
-- cash the customer handed over (for the change on the receipt).
ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_number TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount_tendered DOUBLE PRECISION;
CREATE UNIQUE INDEX IF NOT EXISTS orders_invoice_number_key ON orders (invoice_number) WHERE invoice_number IS NOT NULL;
-- When each line was added: a master account (owners) stays open for days,
-- so its report goes by the day of each line, not the day it was opened.
-- Lines from before this column existed fall back to the order's time.
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS created_at TEXT;
ALTER TABLE order_items ALTER COLUMN created_at SET DEFAULT now_local();
-- A hotel guest's breakfast served on their table tab: 'included' lines cost
-- C$0 on the bill (unit_price 0) and keep the menu price in list_price, so
-- the Desayunos Incluidos report still values them. Extras on the same tab
-- are charged normally.
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS included INTEGER NOT NULL DEFAULT 0;
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS list_price DOUBLE PRECISION;

-- Menu sections (Entradas, Platos fuertes, Pizzas, Postres, Bebidas,
-- Desayunos) and per-dish options: sides (2 or none) and meat doneness.
-- The admin's menu sections (name, icon, order, and whether its dishes
-- print in the kitchen or at the bar). Seeded once with the defaults.
CREATE TABLE IF NOT EXISTS menu_sections (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    icon TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    station TEXT NOT NULL DEFAULT 'kitchen' CHECK(station IN ('kitchen', 'bar'))
);
-- Only into an empty table, so a default the admin deleted stays deleted.
INSERT INTO menu_sections (key, label, icon, sort_order, station)
SELECT * FROM (VALUES
    ('entradas', 'Entradas', '🥗', 1, 'kitchen'),
    ('platos_fuertes', 'Platos fuertes', '🍽️', 2, 'kitchen'),
    ('pizzas', 'Pizzas', '🍕', 3, 'kitchen'),
    ('postres', 'Postres', '🍰', 4, 'kitchen'),
    ('bebidas', 'Bebidas', '🥤', 5, 'bar'),
    ('desayunos', 'Desayunos', '🍳', 6, 'kitchen')) AS v(key, label, icon, sort_order, station)
WHERE NOT EXISTS (SELECT 1 FROM menu_sections);
-- 'none': sections that print no ticket (services, rentals, massages).
ALTER TABLE menu_sections DROP CONSTRAINT IF EXISTS menu_sections_station_check;
ALTER TABLE menu_sections ADD CONSTRAINT menu_sections_station_check CHECK(station IN ('kitchen', 'bar', 'none'));
-- The reception printer prints bills and guest consumption summaries.
ALTER TABLE print_jobs DROP CONSTRAINT IF EXISTS print_jobs_station_check;
ALTER TABLE print_jobs ADD CONSTRAINT print_jobs_station_check CHECK(station IN ('kitchen', 'bar', 'reception'));
ALTER TABLE print_jobs ALTER COLUMN order_id DROP NOT NULL;
-- How many people sit at the account ("Clientes" on the printed bill).
ALTER TABLE orders ADD COLUMN IF NOT EXISTS guest_count INTEGER;
ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS section TEXT;
ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS has_sides INTEGER NOT NULL DEFAULT 0;
ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS asks_doneness INTEGER NOT NULL DEFAULT 0;
-- Existing dishes get a first guess once (the admin can change it): drinks
-- and desserts by category, breakfasts and pizzas by name, starters by
-- common starter words, the rest main courses (which come with sides);
-- steaks and other meats ask for doneness.
UPDATE menu_items m SET section = x.section,
       has_sides = CASE WHEN x.section = 'platos_fuertes' THEN 1 ELSE 0 END,
       asks_doneness = CASE WHEN x.section IN ('platos_fuertes', 'entradas') AND lower(m.name) ~
           '(churrasco|steak|filete|lomo|ribeye|rib eye|t-bone|new york|tomahawk|puyazo|carne de res|carne|hamburguesa|burger)'
           THEN 1 ELSE 0 END
FROM (SELECT id, CASE
        WHEN category = 'Drinks' THEN 'bebidas'
        WHEN category = 'Desserts' THEN 'postres'
        WHEN lower(name) LIKE '%desayuno%' THEN 'desayunos'
        WHEN lower(name) LIKE '%pizza%' THEN 'pizzas'
        WHEN lower(name) ~ '(ceviche|shrimp|camar|soup|sopa|crema de|ensalada|salad|alitas|wings|nachos|entrada|dedos|fingers|bruschetta|tartar|carpaccio)' THEN 'entradas'
        ELSE 'platos_fuertes' END AS section
      FROM menu_items WHERE section IS NULL) x
WHERE m.id = x.id;
-- What the waiter chose for each line, printed on the kitchen ticket.
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS sides TEXT;
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS doneness TEXT;
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS notes TEXT;

-- A cashier's shift: opened and closed by her, with the cash she started
-- with and counted at the end. summary is the printed close (JSON), frozen
-- at closing time.
CREATE TABLE IF NOT EXISTS cash_shifts (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    opened_at TEXT NOT NULL DEFAULT now_local(),
    closed_at TEXT,
    opening_cash DOUBLE PRECISION NOT NULL DEFAULT 0,
    counted_cash DOUBLE PRECISION,
    notes TEXT,
    summary TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_shifts_one_open ON cash_shifts(user_id) WHERE closed_at IS NULL;

-- ---- Maintenance ----------------------------------------------------------
-- The maintenance staff role, and the maintenance storeroom as one more
-- stock pool (fed from the main storeroom by an authorized transfer).
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('admin', 'server', 'front_desk', 'maintenance'));
ALTER TABLE inventory_transfers DROP CONSTRAINT IF EXISTS inventory_transfers_destination_check;
ALTER TABLE inventory_transfers ADD CONSTRAINT inventory_transfers_destination_check
    CHECK(destination IN ('bar', 'restaurant', 'cleaning', 'maintenance'));
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS maintenance_stock DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS maintenance_threshold DOUBLE PRECISION NOT NULL DEFAULT 0;
-- Tools, parts and materials created for maintenance (listed in its storeroom).
ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS is_maintenance INTEGER NOT NULL DEFAULT 0;

-- Breakdowns anyone can report; maintenance / front desk / admin work them.
CREATE TABLE IF NOT EXISTS maintenance_incidents (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    area TEXT NOT NULL,
    room_number TEXT,
    description TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('low', 'medium', 'urgent')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'in_progress', 'resolved')),
    reported_by INTEGER NOT NULL REFERENCES users(id),
    reported_at TEXT NOT NULL DEFAULT now_local(),
    started_by INTEGER REFERENCES users(id),
    started_at TEXT,
    resolved_by INTEGER REFERENCES users(id),
    resolved_at TEXT,
    resolution_note TEXT,
    -- The report took the room out of service (housekeeping block).
    room_out_of_order INTEGER NOT NULL DEFAULT 0
);
-- Optional photos, kept in the database (base64) so they travel with backups.
CREATE TABLE IF NOT EXISTS maintenance_photos (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES maintenance_incidents(id),
    mime TEXT NOT NULL,
    data TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT now_local()
);
-- Every use of maintenance material waits for an administrator's approval;
-- only then does it leave the maintenance storeroom (Kardex 'maintenance_use').
CREATE TABLE IF NOT EXISTS maintenance_usage (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    quantity DOUBLE PRECISION NOT NULL,
    area TEXT,
    room_number TEXT,
    incident_id INTEGER REFERENCES maintenance_incidents(id),
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL DEFAULT now_local(),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_maint_incidents_status ON maintenance_incidents(status, priority);
CREATE INDEX IF NOT EXISTS idx_maint_usage_status ON maintenance_usage(status);
-- Stays booked before pricing existed: nightly rate x nights, no tax.
UPDATE guests SET room_subtotal = ROUND((nightly_rate * GREATEST(checkout_date::date - checkin_date::date, 1))::numeric, 2),
                  total_amount = ROUND((nightly_rate * GREATEST(checkout_date::date - checkin_date::date, 1))::numeric, 2)
WHERE room_subtotal IS NULL;

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_table_status ON orders(table_number, status);
CREATE INDEX IF NOT EXISTS idx_orders_guest ON orders(guest_id);
CREATE INDEX IF NOT EXISTS idx_orders_server ON orders(server_id);
CREATE INDEX IF NOT EXISTS idx_inv_tx_item ON inventory_transactions(inventory_item_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_inv_tx_order_item ON inventory_transactions(order_item_id);
CREATE INDEX IF NOT EXISTS idx_guests_room ON guests(room_number, status);
CREATE INDEX IF NOT EXISTS idx_guests_status ON guests(status);
CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status, next_attempt_at);
'''

# Every table, parents before children — the order backups are written and
# restored in so foreign keys are always satisfied.
TABLES = (
    'users', 'cash_shifts', 'menu_sections', 'menu_items', 'agencies', 'room_rates', 'high_season_periods', 'guests', 'orders', 'rate_adjustments', 'order_items',
    'report_runs', 'backup_runs', 'activity_log', 'inventory_items', 'recipes',
    'inventory_transactions', 'rooms', 'room_blocks', 'table_layout',
    'inventory_receipts', 'inventory_transfers', 'app_settings', 'print_jobs',
    'maintenance_incidents', 'maintenance_photos', 'maintenance_usage',
)


def init_db():
    conn = get_connection(write=True)
    conn.executescript(SCHEMA_SQL)

    if conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c'] == 0:
        seed_users(conn)
    if conn.execute('SELECT COUNT(*) AS c FROM menu_items').fetchone()['c'] == 0:
        seed_menu(conn)
    if conn.execute('SELECT COUNT(*) AS c FROM inventory_items').fetchone()['c'] == 0:
        seed_inventory_and_recipes(conn)
    # Every room type gets a full rate grid (Rack = Net = the standard rate
    # until the admin edits them), including room types added later.
    for room_type in ROOM_TYPES:
        for season in SEASONS:
            for day_type in DAY_TYPES:
                conn.execute('INSERT INTO room_rates (room_type, season, day_type, rack, net) VALUES (?, ?, ?, ?, ?) '
                             'ON CONFLICT (room_type, season, day_type) DO NOTHING',
                             (room_type, season, day_type, NIGHTLY_RATES[room_type], NIGHTLY_RATES[room_type]))

    # Reseed the room roster if it doesn't match the real property's numbering.
    expected_rooms = {n for rooms in ROOM_NUMBERS_BY_TYPE.values() for n in rooms}
    existing_rooms = {r['room_number'] for r in conn.execute('SELECT room_number FROM rooms').fetchall()}
    if existing_rooms != expected_rooms:
        conn.execute('DELETE FROM rooms')
        seed_rooms(conn)

    conn.commit()
    conn.close()


def seed_users(conn):
    users = [
        ('admin', 'Alexandra Morales', 'admin123', 'admin'),
        ('jsmith', 'James Smith', 'server123', 'server'),
        ('agarcia', 'Ana Garcia', 'server123', 'server'),
        ('mrivas', 'Marcos Rivas', 'frontdesk123', 'front_desk'),
    ]
    conn.executemany('''
        INSERT INTO users (username, full_name, password_hash, role)
        VALUES (?, ?, ?, ?)
    ''', [(u, n, generate_password_hash(p), r) for u, n, p, r in users])
    conn.commit()


def seed_menu(conn):
    # Prices in Nicaraguan Cordobas (C$), converted from the prior USD prices
    # at C$36.62/US$1.
    items = [
        ('Coconut Water', 'Drinks', 146.48, '16 oz'),
        ('Rum Punch', 'Drinks', 274.65, '12 oz'),
        ('Iced Tea', 'Drinks', 109.86, '16 oz'),
        ('Grilled Snapper', 'Food', 677.47, 'Whole fish, rice & beans'),
        ('Coconut Shrimp', 'Food', 585.92, '6 pieces'),
        ('Ceviche', 'Food', 439.44, '8 oz'),
        ('Rondon Soup', 'Food', 512.68, '1 bowl'),
        ('Tres Leches Cake', 'Desserts', 238.03, '1 slice'),
        ('Coconut Flan', 'Desserts', 219.72, '1 slice'),
    ]
    conn.executemany('''
        INSERT INTO menu_items (name, category, price, portion_size, is_active)
        VALUES (?, ?, ?, ?, 1)
    ''', items)
    conn.commit()


def seed_rooms(conn):
    """The physical room roster, using the property's real room numbers —
    the authoritative inventory: capacity everywhere else is derived by
    counting these rows, not from a hardcoded number."""
    rooms = [
        (number, room_type)
        for room_type, numbers in ROOM_NUMBERS_BY_TYPE.items()
        for number in numbers
    ]
    conn.executemany('INSERT INTO rooms (room_number, room_type) VALUES (?, ?)', rooms)
    conn.commit()


def seed_inventory_and_recipes(conn):
    inventory = [
        ('Snapper Fillet', 'kg', 20.0, 5.0),
        ('Rice & Beans Mix', 'kg', 30.0, 8.0),
        ('Shrimp', 'kg', 12.0, 3.0),
        ('Coconut Milk', 'L', 15.0, 4.0),
        ('Fresh Fish (Ceviche cut)', 'kg', 10.0, 3.0),
        ('Lime', 'kg', 6.0, 2.0),
        ('Coconut', 'unit', 40.0, 10.0),
        ('Rum', 'L', 8.0, 2.0),
        ('Tea Bags', 'unit', 100.0, 20.0),
        ('Milk (for desserts)', 'L', 10.0, 3.0),
        ('Eggs', 'unit', 60.0, 12.0),
    ]
    conn.executemany('''
        INSERT INTO inventory_items (item_name, unit_of_measure, current_stock, low_stock_threshold)
        VALUES (?, ?, ?, ?)
    ''', inventory)
    conn.commit()

    inv = {row['item_name']: row['id'] for row in conn.execute('SELECT id, item_name FROM inventory_items')}
    menu = {row['name']: row['id'] for row in conn.execute('SELECT id, name FROM menu_items')}

    recipe_map = {
        'Grilled Snapper': [('Snapper Fillet', 0.35), ('Rice & Beans Mix', 0.2)],
        'Coconut Shrimp': [('Shrimp', 0.25), ('Coconut Milk', 0.05)],
        'Ceviche': [('Fresh Fish (Ceviche cut)', 0.2), ('Lime', 0.05)],
        'Rondon Soup': [('Coconut Milk', 0.3), ('Fresh Fish (Ceviche cut)', 0.15)],
        'Coconut Water': [('Coconut', 1.0)],
        'Rum Punch': [('Rum', 0.06)],
        'Iced Tea': [('Tea Bags', 1.0)],
        'Tres Leches Cake': [('Milk (for desserts)', 0.15), ('Eggs', 1.0)],
        'Coconut Flan': [('Eggs', 2.0), ('Milk (for desserts)', 0.1)],
    }
    for menu_name, lines in recipe_map.items():
        menu_item_id = menu.get(menu_name)
        if not menu_item_id:
            continue
        for inv_name, qty in lines:
            inv_item_id = inv.get(inv_name)
            if not inv_item_id:
                continue
            conn.execute('''
                INSERT INTO recipes (menu_item_id, inventory_item_id, quantity_required)
                VALUES (?, ?, ?)
            ''', (menu_item_id, inv_item_id, qty))
    conn.commit()


# ---------------------------------------------------------- Activity log ----

def _log_activity(conn, user_id, action, details=None):
    """Records one staff action for the audit trail. Takes the caller's own
    connection and does not commit/close it — the entry is persisted
    together with whatever change it's documenting."""
    user = conn.execute('SELECT full_name, role FROM users WHERE id = ?', (user_id,)).fetchone() if user_id else None
    conn.execute('''
        INSERT INTO activity_log (user_id, user_name, user_role, action, details)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, user['full_name'] if user else 'Unknown', user['role'] if user else None, action, details))


def list_activity_log(page=1, per_page=20):
    """One page of the activity log, newest first, plus the total count so the
    screen can page through the whole history."""
    per_page = max(1, min(int(per_page), 100))
    conn = get_connection()
    total = conn.execute('SELECT COUNT(*) FROM activity_log').fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = min(max(int(page), 1), pages)
    rows = conn.execute(
        'SELECT * FROM activity_log ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
        (per_page, (page - 1) * per_page)
    ).fetchall()
    conn.close()
    return {'entries': [dict(r) for r in rows], 'total': total, 'page': page, 'pages': pages, 'per_page': per_page}


# ---------------------------------------------------------------- Users ----

def get_user_by_username(username):
    conn = get_connection()
    row = conn.execute('SELECT * FROM users WHERE username = ? AND is_active = 1', (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_connection()
    row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users():
    conn = get_connection()
    rows = conn.execute('SELECT id, username, full_name, role FROM users WHERE is_active = 1 ORDER BY role, full_name').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_servers():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users WHERE role = 'server' AND is_active = 1 ORDER BY full_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


MIN_PASSWORD_LENGTH = 8
# The passwords seed_users() ships with; anyone who has seen this code knows them.
FACTORY_PASSWORDS = {'admin': 'admin123', 'jsmith': 'server123', 'agarcia': 'server123', 'mrivas': 'frontdesk123'}


def _check_password_strength(password):
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f'La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres')
    if password.lower() in {p.lower() for p in FACTORY_PASSWORDS.values()}:
        raise ValueError('Esa contraseña es una de las de fábrica; elija otra')


def set_user_password(username, password):
    _check_password_strength(password)
    conn = get_connection(write=True)
    cur = conn.execute('UPDATE users SET password_hash = ? WHERE username = ?',
                       (generate_password_hash(password), username))
    if cur.rowcount == 0:
        conn.close()
        raise ValueError(f"No existe el usuario '{username}'")
    conn.commit()
    conn.close()


def users_with_factory_passwords():
    from werkzeug.security import check_password_hash
    conn = get_connection()
    rows = conn.execute('SELECT username, password_hash FROM users').fetchall()
    conn.close()
    return [r['username'] for r in rows
            if r['username'] in FACTORY_PASSWORDS and check_password_hash(r['password_hash'], FACTORY_PASSWORDS[r['username']])]


def create_user(username, full_name, password, role='server', created_by_user_id=None):
    if role not in STAFF_CREATABLE_ROLES:
        raise ValueError(f"role must be one of {STAFF_CREATABLE_ROLES}")
    if not username or not full_name or not password:
        raise ValueError('El usuario, nombre completo y contraseña son obligatorios')
    _check_password_strength(password)

    conn = get_connection(write=True)
    existing = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if existing:
        conn.close()
        raise ValueError(f"El usuario '{username}' ya está en uso")

    cur = conn.cursor()
    cur.execute('''
        INSERT INTO users (username, full_name, password_hash, role)
        VALUES (?, ?, ?, ?)
    ''', (username, full_name, generate_password_hash(password), role))
    _log_activity(conn, created_by_user_id, 'staff_created', f"Creó cuenta de {ROLE_LABELS.get(role, role)} para {full_name} ({username})")
    conn.commit()
    user = conn.execute(
        'SELECT id, username, full_name, role FROM users WHERE id = ?', (cur.lastrowid,)
    ).fetchone()
    conn.close()
    return dict(user)


def delete_user(user_id, requesting_user_id):
    if user_id == requesting_user_id:
        raise ValueError('No puede eliminar su propia cuenta')

    conn = get_connection(write=True)
    existing = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Usuario no encontrado')

    if not existing['is_active']:
        conn.close()
        raise ValueError('Esta cuenta ya fue eliminada')

    if existing['role'] == 'admin':
        admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1").fetchone()['n']
        if admins <= 1:
            conn.close()
            raise ValueError('No se puede eliminar al último administrador')

    # Accounts with orders or activity-log entries can't be hard deleted
    # without breaking receipts and reports, so they are deactivated instead:
    # no login, hidden from staff lists, and the username is freed for reuse.
    conn.execute('SAVEPOINT delete_user')
    try:
        conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.execute('RELEASE delete_user')
    except psycopg.errors.IntegrityError:
        conn.execute('ROLLBACK TO delete_user')
        conn.execute('RELEASE delete_user')
        conn.execute('UPDATE users SET is_active = 0, username = ? WHERE id = ?',
                     (f"{existing['username']}#eliminado-{user_id}", user_id))
    _log_activity(conn, requesting_user_id, 'staff_deleted',
                  f"Eliminó cuenta de {ROLE_LABELS.get(existing['role'], existing['role'])} de {existing['full_name']} ({existing['username']})")
    conn.commit()
    conn.close()
    return {'id': user_id, 'deleted': True}


# ---------------------------------------------------------------- Menu ----

def list_menu_items(active_only=False):
    conn = get_connection()
    if active_only:
        rows = conn.execute('SELECT * FROM menu_items WHERE is_active = 1 ORDER BY category, name').fetchall()
    else:
        rows = conn.execute('SELECT * FROM menu_items ORDER BY category, name').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_menu_item_by_name(name, conn=None):
    own_conn = conn is None
    conn = conn or get_connection()
    row = conn.execute('SELECT * FROM menu_items WHERE name = ?', (name,)).fetchone()
    if own_conn:
        conn.close()
    return dict(row) if row else None


def _category_for_section(key, station):
    """Bar sections are Drinks (bar ticket, bar stock); Postres keeps the
    Desserts category; every other kitchen section is Food."""
    if station == 'bar':
        return 'Drinks'
    return 'Desserts' if key == 'postres' else 'Food'


def _section_and_category(section, category):
    """The section chosen (or the one implied by an old-style category) and
    the category that goes with it."""
    if section:
        conn = get_connection()
        row = conn.execute('SELECT key, station FROM menu_sections WHERE key = ?', (section,)).fetchone()
        conn.close()
        if not row:
            raise ValueError('Sección del menú inválida')
        return section, _category_for_section(row['key'], row['station'])
    if category not in MENU_CATEGORIES:
        raise ValueError(f"category must be one of {MENU_CATEGORIES}")
    # The default section may have been deleted by the admin: then the first
    # section of the same station.
    conn = get_connection()
    row = conn.execute('SELECT key FROM menu_sections WHERE key = ?', (CATEGORY_DEFAULT_SECTION[category],)).fetchone() \
        or conn.execute('SELECT key FROM menu_sections WHERE station = ? ORDER BY sort_order LIMIT 1',
                        ('bar' if category == 'Drinks' else 'kitchen',)).fetchone()
    conn.close()
    return (row['key'] if row else CATEGORY_DEFAULT_SECTION[category]), category


# ---- the admin's menu sections ----------------------------------------------

def list_menu_sections():
    conn = get_connection()
    rows = conn.execute('''
        SELECT s.*, (SELECT COUNT(*) FROM menu_items m WHERE m.section = s.key) AS dish_count
        FROM menu_sections s ORDER BY s.sort_order, s.label
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _clean_section_fields(label, icon, station):
    label = ' '.join((label or '').split())[:40]
    if not label:
        raise ValueError('Escriba el nombre de la sección')
    if station not in ('kitchen', 'bar', 'none'):
        raise ValueError('La sección se imprime en Cocina, en Bar o sin ticket')
    return label, (icon or '').strip()[:4] or None


def create_menu_section(label, icon, station, user_id):
    label, icon = _clean_section_fields(label, icon, station)
    base = re.sub(r'[^a-z0-9]+', '_', unicodedata.normalize('NFKD', label).encode('ascii', 'ignore').decode().lower()).strip('_') or 'seccion'
    conn = get_connection(write=True)
    if conn.execute('SELECT 1 FROM menu_sections WHERE lower(label) = lower(?)', (label,)).fetchone():
        conn.close()
        raise ValueError(f'Ya existe la sección «{label}»')
    key, n = base, 2
    while conn.execute('SELECT 1 FROM menu_sections WHERE key = ?', (key,)).fetchone():
        key, n = f'{base}_{n}', n + 1
    order = conn.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM menu_sections').fetchone()['n']
    conn.execute('INSERT INTO menu_sections (key, label, icon, sort_order, station) VALUES (?, ?, ?, ?, ?)',
                 (key, label, icon, order, station))
    _log_activity(conn, user_id, 'menu_section_created', f'Sección del menú: {label}')
    conn.commit()
    conn.close()
    return next(s for s in list_menu_sections() if s['key'] == key)


def update_menu_section(key, label, icon, station, user_id):
    label, icon = _clean_section_fields(label, icon, station)
    conn = get_connection(write=True)
    row = conn.execute('SELECT * FROM menu_sections WHERE key = ?', (key,)).fetchone()
    if not row:
        conn.close()
        raise ValueError('Sección no encontrada')
    if conn.execute('SELECT 1 FROM menu_sections WHERE lower(label) = lower(?) AND key <> ?', (label, key)).fetchone():
        conn.close()
        raise ValueError(f'Ya existe la sección «{label}»')
    conn.execute('UPDATE menu_sections SET label = ?, icon = ?, station = ? WHERE key = ?', (label, icon, station, key))
    if station != row['station']:
        # Its dishes now print (and draw stock) at the other station.
        conn.execute('UPDATE menu_items SET category = ? WHERE section = ?', (_category_for_section(key, station), key))
    _log_activity(conn, user_id, 'menu_section_updated', f"Sección del menú: {row['label']} → {label}")
    conn.commit()
    conn.close()
    return next(s for s in list_menu_sections() if s['key'] == key)


def move_menu_section(key, direction):
    """Swaps a section with the one before (-1) or after (+1) it."""
    sections = list_menu_sections()
    idx = next((i for i, s in enumerate(sections) if s['key'] == key), None)
    if idx is None:
        raise ValueError('Sección no encontrada')
    other = idx + (1 if direction > 0 else -1)
    if not 0 <= other < len(sections):
        return sections
    order = [s['key'] for s in sections]
    order[idx], order[other] = order[other], order[idx]
    conn = get_connection(write=True)
    for n, k in enumerate(order, start=1):
        conn.execute('UPDATE menu_sections SET sort_order = ? WHERE key = ?', (n, k))
    conn.commit()
    conn.close()
    return list_menu_sections()


def delete_menu_section(key, move_to, user_id):
    """Removes a section; its dishes (if any) move to another one first."""
    conn = get_connection(write=True)
    row = conn.execute('SELECT * FROM menu_sections WHERE key = ?', (key,)).fetchone()
    if not row:
        conn.close()
        raise ValueError('Sección no encontrada')
    count = conn.execute('SELECT COUNT(*) AS n FROM menu_items WHERE section = ?', (key,)).fetchone()['n']
    if count:
        target = conn.execute('SELECT * FROM menu_sections WHERE key = ?', (move_to,)).fetchone() if move_to else None
        if not target or move_to == key:
            conn.close()
            raise ValueError(f"«{row['label']}» tiene {count} plato(s): elija a qué sección pasarlos")
        conn.execute('UPDATE menu_items SET section = ?, category = ? WHERE section = ?',
                     (move_to, _category_for_section(move_to, target['station']), key))
    conn.execute('DELETE FROM menu_sections WHERE key = ?', (key,))
    _log_activity(conn, user_id, 'menu_section_deleted',
                  f"Sección del menú eliminada: {row['label']}" + (f' ({count} plato(s) movidos)' if count else ''))
    conn.commit()
    conn.close()


def create_menu_item(name, category, price, portion_size, section=None, has_sides=None, asks_doneness=None):
    section, category = _section_and_category(section, category)
    if has_sides is None:
        has_sides = section == 'platos_fuertes'
    conn = get_connection(write=True)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO menu_items (name, category, price, portion_size, is_active, section, has_sides, asks_doneness)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
    ''', (name, category, price, portion_size, section, 1 if has_sides else 0, _doneness_kind(asks_doneness)))
    conn.commit()
    item = dict(conn.execute('SELECT * FROM menu_items WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return item


def update_menu_item(item_id, name=None, category=None, price=None, portion_size=None,
                     section=None, has_sides=None, asks_doneness=None):
    conn = get_connection(write=True)
    existing = conn.execute('SELECT * FROM menu_items WHERE id = ?', (item_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Artículo del menú no encontrado')
    if section is not None or category is not None:
        try:
            section, category = _section_and_category(section, category)
        except ValueError:
            conn.close()
            raise

    conn.execute('''
        UPDATE menu_items SET
            name = COALESCE(?, name),
            category = COALESCE(?, category),
            price = COALESCE(?, price),
            portion_size = COALESCE(?, portion_size),
            section = COALESCE(?, section),
            has_sides = COALESCE(?, has_sides),
            asks_doneness = COALESCE(?, asks_doneness)
        WHERE id = ?
    ''', (name, category, price, portion_size, section,
          None if has_sides is None else (1 if has_sides else 0),
          None if asks_doneness is None else _doneness_kind(asks_doneness), item_id))
    conn.commit()
    item = dict(conn.execute('SELECT * FROM menu_items WHERE id = ?', (item_id,)).fetchone())
    conn.close()
    return item


def toggle_menu_item(item_id):
    conn = get_connection(write=True)
    existing = conn.execute('SELECT * FROM menu_items WHERE id = ?', (item_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Artículo del menú no encontrado')
    new_state = 0 if existing['is_active'] else 1
    conn.execute('UPDATE menu_items SET is_active = ? WHERE id = ?', (new_state, item_id))
    conn.commit()
    item = dict(conn.execute('SELECT * FROM menu_items WHERE id = ?', (item_id,)).fetchone())
    conn.close()
    return item


# ------------------------------------------------------------- Inventory ----

def list_inventory_items():
    """Bar and Restaurant only get flagged low for items actually used by a
    recipe on that side — otherwise an ingredient that's purely a kitchen
    item (never in a Drinks recipe) would sit at bar_stock=0 forever and
    show as perpetually low for a pool it never draws from."""
    conn = get_connection()
    rows = conn.execute('SELECT * FROM inventory_items ORDER BY item_name').fetchall()
    relevance = {}
    for r in conn.execute('''
        SELECT r.inventory_item_id, m.category
        FROM recipes r JOIN menu_items m ON m.id = r.menu_item_id
    ''').fetchall():
        rel = relevance.setdefault(r['inventory_item_id'], {'bar': False, 'restaurant': False})
        if r['category'] == 'Drinks':
            rel['bar'] = True
        else:
            rel['restaurant'] = True
    conn.close()
    items = [dict(r) for r in rows]
    for i in items:
        rel = relevance.get(i['id'], {'bar': False, 'restaurant': False})
        if not rel['bar'] and not rel['restaurant']:
            # Not in any recipe yet — fall back to the restaurant pool so a
            # brand-new item still gets sensible low-stock behavior.
            rel['restaurant'] = True
        i['bar_relevant'] = rel['bar']
        i['restaurant_relevant'] = rel['restaurant']
        # Low-stock alerts only watch the main warehouse (Bodega Principal).
        i['is_low'] = i['storeroom_stock'] <= i['low_stock_threshold']
    return items


def get_inventory_item(item_id, conn=None):
    own_conn = conn is None
    conn = conn or get_connection()
    row = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if own_conn:
        conn.close()
    return dict(row) if row else None


def create_inventory_item(item_name, unit_of_measure, current_stock, low_stock_threshold, bar_stock=0, unit_cost=0):
    if not item_name or not unit_of_measure:
        raise ValueError('El nombre del artículo y la unidad de medida son obligatorios')
    conn = get_connection(write=True)
    existing = conn.execute('SELECT id FROM inventory_items WHERE item_name = ?', (item_name,)).fetchone()
    if existing:
        conn.close()
        raise ValueError(f"'{item_name}' ya existe en el inventario")
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO inventory_items (item_name, unit_of_measure, current_stock, low_stock_threshold, bar_stock, unit_cost)
        VALUES (?, ?, 0, ?, 0, ?)
    ''', (item_name, unit_of_measure, low_stock_threshold, unit_cost or 0))
    item_id = cur.lastrowid
    # Starting balances are logged as an 'Inventario Inicial' Kardex line
    # instead of just appearing in inventory_items with no history behind them.
    # New products always start in the main warehouse (Bodega Principal).
    initial_stock = (current_stock or 0) + (bar_stock or 0)
    if initial_stock:
        _apply_inventory_movement(conn, item_id, 'storeroom', initial_stock, 'initial')
    conn.commit()
    item = dict(conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone())
    conn.close()
    return item


def update_inventory_item(item_id, item_name=None, unit_of_measure=None, low_stock_threshold=None,
                           current_stock=None, bar_stock=None):
    conn = get_connection(write=True)
    existing = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')

    if item_name and item_name != existing['item_name']:
        clash = conn.execute('SELECT id FROM inventory_items WHERE item_name = ? AND id != ?',
                              (item_name, item_id)).fetchone()
        if clash:
            conn.close()
            raise ValueError(f"'{item_name}' ya existe en el inventario")

    conn.execute('''
        UPDATE inventory_items SET
            item_name = COALESCE(?, item_name),
            unit_of_measure = COALESCE(?, unit_of_measure),
            low_stock_threshold = COALESCE(?, low_stock_threshold)
        WHERE id = ?
    ''', (item_name, unit_of_measure, low_stock_threshold, item_id))

    if current_stock is not None and current_stock != existing['current_stock']:
        delta = current_stock - existing['current_stock']
        _apply_inventory_movement(conn, item_id, 'restaurant', delta, 'manual_correction')

    if bar_stock is not None and bar_stock != existing['bar_stock']:
        delta = bar_stock - existing['bar_stock']
        _apply_inventory_movement(conn, item_id, 'bar', delta, 'manual_correction')

    conn.commit()
    item = dict(conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone())
    conn.close()
    return item


def delete_inventory_item(item_id):
    conn = get_connection(write=True)
    existing = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    try:
        # An item whose only history is its 'Inventario Inicial' line was never
        # really used, so that line goes with it. Any other movement, recipe,
        # receipt or transfer still blocks the delete (rolled back below).
        has_real_history = conn.execute(
            "SELECT 1 FROM inventory_transactions WHERE inventory_item_id = ? AND reason != 'initial' LIMIT 1",
            (item_id,)).fetchone()
        if not has_real_history:
            conn.execute("DELETE FROM inventory_transactions WHERE inventory_item_id = ? AND reason = 'initial'",
                         (item_id,))
        conn.execute('DELETE FROM inventory_items WHERE id = ?', (item_id,))
        conn.commit()
    except psycopg.errors.IntegrityError:
        conn.rollback()
        conn.close()
        raise ValueError(
            f"No se puede eliminar '{existing['item_name']}' — se usa en una receta o tiene historial de transacciones. "
            "Elimínelo primero de cualquier receta."
        )
    conn.close()
    return {'id': item_id, 'deleted': True}


def import_inventory_items(rows):
    """Bulk-add items from an uploaded spreadsheet. rows is a list of
    (item_name, unit_of_measure, quantity). New items are created with that
    quantity as their 'Inventario Inicial' in the main warehouse. An item that
    already exists (ignoring case) only gets the quantity if its main
    warehouse is still at 0 — otherwise it's skipped, so re-uploading the same
    file never counts the stock twice."""
    conn = get_connection(write=True)
    existing = {r['item_name'].strip().lower(): {'id': r['id'], 'storeroom_stock': r['storeroom_stock']}
                for r in conn.execute('SELECT id, item_name, storeroom_stock FROM inventory_items')}
    added, updated, skipped = [], [], []
    for item_name, unit, quantity in rows:
        key = item_name.lower()
        quantity = quantity if quantity and quantity > 0 else 0
        item = existing.get(key)
        if item:
            if quantity and item['storeroom_stock'] <= 0:
                _apply_inventory_movement(conn, item['id'], 'storeroom', quantity, 'initial', notes='Importado desde Excel')
                item['storeroom_stock'] = quantity
                updated.append(item_name)
            else:
                skipped.append(item_name)
            continue
        cur = conn.execute('''
            INSERT INTO inventory_items (item_name, unit_of_measure, current_stock, low_stock_threshold, bar_stock, unit_cost)
            VALUES (?, ?, 0, 0, 0, 0)
        ''', (item_name, unit or 'unidad'))
        if quantity:
            _apply_inventory_movement(conn, cur.lastrowid, 'storeroom', quantity, 'initial', notes='Importado desde Excel')
        existing[key] = {'id': cur.lastrowid, 'storeroom_stock': quantity}
        added.append(item_name)
    conn.commit()
    conn.close()
    return {'added': added, 'updated': updated, 'skipped': skipped}


def adjust_inventory_stock(item_id, quantity_change, reason='manual_adjustment'):
    """Positive quantity_change restocks; negative depletes. Always logged."""
    conn = get_connection(write=True)
    existing = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    if quantity_change < 0 and existing['current_stock'] + quantity_change < -1e-9:
        conn.close()
        raise ValueError(
            f"No se puede reducir {existing['item_name']} en {-quantity_change:g} {existing['unit_of_measure']}: "
            f"solo quedan {max(existing['current_stock'], 0):g}. Las existencias no pueden quedar negativas."
        )
    conn.execute('UPDATE inventory_items SET current_stock = current_stock + ? WHERE id = ?',
                 (quantity_change, item_id))
    conn.execute('''
        INSERT INTO inventory_transactions (inventory_item_id, order_id, quantity_change, reason)
        VALUES (?, NULL, ?, ?)
    ''', (item_id, quantity_change, reason))
    conn.commit()
    item = dict(conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone())
    conn.close()
    return item


# ---------------------------------------------- Inventory & Requisition ----
# Everything comes into the property through the main storeroom first.
# Getting it to the bar or restaurant (where recipes actually deplete it) is
# an authorized transfer; cleaning supplies skip that authorization step
# since they're for general hotel use, not resale — they're just issued
# straight out of the storeroom.

DESTINATION_LABELS_ES = {'bar': 'Bar', 'restaurant': 'Restaurante', 'cleaning': 'Limpieza (uso general)',
                         'maintenance': 'Bodega de Mantenimiento'}


def receive_inventory(inventory_item_id, quantity, received_by_user_id, supplier=None, notes=None, unit_cost=None):
    """Logs an incoming shipment — always lands in the storeroom, never
    directly usable until transferred out. When unit_cost is given, the
    item's running weighted-average cost is updated (old_qty*old_avg +
    new_qty*unit_cost, divided by the new total) — the number the Kardex's
    'Costo promedio' columns come from. Leaving it out keeps the average
    cost untouched, for shipments where the cost isn't known yet."""
    if quantity is None or quantity <= 0:
        raise ValueError('La cantidad debe ser mayor que cero')
    if unit_cost is not None and unit_cost < 0:
        raise ValueError('El costo unitario no puede ser negativo')
    conn = get_connection(write=True)
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')

    new_avg_cost = None
    if unit_cost is not None:
        old_qty = item['storeroom_stock']
        old_avg = item['unit_cost']
        new_total_qty = old_qty + quantity
        new_avg_cost = round(((old_qty * old_avg) + (quantity * unit_cost)) / new_total_qty, 4) \
            if new_total_qty else unit_cost

    _apply_inventory_movement(conn, inventory_item_id, 'storeroom', quantity, 'receipt',
                               unit_cost_override=new_avg_cost)

    cur = conn.cursor()
    cur.execute('''
        INSERT INTO inventory_receipts (inventory_item_id, quantity, supplier, notes, received_by, unit_cost)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (inventory_item_id, quantity, supplier, notes, received_by_user_id, unit_cost))
    details = f"Recibido: {quantity} {item['unit_of_measure']} de {item['item_name']}"
    if unit_cost is not None:
        details += f" a C${unit_cost:,.2f}/u"
    if supplier:
        details += f" — proveedor: {supplier}"
    _log_activity(conn, received_by_user_id, 'inventory_received', details)
    conn.commit()
    receipt = dict(conn.execute('SELECT * FROM inventory_receipts WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return receipt


def list_inventory_receipts(limit=1000):
    conn = get_connection()
    rows = conn.execute('''
        SELECT r.*, i.item_name, i.unit_of_measure, u.full_name AS received_by_name
        FROM inventory_receipts r
        JOIN inventory_items i ON i.id = r.inventory_item_id
        JOIN users u ON u.id = r.received_by
        ORDER BY r.received_at DESC, r.id DESC LIMIT ?
    ''', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def request_inventory_transfer(inventory_item_id, quantity, destination, requested_by_user_id, notes=None):
    """Bar/restaurant requests are staged as 'pending' until an admin/front
    desk authorizes them (moving stock storeroom -> current). A 'cleaning'
    destination skips that — it's issued immediately since it's for general
    hotel use, not for resale through the recipe system."""
    if destination not in DESTINATION_LABELS_ES:
        raise ValueError(f"destination debe ser uno de {tuple(DESTINATION_LABELS_ES)}")
    if quantity is None or quantity <= 0:
        raise ValueError('La cantidad debe ser mayor que cero')

    conn = get_connection(write=True)
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    if quantity > item['storeroom_stock']:
        conn.close()
        raise ValueError(
            f"Solo hay {item['storeroom_stock']} {item['unit_of_measure']} de {item['item_name']} en la bodega principal"
        )

    cur = conn.cursor()
    if destination == 'cleaning':
        _apply_inventory_movement(conn, inventory_item_id, 'storeroom', -quantity, 'issued_cleaning', notes=notes)
        cur.execute('''
            INSERT INTO inventory_transfers (inventory_item_id, quantity, destination, status, notes,
                                              requested_by, decided_by, decided_at)
            VALUES (?, ?, 'cleaning', 'approved', ?, ?, ?, now_local())
        ''', (inventory_item_id, quantity, notes, requested_by_user_id, requested_by_user_id))
        _log_activity(conn, requested_by_user_id, 'inventory_issued_cleaning',
                      f"Entregado para limpieza/uso general: {quantity} {item['unit_of_measure']} de {item['item_name']}")
    else:
        cur.execute('''
            INSERT INTO inventory_transfers (inventory_item_id, quantity, destination, status, notes, requested_by)
            VALUES (?, ?, ?, 'pending', ?, ?)
        ''', (inventory_item_id, quantity, destination, notes, requested_by_user_id))
        _log_activity(conn, requested_by_user_id, 'inventory_transfer_requested',
                      f"Solicitó {quantity} {item['unit_of_measure']} de {item['item_name']} para "
                      f"{DESTINATION_LABELS_ES[destination]}")
    conn.commit()
    transfer = dict(conn.execute('SELECT * FROM inventory_transfers WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return transfer


def list_pending_inventory_transfers():
    conn = get_connection()
    rows = conn.execute('''
        SELECT t.*, i.item_name, i.unit_of_measure, u.full_name AS requested_by_name
        FROM inventory_transfers t
        JOIN inventory_items i ON i.id = t.inventory_item_id
        JOIN users u ON u.id = t.requested_by
        WHERE t.status = 'pending'
        ORDER BY t.requested_at ASC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_inventory_transfers(limit=1000):
    conn = get_connection()
    rows = conn.execute('''
        SELECT t.*, i.item_name, i.unit_of_measure, u.full_name AS requested_by_name,
               d.full_name AS decided_by_name
        FROM inventory_transfers t
        JOIN inventory_items i ON i.id = t.inventory_item_id
        JOIN users u ON u.id = t.requested_by
        LEFT JOIN users d ON d.id = t.decided_by
        ORDER BY t.requested_at DESC, t.id DESC LIMIT ?
    ''', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def decide_inventory_transfer(transfer_id, approver_user_id, approve, note=None):
    """Authorizes (or rejects) a pending bar/restaurant transfer request."""
    conn = get_connection(write=True)
    transfer = conn.execute('SELECT * FROM inventory_transfers WHERE id = ?', (transfer_id,)).fetchone()
    if not transfer:
        conn.close()
        raise ValueError('Solicitud de transferencia no encontrada')
    if transfer['status'] != 'pending':
        conn.close()
        raise ValueError('Esta solicitud ya fue decidida')

    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (transfer['inventory_item_id'],)).fetchone()

    if approve:
        if transfer['quantity'] > item['storeroom_stock']:
            conn.close()
            raise ValueError(
                f"Solo hay {item['storeroom_stock']} {item['unit_of_measure']} de {item['item_name']} en la bodega — "
                "ya no alcanza para esta solicitud"
            )
        dest_pool = transfer['destination'] if transfer['destination'] in ('bar', 'maintenance') else 'restaurant'
        _apply_inventory_movement(conn, transfer['inventory_item_id'], 'storeroom', -transfer['quantity'],
                                   'transfer_out', notes=f"A {DESTINATION_LABELS_ES[transfer['destination']]}")
        _apply_inventory_movement(conn, transfer['inventory_item_id'], dest_pool, transfer['quantity'],
                                   'transfer_in', notes='Desde bodega principal')
        new_status = 'approved'
    else:
        new_status = 'rejected'

    conn.execute('''
        UPDATE inventory_transfers SET status = ?, decided_by = ?, decided_at = now_local(),
                                        decision_note = ?
        WHERE id = ?
    ''', (new_status, approver_user_id, note, transfer_id))

    action = 'inventory_transfer_approved' if approve else 'inventory_transfer_rejected'
    details = f"{transfer['quantity']} {item['unit_of_measure']} de {item['item_name']} a {DESTINATION_LABELS_ES[transfer['destination']]}"
    if note:
        details += f" — {note}"
    _log_activity(conn, approver_user_id, action, details)
    conn.commit()
    updated = dict(conn.execute('SELECT * FROM inventory_transfers WHERE id = ?', (transfer_id,)).fetchone())
    conn.close()
    return updated


def write_off_inventory(inventory_item_id, quantity, pool, user_id, reason_note=None):
    """Records shrinkage — expired, spoiled, broken — as its own explicit,
    auditable Kardex line instead of it silently vanishing through a generic
    manual stock correction."""
    if pool not in ('storeroom', 'bar', 'restaurant', 'maintenance'):
        raise ValueError("pool debe ser 'storeroom', 'bar', 'restaurant' o 'maintenance'")
    if quantity is None or quantity <= 0:
        raise ValueError('La cantidad debe ser mayor que cero')
    conn = get_connection(write=True)
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    stock_col = _stock_column(pool)
    if quantity > item[stock_col]:
        conn.close()
        raise ValueError(f"Solo hay {item[stock_col]} {item['unit_of_measure']} de {item['item_name']} ahí")

    _apply_inventory_movement(conn, inventory_item_id, pool, -quantity, 'writeoff', notes=reason_note)
    details = f"Pérdida registrada: {quantity} {item['unit_of_measure']} de {item['item_name']} ({KARDEX_POOL_LABELS_ES[pool]})"
    if reason_note:
        details += f" — {reason_note}"
    _log_activity(conn, user_id, 'inventory_writeoff', details)
    conn.commit()
    updated = dict(conn.execute('SELECT * FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone())
    conn.close()
    return updated


KARDEX_REASON_LABELS_ES = {
    'initial': 'Inventario Inicial',
    'receipt': 'Recibido',
    'transfer_out': 'Transferencia (Salida)',
    'transfer_in': 'Transferencia (Entrada)',
    'issued_cleaning': 'Entregado a Limpieza',
    'order_depletion': 'Consumido en Orden',
    'item_removed': 'Reingresado (Orden Editada)',
    'order_cancelled': 'Reingresado (Orden Cancelada)',
    'manual_correction': 'Corrección Manual',
    'writeoff': 'Pérdida / Vencimiento',
    'maintenance_use': 'Consumo de Mantenimiento',
}
KARDEX_POOL_LABELS_ES = {'storeroom': 'Bodega Principal', 'bar': 'Bar', 'restaurant': 'Restaurante',
                         'maintenance': 'Bodega de Mantenimiento'}


def get_inventory_kardex(item_id, start_date=None, end_date=None, pool=None):
    """The full chronological ledger for one item — every quantity AND cost
    change it has ever had, in order, so a period's movements and closing
    valuation can be reconstructed exactly like a real Kardex."""
    conn = get_connection()
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')

    query = '''
        SELECT t.*, o.receipt_number
        FROM inventory_transactions t
        LEFT JOIN orders o ON o.id = t.order_id
        WHERE t.inventory_item_id = ?
    '''
    params = [item_id]
    if pool:
        query += ' AND t.pool = ?'
        params.append(pool)
    if start_date:
        query += ' AND left(t.timestamp, 10) >= left(?, 10)'
        params.append(start_date)
    if end_date:
        query += ' AND left(t.timestamp, 10) <= left(?, 10)'
        params.append(end_date)
    query += ' ORDER BY t.timestamp ASC, t.id ASC'
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()

    entries = []
    for r in rows:
        unit_cost_after = r['unit_cost_after'] or 0
        entries.append({
            **r,
            'reason_label': KARDEX_REASON_LABELS_ES.get(r['reason'], r['reason']),
            'pool_label': KARDEX_POOL_LABELS_ES.get(r['pool'], r['pool']),
            'movement_value': round((r['quantity_change'] or 0) * unit_cost_after, 2),
            'value_before': round((r['stock_before'] or 0) * (r['unit_cost_before'] or 0), 2),
            'value_after': round((r['stock_after'] or 0) * unit_cost_after, 2),
        })
    return {'item': dict(item), 'entries': entries}


# --------------------------------------------------------------- Recipes ----

def get_recipe_for_menu_item(menu_item_id):
    conn = get_connection()
    rows = conn.execute('''
        SELECT r.id, r.menu_item_id, r.inventory_item_id, r.quantity_required,
               i.item_name, i.unit_of_measure
        FROM recipes r JOIN inventory_items i ON i.id = r.inventory_item_id
        WHERE r.menu_item_id = ?
        ORDER BY i.item_name
    ''', (menu_item_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_recipes():
    conn = get_connection()
    rows = conn.execute('''
        SELECT r.id, r.menu_item_id, m.name AS menu_item_name, r.inventory_item_id,
               r.quantity_required, i.item_name, i.unit_of_measure
        FROM recipes r
        JOIN menu_items m ON m.id = r.menu_item_id
        JOIN inventory_items i ON i.id = r.inventory_item_id
        ORDER BY m.name, i.item_name
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_recipe_line(menu_item_id, inventory_item_id, quantity_required):
    if quantity_required <= 0:
        raise ValueError('La cantidad requerida debe ser mayor que cero')
    conn = get_connection(write=True)
    menu_item = conn.execute('SELECT id FROM menu_items WHERE id = ?', (menu_item_id,)).fetchone()
    inv_item = conn.execute('SELECT id FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone()
    if not menu_item:
        conn.close()
        raise ValueError('Artículo del menú no encontrado')
    if not inv_item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO recipes (menu_item_id, inventory_item_id, quantity_required)
        VALUES (?, ?, ?)
    ''', (menu_item_id, inventory_item_id, quantity_required))
    conn.commit()
    line = dict(conn.execute('SELECT * FROM recipes WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return line


def delete_recipe_line(recipe_id):
    conn = get_connection(write=True)
    existing = conn.execute('SELECT id FROM recipes WHERE id = ?', (recipe_id,)).fetchone()
    if not existing:
        conn.close()
        raise ValueError('Línea de receta no encontrada')
    conn.execute('DELETE FROM recipes WHERE id = ?', (recipe_id,))
    conn.commit()
    conn.close()
    return {'id': recipe_id, 'deleted': True}


def _menu_item_pool(conn, item_name):
    """Drinks deplete the Bar pool; Food/Desserts deplete the Restaurant
    pool (current_stock) — they're never allowed to share a number."""
    menu_item = conn.execute('SELECT category FROM menu_items WHERE name = ?', (item_name,)).fetchone()
    if menu_item and menu_item['category'] == 'Drinks':
        return 'bar'
    return 'restaurant'


def _stock_column(pool):
    return {'bar': 'bar_stock', 'storeroom': 'storeroom_stock',
            'maintenance': 'maintenance_stock'}.get(pool, 'current_stock')


POOL_LABELS_ES = {'bar': 'el Bar', 'restaurant': 'el Restaurante', 'storeroom': 'la Bodega',
                  'maintenance': 'la Bodega de Mantenimiento'}


class InsufficientStockError(ValueError):
    """A movement would take a stock pool below zero. Subclasses ValueError so
    every route's existing `except ValueError` turns it into a 400 message."""


def _apply_inventory_movement(conn, inventory_item_id, pool, quantity_change, reason,
                               order_id=None, order_item_id=None, notes=None, unit_cost_override=None):
    """Single point that both changes a pool's stock and logs the resulting
    Kardex line, so the ledger can never drift from the actual balances.
    Every stock-mutating function in the system should go through this
    instead of updating inventory_items directly."""
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone()
    stock_col = _stock_column(pool)
    stock_before = item[stock_col]
    stock_after = stock_before + quantity_change
    if quantity_change < 0 and stock_after < 0:
        if stock_after > -1e-9:
            stock_after = 0.0   # float noise from e.g. 3 x 0.05, not a real shortage
        else:
            # Undo everything this request already wrote and close the
            # connection before refusing, so a half-saved order can't be left
            # behind and nothing keeps a lock until garbage collection gets to
            # it. Every caller lets this propagate straight out, so nothing
            # touches conn afterwards.
            conn.rollback()
            conn.close()
            raise InsufficientStockError(
                f"Sin existencias suficientes de {item['item_name']} en {POOL_LABELS_ES[pool]}: "
                f"quedan {max(stock_before, 0):g} {item['unit_of_measure']} y se necesitan "
                f"{-quantity_change:g}. Solicite una transferencia desde la bodega o registre una entrada."
            )
    unit_cost_before = item['unit_cost']
    unit_cost_after = unit_cost_override if unit_cost_override is not None else unit_cost_before

    conn.execute(f'UPDATE inventory_items SET {stock_col} = ? WHERE id = ?', (stock_after, inventory_item_id))
    if unit_cost_override is not None:
        conn.execute('UPDATE inventory_items SET unit_cost = ? WHERE id = ?', (unit_cost_after, inventory_item_id))

    conn.execute('''
        INSERT INTO inventory_transactions
            (inventory_item_id, order_id, order_item_id, quantity_change, reason, pool, notes,
             stock_before, stock_after, unit_cost_before, unit_cost_after)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (inventory_item_id, order_id, order_item_id, quantity_change, reason, pool, notes,
          stock_before, stock_after, unit_cost_before, unit_cost_after))


def _deplete_inventory_for_order_item(conn, order_id, order_item_id, item_name, quantity):
    """Depletes stock for exactly one order line, tagging each transaction
    with that line's id so a single item can be individually removed later
    without touching what the rest of the ticket already used."""
    menu_item = conn.execute('SELECT id FROM menu_items WHERE name = ?', (item_name,)).fetchone()
    if not menu_item:
        return
    pool = _menu_item_pool(conn, item_name)
    recipe_lines = conn.execute(
        'SELECT inventory_item_id, quantity_required FROM recipes WHERE menu_item_id = ?',
        (menu_item['id'],)
    ).fetchall()
    for line in recipe_lines:
        depletion = -1 * line['quantity_required'] * float(quantity)
        try:
            _apply_inventory_movement(conn, line['inventory_item_id'], pool, depletion, 'order_depletion',
                                       order_id=order_id, order_item_id=order_item_id)
        except InsufficientStockError as e:
            raise InsufficientStockError(f'No se puede vender {quantity} × {item_name}. {e}') from None


# ---------------------------------------------------------------- Rooms ----

def room_type_capacity(room_type, conn=None):
    """Live count of physical rooms of this type — the real inventory."""
    own_conn = conn is None
    conn = conn or get_connection()
    row = conn.execute('SELECT COUNT(*) AS c FROM rooms WHERE room_type = ?', (room_type,)).fetchone()
    count = row['c']
    if own_conn:
        conn.close()
    return count if count else ROOM_INVENTORY_COUNTS_FALLBACK.get(room_type, 0)


def list_rooms():
    conn = get_connection()
    rows = conn.execute('SELECT * FROM rooms ORDER BY room_type, LENGTH(room_number), room_number').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_available_rooms(room_type):
    """Rooms of this type not currently occupied by a checked-in guest or
    blocked for maintenance right now — the live pool the front desk can
    actually assign today."""
    conn = get_connection()
    today = datetime.now().date().isoformat()
    rows = conn.execute('''
        SELECT room_number FROM rooms
        WHERE room_type = ?
          AND room_number NOT IN (
              SELECT room_number FROM guests WHERE status = 'checked_in' AND room_number IS NOT NULL
          )
          AND room_number NOT IN (
              SELECT room_number FROM room_blocks
              WHERE unblocked_at IS NULL AND start_date <= ? AND (end_date IS NULL OR end_date > ?)
          )
        ORDER BY LENGTH(room_number), room_number
    ''', (room_type, today, today)).fetchall()
    conn.close()
    return [r['room_number'] for r in rows]


def _room_number_overlap_exists(conn, room_number, checkin_date, checkout_date, exclude_guest_id=None):
    """True if any reserved/checked-in guest already holds this exact room
    for a range overlapping [checkin_date, checkout_date), or the room is
    blocked (maintenance, etc.) for any part of that range."""
    query = '''
        SELECT id FROM guests
        WHERE room_number = ? AND status IN ('reserved', 'checked_in')
          AND checkin_date < ? AND checkout_date > ?
    '''
    params = [room_number, checkout_date, checkin_date]
    if exclude_guest_id:
        query += ' AND id != ?'
        params.append(exclude_guest_id)
    if conn.execute(query, params).fetchone() is not None:
        return True

    block = conn.execute('''
        SELECT id FROM room_blocks
        WHERE room_number = ? AND unblocked_at IS NULL
          AND start_date < ? AND (end_date IS NULL OR end_date > ?)
    ''', (room_number, checkout_date, checkin_date)).fetchone()
    return block is not None


def _find_available_room(conn, room_type, checkin_date, checkout_date):
    """First room of this type with no overlapping reservation/stay for the
    whole range, or None if every room of that type is taken."""
    rooms = conn.execute('SELECT room_number FROM rooms WHERE room_type = ? ORDER BY LENGTH(room_number), room_number', (room_type,)).fetchall()
    for room in rooms:
        if not _room_number_overlap_exists(conn, room['room_number'], checkin_date, checkout_date):
            return room['room_number']
    return None


def get_room_timeline(start_date, days):
    """Every room with the guest segments (reserved/checked-in/checked-out)
    overlapping [start_date, start_date + days) — the data behind the
    front-desk tape chart."""
    conn = get_connection()
    end_date = (datetime.strptime(start_date, '%Y-%m-%d').date() + timedelta(days=days)).isoformat()

    rooms = [dict(r) for r in conn.execute('SELECT * FROM rooms ORDER BY room_type, LENGTH(room_number), room_number').fetchall()]
    segments = conn.execute('''
        SELECT id, name, room_number, room_type, status, source, checkin_date, checkout_date,
               email, phone, nightly_rate, observations, agency, agency_id, rate_type, tax_exempt, tax_rate,
               room_subtotal, tax_amount, total_amount, commission_amount
        FROM guests
        WHERE room_number IS NOT NULL AND status != 'cancelled'
          AND checkin_date < ? AND checkout_date >= ?
        ORDER BY checkin_date
    ''', (end_date, start_date)).fetchall()
    blocks = conn.execute('''
        SELECT id, room_number, reason, start_date, end_date
        FROM room_blocks
        WHERE unblocked_at IS NULL
          AND start_date < ? AND (end_date IS NULL OR end_date > ?)
        ORDER BY start_date
    ''', (end_date, start_date)).fetchall()
    conn.close()

    dates = []
    d = datetime.strptime(start_date, '%Y-%m-%d').date()
    for _ in range(days):
        dates.append(d.isoformat())
        d += timedelta(days=1)

    return {
        'rooms': rooms,
        'dates': dates,
        'segments': [dict(s) for s in segments],
        'blocks': [dict(b) for b in blocks],
    }


def get_room_inventory_status():
    """Every physical room with its live occupancy — the real-time inventory board."""
    conn = get_connection()
    today = datetime.now().date().isoformat()
    rooms = conn.execute('SELECT * FROM rooms ORDER BY room_type, LENGTH(room_number), room_number').fetchall()
    occupied = {
        r['room_number']: dict(r)
        for r in conn.execute('''
            SELECT room_number, id, name, checkin_date, checkout_date
            FROM guests WHERE status = 'checked_in' AND room_number IS NOT NULL
        ''').fetchall()
    }
    blocked = {
        r['room_number']: dict(r)
        for r in conn.execute('''
            SELECT room_number, reason FROM room_blocks
            WHERE unblocked_at IS NULL AND start_date <= ? AND (end_date IS NULL OR end_date > ?)
        ''', (today, today)).fetchall()
    }
    conn.close()
    result = []
    for room in rooms:
        occ = occupied.get(room['room_number'])
        blk = blocked.get(room['room_number'])
        result.append({
            'room_number': room['room_number'],
            'room_type': room['room_type'],
            'occupied': occ is not None,
            'guest_id': occ['id'] if occ else None,
            'guest_name': occ['name'] if occ else None,
            'checkin_date': occ['checkin_date'] if occ else None,
            'checkout_date': occ['checkout_date'] if occ else None,
            'blocked': blk is not None,
            'block_reason': blk['reason'] if blk else None,
            'housekeeping_status': room['housekeeping_status'],
            'housekeeping_notes': room['housekeeping_notes'],
            'housekeeping_updated_at': room['housekeeping_updated_at'],
        })
    return result


def set_room_housekeeping_status(room_number, status, updated_by_user_id, notes=None):
    """Updates a room's cleaning/turnover status (Limpia/Sucia/Inspeccionada/
    Fuera de Servicio) — independent from whether it's bookable. This is
    purely a housekeeping signal EXCEPT for 'out_of_order', which also
    creates a room_blocks entry so the room can't be booked while under
    repair, and lifts that same entry (never someone else's block, for a
    different reason) once the room moves off 'out_of_order'."""
    if status not in HOUSEKEEPING_STATUSES:
        raise ValueError(f'El estado debe ser uno de {HOUSEKEEPING_STATUSES}')
    conn = get_connection(write=True)
    room = conn.execute('SELECT * FROM rooms WHERE room_number = ?', (room_number,)).fetchone()
    if not room:
        conn.close()
        raise ValueError(f'La habitación {room_number} no existe')
    was_out_of_order = room['housekeeping_status'] == 'out_of_order'

    conn.execute('''
        UPDATE rooms SET housekeeping_status = ?, housekeeping_notes = ?,
                          housekeeping_updated_at = now_local(), housekeeping_updated_by = ?
        WHERE room_number = ?
    ''', (status, notes, updated_by_user_id, room_number))
    _log_activity(conn, updated_by_user_id, 'housekeeping_status_changed',
                  f"Habitación {room_number} → {HOUSEKEEPING_STATUS_LABELS_ES[status]}" + (f" — {notes}" if notes else ''))

    active_block = conn.execute(
        'SELECT id, reason FROM room_blocks WHERE room_number = ? AND unblocked_at IS NULL', (room_number,)
    ).fetchone()

    conn.commit()
    updated = dict(conn.execute('SELECT * FROM rooms WHERE room_number = ?', (room_number,)).fetchone())
    conn.close()

    if status == 'out_of_order' and not was_out_of_order and not active_block:
        block_room(room_number, HOUSEKEEPING_BLOCK_REASON, datetime.now().date().isoformat(), updated_by_user_id)
    elif status != 'out_of_order' and was_out_of_order and active_block and active_block['reason'] == HOUSEKEEPING_BLOCK_REASON:
        unblock_room(active_block['id'], updated_by_user_id)

    return updated


def get_restaurant_table_status():
    """Every table across all restaurant areas with its open sub-accounts (up
    to MAX_SUB_ACCOUNTS_PER_TABLE independent tabs), who's serving each one,
    and the table's saved position on the floor-plan map (None until staff
    drag it into place) — the data behind the table-map views."""
    conn = get_connection()
    open_orders = conn.execute('''
        SELECT o.table_number, o.id, o.receipt_number, o.subtotal, o.timestamp, o.sub_account, o.account_name,
               o.guest_type, g.name AS guest_name, g.room_number,
               u.full_name AS server_name
        FROM orders o JOIN users u ON u.id = o.server_id
        LEFT JOIN guests g ON g.id = o.guest_id
        WHERE o.status = 'open' AND o.table_number IS NOT NULL
        ORDER BY o.sub_account
    ''').fetchall()
    accounts_by_table = {}
    for r in open_orders:
        accounts_by_table.setdefault(r['table_number'], []).append(dict(r))
    items_by_order = {}
    open_order_ids = [r['id'] for r in open_orders]
    if open_order_ids:
        placeholders = ','.join('?' * len(open_order_ids))
        for r in conn.execute(f'''
            SELECT order_id, item_name, quantity, unit_price, sides, doneness, notes FROM order_items
            WHERE order_id IN ({placeholders})
        ''', open_order_ids).fetchall():
            items_by_order.setdefault(r['order_id'], []).append(dict(r))
    positions = {
        r['table_number']: (r['pos_x'], r['pos_y'])
        for r in conn.execute('SELECT table_number, pos_x, pos_y FROM table_layout').fetchall()
    }
    conn.close()
    result = []
    for area in RESTAURANT_AREAS:
        for table in RESTAURANT_TABLES_BY_AREA[area]:
            accounts = accounts_by_table.get(table, [])
            for a in accounts:
                order_id = a.pop('id')
                a['order_id'] = order_id
                a['items'] = items_by_order.get(order_id, [])
            pos = positions.get(table)
            result.append({
                'area': area,
                'table_number': table,
                'occupied': len(accounts) > 0,
                'sub_accounts': accounts,
                'account_count': len(accounts),
                'max_accounts': MAX_SUB_ACCOUNTS_PER_TABLE,
                'pos_x': pos[0] if pos else None,
                'pos_y': pos[1] if pos else None,
            })
    return result


def set_table_position(table_number, pos_x, pos_y):
    """Saves where staff dragged a table icon to on its area's floor-plan map."""
    all_tables = {t for tables in RESTAURANT_TABLES_BY_AREA.values() for t in tables}
    if table_number not in all_tables:
        raise ValueError('Mesa desconocida')
    pos_x = max(0.0, min(100.0, float(pos_x)))
    pos_y = max(0.0, min(100.0, float(pos_y)))
    conn = get_connection(write=True)
    conn.execute('''
        INSERT INTO table_layout (table_number, pos_x, pos_y) VALUES (?, ?, ?)
        ON CONFLICT(table_number) DO UPDATE SET pos_x = excluded.pos_x, pos_y = excluded.pos_y
    ''', (table_number, pos_x, pos_y))
    conn.commit()
    conn.close()
    return {'table_number': table_number, 'pos_x': pos_x, 'pos_y': pos_y}


# ------------------------------------------------- Rates, seasons, agencies

SEASONS = ('low', 'high')
SEASON_LABELS_ES = {'low': 'Temporada Baja', 'high': 'Temporada Alta'}
DAY_TYPES = ('week', 'weekend')
DAY_TYPE_LABELS_ES = {'week': 'Semana', 'weekend': 'Fin de semana'}
RATE_TYPES = ('rack', 'net', 'manual')
RATE_TYPE_LABELS_ES = {'rack': 'Rack', 'net': 'Neta (agencia)', 'manual': 'Manual'}
COMMISSION_FIELDS = tuple(f'commission_{s}_{d}' for s in SEASONS for d in DAY_TYPES)
COMMISSION_CHOICES = (0, 10, 20)
DEFAULT_TAX_RATE = 15.0          # IVA
# Nights billed at the weekend rate, by weekday of the night (Monday = 0):
# Thursday, Friday and Saturday nights — a Thursday-to-Sunday stay.
DEFAULT_WEEKEND_NIGHTS = (3, 4, 5)


def _parse_date(value, label):
    try:
        return datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(f'{label}: fecha inválida')


def get_pricing_settings(conn=None):
    own = conn is None
    conn = conn or get_connection()
    rows = {r['key']: r['value'] for r in conn.execute(
        "SELECT key, value FROM app_settings WHERE key IN ('tax_rate_pct', 'weekend_nights')").fetchall()}
    if own:
        conn.close()
    weekend = rows.get('weekend_nights')
    return {
        'tax_rate': float(rows['tax_rate_pct']) if rows.get('tax_rate_pct') else DEFAULT_TAX_RATE,
        'weekend_nights': [int(d) for d in weekend.split(',') if d] if weekend is not None else list(DEFAULT_WEEKEND_NIGHTS),
    }


def save_pricing_settings(tax_rate, weekend_nights, user_id):
    try:
        tax_rate = float(tax_rate)
    except (TypeError, ValueError):
        raise ValueError('El impuesto debe ser un número')
    if not 0 <= tax_rate <= 100:
        raise ValueError('El impuesto debe estar entre 0 y 100 %')
    nights = sorted({int(d) for d in weekend_nights})
    if any(d < 0 or d > 6 for d in nights):
        raise ValueError('Días de fin de semana inválidos')
    conn = get_connection(write=True)
    for key, value in (('tax_rate_pct', str(tax_rate)), ('weekend_nights', ','.join(map(str, nights)))):
        conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    _log_activity(conn, user_id, 'pricing_settings_changed', f'Impuesto {tax_rate:g}% · noches de fin de semana {nights}')
    conn.commit()
    conn.close()
    return get_pricing_settings()


def get_room_rates(conn=None):
    """{room_type: {season: {day_type: {'rack': x, 'net': y}}}}"""
    own = conn is None
    conn = conn or get_connection()
    rows = conn.execute('SELECT * FROM room_rates').fetchall()
    if own:
        conn.close()
    grid = {}
    for r in rows:
        grid.setdefault(r['room_type'], {}).setdefault(r['season'], {})[r['day_type']] = {'rack': r['rack'], 'net': r['net']}
    return grid


def base_rates():
    """Standard 'from' price per room type: Rack, low season, weeknight."""
    grid = get_room_rates()
    return {rt: grid.get(rt, {}).get('low', {}).get('week', {}).get('rack', NIGHTLY_RATES[rt]) for rt in ROOM_TYPES}


def save_room_rates(rates, user_id):
    """rates: [{room_type, season, day_type, rack, net}, ...]"""
    clean = []
    for r in rates:
        if r.get('room_type') not in ROOM_TYPES or r.get('season') not in SEASONS or r.get('day_type') not in DAY_TYPES:
            raise ValueError('Fila de tarifa inválida')
        try:
            rack, net = float(r['rack']), float(r['net'])
        except (TypeError, ValueError, KeyError):
            raise ValueError(f"Tarifa inválida para {ROOM_TYPE_LABELS[r['room_type']]}")
        if rack < 0 or net < 0:
            raise ValueError('Las tarifas no pueden ser negativas')
        clean.append((r['room_type'], r['season'], r['day_type'], round(rack, 2), round(net, 2)))
    conn = get_connection(write=True)
    for row in clean:
        conn.execute('INSERT INTO room_rates (room_type, season, day_type, rack, net) VALUES (?, ?, ?, ?, ?) '
                     'ON CONFLICT (room_type, season, day_type) DO UPDATE SET rack = excluded.rack, net = excluded.net', row)
    _log_activity(conn, user_id, 'room_rates_changed', f'Actualizó {len(clean)} tarifas')
    conn.commit()
    conn.close()
    return get_room_rates()


def list_high_seasons(conn=None):
    own = conn is None
    conn = conn or get_connection()
    rows = [dict(r) for r in conn.execute('SELECT * FROM high_season_periods ORDER BY start_date').fetchall()]
    if own:
        conn.close()
    return rows


def add_high_season(name, start_date, end_date, user_id):
    name = (name or '').strip()
    if not name:
        raise ValueError('Escriba un nombre para la temporada (ej. Semana Santa)')
    start, end = _parse_date(start_date, 'Inicio'), _parse_date(end_date, 'Fin')
    if end < start:
        raise ValueError('La fecha final debe ser igual o posterior a la inicial')
    conn = get_connection(write=True)
    cur = conn.execute('INSERT INTO high_season_periods (name, start_date, end_date) VALUES (?, ?, ?)',
                       (name, start.isoformat(), end.isoformat()))
    _log_activity(conn, user_id, 'high_season_added', f'{name}: {start.isoformat()} a {end.isoformat()}')
    conn.commit()
    period = dict(conn.execute('SELECT * FROM high_season_periods WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return period


def delete_high_season(period_id, user_id):
    conn = get_connection(write=True)
    period = conn.execute('SELECT * FROM high_season_periods WHERE id = ?', (period_id,)).fetchone()
    if not period:
        conn.close()
        raise ValueError('Temporada no encontrada')
    conn.execute('DELETE FROM high_season_periods WHERE id = ?', (period_id,))
    _log_activity(conn, user_id, 'high_season_deleted', f"{period['name']}: {period['start_date']} a {period['end_date']}")
    conn.commit()
    conn.close()
    return {'id': period_id, 'deleted': True}


def list_agencies(active_only=False):
    conn = get_connection()
    query = 'SELECT * FROM agencies' + (' WHERE is_active = 1' if active_only else '') + ' ORDER BY is_active DESC, name'
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


def get_agency(agency_id, conn=None):
    own = conn is None
    conn = conn or get_connection()
    row = conn.execute('SELECT * FROM agencies WHERE id = ?', (agency_id,)).fetchone()
    if own:
        conn.close()
    return dict(row) if row else None


def save_agency(data, user_id, agency_id=None):
    """Creates (agency_id None) or updates an agency. data: name, contact_name,
    phone, email, notes, commission_<season>_<day_type> (percent), is_active."""
    name = ' '.join((data.get('name') or '').split())
    if not name:
        raise ValueError('El nombre de la agencia es obligatorio')
    commissions = {}
    for field in COMMISSION_FIELDS:
        try:
            pct = float(data.get(field, 10))
        except (TypeError, ValueError):
            raise ValueError('La comisión debe ser un número')
        if not 0 <= pct <= 100:
            raise ValueError('La comisión debe estar entre 0 y 100 %')
        commissions[field] = pct
    text = {k: (data.get(k) or '').strip() or None for k in ('contact_name', 'phone', 'email', 'notes')}
    conn = get_connection(write=True)
    dup = conn.execute('SELECT id FROM agencies WHERE lower(name) = lower(?) AND id != ?', (name, agency_id or 0)).fetchone()
    if dup:
        conn.close()
        raise ValueError(f'Ya existe una agencia llamada {name}')
    cols = {'name': name, **text, **commissions}
    if agency_id is None:
        cur = conn.execute(f"INSERT INTO agencies ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                           tuple(cols.values()))
        agency_id = cur.lastrowid
        _log_activity(conn, user_id, 'agency_created', name)
    else:
        if not conn.execute('SELECT 1 FROM agencies WHERE id = ?', (agency_id,)).fetchone():
            conn.close()
            raise ValueError('Agencia no encontrada')
        if 'is_active' in data:
            cols['is_active'] = 1 if data['is_active'] else 0
        conn.execute(f"UPDATE agencies SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                     (*cols.values(), agency_id))
        # Reservations show the agency by name; keep it current.
        conn.execute('UPDATE guests SET agency = ? WHERE agency_id = ?', (name, agency_id))
        _log_activity(conn, user_id, 'agency_updated', name)
    conn.commit()
    agency = get_agency(agency_id, conn=conn)
    conn.close()
    return agency


def quote_stay(room_type, checkin_date, checkout_date, rate_type='rack', agency_id=None,
               manual_rate=None, tax_exempt=False, conn=None):
    """Prices a stay night by night: each night's season (high if it falls in
    a high-season period) and day type (weekend nights per settings) pick its
    Rack or Net rate — or the manual rate for every night. Tax applies to the
    room subtotal unless tax_exempt. An agency booking at Rack (or a manual
    rate) earns its commission % for that season/day type; Net rates carry
    no commission (the agency's margin is built in)."""
    if room_type not in ROOM_TYPES:
        raise ValueError('Tipo de habitación inválido')
    if rate_type not in RATE_TYPES:
        raise ValueError('Tipo de tarifa inválido')
    start, end = _parse_date(checkin_date, 'Entrada'), _parse_date(checkout_date, 'Salida')
    if end <= start:
        raise ValueError('La fecha de salida debe ser posterior a la fecha de entrada')
    own = conn is None
    conn = conn or get_connection()
    try:
        agency = None
        if agency_id:
            agency = get_agency(int(agency_id), conn=conn)
            if not agency:
                raise ValueError('Agencia no encontrada')
        if rate_type == 'net' and not agency:
            raise ValueError('La tarifa Neta es solo para reservas de agencia: seleccione la agencia')
        if rate_type == 'manual':
            try:
                manual_rate = float(manual_rate)
            except (TypeError, ValueError):
                raise ValueError('Ingrese la tarifa manual por noche')
            if manual_rate < 0:
                raise ValueError('La tarifa por noche no puede ser negativa')
        settings = get_pricing_settings(conn)
        grid = get_room_rates(conn).get(room_type, {})
        periods = [(p['start_date'], p['end_date']) for p in list_high_seasons(conn)]
    finally:
        if own:
            conn.close()

    nights = []
    night = start
    while night < end:
        iso = night.isoformat()
        season = 'high' if any(s <= iso <= e for s, e in periods) else 'low'
        day_type = 'weekend' if night.weekday() in settings['weekend_nights'] else 'week'
        if rate_type == 'manual':
            rate = manual_rate
        else:
            rate = grid.get(season, {}).get(day_type, {}).get(rate_type, NIGHTLY_RATES[room_type])
        pct = agency[f'commission_{season}_{day_type}'] if agency and rate_type in ('rack', 'manual') else 0
        nights.append({'date': iso, 'season': season, 'day_type': day_type, 'rate': round(rate, 2), 'commission_pct': pct})
        night += timedelta(days=1)

    subtotal = round(sum(n['rate'] for n in nights), 2)
    tax_rate = 0.0 if tax_exempt else settings['tax_rate']
    tax = round(subtotal * tax_rate / 100, 2)
    return {
        'room_type': room_type, 'rate_type': rate_type,
        'agency_id': agency['id'] if agency else None, 'agency_name': agency['name'] if agency else None,
        'nights': nights, 'night_count': len(nights),
        'average_rate': round(subtotal / len(nights), 2),
        'subtotal': subtotal, 'tax_exempt': bool(tax_exempt), 'tax_rate': tax_rate,
        'tax': tax, 'total': round(subtotal + tax, 2),
        'commission': round(sum(n['rate'] * n['commission_pct'] / 100 for n in nights), 2),
    }


def _pricing_columns(quote):
    """guests columns that store a quote."""
    return {
        'nightly_rate': quote['average_rate'], 'room_subtotal': quote['subtotal'],
        'tax_rate': quote['tax_rate'], 'tax_amount': quote['tax'], 'total_amount': quote['total'],
        'commission_amount': quote['commission'], 'rate_type': quote['rate_type'],
        'agency_id': quote['agency_id'], 'agency': quote['agency_name'],
        'tax_exempt': 1 if quote['tax_exempt'] else 0, 'rate_breakdown': json.dumps(quote['nights']),
    }


def update_guest_pricing(guest_id, user_id, rate_type=None, agency_id=..., manual_rate=None, tax_exempt=None):
    """Re-prices a reservation or in-house stay: change its agency, rate type
    or tax exemption (only the arguments given change). The nights are
    re-quoted at today's rates, except a manual rate which stays as is."""
    conn = get_connection(write=True)
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Huésped no encontrado')
    if guest['status'] not in ('reserved', 'checked_in'):
        conn.close()
        raise ValueError('Solo se puede cambiar la tarifa de una reserva o de un huésped en casa')
    rate_type = rate_type or guest['rate_type']
    agency_id = guest['agency_id'] if agency_id is ... else (agency_id or None)
    if rate_type == 'manual' and manual_rate is None:
        manual_rate = guest['nightly_rate']
    tax_exempt = bool(guest['tax_exempt']) if tax_exempt is None else bool(tax_exempt)
    try:
        quote = quote_stay(guest['room_type'], guest['checkin_date'], guest['checkout_date'], rate_type,
                           agency_id, manual_rate, tax_exempt, conn=conn)
    except ValueError:
        conn.close()
        raise
    cols = _pricing_columns(quote)
    conn.execute(f"UPDATE guests SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), guest_id))
    details = (f"{guest['name']}: {RATE_TYPE_LABELS_ES[rate_type]}"
               f"{' · ' + quote['agency_name'] if quote['agency_name'] else ''}"
               f"{' · exonerado de impuestos' if tax_exempt else ''} · total C${quote['total']:,.2f}")
    _log_activity(conn, user_id, 'pricing_changed', details)
    conn.commit()
    updated = get_guest(guest_id, conn=conn)
    conn.close()
    return updated


# -------------------------------------------------------------- Guests ----

def _nights(checkin_date, checkout_date):
    fmt = '%Y-%m-%d'
    d1 = datetime.strptime(checkin_date, fmt).date()
    d2 = datetime.strptime(checkout_date, fmt).date()
    return max((d2 - d1).days, 1)


def find_agency_by_name(name):
    """The registered agency with this name (any case), or None."""
    conn = get_connection()
    row = conn.execute('SELECT * FROM agencies WHERE lower(name) = lower(?)', (name.strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def check_in_guest(name, room_number, room_type, checkin_date, checkout_date, nightly_rate=None,
                    observations=None, id_number=None, nationality=None, address=None,
                    rate_code=None, adults=None, children=None,
                    country=None, city=None, postal_code=None, booking_reference=None,
                    rate_type='rack', agency_id=None, tax_exempt=False, created_by=None, agency_name=None):
    """A walk-in arriving now. nightly_rate is a manual rate for every night
    — callers (app.py) must only forward it when the requester is an admin;
    it's a discount tool for special guests / family, not something front
    desk can self-serve. Otherwise the stay is priced from the rate grid."""
    if room_type not in ROOM_TYPES:
        raise ValueError(f"room_type must be one of {ROOM_TYPES}")
    if checkin_date > datetime.now().date().isoformat():
        raise ValueError('Para una llegada futura use «Reservar»; el Check-In se hace el día de llegada')
    if nightly_rate is not None:
        rate_type = 'manual'

    conn = get_connection(write=True)
    room = conn.execute('SELECT * FROM rooms WHERE room_number = ?', (room_number,)).fetchone()
    if not room:
        conn.close()
        raise ValueError(f"La habitación {room_number} no existe")
    if room['room_type'] != room_type:
        conn.close()
        raise ValueError(f"La habitación {room_number} es {ROOM_TYPE_LABELS[room['room_type']]}, no {ROOM_TYPE_LABELS[room_type]}")

    if _room_number_overlap_exists(conn, room_number, checkin_date, checkout_date):
        conn.close()
        raise ValueError(f"La habitación {room_number} ya está ocupada, reservada o bloqueada para parte de ese período")

    try:
        quote = quote_stay(room_type, checkin_date, checkout_date, rate_type, agency_id, nightly_rate, tax_exempt, conn=conn)
    except ValueError:
        conn.close()
        raise
    pricing = _pricing_columns(quote)
    if not pricing['agency'] and agency_name:
        pricing['agency'] = agency_name
    cur = conn.cursor()
    cur.execute(f'''
        INSERT INTO guests (name, room_number, room_type, status, checkin_date, checkout_date,
                             source, observations, id_number, nationality, address,
                             rate_code, adults, children, country, city, postal_code, booking_reference,
                             created_at, created_by, {', '.join(pricing)})
        VALUES (?, ?, ?, 'checked_in', ?, ?, 'walk_in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now_local(), ?,
                {', '.join('?' for _ in pricing)})
    ''', (name, room_number, room_type, checkin_date, checkout_date, observations, id_number, nationality, address,
          rate_code, adults, children, country, city, postal_code, booking_reference, created_by, *pricing.values()))
    conn.commit()
    guest = dict(conn.execute('SELECT * FROM guests WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return guest


def update_guest_details(guest_id, observations=None, id_number=None, nationality=None, address=None,
                          rate_code=None, adults=None, children=None,
                          country=None, city=None, postal_code=None, booking_reference=None):
    """Free-form edits to a reservation/stay's profile — observations and the
    registration-card identification fields. Only provided fields change.
    (The agency goes with the price: update_guest_pricing.)"""
    conn = get_connection(write=True)
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Huésped no encontrado')
    conn.execute('''
        UPDATE guests SET
            observations = COALESCE(?, observations),
            id_number = COALESCE(?, id_number),
            nationality = COALESCE(?, nationality),
            address = COALESCE(?, address),
            rate_code = COALESCE(?, rate_code),
            adults = COALESCE(?, adults),
            children = COALESCE(?, children),
            country = COALESCE(?, country),
            city = COALESCE(?, city),
            postal_code = COALESCE(?, postal_code),
            booking_reference = COALESCE(?, booking_reference)
        WHERE id = ?
    ''', (observations, id_number, nationality, address, rate_code, adults, children,
          country, city, postal_code, booking_reference, guest_id))
    conn.commit()
    updated = get_guest(guest_id, conn=conn)
    conn.close()
    return updated


def change_guest_room(guest_id, new_room_number, changed_by_user_id, reason=None):
    """Moves a reserved or checked-in guest to a different room — any room
    the admin or front desk picks, including a different room type
    (an upgrade/downgrade); the rate is untouched unless adjusted separately."""
    conn = get_connection(write=True)
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Huésped no encontrado')
    if guest['status'] not in ('reserved', 'checked_in'):
        conn.close()
        raise ValueError('Solo un huésped reservado o registrado puede ser movido a otra habitación')
    if new_room_number == guest['room_number']:
        conn.close()
        raise ValueError('El huésped ya está en esa habitación')

    room = conn.execute('SELECT * FROM rooms WHERE room_number = ?', (new_room_number,)).fetchone()
    if not room:
        conn.close()
        raise ValueError(f'La habitación {new_room_number} no existe')

    if _room_number_overlap_exists(conn, new_room_number, guest['checkin_date'], guest['checkout_date'],
                                    exclude_guest_id=guest_id):
        conn.close()
        raise ValueError(f'La habitación {new_room_number} ya está ocupada, reservada o bloqueada para parte de ese período')

    old_room = guest['room_number'] or '(ninguna asignada)'
    conn.execute('UPDATE guests SET room_number = ?, room_type = ? WHERE id = ?',
                 (new_room_number, room['room_type'], guest_id))
    details = f"{guest['name']}: Habitación {old_room} -> Habitación {new_room_number}"
    if reason:
        details += f" — {reason}"
    _log_activity(conn, changed_by_user_id, 'room_changed', details)
    conn.commit()
    updated = get_guest(guest_id, conn=conn)
    conn.close()
    return updated


# ------------------------------------------------------------ Room blocks --

def block_room(room_number, reason, start_date, blocked_by_user_id, end_date=None):
    if not reason:
        raise ValueError('Se requiere un motivo para bloquear una habitación')
    conn = get_connection(write=True)
    room = conn.execute('SELECT * FROM rooms WHERE room_number = ?', (room_number,)).fetchone()
    if not room:
        conn.close()
        raise ValueError(f'La habitación {room_number} no existe')
    if end_date and end_date <= start_date:
        conn.close()
        raise ValueError('La fecha de fin debe ser posterior a la fecha de inicio')

    conflict = conn.execute('''
        SELECT id FROM guests
        WHERE room_number = ? AND status IN ('reserved', 'checked_in')
          AND checkin_date < ? AND checkout_date > ?
    ''', (room_number, end_date or '9999-12-31', start_date)).fetchone()
    if conflict:
        conn.close()
        raise ValueError(f'La habitación {room_number} tiene un huésped reservado o registrado durante ese período')

    cur = conn.cursor()
    cur.execute('''
        INSERT INTO room_blocks (room_number, start_date, end_date, reason, blocked_by)
        VALUES (?, ?, ?, ?, ?)
    ''', (room_number, start_date, end_date, reason, blocked_by_user_id))
    until = f" hasta {end_date}" if end_date else ' (indefinido, hasta ser desbloqueada)'
    _log_activity(conn, blocked_by_user_id, 'room_blocked', f"Habitación {room_number} bloqueada desde {start_date}{until} — {reason}")
    conn.commit()
    block = dict(conn.execute('SELECT * FROM room_blocks WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return block


def unblock_room(block_id, unblocked_by_user_id):
    conn = get_connection(write=True)
    block = conn.execute('SELECT * FROM room_blocks WHERE id = ?', (block_id,)).fetchone()
    if not block:
        conn.close()
        raise ValueError('Bloqueo no encontrado')
    if block['unblocked_at']:
        conn.close()
        raise ValueError('Este bloqueo ya fue levantado')
    conn.execute('''
        UPDATE room_blocks SET unblocked_at = now_local(), unblocked_by = ?
        WHERE id = ?
    ''', (unblocked_by_user_id, block_id))
    _log_activity(conn, unblocked_by_user_id, 'room_unblocked', f"Habitación {block['room_number']} desbloqueada")
    conn.commit()
    conn.close()
    return {'id': block_id, 'unblocked': True}


def list_room_blocks(active_only=False):
    conn = get_connection()
    query = '''
        SELECT b.*, u1.full_name AS blocked_by_name, u2.full_name AS unblocked_by_name
        FROM room_blocks b
        LEFT JOIN users u1 ON u1.id = b.blocked_by
        LEFT JOIN users u2 ON u2.id = b.unblocked_by
    '''
    if active_only:
        query += " WHERE b.unblocked_at IS NULL AND (b.end_date IS NULL OR b.end_date >= today_local())"
    query += ' ORDER BY b.start_date DESC, b.id DESC'
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def adjust_guest_rate(guest_id, new_rate, admin_user_id, reason=None):
    """Admin-only rate override/discount for an active stay — logged for audit."""
    if new_rate < 0:
        raise ValueError('La nueva tarifa no puede ser negativa')

    conn = get_connection(write=True)
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Huésped no encontrado')
    if guest['status'] not in ('reserved', 'checked_in'):
        conn.close()
        raise ValueError('La tarifa solo puede ajustarse para un huésped reservado o actualmente registrado')

    old_rate = guest['nightly_rate']
    # A rate set by hand becomes the Manual rate for every night; agency and
    # tax exemption stay as they were.
    try:
        quote = quote_stay(guest['room_type'], guest['checkin_date'], guest['checkout_date'], 'manual',
                           guest['agency_id'], new_rate, bool(guest['tax_exempt']), conn=conn)
    except ValueError:
        conn.close()
        raise
    cols = _pricing_columns(quote)
    conn.execute(f"UPDATE guests SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), guest_id))
    conn.execute('''
        INSERT INTO rate_adjustments (guest_id, old_rate, new_rate, reason, admin_user_id)
        VALUES (?, ?, ?, ?, ?)
    ''', (guest_id, old_rate, new_rate, reason, admin_user_id))
    details = f"{guest['name']} (Habitación {guest['room_number']}): C${old_rate:,.2f} -> C${new_rate:,.2f}"
    if reason:
        details += f" — {reason}"
    _log_activity(conn, admin_user_id, 'rate_adjusted', details)
    conn.commit()
    updated = get_guest(guest_id, conn=conn)
    conn.close()
    return updated


def get_guest(guest_id, conn=None):
    own_conn = conn is None
    conn = conn or get_connection()
    row = conn.execute('SELECT * FROM guests WHERE id = ?', (guest_id,)).fetchone()
    if own_conn:
        conn.close()
    return dict(row) if row else None


def list_guests(status=None):
    conn = get_connection()
    if status:
        rows = conn.execute('SELECT * FROM guests WHERE status = ? ORDER BY checkin_date ASC', (status,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM guests ORDER BY id DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_guest_folio(guest_id):
    conn = get_connection()
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Huésped no encontrado')

    room_charge = guest['room_subtotal']
    room_tax = guest['tax_amount']
    room_total = guest['total_amount']

    order_rows = conn.execute(
        'SELECT * FROM orders WHERE guest_id = ? ORDER BY timestamp ASC', (guest_id,)
    ).fetchall()

    orders = [_hydrate_order(conn, o) for o in order_rows]
    open_orders = [o for o in orders if o['status'] == 'open']

    billed_to_room_total = round(sum(
        o['balance_due'] for o in orders if o['status'] == 'closed' and o['payment_method'] == 'room_charge'
    ), 2)
    paid_upfront_total = round(sum(
        o['balance_due'] for o in orders if o['status'] == 'closed' and o['payment_method'] in ('cash', 'card')
    ), 2)

    rate_history = [dict(r) for r in conn.execute(
        'SELECT * FROM rate_adjustments WHERE guest_id = ? ORDER BY created_at ASC', (guest_id,)
    ).fetchall()]

    conn.close()

    balance_due = round(room_total + billed_to_room_total, 2)

    return {
        'guest': guest,
        'room_charge': room_charge,
        'room_tax': room_tax,
        'room_total': room_total,
        'nights': json.loads(guest['rate_breakdown']) if guest['rate_breakdown'] else [],
        'orders': orders,
        'open_orders_count': len(open_orders),
        'restaurant_billed_to_room_total': billed_to_room_total,
        'restaurant_paid_upfront_total': paid_upfront_total,
        'balance_due': balance_due,
        'rate_history': rate_history,
    }


def checkout_guest(guest_id, checked_out_by_user_id=None):
    folio = get_guest_folio(guest_id)
    if folio['guest']['status'] == 'checked_out':
        raise ValueError('La salida del huésped ya fue registrada')
    if folio['guest']['status'] == 'reserved':
        raise ValueError('El huésped aún no ha registrado su entrada')

    conn = get_connection(write=True)
    conn.execute(
        "UPDATE guests SET status = 'checked_out', checked_out_at = now_local() WHERE id = ?",
        (guest_id,)
    )
    conn.commit()
    guest = get_guest(guest_id, conn=conn)
    conn.close()

    folio['guest'] = guest

    # The room now needs cleaning before it can be sold again.
    if guest['room_number']:
        set_room_housekeeping_status(guest['room_number'], 'dirty', checked_out_by_user_id)

    return folio


# ------------------------------------------------------- Reservations ----

def _room_type_first_full_night(conn, room_type, checkin_date, checkout_date, exclude_guest_id=None):
    """Returns the first date within [checkin_date, checkout_date) where
    room_type has no remaining capacity, or None if the whole range is free."""
    capacity = room_type_capacity(room_type, conn=conn)
    d1 = datetime.strptime(checkin_date, '%Y-%m-%d').date()
    d2 = datetime.strptime(checkout_date, '%Y-%m-%d').date()
    night = d1
    while night < d2:
        night_str = night.isoformat()
        query = '''
            SELECT COUNT(*) AS c FROM guests
            WHERE room_type = ? AND status IN ('reserved', 'checked_in')
              AND checkin_date <= ? AND checkout_date > ?
        '''
        params = [room_type, night_str, night_str]
        if exclude_guest_id:
            query += ' AND id != ?'
            params.append(exclude_guest_id)
        count = conn.execute(query, params).fetchone()['c']
        if count >= capacity:
            return night_str
        night = night + timedelta(days=1)
    return None


def create_reservation(name, room_type, checkin_date, checkout_date, email=None, phone=None,
                        room_number=None, nightly_rate=None, observations=None, source='web_booking',
                        rate_type='rack', agency_id=None, tax_exempt=False,
                        rate_code=None, adults=None, children=None, country=None, city=None,
                        postal_code=None, booking_reference=None, created_by=None, agency_name=None):
    """room_number/nightly_rate let front desk/admin book a specific room and
    a manual rate directly (the public booking portal leaves both to be
    assigned automatically at the Rack rate). agency_id/rate_type/tax_exempt
    price agency bookings (Net rate or commission) and tax-exempt guests."""
    if nightly_rate is not None:
        rate_type = 'manual'
    if room_type not in ROOM_TYPES:
        raise ValueError(f"room_type must be one of {ROOM_TYPES}")
    if not name:
        raise ValueError('El nombre del huésped es obligatorio')
    if checkout_date <= checkin_date:
        raise ValueError('La fecha de salida debe ser posterior a la fecha de entrada')

    conn = get_connection(write=True)
    if room_number:
        room = conn.execute('SELECT * FROM rooms WHERE room_number = ?', (room_number,)).fetchone()
        if not room:
            conn.close()
            raise ValueError(f'La habitación {room_number} no existe')
        if room['room_type'] != room_type:
            conn.close()
            raise ValueError(f"La habitación {room_number} es {ROOM_TYPE_LABELS[room['room_type']]}, no {ROOM_TYPE_LABELS[room_type]}")
        if _room_number_overlap_exists(conn, room_number, checkin_date, checkout_date):
            conn.close()
            raise ValueError(f"La habitación {room_number} ya está ocupada, reservada o bloqueada para parte de ese período")
    else:
        room_number = _find_available_room(conn, room_type, checkin_date, checkout_date)
        if not room_number:
            blocked_date = _room_type_first_full_night(conn, room_type, checkin_date, checkout_date)
            conn.close()
            raise ValueError(f'{ROOM_TYPE_LABELS[room_type]} está completamente reservado el {blocked_date or checkin_date}')

    try:
        quote = quote_stay(room_type, checkin_date, checkout_date, rate_type, agency_id, nightly_rate, tax_exempt, conn=conn)
    except ValueError:
        conn.close()
        raise
    pricing = _pricing_columns(quote)
    if not pricing['agency'] and agency_name:
        pricing['agency'] = agency_name
    cur = conn.cursor()
    cur.execute(f'''
        INSERT INTO guests (name, room_number, room_type, status, checkin_date, checkout_date,
                             email, phone, source, observations,
                             rate_code, adults, children, country, city, postal_code, booking_reference,
                             created_at, created_by, {', '.join(pricing)})
        VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now_local(), ?,
                {', '.join('?' for _ in pricing)})
    ''', (name, room_number, room_type, checkin_date, checkout_date, email, phone, source, observations,
          rate_code, adults, children, country, city, postal_code, booking_reference, created_by,
          *pricing.values()))
    conn.commit()
    guest = dict(conn.execute('SELECT * FROM guests WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return guest


def get_booking_group(guest):
    """Every room booked together with this stay — same Referencia and same
    agency (or both direct) — so one confirmation / rooming list covers the
    whole group, like the paper rooming lists. Cancelled rooms are left out
    unless the stay itself is cancelled. Without a Referencia it's just the
    stay itself."""
    ref = (guest.get('booking_reference') or '').strip()
    if not ref:
        return [guest]
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM guests
        WHERE TRIM(booking_reference) = ? AND agency_id IS NOT DISTINCT FROM ?
          AND (status != 'cancelled' OR id = ?)
        ORDER BY id
    ''', (ref, guest.get('agency_id'), guest['id'])).fetchall()
    conn.close()
    return [dict(r) for r in rows] or [guest]


def list_reservations():
    return list_guests(status='reserved')


ROOM_LIST_KINDS = {'arrivals': 'Llegadas', 'in_house': 'Huéspedes En Casa', 'departures': 'Salidas'}


def room_list(kind, day, agency_id=None):
    """Front-desk operating lists for one day ('YYYY-MM-DD'):
    arrivals   — stays starting that day (confirmed or already checked in);
    in_house   — guests staying that night (for today: everyone in house now);
    departures — stays ending that day (still in house or already out)."""
    if kind not in ROOM_LIST_KINDS:
        raise ValueError('Listado inválido')
    day = _parse_date(day, 'Fecha').isoformat()
    if kind == 'arrivals':
        where, params = "checkin_date = ? AND status IN ('reserved', 'checked_in', 'checked_out')", [day]
    elif kind == 'departures':
        where, params = "checkout_date = ? AND status IN ('checked_in', 'checked_out')", [day]
    elif day == datetime.now().date().isoformat():
        where, params = "status = 'checked_in'", []
    else:
        where, params = "checkin_date <= ? AND checkout_date > ? AND status IN ('checked_in', 'checked_out')", [day, day]
    if agency_id:
        where += ' AND agency_id = ?'
        params.append(int(agency_id))
    conn = get_connection()
    rows = conn.execute(f'''
        SELECT * FROM guests WHERE {where}
        ORDER BY NULLIF(regexp_replace(room_number, '\\D', '', 'g'), '')::int NULLS LAST, room_number, name
    ''', params).fetchall()
    conn.close()
    guests = [dict(r) for r in rows]
    return {
        'kind': kind, 'title': ROOM_LIST_KINDS[kind], 'date': day, 'guests': guests,
        'totals': {
            'count': len(guests),
            'pax': sum((g['adults'] or 0) + (g['children'] or 0) for g in guests),
            'subtotal': round(sum(g['room_subtotal'] or 0 for g in guests), 2),
            'tax': round(sum(g['tax_amount'] or 0 for g in guests), 2),
            'total': round(sum(g['total_amount'] or 0 for g in guests), 2),
        },
    }


def checkin_reservation(guest_id, room_number=None, id_number=None, nationality=None,
                         address=None, observations=None,
                         rate_code=None, adults=None, children=None,
                         country=None, city=None, postal_code=None, booking_reference=None):
    """Check-In: flips a confirmed reservation to in-house, from its arrival
    day on. A room was already assigned at booking time; pass room_number
    only to reassign (e.g. maintenance issue). id_number/nationality/address/
    observations capture the registration-card details at the moment of
    check-in."""
    conn = get_connection(write=True)
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Reservación no encontrada')
    if guest['status'] != 'reserved':
        conn.close()
        raise ValueError('Esta reservación no está confirmada (ya entró, salió o fue cancelada)')
    if guest['checkin_date'] > datetime.now().date().isoformat():
        conn.close()
        arrival = datetime.strptime(guest['checkin_date'], '%Y-%m-%d').strftime('%d/%m/%Y')
        raise ValueError(f'La llegada de esta reservación es el {arrival}; el Check-In se habilita ese día')

    target_room = room_number or guest['room_number']
    if not target_room:
        conn.close()
        raise ValueError('No hay ninguna habitación asignada a esta reservación — especifique un room_number')

    room = conn.execute('SELECT * FROM rooms WHERE room_number = ?', (target_room,)).fetchone()
    if not room:
        conn.close()
        raise ValueError(f'La habitación {target_room} no existe')
    if room['room_type'] != guest['room_type']:
        conn.close()
        raise ValueError(
            f"La habitación {target_room} es {ROOM_TYPE_LABELS[room['room_type']]}, "
            f"pero esta reservación es para {ROOM_TYPE_LABELS[guest['room_type']]}"
        )

    if _room_number_overlap_exists(conn, target_room, guest['checkin_date'], guest['checkout_date'],
                                    exclude_guest_id=guest_id):
        conn.close()
        raise ValueError(f'La habitación {target_room} ya está ocupada, reservada o bloqueada para parte de ese período')

    conn.execute('''
        UPDATE guests SET status = 'checked_in', room_number = ?,
            id_number = COALESCE(?, id_number), nationality = COALESCE(?, nationality),
            address = COALESCE(?, address), observations = COALESCE(?, observations),
            rate_code = COALESCE(?, rate_code),
            adults = COALESCE(?, adults), children = COALESCE(?, children),
            country = COALESCE(?, country), city = COALESCE(?, city),
            postal_code = COALESCE(?, postal_code), booking_reference = COALESCE(?, booking_reference)
        WHERE id = ?
    ''', (target_room, id_number, nationality, address, observations,
          rate_code, adults, children, country, city, postal_code, booking_reference, guest_id))
    conn.commit()
    updated = get_guest(guest_id, conn=conn)
    conn.close()
    return updated


def cancel_reservation(guest_id, cancelled_by_user_id=None, reason=None):
    conn = get_connection(write=True)
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Reservación no encontrada')
    if guest['status'] != 'reserved':
        conn.close()
        raise ValueError('Solo una reservación pendiente puede ser cancelada')
    conn.execute('''
        UPDATE guests SET status = 'cancelled', cancelled_at = now_local(), cancelled_by = ?
        WHERE id = ?
    ''', (cancelled_by_user_id, guest_id))
    details = f"{guest['name']} — Habitación {guest['room_number']}, {guest['checkin_date']} a {guest['checkout_date']}"
    if reason:
        details += f" — {reason}"
    _log_activity(conn, cancelled_by_user_id, 'reservation_cancelled', details)
    conn.commit()
    conn.close()
    return {'id': guest_id, 'cancelled': True}


def get_month_availability(year, month):
    conn = get_connection()
    first_day = datetime(year, month, 1).date()
    next_month = datetime(year + 1, 1, 1).date() if month == 12 else datetime(year, month + 1, 1).date()

    capacities = {rt: room_type_capacity(rt, conn=conn) for rt in ROOM_TYPES}
    total_capacity = sum(capacities.values())
    days = []
    day = first_day
    while day < next_month:
        day_str = day.isoformat()
        occupied_by_type = {}
        for rt in ROOM_TYPES:
            count = conn.execute('''
                SELECT COUNT(*) AS c FROM guests
                WHERE room_type = ? AND status IN ('reserved', 'checked_in')
                  AND checkin_date <= ? AND checkout_date > ?
            ''', (rt, day_str, day_str)).fetchone()['c']
            occupied_by_type[rt] = count

        is_checkout_day = conn.execute('''
            SELECT COUNT(*) AS c FROM guests
            WHERE status IN ('reserved', 'checked_in') AND checkout_date = ?
        ''', (day_str,)).fetchone()['c'] > 0

        total_occupied = sum(occupied_by_type.values())
        remaining_by_type = {rt: max(capacities[rt] - occupied_by_type[rt], 0) for rt in ROOM_TYPES}

        if total_occupied < total_capacity:
            color = 'green'
        elif is_checkout_day:
            color = 'yellow'
        else:
            color = 'red'

        days.append({
            'date': day_str,
            'color': color,
            'remaining_by_type': remaining_by_type,
            'is_checkout_day': is_checkout_day,
        })
        day = day + timedelta(days=1)

    conn.close()
    return days


# --------------------------------------------------------------- Orders ----

def generate_receipt_number():
    return f"RB-{uuid.uuid4().hex[:8].upper()}"


def order_revenue(order):
    """Actual cash that changes hands for this order: for walk-ins the $10
    day-pass fee is always collected (unspent credit is forfeited, not
    refunded), so revenue is at least $10 even if the ticket came in under.
    Master accounts (breakfasts, owners) are courtesies: no money changes hands."""
    if order.get('payment_method') == 'house' or order.get('guest_type') in MASTER_ACCOUNT_TYPES:
        return 0.0
    if order['guest_type'] == 'walk_in':
        return round(max(WALK_IN_CREDIT, order['subtotal']), 2)
    return order['subtotal']


def _recompute_order_totals(conn, order_id):
    """Recalculates subtotal/credit_applied/balance_due from the order's
    current line items. Called every time items are added or the ticket
    is closed, so the running tab is always accurate."""
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    items = conn.execute('SELECT quantity, unit_price FROM order_items WHERE order_id = ?', (order_id,)).fetchall()
    subtotal = round(sum(i['quantity'] * i['unit_price'] for i in items), 2)

    if order['guest_type'] == 'walk_in':
        credit_applied = round(min(WALK_IN_CREDIT, subtotal), 2)
        balance_due = round(max(subtotal - WALK_IN_CREDIT, 0), 2)
    else:
        credit_applied = 0.0
        balance_due = subtotal

    conn.execute(
        'UPDATE orders SET subtotal = ?, credit_applied = ?, balance_due = ? WHERE id = ?',
        (subtotal, credit_applied, balance_due, order_id)
    )


def _hydrate_order(conn, order):
    order = dict(order)
    items = conn.execute(
        'SELECT id, item_name, quantity, unit_price, room_number, included, list_price, sides, doneness, notes, '
        'sent_quantity FROM order_items WHERE order_id = ? ORDER BY id', (order['id'],)
    ).fetchall()
    order['items'] = [dict(i) for i in items]
    order['is_master'] = order['guest_type'] in MASTER_ACCOUNT_TYPES
    server = conn.execute('SELECT full_name FROM users WHERE id = ?', (order['server_id'],)).fetchone()
    order['server_name'] = server['full_name'] if server else 'Unknown'
    if order['guest_id']:
        guest = conn.execute('SELECT name, room_number FROM guests WHERE id = ?', (order['guest_id'],)).fetchone()
        order['guest_name'] = guest['name'] if guest else None
        order['room_number'] = guest['room_number'] if guest else None
    else:
        order['guest_name'] = None
        order['room_number'] = None
    if order['cancel_requested_by']:
        u = conn.execute('SELECT full_name FROM users WHERE id = ?', (order['cancel_requested_by'],)).fetchone()
        order['cancel_requested_by_name'] = u['full_name'] if u else 'Unknown'
    if order['cancelled_by']:
        u = conn.execute('SELECT full_name FROM users WHERE id = ?', (order['cancelled_by'],)).fetchone()
        order['cancelled_by_name'] = u['full_name'] if u else 'Unknown'
    order['amount_collected'] = order_revenue(order) if order['status'] == 'closed' else 0.0
    if order['guest_type'] == 'walk_in':
        order['day_pass_fee'] = WALK_IN_CREDIT
        order['credit_forfeited'] = round(max(WALK_IN_CREDIT - order['subtotal'], 0), 2)
    else:
        order['day_pass_fee'] = None
        order['credit_forfeited'] = 0.0
    return order


MAX_ITEM_QUANTITY = 100


def _priced_order_items(conn, items):
    """Validates the lines a device sent and prices them from the menu. The
    device's own unit_price is ignored, so a modified phone can't sell a
    dish for C$1, and quantities must be whole numbers 1-100 (a negative
    quantity would otherwise shrink the bill and add stock back)."""
    priced = []
    for i in items:
        name = (i.get('name') or '').strip() if isinstance(i, dict) else ''
        menu_item = conn.execute('SELECT name, price, is_active, has_sides, asks_doneness, section FROM menu_items WHERE name = ?',
                                 (name,)).fetchone()
        error = None
        if not menu_item:
            error = f"'{name}' no está en el menú"
        elif not menu_item['is_active']:
            error = f"'{name}' está desactivado en el menú"
        elif menu_item['price'] <= 0:
            error = f"'{name}' todavía no tiene precio en el menú"
        else:
            try:
                quantity = int(i.get('quantity'))
                if quantity != float(i.get('quantity')):
                    raise ValueError
            except (TypeError, ValueError):
                quantity = None
            if quantity is None or not (1 <= quantity <= MAX_ITEM_QUANTITY):
                error = f"Cantidad inválida para {name}: debe ser un número entero entre 1 y {MAX_ITEM_QUANTITY}"
        if error:
            conn.rollback()
            conn.close()
            raise ValueError(error)
        try:
            options = _dish_options(menu_item, i)
        except ValueError:
            conn.rollback()
            conn.close()
            raise
        # A breakfast dish is always included for a hotel guest (only their tab
        # applies it, in _insert_order_line); the device can't switch it.
        priced.append({'name': menu_item['name'], 'quantity': quantity, 'unit_price': menu_item['price'],
                       'included': menu_item['section'] == 'desayunos', **options})
    return priced


def _dish_options(menu_item, line):
    """Sides (two — they may repeat — or none), doneness and the waiter's
    note for one line, checked against what the dish allows."""
    name = menu_item['name']
    sides = line.get('sides') or []
    if isinstance(sides, str):
        sides = [x.strip() for x in sides.split('+') if x.strip()]
    if sides:
        if not menu_item['has_sides']:
            raise ValueError(f'{name} no lleva guarniciones')
        if len(sides) != 2 or any(x not in SIDE_OPTIONS for x in sides):
            raise ValueError(f'{name}: elija 2 guarniciones (pueden ser iguales) o ninguna')
    doneness = (line.get('doneness') or '').strip() or None
    if menu_item['asks_doneness'] == 2:
        if doneness not in EGG_OPTIONS:
            raise ValueError(f'{name}: elija cómo quiere los huevos')
    elif menu_item['asks_doneness']:
        if doneness not in DONENESS_OPTIONS:
            raise ValueError(f'{name}: elija el término de la carne')
    elif doneness:
        raise ValueError(f'{name} no pide término de cocción')
    notes = ' '.join(str(line.get('notes') or '').split())[:MAX_LINE_NOTE] or None
    return {'sides': ' + '.join(sides) or None, 'doneness': doneness, 'notes': notes}


def line_details(row):
    """The extra lines a kitchen ticket prints under a dish."""
    out = []
    if row.get('sides'):
        out.append(f"Guarnición: {row['sides']}")
    if row.get('doneness'):
        out.append(f"{'Huevos' if row['doneness'] in EGG_OPTIONS else 'Término'}: {row['doneness']}")
    if row.get('notes'):
        out.append(f"Nota: {row['notes']}")
    return out


def _insert_order_line(cur, order_id, item, guest_type):
    """One ticket line. Only a hotel guest's tab can carry included-breakfast
    lines: those are billed at C$0 and remember the menu price."""
    included = bool(item.get('included')) and guest_type == 'hotel_guest'
    cur.execute('''
        INSERT INTO order_items (order_id, item_name, quantity, unit_price, included, list_price, sides, doneness, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (order_id, item['name'], item['quantity'], 0 if included else item['unit_price'],
          1 if included else 0, item['unit_price'], item.get('sides'), item.get('doneness'), item.get('notes')))
    return cur.lastrowid


def set_order_account_name(order_id, account_name):
    """Rename an open tab (e.g. 'Juan') — blank clears it back to 'Cuenta N'."""
    name = ' '.join((account_name or '').split())[:40] or None
    conn = get_connection(write=True)
    order = conn.execute('SELECT status, guest_type FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open':
        conn.close()
        raise ValueError('Solo se puede cambiar el nombre de una cuenta abierta')
    if order['guest_type'] == 'breakfast':
        conn.close()
        raise ValueError('La cuenta de Desayunos lleva el nombre del día y no se puede cambiar')
    if order['guest_type'] == 'owners' and not name:
        conn.close()
        raise ValueError('La cuenta de Propietarios necesita un nombre')
    conn.execute('UPDATE orders SET account_name = ? WHERE id = ?', (name, order_id))
    conn.commit()
    conn.close()
    return {'id': order_id, 'account_name': name}


def _all_restaurant_tables():
    return {t for area in RESTAURANT_AREAS for t in RESTAURANT_TABLES_BY_AREA[area]}


def _free_sub_accounts(conn, table_number, how_many):
    """The lowest `how_many` free account slots at a table, or a ValueError
    naming how many it still has room for."""
    used = {r['sub_account'] for r in conn.execute(
        "SELECT sub_account FROM orders WHERE table_number = ? AND status = 'open'", (table_number,)
    ).fetchall()}
    free = [n for n in range(1, MAX_SUB_ACCOUNTS_PER_TABLE + 1) if n not in used]
    if len(free) < how_many:
        conn.rollback()
        conn.close()
        raise ValueError(f'La mesa {table_number} solo tiene espacio para {len(free)} cuenta(s) más '
                         f'(máximo {MAX_SUB_ACCOUNTS_PER_TABLE})')
    return free[:how_many]


def _open_order_or_raise(conn, order_id):
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.rollback()
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open' or order['cancel_requested_at']:
        conn.rollback()
        conn.close()
        raise ValueError(f"La cuenta #{order['receipt_number']} no está abierta — solo se trasladan cuentas abiertas")
    return order


def move_order_to_table(order_id, target_table, moved_by_user_id=None):
    """Moves one open account to another table, taking that table's next free
    account slot. The account keeps its items, server and receipt number."""
    target_table = (target_table or '').strip()
    if target_table not in _all_restaurant_tables():
        raise ValueError('Mesa de destino inválida')
    conn = get_connection(write=True)
    order = _open_order_or_raise(conn, order_id)
    if order['table_number'] == target_table:
        conn.rollback()
        conn.close()
        raise ValueError('La cuenta ya está en esa mesa')
    (slot,) = _free_sub_accounts(conn, target_table, 1)
    conn.execute('UPDATE orders SET table_number = ?, sub_account = ? WHERE id = ?', (target_table, slot, order_id))
    _log_activity(conn, moved_by_user_id, 'order_moved',
                  f"Cuenta #{order['receipt_number']}: mesa {order['table_number'] or '—'} → {target_table}")
    conn.commit()
    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def merge_orders(source_order_id, target_order_id, merged_by_user_id=None):
    """Moves every line of one open account into another open account and
    removes the now-empty source. Inventory already depleted moves with the
    lines (its Kardex rows are re-tagged), so nothing is restocked or
    depleted twice."""
    if int(source_order_id) == int(target_order_id):
        raise ValueError('Seleccione una cuenta distinta')
    conn = get_connection(write=True)
    source = _open_order_or_raise(conn, source_order_id)
    target = _open_order_or_raise(conn, target_order_id)
    conn.execute('UPDATE order_items SET order_id = ? WHERE order_id = ?', (target['id'], source['id']))
    conn.execute('UPDATE inventory_transactions SET order_id = ? WHERE order_id = ?', (target['id'], source['id']))
    conn.execute('DELETE FROM orders WHERE id = ?', (source['id'],))
    _recompute_order_totals(conn, target['id'])
    _log_activity(conn, merged_by_user_id, 'order_merged',
                  f"Cuenta #{source['receipt_number']} (mesa {source['table_number'] or '—'}) trasladada a "
                  f"#{target['receipt_number']} (mesa {target['table_number'] or '—'})")
    conn.commit()
    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (target['id'],)).fetchone())
    conn.close()
    return updated


def move_table(source_table, target_table, moved_by_user_id=None):
    """Moves every open account at one table to another (e.g. the party
    changes tables). They take the destination's free account slots."""
    tables = _all_restaurant_tables()
    if source_table not in tables or target_table not in tables:
        raise ValueError('Mesa inválida')
    if source_table == target_table:
        raise ValueError('Seleccione una mesa distinta')
    conn = get_connection(write=True)
    orders = conn.execute(
        "SELECT id FROM orders WHERE table_number = ? AND status = 'open' ORDER BY sub_account", (source_table,)
    ).fetchall()
    if not orders:
        conn.rollback()
        conn.close()
        raise ValueError(f'La mesa {source_table} no tiene cuentas abiertas')
    slots = _free_sub_accounts(conn, target_table, len(orders))
    for o, slot in zip(orders, slots):
        conn.execute('UPDATE orders SET table_number = ?, sub_account = ? WHERE id = ?', (target_table, slot, o['id']))
    _log_activity(conn, moved_by_user_id, 'table_moved',
                  f'Mesa {source_table} → {target_table} ({len(orders)} cuenta(s))')
    conn.commit()
    conn.close()
    return {'moved': len(orders), 'target_table': target_table}


def transfer_orders_to_server(new_server_id, changed_by_user_id=None, order_ids=None,
                              table_number=None, from_server_id=None):
    """Hands open accounts to another server — one account, a whole table, or
    (shift change) every open account a server has."""
    conn = get_connection(write=True)
    server = conn.execute("SELECT id, full_name FROM users WHERE id = ?", (new_server_id,)).fetchone()
    if not server:
        conn.rollback()
        conn.close()
        raise ValueError('Mesero no encontrado')
    where, params = ["status = 'open'"], []
    if order_ids:
        where.append(f"id IN ({','.join('?' * len(order_ids))})")
        params.extend(int(i) for i in order_ids)
    elif table_number:
        where.append('table_number = ?')
        params.append(table_number)
    elif from_server_id:
        where.append('server_id = ?')
        params.append(int(from_server_id))
    else:
        conn.rollback()
        conn.close()
        raise ValueError('Indique qué cuentas transferir')
    rows = conn.execute(f"SELECT id FROM orders WHERE {' AND '.join(where)}", params).fetchall()
    if not rows:
        conn.rollback()
        conn.close()
        raise ValueError('No hay cuentas abiertas para transferir')
    ids = [r['id'] for r in rows]
    conn.execute(f"UPDATE orders SET server_id = ? WHERE id IN ({','.join('?' * len(ids))})", (server['id'], *ids))
    scope = (f'mesa {table_number}' if table_number and not order_ids
             else 'cambio de turno' if from_server_id and not order_ids else 'cuenta(s)')
    _log_activity(conn, changed_by_user_id, 'server_transferred',
                  f"{len(ids)} cuenta(s) → {server['full_name']} ({scope})")
    conn.commit()
    conn.close()
    return {'transferred': len(ids), 'server_name': server['full_name']}


def list_owner_account_names():
    """Names already used for owners' accounts (most recent first) — offered
    as suggestions so the same owner is always typed the same way."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT account_name FROM orders
        WHERE guest_type = 'owners' AND account_name IS NOT NULL AND status != 'cancelled'
        GROUP BY account_name ORDER BY MAX(id) DESC
    ''').fetchall()
    conn.close()
    seen, names = set(), []
    for r in rows:
        key = r['account_name'].lower()
        if key not in seen:
            seen.add(key)
            names.append(r['account_name'])
    return names


def list_staff_for_transfer():
    """Everyone who can carry a table: servers plus admins."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, full_name, role FROM users WHERE role IN ('server', 'admin') AND is_active = 1 ORDER BY full_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_MONTHS_ES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
              'octubre', 'noviembre', 'diciembre')


def get_daily_item_sales(day):
    """'Artículos vendidos el <día>': units per item for one day, split into
    Matutino (M) and Vespertino (V) like the cashier's printout. The split is
    the opening of the day's second cash shift (or 2:00 p.m. when there is no
    second shift). A paid account counts when it was paid; breakfasts and
    owners' lines count when they were served, as their own rows."""
    try:
        d = datetime.strptime(day or '', '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('Elija el día')
    conn = get_connection()
    shifts = [dict(r) for r in conn.execute(
        "SELECT id, opened_at FROM cash_shifts WHERE left(opened_at, 10) = ? ORDER BY opened_at", (d.isoformat(),)).fetchall()]
    boundary = shifts[1]['opened_at'][11:19] if len(shifts) >= 2 else '14:00:00'
    rows = conn.execute("""
        SELECT oi.item_name, oi.quantity, oi.included, o.guest_type,
               CASE WHEN o.guest_type IN ('breakfast', 'owners') THEN COALESCE(oi.created_at, o.timestamp)
                    ELSE o.closed_at END AS sold_at,
               (SELECT MIN(m.id) FROM menu_items m WHERE m.name = oi.item_name) AS menu_id
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.status != 'cancelled'
          AND ((o.guest_type IN ('breakfast', 'owners') AND left(COALESCE(oi.created_at, o.timestamp), 10) = ?)
               OR (o.guest_type NOT IN ('breakfast', 'owners') AND o.status = 'closed' AND left(o.closed_at, 10) = ?))
    """, (d.isoformat(), d.isoformat())).fetchall()
    conn.close()
    agg = {}
    for r in rows:
        if r['guest_type'] == 'owners':
            name, group = f"{r['item_name']} (PROPIETARIOS)", 2
        elif r['guest_type'] == 'breakfast' or r['included']:
            name, group = f"{r['item_name']} (DESAYUNO INCLUIDO)", 1
        else:
            name, group = r['item_name'], 0
        a = agg.setdefault(name, {'group': group, 'menu_id': r['menu_id'], 'm': 0, 'v': 0})
        a['m' if (r['sold_at'] or '')[11:19] < boundary else 'v'] += r['quantity']
    items = sorted(agg.items(), key=lambda kv: (kv[1]['group'], kv[1]['menu_id'] or 10 ** 9, kv[0].lower()))
    out = [{'code': f"{a['menu_id']:05d}" if a['menu_id'] else '', 'name': n, 'm': a['m'], 'v': a['v'],
            't': a['m'] + a['v']} for n, a in items]
    m_total, v_total = sum(i['m'] for i in out), sum(i['v'] for i in out)
    if len(shifts) >= 2:
        legend = (f"M = Matutino (turno {shifts[0]['id']}) · V = Vespertino (turno {shifts[1]['id']}"
                  f"{', '.join([''] + [str(s['id']) for s in shifts[2:]])}) · T = Total del día")
    else:
        legend = 'M = Matutino (antes de las 2:00 p.m.) · V = Vespertino (desde las 2:00 p.m.) · T = Total del día'
    return {
        'date': d.isoformat(),
        'title': f'Artículos vendidos el {d.day} de {_MONTHS_ES[d.month - 1]} de {d.year}',
        'file_label': f'{d.day}_{_MONTHS_ES[d.month - 1]}_{d.year}',
        'business': get_receipt_settings()['receipt_pos_name'],
        'summary': f'{len(out)} artículos distintos  |  Mañana: {m_total}  |  Tarde: {v_total}  |  '
                   f'Total del día: {m_total + v_total} unidades',
        'legend': legend,
        'items': out,
        'm_total': m_total, 'v_total': v_total,
    }


def get_item_sales(start_date, end_date, include_open=False, server_id=None):
    """What the restaurant sold, item by item, between two dates (inclusive):
    quantity and amount per menu item plus a header like the cashier's
    'Ventas por Artículos' close. Closed accounts count by closed_at; open
    ones (optional) by when they were opened. Cancelled accounts never count."""
    conn = get_connection()
    status_clause = ("((o.status = 'closed' AND left(o.closed_at, 10) BETWEEN ? AND ?) OR "
                     "(o.status = 'open' AND left(o.timestamp, 10) BETWEEN ? AND ?))") if include_open else \
                    "(o.status = 'closed' AND left(o.closed_at, 10) BETWEEN ? AND ?)"
    # Breakfasts and owners' courtesies aren't sales.
    status_clause = f"({status_clause}) AND o.guest_type NOT IN ('breakfast', 'owners')"
    params = [start_date, end_date, start_date, end_date] if include_open else [start_date, end_date]
    server_clause = ''
    if server_id:
        server_clause = ' AND o.server_id = ?'
        params.append(int(server_id))
    rows = conn.execute(f'''
        SELECT oi.item_name, MIN(m.id) AS menu_id, COALESCE(MIN(m.category), '') AS category,
               SUM(oi.quantity) AS quantity, SUM(oi.quantity * oi.unit_price) AS total
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN menu_items m ON m.name = oi.item_name
        WHERE {status_clause}{server_clause}
        GROUP BY oi.item_name
        ORDER BY MIN(m.id) IS NULL, MIN(m.id), oi.item_name
    ''', params).fetchall()
    summary = conn.execute(f'''
        SELECT COUNT(*) AS orders, MIN(o.timestamp) AS first_opened,
               SUM(CASE WHEN o.status = 'open' THEN 1 ELSE 0 END) AS open_orders
        FROM orders o WHERE {status_clause}{server_clause}
    ''', params).fetchone()
    server = conn.execute('SELECT full_name FROM users WHERE id = ?', (server_id,)).fetchone() if server_id else None
    conn.close()
    items = []
    for r in rows:
        qty = r['quantity'] or 0
        total = round(r['total'] or 0, 2)
        items.append({
            'code': f"{r['menu_id']:05d}" if r['menu_id'] else '',
            'item_name': r['item_name'],
            'category': r['category'],
            'quantity': qty,
            'unit_price': round(total / qty, 2) if qty else 0,
            'total': total,
        })
    return {
        'start_date': start_date,
        'end_date': end_date,
        'include_open': bool(include_open),
        'server_name': server['full_name'] if server else None,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'first_opened': summary['first_opened'],
        'order_count': summary['orders'] or 0,
        'open_order_count': summary['open_orders'] or 0,
        'items': items,
        'total_quantity': sum(i['quantity'] for i in items),
        'total_amount': round(sum(i['total'] for i in items), 2),
    }


def start_order(server_id, items, guest_type, guest_id=None, table_number=None, sub_account=None, account_name=None,
                guest_count=None):
    """Opens a new ticket (no payment method yet — that's chosen at checkout).
    When table_number is set, the ticket becomes one of up to
    MAX_SUB_ACCOUNTS_PER_TABLE separate accounts at that table — sub_account
    picks a specific slot (must be free) or, left blank, the next free one is
    assigned automatically."""
    if guest_type not in GUEST_TYPES:
        raise ValueError(f"guest_type must be one of {GUEST_TYPES}")
    if not items:
        raise ValueError('Se requiere al menos un artículo')
    account_name = ' '.join((account_name or '').split())[:40] or None
    if guest_count in (None, ''):
        guest_count = None
    else:
        try:
            guest_count = int(guest_count)
        except (TypeError, ValueError):
            raise ValueError('Clientes debe ser un número')
        if not 1 <= guest_count <= 99:
            raise ValueError('Clientes debe estar entre 1 y 99')
    if guest_type == 'owners' and not account_name:
        raise ValueError('Escriba un nombre para la cuenta de Propietarios (ej. Familia Pérez)')

    conn = get_connection(write=True)
    items = _priced_order_items(conn, items)

    if guest_type == 'owners':
        # Each owner keeps one running account, so two owners' consumption
        # never ends up mixed and one owner's isn't split across tabs.
        existing = conn.execute('''
            SELECT receipt_number FROM orders
            WHERE guest_type = 'owners' AND status = 'open' AND lower(account_name) = lower(?)
        ''', (account_name,)).fetchone()
        if existing:
            conn.close()
            raise ValueError(f'«{account_name}» ya tiene una cuenta abierta ({existing["receipt_number"]}). '
                             'Agregue los consumos a esa cuenta desde Órdenes Recientes.')

    if guest_type == 'hotel_guest':
        if not guest_id:
            conn.close()
            raise ValueError('guest_id es obligatorio para una orden de huésped del hotel')
        guest = get_guest(guest_id, conn=conn)
        if not guest or guest['status'] != 'checked_in':
            conn.close()
            raise ValueError('El huésped no está actualmente registrado')

    if table_number:
        used = {r['sub_account'] for r in conn.execute(
            "SELECT sub_account FROM orders WHERE table_number = ? AND status = 'open'", (table_number,)
        ).fetchall()}
        if sub_account is None or sub_account == '':
            sub_account = next((n for n in range(1, MAX_SUB_ACCOUNTS_PER_TABLE + 1) if n not in used), None)
            if sub_account is None:
                conn.close()
                raise ValueError(f'La mesa {table_number} ya tiene el máximo de {MAX_SUB_ACCOUNTS_PER_TABLE} cuentas abiertas')
        else:
            sub_account = int(sub_account)
            if not (1 <= sub_account <= MAX_SUB_ACCOUNTS_PER_TABLE):
                conn.close()
                raise ValueError(f'La cuenta debe estar entre 1 y {MAX_SUB_ACCOUNTS_PER_TABLE}')
            if sub_account in used:
                conn.close()
                raise ValueError(f'La cuenta {sub_account} ya está en uso en la mesa {table_number}')
    else:
        sub_account = 1

    receipt_number = generate_receipt_number()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO orders (receipt_number, server_id, guest_id, guest_type, table_number, sub_account, account_name,
                            guest_count, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
    ''', (receipt_number, server_id, guest_id if guest_type == 'hotel_guest' else None, guest_type, table_number,
          sub_account, account_name, guest_count))
    order_id = cur.lastrowid

    for i in items:
        line_id = _insert_order_line(cur, order_id, i, guest_type)
        _deplete_inventory_for_order_item(conn, order_id, line_id, i['name'], i['quantity'])

    _recompute_order_totals(conn, order_id)
    _send_unsent_lines(conn, order_id, is_addition=False)   # "Agregar Orden" is the confirmation
    conn.commit()

    order = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return order


# ------------------------------------------------------- Kitchen/Bar Tickets

# Which printer each menu category's lines go to. Desserts are plated in the
# kitchen, so they print there with the food.
STATION_FOR_CATEGORY = {'Drinks': 'bar', 'Food': 'kitchen', 'Desserts': 'kitchen'}
STATION_LABELS_ES = {'kitchen': 'Cocina', 'bar': 'Bar', 'reception': 'Recepción'}
GUEST_TYPE_LABELS_ES = {'hotel_guest': 'Huésped', 'walk_in': 'Pase de día', 'visitor': 'Visitante',
                        'owners': 'Propietarios', 'breakfast': 'Desayuno incluido'}

PRINTER_SETTING_DEFAULTS = {
    'printing_enabled': '0',
    'printer_kitchen_host': '',
    'printer_bar_host': '',
    'printer_reception_host': '',
    'printer_port': '9100',
}


def get_printer_settings(conn=None):
    own = conn is None
    conn = conn or get_connection()
    rows = conn.execute(
        f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(PRINTER_SETTING_DEFAULTS))})",
        tuple(PRINTER_SETTING_DEFAULTS),
    ).fetchall()
    if own:
        conn.close()
    s = {**PRINTER_SETTING_DEFAULTS, **{r['key']: r['value'] for r in rows}}
    return {
        'enabled': s['printing_enabled'] == '1',
        'kitchen_host': s['printer_kitchen_host'],
        'bar_host': s['printer_bar_host'],
        'reception_host': s['printer_reception_host'],
        'port': int(s['printer_port'] or 9100),
    }


def save_printer_settings(enabled, kitchen_host, bar_host, port, reception_host=None):
    kitchen_host = (kitchen_host or '').strip()
    bar_host = (bar_host or '').strip()
    if reception_host is None:   # an older screen that doesn't send it keeps the saved one
        reception_host = get_printer_settings()['reception_host']
    reception_host = (reception_host or '').strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError('El puerto debe ser un número (normalmente 9100)')
    if not 1 <= port <= 65535:
        raise ValueError('El puerto debe estar entre 1 y 65535')
    if enabled and not (kitchen_host or bar_host):
        raise ValueError('Ingrese la IP de al menos una impresora antes de activar la impresión')
    values = {
        'printing_enabled': '1' if enabled else '0',
        'printer_kitchen_host': kitchen_host,
        'printer_bar_host': bar_host,
        'printer_reception_host': reception_host,
        'printer_port': str(port),
    }
    conn = get_connection(write=True)
    for key, value in values.items():
        conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    conn.commit()
    conn.close()
    return get_printer_settings()


def queue_document_print(lines, order_id=None, reference=None):
    """Queues a document (bill, pre-cuenta, guest consumption summary) for the
    reception printer. Returns False when no reception printer is set up, so
    the screen falls back to the browser's print dialog."""
    if not get_printer_settings()['reception_host']:
        return False
    payload = {'kind': 'document', 'station_label': STATION_LABELS_ES['reception'],
               'receipt_number': reference, 'lines': lines}
    conn = get_connection(write=True)
    conn.execute('INSERT INTO print_jobs (order_id, station, payload) VALUES (?, ?, ?)',
                 (order_id, 'reception', json.dumps(payload)))
    conn.commit()
    conn.close()
    return True


def _money(v):
    return f"{v:,.2f}"


def receipt_print_lines(view):
    """The same document as templates/receipt.html, as lines for the thermal
    printer (see tickets.build_document for the line format)."""
    s, r = view['s'], view['r']
    sign_block = []
    if r['sign']:
        if r['room_fields']:
            sign_block += [{'t': ''}, {'t': '______________________________', 'a': 'c'}, {'t': 'Habitación - Room', 'a': 'c'},
                           {'t': '______________________________', 'a': 'c'}, {'t': 'Nombre - Name', 'a': 'c'}]
        sign_block += [{'t': ''}, {'t': '______________________________', 'a': 'c'}, {'t': 'Firma - Signature', 'a': 'c'}]
    authorize = [{'t': 'AUTORIZO PARA CARGAR A MI CUENTA EL IMPORTE ARRIBA INDICADO', 'a': 'c'}]

    if r['kind'] == 'control':
        L = [{'t': s['receipt_business_name']}, {'t': s['receipt_pos_name']},
             {'t': f"Control de Consumo No. {r['folio']}"}, {'t': f"Fecha: {r['date']}"}]
        if r['hab_cred']:
            L.append({'t': f"Hab/Cred: {r['hab_cred']}"})
        if r['note']:
            L += [{'t': 'Nota:'}, {'t': r['note']}]
        L += [{'l': 'Cant. Artículo', 'r': ''}, {'rule': 1}]
        for ln in r['lines']:
            L.append({'l': f"{ln['quantity']:.2f} {ln['name']}", 'r': ''})
            L += [{'t': f"   > {d}"} for d in ln['details']]
        L += [{'t': ''}, {'t': '______________________________', 'a': 'c'}, {'t': 'Firma - Signature', 'a': 'c'}, {'t': ''},
              {'t': f"Le atendió: {r['server'].upper()}"}, {'t': f"Cajero: {r['cashier']}"},
              {'t': '-------- *** --------', 'a': 'c'}]
        return L

    L = [{'t': s['receipt_business_name'], 'a': 'c', 'b': 1}]
    L += [{'t': s[k], 'a': 'c'} for k in ('receipt_company', 'receipt_ruc', 'receipt_address', 'receipt_phone') if s.get(k)]
    L += [{'t': ''}, {'t': f"({r['copy_label']})", 'a': 'c'}]
    if r['kind'] == 'charge':
        L.append({'t': 'CARGO A CUENTA', 'a': 'c'})
    if r['kind'] == 'invoice':
        L.append({'t': f"FACTURA: {r['number']}", 'b': 1})
    L += [{'t': s['receipt_pos_name'], 'b': 1}, {'t': f"Folio Nro.: {r['folio']}"},
          {'t': f"Cajero: {r['cashier']}"}, {'t': f"Salonero: {r['server'].upper()}"},
          {'t': f"Mesa: {r['table']} Orden: {r['sub_account']} Mon: C$"}]
    if r['hab_cred']:
        L.append({'t': f"Hab/Cred: {r['hab_cred']}"})
    if r['guest_count']:
        L.append({'t': f"Clientes: {r['guest_count']}"})
    L.append({'t': f"{r['date_label']}: {r['date']}"})
    if r['note']:
        L += [{'t': 'Nota:'}, {'t': r['note'], 'b': 1}]
    L += [{'l': 'Cant. Artículo', 'r': 'Importe'}, {'rule': 1}]
    for ln in r['lines']:
        L.append({'l': f"{ln['quantity']:.2f} {ln['name']}", 'r': _money(ln['amount'])})
        L += [{'t': f"   > {d}"} for d in ln['details']]
    L += [{'rule': 1}, {'l': 'Subtotal a Cobrar:', 'r': _money(r['subtotal'])}]
    if r['day_pass_minimum']:
        L.append({'l': 'Pase de día (consumo mínimo)', 'r': _money(r['day_pass_minimum'])})
    L += [{'rule': 1}, {'l': 'A Pagar:', 'r': f"{_money(r['to_pay'])} (C$)", 'b': 1}]
    if r['kind'] in ('invoice', 'charge'):
        L += [{'rule': 1}, {'l': 'Forma de Pago  Entregado', 'r': 'Cobrado'},
              {'l': f"{r['method_label']}  {_money(r['tendered']) if r['tendered'] is not None else ''}", 'r': _money(r['to_pay'])}]
        if r['change']:
            L.append({'l': 'Vuelto', 'r': _money(r['change'])})
        if r['kind'] == 'charge' and r['hab_cred']:
            L.append({'t': r['hab_cred']})
        L += [{'t': ''}, {'t': f"CONTADO: [{' ' if r['credit'] else 'X'}]   CRÉDITO: [{'X' if r['credit'] else ' '}]", 'a': 'c'}]
    if r['sign']:
        L += sign_block + authorize
    L += [{'t': ''}, {'t': 'GRACIAS POR SU VISITA', 'a': 'c'}]
    return L

# A guest's consumption by day and meal. Lines are placed by the time they
# were ordered; a Desayunos-account line is always breakfast.
MEALS = (('desayuno', 'Desayuno'), ('almuerzo', 'Almuerzo'), ('cena', 'Cena'))
MEAL_LABELS = dict(MEALS)
LUNCH_FROM_HOUR, DINNER_FROM_HOUR = 11, 17


def _meal_of(when, guest_type):
    if guest_type == 'breakfast':
        return 'desayuno'
    hour = int(when[11:13])
    return 'desayuno' if hour < LUNCH_FROM_HOUR else ('almuerzo' if hour < DINNER_FROM_HOUR else 'cena')


def guest_consumption(guest_id, date_from=None, date_to=None, meal=None):
    """What a hotel guest has consumed in the restaurant, day by day and meal
    by meal: their own accounts (open, charged to the room or paid) and the
    breakfasts served to their room. A running pre-cuenta: nothing is closed."""
    conn = get_connection()
    guest = get_guest(guest_id, conn=conn)
    if not guest:
        conn.close()
        raise ValueError('Huésped no encontrado')
    today = datetime.now().strftime('%Y-%m-%d')
    date_from = date_from or guest['checkin_date']
    # still in the house (even past the planned check-out): up to today
    date_to = date_to or (today if guest['status'] == 'checked_in' else min(today, guest['checkout_date']))
    for d in (date_from, date_to):
        try:
            datetime.strptime(d, '%Y-%m-%d')
        except (TypeError, ValueError):
            conn.close()
            raise ValueError('Fecha inválida (use AAAA-MM-DD)')
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    if meal and meal not in MEAL_LABELS:
        conn.close()
        raise ValueError('Comida inválida')
    rows = conn.execute('''
        SELECT oi.item_name, oi.quantity, oi.unit_price, oi.included, oi.sides, oi.doneness, oi.notes,
               COALESCE(oi.created_at, o.timestamp) AS at, o.receipt_number, o.status, o.payment_method,
               o.guest_type, o.table_number
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.status != 'cancelled'
          AND (o.guest_id = ? OR (o.guest_type = 'breakfast' AND oi.room_number = ? AND oi.guest_name = ?))
          AND left(COALESCE(oi.created_at, o.timestamp), 10) BETWEEN ? AND ?
        ORDER BY at, oi.id
    ''', (guest_id, guest['room_number'], guest['name'], date_from, date_to)).fetchall()
    conn.close()

    days, totals = {}, {'consumo': 0.0, 'pagado': 0.0, 'pendiente': 0.0}
    for r in rows:
        r = dict(r)
        m = _meal_of(r['at'], r['guest_type'])
        if meal and m != meal:
            continue
        amount = round(r['quantity'] * r['unit_price'], 2)
        if r['included'] or r['guest_type'] == 'breakfast':
            state = 'Incluido'
        elif r['status'] == 'open':
            state = 'Pendiente'
        elif r['payment_method'] == 'room_charge':
            state = 'Cargado a la habitación'
        else:
            state = 'Pagado'
        totals['consumo'] += amount
        if state == 'Pagado':
            totals['pagado'] += amount
        elif state != 'Incluido':
            totals['pendiente'] += amount
        day = days.setdefault(r['at'][:10], {'date': r['at'][:10], 'meals': {}, 'total': 0.0})
        bucket = day['meals'].setdefault(m, {'meal': m, 'label': MEAL_LABELS[m], 'lines': [], 'total': 0.0})
        bucket['lines'].append({'time': r['at'][11:16], 'name': r['item_name'], 'quantity': r['quantity'],
                                'amount': amount, 'state': state, 'receipt_number': r['receipt_number'],
                                'details': line_details(r)})
        bucket['total'] = round(bucket['total'] + amount, 2)
        day['total'] = round(day['total'] + amount, 2)
    out_days = []
    for d in sorted(days):
        day = days[d]
        day['meals'] = [day['meals'][k] for k, _ in MEALS if k in day['meals']]
        out_days.append(day)
    return {
        'guest': {'id': guest['id'], 'name': guest['name'], 'room_number': guest['room_number'],
                  'checkin_date': guest['checkin_date'], 'checkout_date': guest['checkout_date']},
        'date_from': date_from, 'date_to': date_to, 'meal': meal or '',
        'meal_label': MEAL_LABELS.get(meal, 'Todas las comidas'),
        'days': out_days, 'totals': {k: round(v, 2) for k, v in totals.items()},
        'meals': [{'key': k, 'label': v} for k, v in MEALS],
    }


def consumption_print_lines(data, business_name):
    fmt = lambda d: datetime.strptime(d, '%Y-%m-%d').strftime('%d/%m/%Y')
    g = data['guest']
    period = (fmt(data['date_from']) if data['date_from'] == data['date_to']
              else f"{fmt(data['date_from'])} al {fmt(data['date_to'])}")
    L = [{'t': business_name, 'a': 'c', 'b': 1}, {'t': 'RESUMEN DE CONSUMO', 'a': 'c', 'b': 1},
         {'t': 'PRE-CUENTA — NO ES FACTURA', 'a': 'c'}, {'t': ''},
         {'t': f"Hab. {g['room_number']} — {g['name']}", 'b': 1}, {'t': f"Período: {period}"},
         {'t': f"Comidas: {data['meal_label']}"}, {'t': f"Impreso: {datetime.now().strftime('%d/%m/%Y %H:%M')}"}]
    for day in data['days']:
        L += [{'rule': 1}, {'l': fmt(day['date']), 'r': f"C$ {_money(day['total'])}", 'b': 1}]
        for m in day['meals']:
            L.append({'t': m['label'].upper(), 'b': 1})
            for ln in m['lines']:
                tag = '' if ln['state'] in ('Pendiente', 'Cargado a la habitación') else f" ({ln['state'].lower()})"
                L.append({'l': f"{ln['time']} {ln['quantity']} {ln['name']}{tag}", 'r': _money(ln['amount'])})
    if not data['days']:
        L += [{'rule': 1}, {'t': 'Sin consumo en este período.', 'a': 'c'}]
    t = data['totals']
    L += [{'rule': 1}, {'l': 'Consumo total', 'r': f"C$ {_money(t['consumo'])}"},
          {'l': 'Ya pagado', 'r': f"C$ {_money(t['pagado'])}"},
          {'l': 'Pendiente de pago', 'r': f"C$ {_money(t['pendiente'])}", 'b': 1}, {'t': ''},
          {'t': 'GRACIAS POR SU VISITA', 'a': 'c'}]
    return L


def get_setting(key, default=None):
    conn = get_connection()
    row = conn.execute('SELECT value FROM app_settings WHERE key = ?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    """Stores one app_settings value (None removes it). Its own short
    transaction, outside the write lock — for small status flags only."""
    conn = get_connection()
    if value is None:
        conn.execute('DELETE FROM app_settings WHERE key = ?', (key,))
    else:
        conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))
    conn.commit()
    conn.close()


def _queue_print_jobs(conn, order_id, items, is_addition, header_overrides=None):
    """Splits the lines just added to an order into a kitchen ticket and a bar
    ticket and queues them. Runs inside the caller's transaction, so the
    tickets exist if and only if the order lines do. Only the new lines are
    printed — 'order more' never reprints what the kitchen already has.
    header_overrides replaces ticket header fields (a breakfast prints the
    room it's for, not the day's Desayunos account)."""
    settings = get_printer_settings(conn)
    if not settings['enabled']:
        return []

    by_station = {}
    for i in items:
        row = conn.execute('''SELECT m.category, s.station AS section_station FROM menu_items m
                              LEFT JOIN menu_sections s ON s.key = m.section WHERE m.name = ?''', (i['name'],)).fetchone()
        if row and row['section_station'] == 'none':
            continue   # services, rentals: nothing to prepare
        station = STATION_FOR_CATEGORY.get(row['category'] if row else None, 'kitchen')
        if not settings[f'{station}_host']:
            continue   # no printer configured for this station
        by_station.setdefault(station, []).append({'name': i['name'], 'quantity': i['quantity'],
                                                   'details': i.get('details') or line_details(i)})
    if not by_station:
        return []

    order = conn.execute('''
        SELECT o.*, u.full_name AS server_name, g.name AS guest_name, g.room_number
        FROM orders o
        LEFT JOIN users u ON u.id = o.server_id
        LEFT JOIN guests g ON g.id = o.guest_id
        WHERE o.id = ?
    ''', (order_id,)).fetchone()
    header = {
        'order_id': order_id,
        'receipt_number': order['receipt_number'],
        'table_number': order['table_number'],
        'sub_account': order['sub_account'],
        'account_name': order['account_name'],
        'guest_type': GUEST_TYPE_LABELS_ES.get(order['guest_type'], order['guest_type']),
        'guest_name': order['guest_name'],
        'room_number': order['room_number'],
        'server_name': order['server_name'],
        'is_addition': is_addition,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    header.update(header_overrides or {})
    job_ids = []
    for station, lines in by_station.items():
        payload = {**header, 'station': station, 'station_label': STATION_LABELS_ES[station], 'items': lines}
        cur = conn.execute('INSERT INTO print_jobs (order_id, station, payload) VALUES (?, ?, ?)',
                           (order_id, station, json.dumps(payload, ensure_ascii=False)))
        job_ids.append(cur.lastrowid)
    return job_ids


def _send_unsent_lines(conn, order_id, is_addition):
    """Queues tickets for everything on the order the kitchen/bar doesn't
    have yet (quantity beyond sent_quantity) and marks it sent. Runs inside
    the caller's transaction. Lines are marked sent even with printing off,
    so switching printing on later never prints old orders."""
    rows = conn.execute('''
        SELECT item_name, sides, doneness, notes, SUM(quantity - sent_quantity) AS pending
        FROM order_items WHERE order_id = ? AND quantity > sent_quantity
        GROUP BY item_name, sides, doneness, notes ORDER BY MIN(id)
    ''', (order_id,)).fetchall()
    # A line reduced after it was sent: lower the mark too, so raising it
    # again later prints the difference.
    conn.execute('UPDATE order_items SET sent_quantity = quantity WHERE order_id = ? AND quantity != sent_quantity',
                 (order_id,))
    return _queue_print_jobs(conn, order_id, [{'name': r['item_name'], 'quantity': r['pending'],
                                               'details': line_details(r)} for r in rows],
                             is_addition) if rows else []


def send_order_to_stations(order_id):
    """The server confirmed their changes to an open order (✓ Listo): print
    what was added since the last ticket."""
    conn = get_connection(write=True)
    order = conn.execute('SELECT status FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    jobs = _send_unsent_lines(conn, order_id, is_addition=True) if order['status'] == 'open' else []
    conn.commit()
    conn.close()
    return {'tickets': len(jobs)}


# ------------------------------------------------- Master accounts: breakfast

def _breakfast_account(conn, server_id):
    """Today's open Desayunos account, opened on first use. Earlier days'
    accounts still open are closed as Cuenta de la Casa first, so each day's
    breakfasts stay in their own account."""
    today = datetime.now().strftime('%Y-%m-%d')
    for stale in conn.execute("SELECT id FROM orders WHERE guest_type = 'breakfast' AND status = 'open' "
                              "AND left(timestamp, 10) < ?", (today,)).fetchall():
        _recompute_order_totals(conn, stale['id'])
        conn.execute("UPDATE orders SET status = 'closed', payment_method = 'house', "
                     "closed_at = left(timestamp, 10) || ' 23:59:59' WHERE id = ?", (stale['id'],))
    row = conn.execute("SELECT id FROM orders WHERE guest_type = 'breakfast' AND status = 'open' "
                       "AND left(timestamp, 10) = ? ORDER BY id LIMIT 1", (today,)).fetchone()
    if row:
        return row['id']
    cur = conn.execute('''
        INSERT INTO orders (receipt_number, server_id, guest_type, sub_account, account_name, status)
        VALUES (?, ?, 'breakfast', 1, ?, 'open')
    ''', (generate_receipt_number(), server_id, f"Desayunos {datetime.now().strftime('%d/%m')}"))
    return cur.lastrowid


def checked_in_rooms():
    """Rooms with a guest staying now, for the breakfast room picker."""
    conn = get_connection()
    rows = conn.execute("SELECT room_number, name FROM guests WHERE status = 'checked_in' AND room_number IS NOT NULL "
                        "ORDER BY NULLIF(regexp_replace(room_number, '\\D', '', 'g'), '')::int NULLS LAST, room_number").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_breakfast(server_id, room_number, items):
    """Records included breakfasts served to one room on today's Desayunos
    account, and sends them to the kitchen right away with the room on the
    ticket. Returns the account plus how many breakfasts that room has had
    today, so staff notice a room taking more than its guests."""
    room_number = str(room_number or '').strip()
    if not room_number:
        raise ValueError('Seleccione la habitación')
    if not items:
        raise ValueError('Agregue al menos un artículo de desayuno')
    conn = get_connection(write=True)
    guest = conn.execute("SELECT id, name FROM guests WHERE status = 'checked_in' AND room_number = ?",
                         (room_number,)).fetchone()
    if not guest:
        conn.close()
        raise ValueError(f'La habitación {room_number} no tiene huéspedes registrados ahora')
    items = _priced_order_items(conn, items)
    order_id = _breakfast_account(conn, server_id)
    for i in items:
        cur = conn.execute('''
            INSERT INTO order_items (order_id, item_name, quantity, unit_price, room_number, guest_name, sent_quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (order_id, i['name'], i['quantity'], i['unit_price'], room_number, guest['name'], i['quantity']))
        _deplete_inventory_for_order_item(conn, order_id, cur.lastrowid, i['name'], i['quantity'])
    _recompute_order_totals(conn, order_id)
    _queue_print_jobs(conn, order_id, items, is_addition=False, header_overrides={
        'table_number': None, 'room_number': room_number, 'guest_name': guest['name'],
        'guest_type': 'Desayuno incluido', 'account_name': None,
    })
    served_today = conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM order_items WHERE order_id = ? AND room_number = ?',
                                (order_id, room_number)).fetchone()[0]
    conn.commit()
    order = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return {'order': order, 'room_number': room_number, 'guest_name': guest['name'], 'room_items_today': served_today}


COURTESY_KIND_LABELS = {'breakfast': 'Desayunos Incluidos', 'owners': 'Propietarios'}


def courtesy_report(kind, start_date, end_date):
    """What the master accounts gave away between two dates (by the day the
    account was opened), for front desk to review or download. Breakfasts are
    grouped per day and room, with the guest who had that room that day;
    owners per account name. Cancelled accounts never count; open ones do,
    marked as still open."""
    if kind not in COURTESY_KIND_LABELS:
        raise ValueError('Tipo de cortesía inválido')
    for d in (start_date, end_date):
        datetime.strptime(d, '%Y-%m-%d')   # raises ValueError on a bad date
    if end_date < start_date:
        raise ValueError('La fecha final debe ser igual o posterior a la inicial')
    conn = get_connection()
    rows = conn.execute('''
        SELECT o.id AS order_id, o.account_name, o.status, left(COALESCE(oi.created_at, o.timestamp), 10) AS day,
               o.timestamp, o.closed_at,
               u.full_name AS server_name, oi.item_name, oi.quantity,
               COALESCE(oi.list_price, oi.unit_price) AS unit_price,
               COALESCE(oi.room_number, g.room_number) AS room_number,
               COALESCE(oi.guest_name, g.name) AS served_guest
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN users u ON u.id = o.server_id
        LEFT JOIN guests g ON g.id = o.guest_id
        WHERE (o.guest_type = ? OR (? = 'breakfast' AND o.guest_type = 'hotel_guest' AND oi.included = 1))
          AND o.status != 'cancelled'
          AND left(COALESCE(oi.created_at, o.timestamp), 10) BETWEEN ? AND ?
        ORDER BY COALESCE(oi.created_at, o.timestamp), oi.room_number, oi.id
    ''', (kind, kind, start_date, end_date)).fetchall()

    guest_cache = {}
    def guest_in_room(room, day):
        if (room, day) not in guest_cache:
            g = conn.execute('''
                SELECT name FROM guests WHERE room_number = ? AND status IN ('checked_in', 'checked_out')
                AND checkin_date <= ? AND checkout_date >= ? ORDER BY id DESC LIMIT 1
            ''', (room, day, day)).fetchone()
            guest_cache[(room, day)] = g['name'] if g else ''
        return guest_cache[(room, day)]

    lines, groups = [], {}
    for r in rows:
        value = round(r['quantity'] * r['unit_price'], 2)
        line = {'day': r['day'], 'account_name': r['account_name'], 'status': r['status'],
                'server_name': r['server_name'], 'item_name': r['item_name'], 'quantity': r['quantity'],
                'unit_price': r['unit_price'], 'value': value, 'room_number': r['room_number'],
                'guest_name': (r['served_guest'] or guest_in_room(r['room_number'], r['day']))
                              if kind == 'breakfast' and r['room_number'] else ''}
        lines.append(line)
        key = (r['day'], r['room_number']) if kind == 'breakfast' else (r['account_name'] or '').lower()
        g = groups.setdefault(key, {
            'day': r['day'], 'room_number': r['room_number'], 'guest_name': line['guest_name'],
            'account_name': r['account_name'], 'status': r['status'], 'server_name': r['server_name'],
            'opened_at': r['timestamp'], 'closed_at': r['closed_at'], 'items': {}, 'quantity': 0, 'value': 0.0})
        if r['status'] == 'open':
            g['status'] = 'open'
        g['items'][r['item_name']] = g['items'].get(r['item_name'], 0) + r['quantity']
        g['quantity'] += r['quantity']
        g['value'] = round(g['value'] + value, 2)
    conn.close()

    group_list = []
    for g in groups.values():
        g['items'] = [{'item_name': n, 'quantity': q} for n, q in g['items'].items()]
        group_list.append(g)
    return {
        'kind': kind, 'kind_label': COURTESY_KIND_LABELS[kind],
        'start_date': start_date, 'end_date': end_date,
        'generated_at': datetime.now().strftime('%d/%m/%Y %I:%M %p'),
        'groups': group_list, 'lines': lines,
        'total_quantity': sum(l['quantity'] for l in lines),
        'total_value': round(sum(l['value'] for l in lines), 2),
        'days': len({l['day'] for l in lines}),
        'rooms': len({(l['day'], l['room_number']) for l in lines}) if kind == 'breakfast' else None,
        'accounts': len(group_list) if kind == 'owners' else None,
    }


PRINT_MAX_ATTEMPTS = 5
PRINT_RETRY_SECONDS = 20


def due_print_jobs(limit=20):
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM print_jobs
        WHERE status = 'pending' AND next_attempt_at <= now_local()
        ORDER BY id LIMIT ?
    ''', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_print_job_printed(job_id):
    conn = get_connection(write=True)
    conn.execute("UPDATE print_jobs SET status = 'printed', attempts = attempts + 1, last_error = NULL, "
                 "printed_at = now_local() WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()


def mark_print_job_failed(job_id, error):
    """Records a failed send. The job is retried every PRINT_RETRY_SECONDS
    (the printer may just be out of paper) until PRINT_MAX_ATTEMPTS, then
    left as 'failed' for someone to fix the printer and press Reimprimir.
    Returns True when the job has now given up."""
    conn = get_connection(write=True)
    job = conn.execute('SELECT attempts FROM print_jobs WHERE id = ?', (job_id,)).fetchone()
    attempts = (job['attempts'] if job else 0) + 1
    gave_up = attempts >= PRINT_MAX_ATTEMPTS
    conn.execute(f'''
        UPDATE print_jobs SET attempts = ?, last_error = ?, status = ?,
               next_attempt_at = to_char(LOCALTIMESTAMP + interval '{PRINT_RETRY_SECONDS} seconds', 'YYYY-MM-DD HH24:MI:SS')
        WHERE id = ?
    ''', (attempts, str(error)[:300], 'failed' if gave_up else 'pending', job_id))
    conn.commit()
    conn.close()
    return gave_up


def requeue_print_job(job_id):
    conn = get_connection(write=True)
    cur = conn.execute("UPDATE print_jobs SET status = 'pending', attempts = 0, last_error = NULL, "
                       "next_attempt_at = now_local() WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError('Comanda no encontrada')


def requeue_failed_print_jobs():
    conn = get_connection(write=True)
    cur = conn.execute("UPDATE print_jobs SET status = 'pending', attempts = 0, last_error = NULL, "
                       "next_attempt_at = now_local() WHERE status = 'failed'")
    conn.commit()
    conn.close()
    return cur.rowcount


def list_print_jobs(limit=50):
    conn = get_connection()
    rows = conn.execute('SELECT * FROM print_jobs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    jobs = []
    for r in rows:
        job = dict(r)
        payload = json.loads(job.pop('payload'))
        job.update(receipt_number=payload.get('receipt_number'), table_number=payload.get('table_number'),
                   station_label=payload.get('station_label'), is_addition=payload.get('is_addition'),
                   items=payload.get('items', []))
        jobs.append(job)
    return jobs


def print_status():
    """Counts the POS polls to warn staff that a printer isn't printing."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT station, status, COUNT(*) AS c FROM print_jobs
        WHERE status = 'failed' OR (status = 'pending' AND attempts > 0)
        GROUP BY station, status
    ''').fetchall()
    conn.close()
    stuck = {}
    for r in rows:
        stuck[r['station']] = stuck.get(r['station'], 0) + r['c']
    return {'stuck': [{'station': s, 'station_label': STATION_LABELS_ES[s], 'count': c} for s, c in stuck.items()]}


def add_items_to_order(order_id, items):
    """Adds more items to a still-open ticket — 'the client wants to order more'."""
    if not items:
        raise ValueError('Se requiere al menos un artículo')

    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open':
        conn.close()
        raise ValueError(f"La orden está {ORDER_STATUS_LABELS_ES.get(order['status'], order['status'])} — solo se pueden agregar artículos a órdenes abiertas")
    if order['guest_type'] == 'breakfast':
        conn.close()
        raise ValueError('Los desayunos se agregan con "Registrar Desayuno", indicando la habitación')
    items = _priced_order_items(conn, items)

    cur = conn.cursor()
    for i in items:
        line_id = _insert_order_line(cur, order_id, i, order['guest_type'])
        _deplete_inventory_for_order_item(conn, order_id, line_id, i['name'], i['quantity'])

    _recompute_order_totals(conn, order_id)
    conn.commit()

    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def set_order_item_notes(order_id, order_item_id, notes):
    """Changes the note on a line not yet sent to the kitchen (once sent, the
    kitchen already has the ticket: tell them in person or add a new line)."""
    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order or order['status'] != 'open':
        conn.close()
        raise ValueError('Solo se pueden cambiar cuentas abiertas')
    item = conn.execute('SELECT * FROM order_items WHERE id = ? AND order_id = ?', (order_item_id, order_id)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo no encontrado en esta orden')
    if item['sent_quantity'] > 0:
        conn.close()
        raise ValueError('Ese plato ya se envió a cocina: avise en persona o agregue el plato de nuevo con la nota')
    conn.execute('UPDATE order_items SET notes = ? WHERE id = ?',
                 (' '.join(str(notes or '').split())[:MAX_LINE_NOTE] or None, order_item_id))
    conn.commit()
    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def set_order_item_included(order_id, order_item_id, included):
    """Marks a line of a hotel guest's tab as included breakfast (C$0) or
    back to a charged item (menu price)."""
    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open':
        conn.close()
        raise ValueError('Solo se pueden cambiar cuentas abiertas')
    if order['guest_type'] != 'hotel_guest':
        conn.close()
        raise ValueError('Solo la cuenta de un huésped del hotel lleva desayunos incluidos')
    item = conn.execute('SELECT * FROM order_items WHERE id = ? AND order_id = ?', (order_item_id, order_id)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo no encontrado en esta orden')
    price = item['list_price'] if item['list_price'] is not None else item['unit_price']
    if price == 0:   # an old line already at 0: take the menu price
        menu = conn.execute('SELECT price FROM menu_items WHERE name = ?', (item['item_name'],)).fetchone()
        price = menu['price'] if menu else 0
    conn.execute('UPDATE order_items SET included = ?, unit_price = ?, list_price = ? WHERE id = ?',
                 (1 if included else 0, 0 if included else price, price, order_item_id))
    _recompute_order_totals(conn, order_id)
    conn.commit()
    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def delete_order_item(order_id, order_item_id, deleted_by_user_id=None):
    """Removes one line from a still-open ticket — 'the client changed their
    mind about this item' — and restocks exactly what that line depleted."""
    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open':
        conn.close()
        raise ValueError(f"La orden está {ORDER_STATUS_LABELS_ES.get(order['status'], order['status'])} — solo se pueden quitar artículos de órdenes abiertas")

    item = conn.execute('SELECT * FROM order_items WHERE id = ? AND order_id = ?', (order_item_id, order_id)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo no encontrado en esta orden')

    remaining = conn.execute('SELECT COUNT(*) AS c FROM order_items WHERE order_id = ?', (order_id,)).fetchone()['c']
    if remaining <= 1:
        conn.close()
        raise ValueError('No se puede eliminar el último artículo — cancele toda la orden en su lugar')

    txns = conn.execute('''
        SELECT inventory_item_id, quantity_change, pool FROM inventory_transactions
        WHERE order_item_id = ? AND reason = 'order_depletion'
    ''', (order_item_id,)).fetchall()
    for t in txns:
        restock = -t['quantity_change']
        _apply_inventory_movement(conn, t['inventory_item_id'], t['pool'], restock, 'item_removed',
                                   order_id=order_id, order_item_id=order_item_id)

    conn.execute('DELETE FROM order_items WHERE id = ?', (order_item_id,))
    _recompute_order_totals(conn, order_id)
    _log_activity(conn, deleted_by_user_id, 'order_item_removed',
                  f"Eliminó {item['quantity']} x {item['item_name']} de la orden #{order['receipt_number']}")
    conn.commit()

    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def update_order_item_quantity(order_id, order_item_id, new_quantity, changed_by_user_id=None):
    """Changes how many of an existing line the guest wants — the common
    'actually make that two' edit — depleting or restocking exactly the
    difference. new_quantity <= 0 removes the line, same as delete_order_item
    (including its 'can't remove the last item' rule)."""
    if new_quantity <= 0:
        return delete_order_item(order_id, order_item_id, deleted_by_user_id=changed_by_user_id)
    if new_quantity > MAX_ITEM_QUANTITY:
        raise ValueError(f'La cantidad no puede ser mayor que {MAX_ITEM_QUANTITY}')

    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open':
        conn.close()
        raise ValueError(f"La orden está {ORDER_STATUS_LABELS_ES.get(order['status'], order['status'])} — solo se pueden modificar artículos de órdenes abiertas")

    item = conn.execute('SELECT * FROM order_items WHERE id = ? AND order_id = ?', (order_item_id, order_id)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo no encontrado en esta orden')

    delta = new_quantity - item['quantity']
    if delta != 0:
        menu_item = conn.execute('SELECT id FROM menu_items WHERE name = ?', (item['item_name'],)).fetchone()
        if menu_item:
            pool = _menu_item_pool(conn, item['item_name'])
            recipe_lines = conn.execute(
                'SELECT inventory_item_id, quantity_required FROM recipes WHERE menu_item_id = ?',
                (menu_item['id'],)
            ).fetchall()
            for line in recipe_lines:
                change = -1 * line['quantity_required'] * float(delta)
                _apply_inventory_movement(conn, line['inventory_item_id'], pool, change, 'order_depletion',
                                           order_id=order_id, order_item_id=order_item_id)
        conn.execute('UPDATE order_items SET quantity = ? WHERE id = ?', (new_quantity, order_item_id))
        _recompute_order_totals(conn, order_id)
        conn.commit()

    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def close_order(order_id, payment_method, closed_by=None, amount_tendered=None):
    """Checkout: the guest is ready to pay — pick cash/card/room charge and settle the tab.
    closed_by is whoever settled it; their shift close counts it. A paid account
    paid in cash or card takes the next invoice number (a room charge is invoiced
    with the stay at check-out); amount_tendered (cash only) gives the change."""
    if payment_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {PAYMENT_METHODS}")

    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] != 'open':
        conn.close()
        raise ValueError(f"La orden ya está {ORDER_STATUS_LABELS_ES.get(order['status'], order['status'])}")
    if order['guest_type'] == 'breakfast':
        conn.close()
        raise ValueError('La cuenta de Desayunos no se cierra a mano: se cierra sola al día siguiente')
    if order['guest_type'] != 'hotel_guest' and payment_method == 'room_charge':
        conn.close()
        raise ValueError('Los clientes externos / pase de día no tienen habitación a la cual cargar')
    # Master accounts always close as Cuenta de la Casa, and only they may.
    if order['guest_type'] in MASTER_ACCOUNT_TYPES:
        payment_method = 'house'
    elif payment_method == 'house':
        conn.close()
        raise ValueError('Solo las cuentas de Propietarios y Desayunos se cierran como Cuenta de la Casa')

    _recompute_order_totals(conn, order_id)
    fresh = dict(conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    due = order_revenue({**fresh, 'payment_method': payment_method})
    tendered = None
    if payment_method == 'cash' and amount_tendered not in (None, ''):
        try:
            tendered = round(float(amount_tendered), 2)
        except (TypeError, ValueError):
            conn.close()
            raise ValueError('El efectivo recibido debe ser un número')
        if tendered < due - 0.005:
            conn.close()
            raise ValueError(f'El efectivo recibido (C${tendered:,.2f}) es menor que el total (C${due:,.2f})')
    invoice = _take_invoice_number(conn) if payment_method in ('cash', 'card') and due > 0.005 else None
    # Items added but never confirmed with ✓ Listo still have to be made.
    _send_unsent_lines(conn, order_id, is_addition=True)
    conn.execute('''
        UPDATE orders SET status = 'closed', payment_method = ?, closed_at = now_local(), closed_by = ?,
                          invoice_number = ?, amount_tendered = ?
        WHERE id = ?
    ''', (payment_method, closed_by, invoice, tendered, order_id))
    conn.commit()

    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def _restock_inventory_for_order(conn, order_id):
    """Reverses whatever this order actually depleted, exactly (not
    recomputed from the current recipe, in case it changed since)."""
    txns = conn.execute('''
        SELECT inventory_item_id, quantity_change, pool FROM inventory_transactions
        WHERE order_id = ? AND reason = 'order_depletion'
    ''', (order_id,)).fetchall()
    for t in txns:
        restock = -t['quantity_change']
        _apply_inventory_movement(conn, t['inventory_item_id'], t['pool'], restock, 'order_cancelled',
                                   order_id=order_id)


def request_order_cancellation(order_id, requested_by_user_id, reason=None):
    """A server flags an order for cancellation — front desk/admin must authorize it."""
    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if order['status'] == 'cancelled':
        conn.close()
        raise ValueError('La orden ya está cancelada')
    if order['cancel_requested_at']:
        conn.close()
        raise ValueError('Ya hay una solicitud de cancelación pendiente para esta orden')

    conn.execute('''
        UPDATE orders SET cancel_requested_at = now_local(),
                           cancel_requested_by = ?, cancel_reason = ?
        WHERE id = ?
    ''', (requested_by_user_id, reason, order_id))
    details = f"Orden #{order['receipt_number']}"
    if reason:
        details += f" — {reason}"
    _log_activity(conn, requested_by_user_id, 'cancel_requested', details)
    conn.commit()
    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def list_pending_cancellations():
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM orders WHERE cancel_requested_at IS NOT NULL AND status != 'cancelled'
        ORDER BY cancel_requested_at ASC
    ''').fetchall()
    orders = [_hydrate_order(conn, r) for r in rows]
    conn.close()
    return orders


def decide_order_cancellation(order_id, approver_user_id, approve, note=None):
    """Front desk / admin authorizes (or rejects) a pending cancellation request."""
    conn = get_connection(write=True)
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        raise ValueError('Orden no encontrada')
    if not order['cancel_requested_at']:
        conn.close()
        raise ValueError('Esta orden no tiene ninguna solicitud de cancelación pendiente')

    if approve:
        was_closed_room_charge = order['status'] == 'closed' and order['payment_method'] == 'room_charge'
        _restock_inventory_for_order(conn, order_id)
        conn.execute('''
            UPDATE orders SET status = 'cancelled', cancelled_at = now_local(),
                               cancelled_by = ?, cancel_decision_note = ?
            WHERE id = ?
        ''', (approver_user_id, note, order_id))
        action, details = 'cancel_approved', f"Orden #{order['receipt_number']}"
    else:
        conn.execute('''
            UPDATE orders SET cancel_requested_at = NULL, cancel_requested_by = NULL, cancel_reason = NULL,
                               cancel_decision_note = ?
            WHERE id = ?
        ''', (note, order_id))
        action, details = 'cancel_rejected', f"Orden #{order['receipt_number']}"
    if note:
        details += f" — {note}"
    _log_activity(conn, approver_user_id, action, details)
    conn.commit()
    updated = _hydrate_order(conn, conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone())
    conn.close()
    return updated


def list_orders(limit=100, server_id=None, include_cancelled=True):
    """include_cancelled=False is used for the server-facing POS view — front
    desk and admin still see everything (list_orders is unaffected there)."""
    conn = get_connection()
    where = []
    params = []
    if server_id:
        where.append('server_id = ?')
        params.append(server_id)
    if not include_cancelled:
        where.append("status != 'cancelled'")
    clause = f"WHERE {' AND '.join(where)}" if where else ''
    rows = conn.execute(
        f'SELECT * FROM orders {clause} ORDER BY timestamp DESC LIMIT ?', (*params, limit)
    ).fetchall()
    orders = [_hydrate_order(conn, r) for r in rows]
    conn.close()
    return orders


def get_order(order_id):
    conn = get_connection()
    row = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    order = _hydrate_order(conn, row) if row else None
    conn.close()
    return order


# ----------------------------------------------------------- Analytics ----

def server_performance():
    conn = get_connection()
    servers = conn.execute("SELECT * FROM users WHERE role = 'server' ORDER BY full_name").fetchall()
    results = []
    for s in servers:
        orders = [dict(o) for o in conn.execute(
            "SELECT * FROM orders WHERE server_id = ? AND status = 'closed'", (s['id'],)
        ).fetchall()]
        total_orders = len(orders)
        total_revenue = round(sum(order_revenue(o) for o in orders), 2)
        tables_served = len({o['table_number'] for o in orders if o['table_number']})
        room_charge_orders = sum(1 for o in orders if o['payment_method'] == 'room_charge')
        results.append({
            'server_id': s['id'],
            'full_name': s['full_name'],
            'username': s['username'],
            'total_orders': total_orders,
            'total_revenue': total_revenue,
            'tables_served': tables_served,
            'room_charge_orders': room_charge_orders,
        })
    conn.close()
    return results


def revenue_summary():
    conn = get_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT payment_method, guest_type, subtotal FROM orders WHERE status = 'closed'"
    ).fetchall()]
    conn.close()
    total_revenue = round(sum(order_revenue(r) for r in rows), 2)
    by_method = {'cash': 0.0, 'card': 0.0, 'room_charge': 0.0}
    for r in rows:
        if r['payment_method'] in by_method:   # 'house' courtesies bring in nothing
            by_method[r['payment_method']] += order_revenue(r)
    for k in by_method:
        by_method[k] = round(by_method[k], 2)
    return {
        'total_orders': len(rows),
        'total_revenue': total_revenue,
        'by_payment_method': by_method,
    }


def get_dashboard_kpis():
    """Executive KPIs computed from live data — occupancy, ADR, RevPAR,
    RevPOR (room + in-house restaurant revenue per occupied room), and
    today's arrival/departure schedule."""
    conn = get_connection()
    today = datetime.now().date().isoformat()

    total_rooms = conn.execute('SELECT COUNT(*) AS c FROM rooms').fetchone()['c']
    checked_in = conn.execute("SELECT nightly_rate FROM guests WHERE status = 'checked_in'").fetchall()
    occupied_count = len(checked_in)
    room_revenue_today = sum(r['nightly_rate'] for r in checked_in)

    orders_today = conn.execute('''
        SELECT * FROM orders
        WHERE status = 'closed' AND left(closed_at, 10) = today_local() AND guest_type = 'hotel_guest'
    ''').fetchall()
    restaurant_revenue_today = sum(order_revenue(dict(o)) for o in orders_today)

    adr = round(room_revenue_today / occupied_count, 2) if occupied_count else 0.0
    revpar = round(room_revenue_today / total_rooms, 2) if total_rooms else 0.0
    revpor = round((room_revenue_today + restaurant_revenue_today) / occupied_count, 2) if occupied_count else 0.0
    occupancy_rate = round(occupied_count / total_rooms * 100, 1) if total_rooms else 0.0

    arrivals_today = conn.execute('''
        SELECT COUNT(*) AS c FROM guests WHERE checkin_date = ? AND status IN ('reserved', 'checked_in')
    ''', (today,)).fetchone()['c']
    departures_today = conn.execute('''
        SELECT COUNT(*) AS c FROM guests WHERE checkout_date = ? AND status IN ('checked_in', 'checked_out')
    ''', (today,)).fetchone()['c']
    pending_reservations = conn.execute("SELECT COUNT(*) AS c FROM guests WHERE status = 'reserved'").fetchone()['c']

    low_stock_rows = conn.execute(
        'SELECT item_name, unit_of_measure, storeroom_stock, low_stock_threshold FROM inventory_items '
        'WHERE storeroom_stock <= low_stock_threshold ORDER BY item_name'
    ).fetchall()
    low_stock_items = [dict(r) for r in low_stock_rows]

    conn.close()

    return {
        'total_rooms': total_rooms,
        'occupied_rooms': occupied_count,
        'available_rooms': total_rooms - occupied_count,
        'occupancy_rate': occupancy_rate,
        'adr': adr,
        'revpar': revpar,
        'revpor': revpor,
        'room_revenue_today': round(room_revenue_today, 2),
        'restaurant_revenue_today': round(restaurant_revenue_today, 2),
        'arrivals_today': arrivals_today,
        'departures_today': departures_today,
        'pending_reservations': pending_reservations,
        'low_stock_count': len(low_stock_items),
        'low_stock_items': low_stock_items,
    }


def get_revenue_trend(days=8):
    """Room + restaurant revenue for each of the last `days` days, oldest first."""
    conn = get_connection()
    today = datetime.now().date()
    series = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        room_rev = conn.execute('''
            SELECT COALESCE(SUM(nightly_rate), 0) AS total FROM guests
            WHERE status IN ('checked_in', 'checked_out') AND checkin_date <= ? AND checkout_date > ?
        ''', (day, day)).fetchone()['total']
        orders = conn.execute(
            "SELECT * FROM orders WHERE status = 'closed' AND left(closed_at, 10) = ?", (day,)
        ).fetchall()
        restaurant_rev = sum(order_revenue(dict(o)) for o in orders)
        series.append({
            'date': day,
            'room_revenue': round(room_rev, 2),
            'restaurant_revenue': round(restaurant_rev, 2),
            'total_revenue': round(room_rev + restaurant_rev, 2),
        })
    conn.close()
    return series


# ------------------------------------------------------- Periodic reports --

def list_checkouts_in_range(start_date, end_date):
    """Guests checked out within [start_date, end_date] (YYYY-MM-DD, inclusive)."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM guests
        WHERE status = 'checked_out'
          AND checked_out_at IS NOT NULL
          AND left(checked_out_at, 10) BETWEEN left(?, 10) AND left(?, 10)
        ORDER BY checked_out_at ASC
    ''', (start_date, end_date)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_orders_in_range(start_date, end_date):
    """Closed (paid) orders, by the date they were actually settled, within
    [start_date, end_date] inclusive."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM orders
        WHERE status = 'closed' AND left(closed_at, 10) BETWEEN left(?, 10) AND left(?, 10)
        ORDER BY closed_at ASC
    ''', (start_date, end_date)).fetchall()
    orders = []
    for r in rows:
        o = dict(r)
        server = conn.execute('SELECT full_name FROM users WHERE id = ?', (o['server_id'],)).fetchone()
        o['server_name'] = server['full_name'] if server else 'Unknown'
        o['amount_collected'] = order_revenue(o)
        orders.append(o)
    conn.close()
    return orders


def list_cancelled_orders_in_range(start_date, end_date):
    """For the periodic report's transparency sheet — every server/front
    desk/admin cancellation decided within the period, so owners can see
    what was voided and why, even though it's excluded from revenue."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM orders
        WHERE status = 'cancelled' AND left(cancelled_at, 10) BETWEEN left(?, 10) AND left(?, 10)
        ORDER BY cancelled_at ASC
    ''', (start_date, end_date)).fetchall()
    orders = []
    for r in rows:
        o = dict(r)
        server = conn.execute('SELECT full_name FROM users WHERE id = ?', (o['server_id'],)).fetchone()
        o['server_name'] = server['full_name'] if server else 'Unknown'
        req = conn.execute('SELECT full_name FROM users WHERE id = ?', (o['cancel_requested_by'],)).fetchone()
        o['cancel_requested_by_name'] = req['full_name'] if req else 'Unknown'
        approver = conn.execute('SELECT full_name FROM users WHERE id = ?', (o['cancelled_by'],)).fetchone()
        o['cancelled_by_name'] = approver['full_name'] if approver else 'Unknown'
        orders.append(o)
    conn.close()
    return orders


def list_inventory_transactions_in_range(start_date, end_date):
    conn = get_connection()
    rows = conn.execute('''
        SELECT t.*, i.item_name, i.unit_of_measure
        FROM inventory_transactions t
        JOIN inventory_items i ON i.id = t.inventory_item_id
        WHERE left(t.timestamp, 10) BETWEEN left(?, 10) AND left(?, 10)
        ORDER BY t.timestamp ASC
    ''', (start_date, end_date)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_report_run(report_type, period_start, period_end, file_name, file_path, cloud_synced=False,
                       cloud_path=None, emailed=False, email_error=None, triggered_by='scheduled'):
    if report_type not in REPORT_TYPES:
        raise ValueError(f"report_type must be one of {REPORT_TYPES}")
    conn = get_connection(write=True)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO report_runs
            (report_type, period_start, period_end, file_name, file_path, cloud_synced, cloud_path,
             emailed, email_error, triggered_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (report_type, period_start, period_end, file_name, file_path, int(cloud_synced), cloud_path,
          int(emailed), email_error, triggered_by))
    conn.commit()
    run = dict(conn.execute('SELECT * FROM report_runs WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return run


def list_report_runs(report_type=None, limit=50):
    conn = get_connection()
    if report_type:
        rows = conn.execute(
            'SELECT * FROM report_runs WHERE report_type = ? ORDER BY generated_at DESC LIMIT ?',
            (report_type, limit)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM report_runs ORDER BY generated_at DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_report_run(run_id):
    conn = get_connection()
    row = conn.execute('SELECT * FROM report_runs WHERE id = ?', (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# --------------------------------------------------------------- Backups ---

def create_backup_run(kind, file_name, file_path, file_size_bytes=0, cloud_synced=False,
                       cloud_path=None, triggered_by='scheduled'):
    conn = get_connection(write=True)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO backup_runs (kind, file_name, file_path, file_size_bytes, cloud_synced, cloud_path, triggered_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (kind, file_name, file_path, file_size_bytes, int(cloud_synced), cloud_path, triggered_by))
    conn.commit()
    run = dict(conn.execute('SELECT * FROM backup_runs WHERE id = ?', (cur.lastrowid,)).fetchone())
    conn.close()
    return run


def list_backup_runs(kind=None, limit=50):
    conn = get_connection()
    if kind:
        rows = conn.execute(
            'SELECT * FROM backup_runs WHERE kind = ? ORDER BY created_at DESC LIMIT ?', (kind, limit)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM backup_runs ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_backup_run(run_id):
    conn = get_connection()
    row = conn.execute('SELECT * FROM backup_runs WHERE id = ?', (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------- Cashier shifts --

PAYMENT_METHOD_LABELS_ES = {'cash': 'Efectivo', 'card': 'Tarjeta', 'room_charge': 'Cargo a habitación',
                            'house': 'Cortesía (cuenta de la casa)'}


def get_open_shift(user_id):
    conn = get_connection()
    row = conn.execute('SELECT * FROM cash_shifts WHERE user_id = ? AND closed_at IS NULL', (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def open_shift(user_id, opening_cash=0):
    try:
        opening_cash = round(float(opening_cash or 0), 2)
    except (TypeError, ValueError):
        raise ValueError('El fondo inicial debe ser un número')
    if opening_cash < 0:
        raise ValueError('El fondo inicial no puede ser negativo')
    conn = get_connection(write=True)
    if conn.execute('SELECT 1 FROM cash_shifts WHERE user_id = ? AND closed_at IS NULL', (user_id,)).fetchone():
        conn.close()
        raise ValueError('Ya tiene un turno abierto')
    cur = conn.cursor()
    cur.execute('INSERT INTO cash_shifts (user_id, opening_cash) VALUES (?, ?)', (user_id, opening_cash))
    shift_id = cur.lastrowid
    _log_activity(conn, user_id, 'shift_opened', f'Abrió turno de caja · fondo C${opening_cash:,.2f}')
    conn.commit()
    row = dict(conn.execute('SELECT * FROM cash_shifts WHERE id = ?', (shift_id,)).fetchone())
    conn.close()
    return row


def _shift_summary(conn, shift, until):
    """The cashier's close: every account she settled between opening and
    `until`, totals per payment method, and the accounts still open."""
    rows = conn.execute('''
        SELECT o.*, u.full_name AS server_name, g.name AS guest_name, g.room_number
        FROM orders o
        JOIN users u ON u.id = o.server_id
        LEFT JOIN guests g ON g.id = o.guest_id
        WHERE o.status = 'closed' AND o.closed_by = ? AND o.closed_at >= ? AND o.closed_at <= ?
        ORDER BY o.closed_at
    ''', (shift['user_id'], shift['opened_at'], until)).fetchall()
    by_method = {m: {'method': m, 'label': PAYMENT_METHOD_LABELS_ES[m], 'count': 0, 'amount': 0.0}
                 for m in ('cash', 'card', 'room_charge', 'house')}
    orders = []
    for r in rows:
        o = dict(r)
        # Cash/card: what was actually collected (a day pass is never less
        # than its fee). Room charges and courtesies: the account's value.
        amount = order_revenue(o) if o['payment_method'] in ('cash', 'card') else o['subtotal']
        m = by_method[o['payment_method']]
        m['count'] += 1
        m['amount'] = round(m['amount'] + amount, 2)
        orders.append({'receipt_number': o['receipt_number'], 'opened_at': o['timestamp'], 'closed_at': o['closed_at'],
                       'who': (f"Hab. {o['room_number']} — {o['guest_name']}" if o['guest_name']
                               else o['account_name'] or GUEST_TYPE_LABELS_ES.get(o['guest_type'], o['guest_type'])),
                       'table_number': o['table_number'], 'server_name': o['server_name'],
                       'method': PAYMENT_METHOD_LABELS_ES[o['payment_method']],
                       'amount': round(amount, 2)})
    # Unpaid tables are never closed by the shift: they stay open and are
    # charged (and counted) in whichever shift collects them.
    pending = [dict(r) for r in conn.execute(f'''
        SELECT o.receipt_number, o.table_number, o.timestamp, o.subtotal, o.account_name, o.guest_type,
               u.full_name AS server_name, g.name AS guest_name, g.room_number
        FROM orders o
        JOIN users u ON u.id = o.server_id
        LEFT JOIN guests g ON g.id = o.guest_id
        WHERE o.status = 'open' AND o.guest_type NOT IN ({','.join('?' * len(MASTER_ACCOUNT_TYPES))})
        ORDER BY o.table_number, o.timestamp
    ''', MASTER_ACCOUNT_TYPES).fetchall()]
    pending_orders = [{'receipt_number': p['receipt_number'], 'table_number': p['table_number'], 'opened_at': p['timestamp'],
                       'who': (f"Hab. {p['room_number']} — {p['guest_name']}" if p['guest_name']
                               else p['account_name'] or GUEST_TYPE_LABELS_ES.get(p['guest_type'], p['guest_type'])),
                       'server_name': p['server_name'], 'amount': round(p['subtotal'], 2)} for p in pending]
    return {
        'order_count': len(orders),
        'methods': list(by_method.values()),
        'collected_total': round(by_method['cash']['amount'] + by_method['card']['amount'], 2),
        'expected_cash': round(shift['opening_cash'] + by_method['cash']['amount'], 2),
        'open_accounts': len(pending_orders),
        'open_accounts_total': round(sum(p['amount'] for p in pending_orders), 2),
        'pending_orders': pending_orders,
        'orders': orders,
    }


def get_shift(shift_id):
    conn = get_connection()
    row = conn.execute('''
        SELECT s.*, u.full_name AS user_name FROM cash_shifts s JOIN users u ON u.id = s.user_id WHERE s.id = ?
    ''', (shift_id,)).fetchone()
    if not row:
        conn.close()
        return None
    shift = dict(row)
    if shift['summary']:
        shift['summary'] = json.loads(shift['summary'])
    else:   # still open: the close so far
        shift['summary'] = _shift_summary(conn, shift, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    conn.close()
    return shift


def close_shift(user_id, counted_cash=None, notes=None):
    """Closes the user's open shift and freezes its summary (what gets printed)."""
    if counted_cash in (None, ''):
        counted_cash = None
    else:
        try:
            counted_cash = round(float(counted_cash), 2)
        except (TypeError, ValueError):
            raise ValueError('El efectivo contado debe ser un número')
    conn = get_connection(write=True)
    row = conn.execute('SELECT * FROM cash_shifts WHERE user_id = ? AND closed_at IS NULL', (user_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError('No tiene un turno abierto')
    shift = dict(row)
    closed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    summary = _shift_summary(conn, shift, closed_at)
    if counted_cash is not None:
        summary['counted_cash'] = counted_cash
        summary['cash_difference'] = round(counted_cash - summary['expected_cash'], 2)
    conn.execute('UPDATE cash_shifts SET closed_at = ?, counted_cash = ?, notes = ?, summary = ? WHERE id = ?',
                 (closed_at, counted_cash, (notes or '').strip() or None, json.dumps(summary), shift['id']))
    _log_activity(conn, user_id, 'shift_closed',
                  f"Cerró turno de caja · {summary['order_count']} cuenta(s) · cobrado C${summary['collected_total']:,.2f}")
    conn.commit()
    conn.close()
    return get_shift(shift['id'])


def list_shifts(user_id=None, limit=30):
    """Recent shifts (one cashier's, or everyone's for an admin)."""
    conn = get_connection()
    where, params = ('WHERE s.user_id = ?', [user_id]) if user_id else ('', [])
    rows = conn.execute(f'''
        SELECT s.id, s.user_id, s.opened_at, s.closed_at, s.opening_cash, s.counted_cash, s.summary,
               u.full_name AS user_name
        FROM cash_shifts s JOIN users u ON u.id = s.user_id {where}
        ORDER BY s.id DESC LIMIT ?
    ''', (*params, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        summ = json.loads(d.pop('summary')) if d['summary'] else None
        d['order_count'] = summ['order_count'] if summ else None
        d['collected_total'] = summ['collected_total'] if summ else None
        out.append(d)
    return out


# ---------------------------------------------------------- Shift history --
# 'Históricos de Turno': the old POS's per-shift reports. Each section has
# what it is, a few headline numbers and its full table (what the Excel has).
# Columns are [label, kind] with kind text / int / money / datetime.

_POOL_LABELS_ES = {'restaurant': 'Restaurante', 'bar': 'Bar', 'storeroom': 'Bodega Principal',
                   'maintenance': 'Mantenimiento'}
_ITEM_COLS = [['Artículo', 'text'], ['Sección', 'text'], ['Cant', 'int'], ['Total', 'money']]


def _order_who(o):
    if o.get('guest_name'):
        return f"Hab. {o['room_number']} — {o['guest_name']}"
    return o.get('account_name') or GUEST_TYPE_LABELS_ES.get(o['guest_type'], o['guest_type'])


def _order_table(o):
    """'Mesa 5', 'Mesa 5 · cuenta 2', or '—' for an account without a table."""
    if not o.get('table_number'):
        return '—'
    return f"Mesa {o['table_number']}" + (f" · cuenta {o['sub_account']}" if (o.get('sub_account') or 1) > 1 else '')


def _order_amount(o):
    """Cash/card: what was collected (a day pass is never less than its fee);
    room charges, courtesies and open accounts: the account's value."""
    return round(order_revenue(o) if o.get('payment_method') in ('cash', 'card') else o['subtotal'], 2)


def _item_rows(lines):
    agg = {}
    for l in lines:
        a = agg.setdefault(l['item_name'], {'order': l['section_order'], 'section': l['section_label'], 'qty': 0, 'total': 0.0})
        a['qty'] += l['quantity']
        a['total'] += l['quantity'] * l['unit_price']
    ordered = sorted(agg.items(), key=lambda kv: (kv[1]['order'], kv[0].lower()))
    return [[name, a['section'], a['qty'], round(a['total'], 2)] for name, a in ordered]


def _sum_col(rows, i):
    return round(sum(r[i] or 0 for r in rows), 2)


def _sec(key, title, description, columns, rows, summary, total=None):
    return {'key': key, 'title': title, 'description': description, 'columns': columns, 'rows': rows,
            'total': total, 'summary': [{'label': l, 'value': v, 'kind': k} for l, v, k in summary]}


def _fmt_dt_es(v):
    return datetime.strptime(v, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %I:%M %p') if v else ''


def shift_history(shift_id):
    """Every report of one cashier shift (closed or still open)."""
    shift = get_shift(shift_id)
    if not shift:
        return None
    summ = shift['summary']
    data = _history_report(shift['opened_at'], shift['closed_at'] or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           shift['user_id'], {
                               'opening_cash': shift['opening_cash'], 'counted_cash': summ.get('counted_cash'),
                               'cash_difference': summ.get('cash_difference'),
                               'no_count_text': 'Sin contar' if shift['closed_at'] else 'Turno abierto'})
    data['shift'] = {'id': shift['id'], 'user_name': shift['user_name'], 'opened_at': shift['opened_at'],
                     'closed_at': shift['closed_at'], 'opening_cash': shift['opening_cash'],
                     'counted_cash': shift['counted_cash'], 'order_count': data['order_count'],
                     'collected_total': data['collected_total']}
    data['header_lines'] = [f"Turno #{shift['id']} · Cajero: {shift['user_name']}",
                            f"Apertura: {_fmt_dt_es(shift['opened_at'])}",
                            f"Cierre: {_fmt_dt_es(shift['closed_at']) if shift['closed_at'] else 'turno abierto'}"]
    return data


def general_history(date_from, date_to):
    """All cashiers together for a day or a date range — the old POS's single
    shift. Fondo, contado and diferencia add up the cashier shifts opened in it."""
    try:
        d1 = datetime.strptime(date_from or '', '%Y-%m-%d').date()
        d2 = datetime.strptime(date_to or '', '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('Elija las fechas del periodo')
    if d2 < d1:
        raise ValueError('La fecha final no puede ser anterior a la inicial')
    if (d2 - d1).days > 366:
        raise ValueError('El periodo no puede pasar de un año')
    start, until = f'{d1.isoformat()} 00:00:00', f'{d2.isoformat()} 23:59:59'
    conn = get_connection()
    shifts = [dict(r) for r in conn.execute("""
        SELECT s.*, u.full_name AS user_name FROM cash_shifts s JOIN users u ON u.id = s.user_id
        WHERE s.opened_at >= ? AND s.opened_at <= ? ORDER BY s.opened_at""", (start, until)).fetchall()]
    conn.close()
    diffs = [json.loads(s['summary']).get('cash_difference') for s in shifts if s['summary']]
    counted = [s['counted_cash'] for s in shifts if s['counted_cash'] is not None]
    data = _history_report(start, until, None, {
        'opening_cash': round(sum(s['opening_cash'] for s in shifts), 2),
        'counted_cash': round(sum(counted), 2) if counted else None,
        'cash_difference': round(sum(d for d in diffs if d is not None), 2) if any(d is not None for d in diffs) else None,
        'no_count_text': 'Sin contar'})
    cashiers = sorted({s['user_name'] for s in shifts})
    period = d1.strftime('%d/%m/%Y') + ('' if d1 == d2 else f" al {d2.strftime('%d/%m/%Y')}")
    data['general'] = {'from': d1.isoformat(), 'to': d2.isoformat(), 'period': period, 'shift_count': len(shifts),
                       'cashiers': cashiers, 'opening_cash': data['opening_cash'],
                       'order_count': data['order_count'], 'collected_total': data['collected_total']}
    data['header_lines'] = [f'Histórico general · {period}',
                            f"Turnos de caja: {len(shifts)}" + (f" ({', '.join(cashiers)})" if cashiers else '')]
    return data


def _history_report(start, until, cashier_id, cash_info):
    """The report sections for a time window: accounts closed in it (by one
    cashier, or by anyone), the ones left open, the cancelled ones."""
    conn = get_connection()
    base = '''SELECT o.*, u.full_name AS server_name, g.name AS guest_name, g.room_number,
                     cu.full_name AS cancelled_by_name
              FROM orders o JOIN users u ON u.id = o.server_id
              LEFT JOIN guests g ON g.id = o.guest_id
              LEFT JOIN users cu ON cu.id = o.cancelled_by '''
    if cashier_id:
        closed = [dict(r) for r in conn.execute(base + '''
            WHERE o.status = 'closed' AND o.closed_by = ? AND o.closed_at >= ? AND o.closed_at <= ?
            ORDER BY o.closed_at''', (cashier_id, start, until)).fetchall()]
    else:
        closed = [dict(r) for r in conn.execute(base + '''
            WHERE o.status = 'closed' AND o.closed_at >= ? AND o.closed_at <= ?
            ORDER BY o.closed_at''', (start, until)).fetchall()]
    # Opened during the shift and neither paid nor cancelled by its end.
    pending = [dict(r) for r in conn.execute(base + '''
        WHERE o.timestamp >= ? AND o.timestamp <= ?
          AND (o.closed_at IS NULL OR o.closed_at > ?) AND (o.cancelled_at IS NULL OR o.cancelled_at > ?)
          AND o.guest_type NOT IN ('breakfast', 'owners')
        ORDER BY o.timestamp''', (start, until, until, until)).fetchall()]
    cancelled = [dict(r) for r in conn.execute(base + '''
        WHERE o.status = 'cancelled' AND o.cancelled_at >= ? AND o.cancelled_at <= ?
        ORDER BY o.cancelled_at''', (start, until)).fetchall()]
    by_id = {o['id']: o for o in closed + pending}

    def lines_of(orders):
        ids = [o['id'] for o in orders]
        if not ids:
            return []
        return [dict(r) for r in conn.execute(f"""
            SELECT oi.*, COALESCE(s.label, 'Sin sección') AS section_label, COALESCE(s.sort_order, 999) AS section_order
            FROM order_items oi
            LEFT JOIN LATERAL (SELECT m.section FROM menu_items m WHERE m.name = oi.item_name
                               ORDER BY m.is_active DESC, m.id LIMIT 1) mi ON TRUE
            LEFT JOIN menu_sections s ON s.key = mi.section
            WHERE oi.order_id IN ({','.join('?' * len(ids))}) ORDER BY oi.id""", ids).fetchall()]

    charged = [o for o in closed if o['guest_type'] not in MASTER_ACCOUNT_TYPES]
    masters = [o for o in closed if o['guest_type'] in MASTER_ACCOUNT_TYPES]
    sold, owed, master_lines = lines_of(charged), lines_of(pending), lines_of(masters)
    sections = []

    # Resumen de Caja
    by_method = {m: {'method': m, 'label': PAYMENT_METHOD_LABELS_ES[m], 'count': 0, 'amount': 0.0}
                 for m in ('cash', 'card', 'room_charge', 'house')}
    for o in closed:
        m = by_method[o['payment_method']]
        m['count'] += 1
        m['amount'] = round(m['amount'] + _order_amount(o), 2)
    cash, card = by_method['cash']['amount'], by_method['card']['amount']
    collected = round(cash + card, 2)
    opening = cash_info['opening_cash']
    expected = round(opening + cash, 2)
    diff = cash_info['cash_difference']
    rows = [['Fondo inicial', opening], ['Cobros en efectivo (a depositar)', cash],
            ['Efectivo final esperado en caja', expected],
            ['Efectivo contado al cerrar', cash_info['counted_cash']], ['Diferencia (contado − esperado)', diff],
            ['Cobros con tarjeta', card], ['Cargos a habitación', by_method['room_charge']['amount']],
            ['Cortesías (cuenta de la casa)', by_method['house']['amount']]]
    sections.append(_sec('caja', 'Resumen de Caja',
        'Lo que se debe entregar: fondo inicial, efectivo cobrado (a depositar), efectivo que debe haber en caja y la diferencia con lo que se contó.',
        [['Concepto', 'text'], ['Monto', 'money']], rows,
        [('A depositar', cash, 'money'), ('Efectivo final', expected, 'money'), ('Fondo', opening, 'money'),
         ('Diferencia', diff if diff is not None else cash_info['no_count_text'], 'money' if diff is not None else 'text')]))

    # Resumen de Cobros
    rows = [[m['label'], m['count'], m['amount']] for m in by_method.values() if m['count']]
    sections.append(_sec('cobros', 'Resumen de Cobros',
        'Cuántas cuentas se cobraron y cuánto, por forma de pago (efectivo, tarjeta, cargo a habitación, cortesía).',
        [['Forma de pago', 'text'], ['Cuentas', 'int'], ['Monto', 'money']], rows,
        [('Cobrado', collected, 'money'), ('Cuentas', len(closed), 'int')],
        ['Total', _sum_col(rows, 1), _sum_col(rows, 2)] if rows else None))

    # Resumen de Cobros (Salonero)
    agg = {}
    for o in closed:
        a = agg.setdefault((o['server_name'], PAYMENT_METHOD_LABELS_ES[o['payment_method']]), [0, 0.0])
        a[0] += 1
        a[1] += _order_amount(o)
    rows = [[s, m, n, round(v, 2)] for (s, m), (n, v) in sorted(agg.items())]
    sections.append(_sec('cobros_salonero', 'Resumen de Cobros (Salonero)',
        'Los cobros separados por mesero y forma de pago.',
        [['Salonero', 'text'], ['Forma de pago', 'text'], ['Cuentas', 'int'], ['Monto', 'money']], rows,
        [('Saloneros', len({r[0] for r in rows}), 'int'), ('Monto', _sum_col(rows, 3), 'money')],
        ['Total', '', _sum_col(rows, 2), _sum_col(rows, 3)] if rows else None))

    # Ventas por Salonero
    agg = {}
    for o in charged:
        a = agg.setdefault(o['server_name'], [0, 0.0])
        a[0] += 1
        a[1] += _order_amount(o)
    rows = sorted([[s, n, round(v, 2), round(v / n, 2)] for s, (n, v) in agg.items()], key=lambda r: -r[2])
    sections.append(_sec('ventas_salonero', 'Ventas por Salonero',
        'Ventas netas de cada mesero en las cuentas cobradas (sin cortesías), con su ticket promedio.',
        [['Salonero', 'text'], ['Cuentas', 'int'], ['Ventas netas', 'money'], ['Ticket promedio', 'money']], rows,
        [('Ventas netas', _sum_col(rows, 2), 'money'), ('Mejor salonero', rows[0][0] if rows else '—', 'text')],
        ['Total', _sum_col(rows, 1), _sum_col(rows, 2), ''] if rows else None))

    # Ventas por Artículos: cobrados, por cobrar, total
    for key, title, desc, ls in [
            ('articulos_cobrados', 'Ventas por Artículos (Cobrados)',
             'Cada artículo vendido en las cuentas cobradas, por sección y en orden alfabético.', sold),
            ('articulos_por_cobrar', 'Ventas por Artículos (Por Cobrar)',
             'Artículos de las cuentas abiertas en el periodo que todavía no se habían cobrado al final.', owed),
            ('articulos_total', 'Ventas por Artículos (Total)',
             'Cobrados más por cobrar: todo lo que se pidió en el periodo.', sold + owed)]:
        rows = _item_rows(ls)
        top = max(rows, key=lambda r: r[2]) if rows else None
        sections.append(_sec(key, title, desc, _ITEM_COLS, rows,
            [('Artículos', _sum_col(rows, 2), 'int'), ('Total', _sum_col(rows, 3), 'money'),
             ('Más vendido', f'{top[0]} ({top[2]})' if top else '—', 'text')],
            ['Total', '', _sum_col(rows, 2), _sum_col(rows, 3)] if rows else None))

    # Ventas por Familias (secciones del menú)
    agg = {}
    for l in sold:
        a = agg.setdefault(l['section_label'], [l['section_order'], 0, 0.0])
        a[1] += l['quantity']
        a[2] += l['quantity'] * l['unit_price']
    rows = [[s, q, round(t, 2)] for s, (_, q, t) in sorted(agg.items(), key=lambda kv: kv[1][0])]
    best = max(rows, key=lambda r: r[2]) if rows else None
    sections.append(_sec('familias', 'Ventas por Familias',
        'Lo cobrado agrupado por sección del menú (Mariscos, Cervezas, Cócteles…).',
        [['Sección', 'text'], ['Cant', 'int'], ['Total', 'money']], rows,
        [('Secciones con ventas', len(rows), 'int'), ('La que más vendió', best[0] if best else '—', 'text')],
        ['Total', _sum_col(rows, 1), _sum_col(rows, 2)] if rows else None))

    # Modificadores de Línea (guarniciones, término, notas)
    agg = {}
    for l in sold + owed:
        mods = [('Guarnición', s.strip()) for s in (l.get('sides') or '').split(' + ') if s.strip()]
        if l.get('doneness'):
            mods.append(('Huevos' if l['doneness'] in EGG_OPTIONS else 'Término', l['doneness']))
        if (l.get('notes') or '').strip():
            mods.append(('Nota', l['notes'].strip()))
        for m in mods:
            agg[m] = agg.get(m, 0) + l['quantity']
    order_of = {'Guarnición': 0, 'Término': 1, 'Huevos': 2, 'Nota': 3}
    rows = [[t, o, n] for (t, o), n in sorted(agg.items(), key=lambda kv: (order_of[kv[0][0]], -kv[1], kv[0][1]))]
    sides = [r for r in rows if r[0] == 'Guarnición']
    sections.append(_sec('modificadores', 'Resumen de Modificadores de Línea',
        'Cuántas veces se pidió cada guarnición y cada término de la carne, y las notas que se escribieron a cocina.',
        [['Tipo', 'text'], ['Opción', 'text'], ['Veces', 'int']], rows,
        [('Guarniciones', _sum_col(sides, 2), 'int'), ('Más pedida', sides[0][1] if sides else '—', 'text'),
         ('Términos', sum(r[2] for r in rows if r[0] == 'Término'), 'int')]))

    # Facturas Cerradas
    rows = [[o.get('invoice_number') or o['receipt_number'], o['timestamp'], o['closed_at'], _order_table(o), _order_who(o), o['server_name'],
             PAYMENT_METHOD_LABELS_ES[o['payment_method']], _order_amount(o)] for o in closed]
    sections.append(_sec('facturas_cerradas', 'Facturas Cerradas',
        'Cada cuenta cerrada: cuándo se abrió y se cobró, mesa, cliente, mesero, forma de pago y monto.',
        [['Factura', 'text'], ['Abierta', 'datetime'], ['Cerrada', 'datetime'], ['Mesa', 'text'], ['Cliente', 'text'],
         ['Salonero', 'text'], ['Forma de pago', 'text'], ['Monto', 'money']], rows,
        [('Facturas', len(rows), 'int'), ('Monto', _sum_col(rows, 7), 'money')],
        ['Total', '', '', '', '', '', '', _sum_col(rows, 7)] if rows else None))

    # Mesas / Facturas Pendientes
    rows = [[o['receipt_number'], o['timestamp'], _order_table(o), _order_who(o), o['server_name'],
             round(o['subtotal'], 2), round(o['balance_due'], 2)] for o in pending]
    sections.append(_sec('pendientes', 'Mesas / Facturas Pendientes',
        'Cuentas abiertas en el periodo que quedaron sin cobrar al final.',
        [['Factura', 'text'], ['Abierta', 'datetime'], ['Mesa', 'text'], ['Cliente', 'text'], ['Salonero', 'text'],
         ['Total', 'money'], ['Por cobrar', 'money']], rows,
        [('Pendientes', len(rows), 'int'), ('Por cobrar', _sum_col(rows, 6), 'money')],
        ['Total', '', '', '', '', _sum_col(rows, 5), _sum_col(rows, 6)] if rows else None))

    # Facturas Anuladas
    rows = [[o['receipt_number'], o['timestamp'], o['cancelled_at'], _order_table(o), o['server_name'],
             round(o['subtotal'], 2), o['cancel_reason'] or '', o['cancelled_by_name'] or ''] for o in cancelled]
    sections.append(_sec('facturas_anuladas', 'Facturas Anuladas',
        'Cuentas canceladas en el periodo, con el motivo y quién aprobó la anulación.',
        [['Factura', 'text'], ['Abierta', 'datetime'], ['Anulada', 'datetime'], ['Mesa', 'text'], ['Salonero', 'text'],
         ['Monto', 'money'], ['Motivo', 'text'], ['Anuló', 'text']], rows,
        [('Anuladas', len(rows), 'int'), ('Monto', _sum_col(rows, 5), 'money')],
        ['Total', '', '', '', '', _sum_col(rows, 5), '', ''] if rows else None))

    # Líneas Anuladas por Factura
    rows = [[r['created_at'], r['user_name'], r['details']] for r in conn.execute('''
        SELECT created_at, user_name, details FROM activity_log
        WHERE action = 'order_item_removed' AND created_at >= ? AND created_at <= ? ORDER BY created_at''',
        (start, until)).fetchall()]
    sections.append(_sec('lineas_anuladas', 'Líneas Anuladas por Factura',
        'Artículos que se quitaron de una cuenta abierta (el cliente cambió de idea), quién y cuándo.',
        [['Hora', 'datetime'], ['Usuario', 'text'], ['Detalle', 'text']], rows,
        [('Líneas quitadas', len(rows), 'int'), ('Usuarios', len({r[1] for r in rows}), 'int')]))

    # Cortesías y desayunos incluidos
    rows = []
    for l in sold + owed:
        if l.get('included'):
            o = by_id[l['order_id']]
            rows.append([o['receipt_number'], f"Hab. {l['room_number']}" if l.get('room_number') else _order_who(o),
                         l['item_name'], l['quantity'], round((l.get('list_price') or 0) * l['quantity'], 2), 'Desayuno incluido'])
    for l in master_lines:
        o = by_id[l['order_id']]
        price = l['unit_price'] or l.get('list_price') or 0
        rows.append([o['receipt_number'], f"Hab. {l['room_number']} — {l['guest_name']}" if l.get('room_number') else _order_who(o),
                     l['item_name'], l['quantity'], round(price * l['quantity'], 2),
                     'Desayunos incluidos' if o['guest_type'] == 'breakfast' else 'Propietarios'])
    sections.append(_sec('cortesias', 'Cortesías y Desayunos Incluidos',
        'Lo que se sirvió sin cobrar: desayunos incluidos de huéspedes y consumos de propietarios, con su valor de menú.',
        [['Factura', 'text'], ['Habitación / cliente', 'text'], ['Artículo', 'text'], ['Cant', 'int'],
         ['Valor', 'money'], ['Tipo', 'text']], rows,
        [('Artículos', _sum_col(rows, 3), 'int'), ('Valor', _sum_col(rows, 4), 'money')],
        ['Total', '', '', _sum_col(rows, 3), _sum_col(rows, 4), ''] if rows else None))

    # Rebaja de Inventarios
    ids = list(by_id)
    rows = []
    if ids:
        rows = [[r['item_name'], r['unit_of_measure'], _POOL_LABELS_ES.get(r['pool'], r['pool']), round(r['used'], 3)]
                for r in conn.execute(f"""
            SELECT i.item_name, i.unit_of_measure, t.pool, -SUM(t.quantity_change) AS used
            FROM inventory_transactions t JOIN inventory_items i ON i.id = t.inventory_item_id
            WHERE t.order_id IN ({','.join('?' * len(ids))})
            GROUP BY i.item_name, i.unit_of_measure, t.pool
            HAVING SUM(t.quantity_change) <> 0 ORDER BY i.item_name""", ids).fetchall()]
    sections.append(_sec('rebaja_inventario', 'Rebaja de Inventarios',
        'Lo que las ventas sacaron del inventario según las recetas de cada plato.',
        [['Insumo', 'text'], ['Unidad', 'text'], ['Bodega', 'text'], ['Cantidad', 'int']], rows,
        [('Insumos', len(rows), 'int'),
         ('Origen', 'Recetas' if rows else 'Sin recetas', 'text')]))

    # Estadísticas
    revenue = round(sum(_order_amount(o) for o in charged), 2)
    hours = {}
    for o in charged:
        h = (o['closed_at'] or '')[11:13]
        if h:
            hours[h] = hours.get(h, 0) + 1
    peak = max(hours.items(), key=lambda kv: kv[1]) if hours else None
    items = _item_rows(sold)
    top = max(items, key=lambda r: r[2]) if items else None
    rows = [['Cuentas cobradas', len(charged)], ['Ventas netas', f'C${revenue:,.2f}'],
            ['Ticket promedio', f'C${revenue / len(charged):,.2f}' if charged else '—'],
            ['Artículos vendidos', _sum_col(items, 2)],
            ['Mesas atendidas', len({o['table_number'] for o in charged if o['table_number']})],
            ['Saloneros', len({o['server_name'] for o in charged})],
            ['Hora con más cobros', f"{int(peak[0]):02d}:00 ({peak[1]} cuentas)" if peak else '—'],
            ['Artículo más vendido', f'{top[0]} ({top[2]})' if top else '—'],
            ['Cuentas pendientes al cerrar', len(pending)], ['Cuentas anuladas', len(cancelled)]]
    sections.append(_sec('estadisticas', 'Estadísticas',
        'Los números del periodo de un vistazo: ventas, ticket promedio, hora más fuerte y lo más vendido.',
        [['Indicador', 'text'], ['Valor', 'text']], [[k, str(v)] for k, v in rows],
        [('Ticket promedio', round(revenue / len(charged), 2) if charged else 0, 'money'),
         ('Hora fuerte', f"{int(peak[0]):02d}:00" if peak else '—', 'text')]))
    conn.close()
    return {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'sections': sections,
            'order_count': len(closed), 'collected_total': collected, 'opening_cash': opening}


# ------------------------------------------------------- Customer receipt --
# The printed bill ("factura") as the hotel's authorized format has it: the
# header, a sequential invoice number taken when the account is paid, and
# the authorization footer. All texts are settings the admin can edit.

RECEIPT_SETTING_DEFAULTS = {
    'receipt_business_name': 'ARENAS BOAT BAR AND RESTAURANTE',
    'receipt_company': 'ARENAS BEACH HOTEL',
    'receipt_ruc': '6012809670001J',
    'receipt_address': 'SOUTHWEST BAY, CORN ISLAND',
    'receipt_phone': '25755223',
    'receipt_pos_name': 'ARENAS BOAT BAR AND RESTAURANT',
    'receipt_footer': 'Autorizado mediante Resolución 11-11-97\nGaceta 11-171 pag. 118 de la D.G.T.D.',
    'invoice_prefix': 'RES',
    'invoice_next': '7878',
}


def get_receipt_settings(conn=None):
    own = conn is None
    conn = conn or get_connection()
    rows = conn.execute(
        f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(RECEIPT_SETTING_DEFAULTS))})",
        tuple(RECEIPT_SETTING_DEFAULTS)).fetchall()
    if own:
        conn.close()
    return {**RECEIPT_SETTING_DEFAULTS, **{r['key']: r['value'] for r in rows}}


def save_receipt_settings(values, user_id=None):
    clean = {}
    for key in RECEIPT_SETTING_DEFAULTS:
        if key not in values:
            continue
        v = str(values[key] if values[key] is not None else '').replace('\r', '').strip()
        clean[key] = v[:400] if key == 'receipt_footer' else ' '.join(v.split())[:80]
    if 'invoice_prefix' in clean and not re.fullmatch(r'[A-Za-z0-9-]{0,10}', clean['invoice_prefix']):
        raise ValueError('El prefijo de la factura solo lleva letras, números o guion (máximo 10)')
    conn = get_connection(write=True)
    current = get_receipt_settings(conn)
    if 'invoice_next' in clean:
        try:
            nxt = int(clean['invoice_next'])
        except ValueError:
            conn.close()
            raise ValueError('El próximo número de factura debe ser un número entero')
        if nxt < 1:
            conn.close()
            raise ValueError('El próximo número de factura debe ser mayor que cero')
        # Never hand out a number already printed with this prefix.
        prefix = clean.get('invoice_prefix', current['invoice_prefix'])
        used = conn.execute("SELECT invoice_number FROM orders WHERE invoice_number LIKE ?", (f'{prefix}%',)).fetchall()
        highest = max([int(u['invoice_number'][len(prefix):]) for u in used
                       if u['invoice_number'][len(prefix):].isdigit()] or [0])
        if nxt <= highest:
            conn.close()
            raise ValueError(f'Ya se imprimió la factura {prefix}{highest}; el próximo número debe ser {highest + 1} o mayor')
        clean['invoice_next'] = str(nxt)
    for key, value in clean.items():
        conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    _log_activity(conn, user_id, 'receipt_settings_saved', 'Actualizó los datos de la factura del restaurante')
    conn.commit()
    settings = get_receipt_settings(conn)
    conn.close()
    return settings


def _take_invoice_number(conn):
    """The next sequential invoice number, inside the closing transaction
    (row-locked, so two cashiers never get the same one)."""
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('invoice_next', ?) ON CONFLICT (key) DO NOTHING",
                 (RECEIPT_SETTING_DEFAULTS['invoice_next'],))
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'invoice_next' FOR UPDATE").fetchone()
    n = int(row['value'])
    prefix_row = conn.execute("SELECT value FROM app_settings WHERE key = 'invoice_prefix'").fetchone()
    prefix = prefix_row['value'] if prefix_row else RECEIPT_SETTING_DEFAULTS['invoice_prefix']
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'invoice_next'", (str(n + 1),))
    return f'{prefix}{n}'


_RECEIPT_METHOD_LABELS = {'cash': 'Efectivo (C$)', 'card': 'Tarjeta', 'room_charge': 'Habit. (C$)',
                          'house': 'Cortesía'}


def receipt_view(order_id, viewer_name, copy=False):
    """Everything the printed document shows for one account, in the hotel's
    four formats:
    - statement: an open account (estado de cuenta) the guest signs at the table;
    - invoice: paid cash / card — FACTURA with its number;
    - charge: charged to the room — CARGO A CUENTA, signed by the guest, no
      invoice number (the hotel invoices the stay at check-out);
    - control: nothing to pay (included breakfast, courtesy) — Control de
      Consumo, items without amounts."""
    conn = get_connection()
    row = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not row:
        conn.close()
        return None
    o = _hydrate_order(conn, row)
    cashier = None
    if o.get('closed_by'):
        u = conn.execute('SELECT full_name FROM users WHERE id = ?', (o['closed_by'],)).fetchone()
        cashier = u['full_name'] if u else None
    settings = get_receipt_settings(conn)
    conn.close()

    paid = o['status'] == 'closed'
    master = o['guest_type'] in MASTER_ACCOUNT_TYPES
    lines = [{'quantity': i['quantity'], 'name': i['item_name'] + (' (DESAYUNO INCLUIDO)' if i.get('included') else ''),
              'included': bool(i.get('included')), 'amount': round(i['quantity'] * i['unit_price'], 2),
              'details': line_details(i)} for i in o['items']]
    subtotal = round(o['subtotal'], 2)
    to_pay = 0.0 if master else (round(order_revenue(o), 2) if paid else
                                 round(max(WALK_IN_CREDIT, subtotal) if o['guest_type'] == 'walk_in' else subtotal, 2))
    if master or (to_pay < 0.005 and lines and all(l['included'] for l in lines)):
        kind = 'control'
    elif not paid:
        kind = 'statement'
    elif o['payment_method'] == 'room_charge':
        kind = 'charge'
    else:
        kind = 'invoice'
    when = o['closed_at'] if paid else datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if o['guest_type'] == 'hotel_guest' and o.get('guest_name'):
        hab_cred = f"{o['room_number']}-{o['guest_name'].upper()}"
    elif o['guest_type'] == 'breakfast':
        hab_cred = 'Desayuno incluido'
    elif o['guest_type'] == 'owners':
        hab_cred = f"Propietarios - {o.get('account_name') or ''}".rstrip(' -')
    else:
        hab_cred = None
    if o['guest_type'] == 'breakfast':
        rooms = sorted({i['room_number'] for i in o['items'] if i.get('room_number')}, key=lambda r: (len(r), r))
        note = ', '.join(f'hab #{r}' for r in rooms)
    elif o['guest_type'] == 'owners':
        note = ''
    else:
        note = o.get('account_name') or ''
    tendered = o.get('amount_tendered')
    if paid and o['payment_method'] == 'cash' and tendered is None:
        tendered = to_pay
    elif paid and o['payment_method'] != 'cash':
        tendered = to_pay if o['payment_method'] in ('card', 'room_charge') else None
    labels = {'statement': 'ESTADO DE CUENTA', 'invoice': 'FACTURA', 'charge': 'CARGO A CUENTA',
              'control': 'CONTROL DE CONSUMO'}
    return {
        's': settings,
        'r': {
            'kind': kind, 'paid': paid, 'title': labels[kind],
            'number': o.get('invoice_number') or o['receipt_number'],
            'copy_label': 'COPIA' if copy and paid else 'ORIGINAL',
            'folio': o['id'],
            'cashier': cashier or viewer_name or '',
            'server': o['server_name'] or '',
            'table': f"M{o['table_number']}" if o.get('table_number') else '—',
            'sub_account': o.get('sub_account') or 1,
            'guest_count': o.get('guest_count'),
            'date_label': 'Facturado' if paid else 'Emitido',
            'date': datetime.strptime(when[:19], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M:%S') if when else '',
            'hab_cred': hab_cred,
            'note': note,
            'lines': lines,
            'subtotal': subtotal,
            'day_pass_minimum': WALK_IN_CREDIT if o['guest_type'] == 'walk_in' and subtotal < WALK_IN_CREDIT else None,
            'to_pay': to_pay,
            'method_label': _RECEIPT_METHOD_LABELS.get(o.get('payment_method'), ''),
            'tendered': tendered,
            'change': round(tendered - to_pay, 2) if tendered is not None and tendered > to_pay + 0.005 else None,
            'credit': kind == 'charge',
            # the guest signs what is charged to their room (and the breakfast / owners slips)
            'sign': kind in ('statement', 'charge', 'control') and o['guest_type'] in ('hotel_guest', 'breakfast', 'owners'),
            'room_fields': o['guest_type'] == 'hotel_guest' and kind in ('statement', 'charge'),
        },
    }

# ------------------------------------------------------------- Maintenance --
# The maintenance storeroom is one more stock pool (inventory_items.
# maintenance_stock), fed from the main storeroom by the usual authorized
# transfer. Nothing leaves it without an administrator's approval: every use
# is a maintenance_usage request until an admin approves it, and only then
# is it taken off the pool through the Kardex. Breakdowns (maintenance_
# incidents) can be reported by anyone; maintenance, front desk and admins
# work and resolve them.

MAINTENANCE_AREAS = ('Habitación', 'Restaurante', 'Bar', 'Cocina', 'Recepción', 'Piscina',
                     'Playa / Ranchos', 'Áreas comunes', 'Oficinas', 'Otro')
INCIDENT_PRIORITIES = ('low', 'medium', 'urgent')
INCIDENT_PRIORITY_LABELS_ES = {'low': 'Baja', 'medium': 'Media', 'urgent': 'Urgente'}
INCIDENT_STATUSES = ('open', 'in_progress', 'resolved')
INCIDENT_STATUS_LABELS_ES = {'open': 'Abierta', 'in_progress': 'En proceso', 'resolved': 'Resuelta'}
USAGE_STATUS_LABELS_ES = {'pending': 'Pendiente', 'approved': 'Aprobado', 'rejected': 'Rechazado'}
PHOTO_MIME_TYPES = ('image/jpeg', 'image/png', 'image/webp')
MAX_PHOTO_BASE64 = 3_000_000   # ~2.2 MB image; the page shrinks photos well below this


def _incident_target(area, room_number):
    return f'Habitación {room_number}' if room_number else area


def list_maintenance_items():
    """Everything the maintenance storeroom can hold: items created for
    maintenance plus anything that has stock there, with low-stock flags."""
    conn = get_connection()
    rows = conn.execute('''
        SELECT * FROM inventory_items WHERE is_maintenance = 1 OR maintenance_stock > 0 ORDER BY item_name
    ''').fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    for i in items:
        i['is_low_maintenance'] = i['maintenance_stock'] <= i['maintenance_threshold']
    return items


def create_maintenance_item(item_name, unit_of_measure, maintenance_threshold, initial_stock, user_id):
    """A tool/part/material for maintenance. Stock already sitting in the
    maintenance storeroom goes in as its 'Inventario Inicial' Kardex line;
    later purchases are received in the main storeroom and transferred."""
    item_name = (item_name or '').strip()
    unit_of_measure = (unit_of_measure or '').strip()
    if not item_name or not unit_of_measure:
        raise ValueError('El nombre del artículo y la unidad de medida son obligatorios')
    try:
        maintenance_threshold = float(maintenance_threshold or 0)
        initial_stock = float(initial_stock or 0)
    except (TypeError, ValueError):
        raise ValueError('El mínimo y la existencia inicial deben ser números')
    if maintenance_threshold < 0 or initial_stock < 0:
        raise ValueError('El mínimo y la existencia inicial no pueden ser negativos')
    conn = get_connection(write=True)
    if conn.execute('SELECT id FROM inventory_items WHERE lower(item_name) = lower(?)', (item_name,)).fetchone():
        conn.close()
        raise ValueError(f"'{item_name}' ya existe en el inventario")
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO inventory_items (item_name, unit_of_measure, current_stock, low_stock_threshold,
                                     is_maintenance, maintenance_threshold)
        VALUES (?, ?, 0, 0, 1, ?)
    ''', (item_name, unit_of_measure, maintenance_threshold))
    item_id = cur.lastrowid
    if initial_stock:
        _apply_inventory_movement(conn, item_id, 'maintenance', initial_stock, 'initial')
    _log_activity(conn, user_id, 'maintenance_item_created',
                  f'Artículo de mantenimiento: {item_name} ({initial_stock:g} {unit_of_measure} iniciales)')
    conn.commit()
    item = dict(conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone())
    conn.close()
    return item


def import_maintenance_items(rows, user_id):
    """Builds the maintenance storeroom from a spreadsheet. rows are
    (item_name, unit, quantity, minimum). New items are created as maintenance
    items with that quantity as their 'Inventario Inicial' in the maintenance
    storeroom. An item that already exists (ignoring case) becomes a
    maintenance item and only gets the quantity if its maintenance stock is
    still 0 — so uploading the same file twice never counts stock twice. A
    minimum in the file always updates the item's minimum."""
    conn = get_connection(write=True)
    existing = {r['item_name'].strip().lower(): dict(r) for r in conn.execute(
        'SELECT id, item_name, maintenance_stock FROM inventory_items').fetchall()}
    added, updated, skipped = [], [], []
    for item_name, unit, quantity, minimum in rows:
        key = item_name.lower()
        quantity = quantity if quantity and quantity > 0 else 0
        item = existing.get(key)
        if item:
            conn.execute('UPDATE inventory_items SET is_maintenance = 1 WHERE id = ?', (item['id'],))
            if minimum is not None:
                conn.execute('UPDATE inventory_items SET maintenance_threshold = ? WHERE id = ?', (minimum, item['id']))
            if quantity and item['maintenance_stock'] <= 0:
                _apply_inventory_movement(conn, item['id'], 'maintenance', quantity, 'initial', notes='Importado desde Excel')
                item['maintenance_stock'] = quantity
                updated.append(item_name)
            else:
                skipped.append(item_name)
            continue
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO inventory_items (item_name, unit_of_measure, current_stock, low_stock_threshold,
                                         is_maintenance, maintenance_threshold)
            VALUES (?, ?, 0, 0, 1, ?)
        ''', (item_name, unit or 'unidad', minimum or 0))
        if quantity:
            _apply_inventory_movement(conn, cur.lastrowid, 'maintenance', quantity, 'initial', notes='Importado desde Excel')
        existing[key] = {'id': cur.lastrowid, 'item_name': item_name, 'maintenance_stock': quantity}
        added.append(item_name)
    _log_activity(conn, user_id, 'maintenance_items_imported',
                  f'Bodega de Mantenimiento desde Excel: {len(added)} nuevo(s), {len(updated)} con saldo cargado, '
                  f'{len(skipped)} sin cambios')
    conn.commit()
    conn.close()
    return {'added': added, 'updated': updated, 'skipped': skipped}


def set_maintenance_threshold(item_id, threshold, user_id):
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise ValueError('El mínimo debe ser un número')
    if threshold < 0:
        raise ValueError('El mínimo no puede ser negativo')
    conn = get_connection(write=True)
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    conn.execute('UPDATE inventory_items SET maintenance_threshold = ?, is_maintenance = 1 WHERE id = ?',
                 (threshold, item_id))
    _log_activity(conn, user_id, 'maintenance_threshold_changed',
                  f"Mínimo en Mantenimiento de {item['item_name']}: {threshold:g} {item['unit_of_measure']}")
    conn.commit()
    conn.close()


# ---- breakdowns -----------------------------------------------------------

def report_incident(user_id, area, description, priority='medium', room_number=None,
                    photo_mime=None, photo_data=None, mark_out_of_order=False):
    """Anyone can report a breakdown. A room can be taken out of service at
    the same time (the existing housekeeping 'Fuera de servicio' block)."""
    area = (area or '').strip()
    description = ' '.join((description or '').split())
    room_number = (room_number or '').strip() or None
    if area not in MAINTENANCE_AREAS:
        raise ValueError('Elija el área de la avería')
    if area == 'Habitación' and not room_number:
        raise ValueError('Indique el número de habitación')
    if area != 'Habitación':
        room_number = None
        mark_out_of_order = False
    if not description:
        raise ValueError('Describa la avería')
    if priority not in INCIDENT_PRIORITIES:
        raise ValueError('Prioridad inválida')
    if photo_data:
        if photo_mime not in PHOTO_MIME_TYPES:
            raise ValueError('La foto debe ser JPG, PNG o WEBP')
        if len(photo_data) > MAX_PHOTO_BASE64:
            raise ValueError('La foto es demasiado grande')
    conn = get_connection(write=True)
    if room_number and not conn.execute('SELECT 1 FROM rooms WHERE room_number = ?', (room_number,)).fetchone():
        conn.close()
        raise ValueError(f'La habitación {room_number} no existe')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO maintenance_incidents (area, room_number, description, priority, reported_by, room_out_of_order)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (area, room_number, description[:1000], priority, user_id, 1 if mark_out_of_order else 0))
    incident_id = cur.lastrowid
    if photo_data:
        conn.execute('INSERT INTO maintenance_photos (incident_id, mime, data) VALUES (?, ?, ?)',
                     (incident_id, photo_mime, photo_data))
    _log_activity(conn, user_id, 'maintenance_incident_reported',
                  f"Avería #{incident_id} ({INCIDENT_PRIORITY_LABELS_ES[priority]}) en "
                  f"{_incident_target(area, room_number)}: {description[:120]}")
    conn.commit()
    conn.close()
    if mark_out_of_order:
        set_room_housekeeping_status(room_number, 'out_of_order', user_id,
                                     notes=f'Avería #{incident_id}: {description[:80]}')
    return get_incident(incident_id)


def _hydrate_incident(conn, row):
    i = dict(row)
    i['priority_label'] = INCIDENT_PRIORITY_LABELS_ES[i['priority']]
    i['status_label'] = INCIDENT_STATUS_LABELS_ES[i['status']]
    i['target'] = _incident_target(i['area'], i['room_number'])
    i['photo_ids'] = [r['id'] for r in conn.execute(
        'SELECT id FROM maintenance_photos WHERE incident_id = ? ORDER BY id', (i['id'],)).fetchall()]
    i['materials'] = [dict(r) for r in conn.execute('''
        SELECT u.id, u.quantity, u.status, i.item_name, i.unit_of_measure
        FROM maintenance_usage u JOIN inventory_items i ON i.id = u.inventory_item_id
        WHERE u.incident_id = ? ORDER BY u.id
    ''', (i['id'],)).fetchall()]
    return i


_INCIDENT_SELECT = '''
    SELECT m.*, r.full_name AS reported_by_name, s.full_name AS started_by_name, v.full_name AS resolved_by_name
    FROM maintenance_incidents m
    JOIN users r ON r.id = m.reported_by
    LEFT JOIN users s ON s.id = m.started_by
    LEFT JOIN users v ON v.id = m.resolved_by
'''


def get_incident(incident_id):
    conn = get_connection()
    row = conn.execute(_INCIDENT_SELECT + ' WHERE m.id = ?', (incident_id,)).fetchone()
    incident = _hydrate_incident(conn, row) if row else None
    conn.close()
    return incident


def list_incidents(status=None, reported_by=None, room_number=None, area=None, start_date=None, end_date=None,
                   limit=300):
    where, params = [], []
    if status == 'active':
        where.append("m.status != 'resolved'")
    elif status:
        where.append('m.status = ?')
        params.append(status)
    if reported_by:
        where.append('m.reported_by = ?')
        params.append(reported_by)
    if room_number:
        where.append('m.room_number = ?')
        params.append(room_number)
    if area:
        where.append('m.area = ?')
        params.append(area)
    if start_date:
        where.append('left(m.reported_at, 10) >= ?')
        params.append(start_date)
    if end_date:
        where.append('left(m.reported_at, 10) <= ?')
        params.append(end_date)
    sql = _INCIDENT_SELECT + (' WHERE ' + ' AND '.join(where) if where else '') + '''
        ORDER BY (m.status = 'resolved'), CASE m.priority WHEN 'urgent' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 m.reported_at DESC LIMIT ?'''
    conn = get_connection()
    rows = conn.execute(sql, (*params, limit)).fetchall()
    out = [_hydrate_incident(conn, r) for r in rows]
    conn.close()
    return out


def start_incident(incident_id, user_id):
    conn = get_connection(write=True)
    row = conn.execute('SELECT * FROM maintenance_incidents WHERE id = ?', (incident_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError('Avería no encontrada')
    if row['status'] != 'open':
        conn.close()
        raise ValueError('Esta avería ya está en proceso o resuelta')
    conn.execute("UPDATE maintenance_incidents SET status = 'in_progress', started_by = ?, started_at = now_local() "
                 "WHERE id = ?", (user_id, incident_id))
    _log_activity(conn, user_id, 'maintenance_incident_started',
                  f"Avería #{incident_id} en proceso ({_incident_target(row['area'], row['room_number'])})")
    conn.commit()
    conn.close()
    return get_incident(incident_id)


def resolve_incident(incident_id, user_id, resolution_note, materials=None, return_to_service=False):
    """Closes a breakdown. The materials used become usage requests on the
    maintenance storeroom — taken off stock only once an admin approves them.
    A room this breakdown took out of service can go back (as 'Sucia', so
    housekeeping cleans it before it is sold again)."""
    resolution_note = ' '.join((resolution_note or '').split())
    if not resolution_note:
        raise ValueError('Describa qué se hizo para resolverla')
    materials = [m for m in (materials or []) if m.get('inventory_item_id') and m.get('quantity') not in (None, '')]
    conn = get_connection(write=True)
    row = conn.execute('SELECT * FROM maintenance_incidents WHERE id = ?', (incident_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError('Avería no encontrada')
    if row['status'] == 'resolved':
        conn.close()
        raise ValueError('Esta avería ya fue resuelta')
    target = _incident_target(row['area'], row['room_number'])
    for m in materials:
        try:
            item_id, qty = int(m['inventory_item_id']), float(m['quantity'])
        except (TypeError, ValueError):
            conn.close()
            raise ValueError('Cantidad de material inválida')
        _insert_usage_request(conn, item_id, qty, row['area'], row['room_number'], incident_id,
                              f'Avería #{incident_id}', user_id)
    conn.execute('''
        UPDATE maintenance_incidents SET status = 'resolved', resolved_by = ?, resolved_at = now_local(),
               resolution_note = ?, started_by = COALESCE(started_by, ?), started_at = COALESCE(started_at, now_local())
        WHERE id = ?
    ''', (user_id, resolution_note[:1000], user_id, incident_id))
    _log_activity(conn, user_id, 'maintenance_incident_resolved',
                  f"Avería #{incident_id} resuelta ({target}): {resolution_note[:120]}"
                  + (f" · {len(materials)} material(es) por aprobar" if materials else ''))
    conn.commit()
    conn.close()
    if return_to_service and row['room_out_of_order'] and row['room_number']:
        check = get_connection()
        status = check.execute('SELECT housekeeping_status FROM rooms WHERE room_number = ?',
                               (row['room_number'],)).fetchone()
        check.close()
        if status and status['housekeeping_status'] == 'out_of_order':
            set_room_housekeeping_status(row['room_number'], 'dirty', user_id,
                                         notes=f'Avería #{incident_id} resuelta')
    return get_incident(incident_id)


def get_incident_photo(photo_id):
    conn = get_connection()
    row = conn.execute('''
        SELECT p.mime, p.data, m.reported_by FROM maintenance_photos p
        JOIN maintenance_incidents m ON m.id = p.incident_id WHERE p.id = ?
    ''', (photo_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---- using material from the maintenance storeroom (admin-approved) ---------

def _insert_usage_request(conn, inventory_item_id, quantity, area, room_number, incident_id, notes, user_id):
    if quantity <= 0:
        conn.close()
        raise ValueError('La cantidad debe ser mayor que cero')
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (inventory_item_id,)).fetchone()
    if not item:
        conn.close()
        raise ValueError('Artículo de inventario no encontrado')
    if quantity > item['maintenance_stock'] + 1e-9:
        conn.close()
        raise ValueError(f"Solo hay {item['maintenance_stock']:g} {item['unit_of_measure']} de {item['item_name']} "
                         'en la bodega de Mantenimiento')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO maintenance_usage (inventory_item_id, quantity, area, room_number, incident_id, notes, requested_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (inventory_item_id, quantity, area, room_number, incident_id, (notes or '').strip() or None, user_id))
    _log_activity(conn, user_id, 'maintenance_usage_requested',
                  f"Solicitó usar {quantity:g} {item['unit_of_measure']} de {item['item_name']} "
                  f"({_incident_target(area or 'Otro', room_number)})")
    return cur.lastrowid


def request_maintenance_usage(user_id, inventory_item_id, quantity, area, room_number=None, incident_id=None, notes=None):
    area = (area or '').strip() or 'Otro'
    if area not in MAINTENANCE_AREAS:
        raise ValueError('Área inválida')
    room_number = ((room_number or '').strip() or None) if area == 'Habitación' else None
    try:
        inventory_item_id, quantity = int(inventory_item_id), float(quantity)
        incident_id = int(incident_id) if incident_id else None
    except (TypeError, ValueError):
        raise ValueError('Artículo o cantidad inválidos')
    conn = get_connection(write=True)
    usage_id = _insert_usage_request(conn, inventory_item_id, quantity, area, room_number, incident_id, notes, user_id)
    conn.commit()
    conn.close()
    return next(u for u in list_maintenance_usage() if u['id'] == usage_id)


def list_maintenance_usage(status=None, limit=300):
    conn = get_connection()
    where, params = ('WHERE u.status = ?', [status]) if status else ('', [])
    rows = conn.execute(f'''
        SELECT u.*, i.item_name, i.unit_of_measure, i.maintenance_stock,
               r.full_name AS requested_by_name, d.full_name AS decided_by_name
        FROM maintenance_usage u
        JOIN inventory_items i ON i.id = u.inventory_item_id
        JOIN users r ON r.id = u.requested_by
        LEFT JOIN users d ON d.id = u.decided_by
        {where}
        ORDER BY (u.status != 'pending'), u.requested_at DESC, u.id DESC LIMIT ?
    ''', (*params, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        u = dict(r)
        u['status_label'] = USAGE_STATUS_LABELS_ES[u['status']]
        u['target'] = _incident_target(u['area'] or 'Otro', u['room_number'])
        out.append(u)
    return out


def decide_maintenance_usage(usage_id, admin_user_id, approve, note=None):
    """An administrator approves (stock leaves the maintenance storeroom,
    through the Kardex) or rejects a use of maintenance material."""
    conn = get_connection(write=True)
    u = conn.execute('SELECT * FROM maintenance_usage WHERE id = ?', (usage_id,)).fetchone()
    if not u:
        conn.close()
        raise ValueError('Solicitud no encontrada')
    if u['status'] != 'pending':
        conn.close()
        raise ValueError('Esta solicitud ya fue decidida')
    item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (u['inventory_item_id'],)).fetchone()
    target = _incident_target(u['area'] or 'Otro', u['room_number'])
    if approve:
        # Raises (and rolls back) if the storeroom no longer has enough.
        _apply_inventory_movement(conn, u['inventory_item_id'], 'maintenance', -u['quantity'], 'maintenance_use',
                                  notes=f"{target}" + (f" · Avería #{u['incident_id']}" if u['incident_id'] else ''))
    conn.execute('''
        UPDATE maintenance_usage SET status = ?, decided_by = ?, decided_at = now_local(), decision_note = ?
        WHERE id = ?
    ''', ('approved' if approve else 'rejected', admin_user_id, (note or '').strip() or None, usage_id))
    _log_activity(conn, admin_user_id, 'maintenance_usage_approved' if approve else 'maintenance_usage_rejected',
                  f"{u['quantity']:g} {item['unit_of_measure']} de {item['item_name']} ({target})"
                  + (f' — {note}' if note else ''))
    conn.commit()
    conn.close()
    return next(x for x in list_maintenance_usage() if x['id'] == usage_id)


def list_maintenance_transfer_requests(limit=100):
    """Requests from the main storeroom to the maintenance storeroom."""
    return [t for t in list_inventory_transfers(limit=1000) if t['destination'] == 'maintenance'][:limit]


def maintenance_alerts():
    """What front desk / admins must notice: urgent breakdowns still open and
    the counts behind the Mantenimiento tab badge."""
    conn = get_connection()
    urgent = [dict(r) for r in conn.execute('''
        SELECT m.id, m.area, m.room_number, m.description, m.reported_at, m.status, u.full_name AS reported_by_name
        FROM maintenance_incidents m JOIN users u ON u.id = m.reported_by
        WHERE m.priority = 'urgent' AND m.status != 'resolved' ORDER BY m.reported_at
    ''').fetchall()]
    counts = conn.execute('''
        SELECT (SELECT COUNT(*) FROM maintenance_incidents WHERE status != 'resolved') AS open_incidents,
               (SELECT COUNT(*) FROM maintenance_usage WHERE status = 'pending') AS pending_usage,
               (SELECT COUNT(*) FROM inventory_transfers WHERE status = 'pending' AND destination = 'maintenance')
                   AS pending_transfers,
               (SELECT COUNT(*) FROM inventory_items WHERE (is_maintenance = 1 OR maintenance_stock > 0)
                   AND maintenance_stock <= maintenance_threshold) AS low_items
    ''').fetchone()
    conn.close()
    for u in urgent:
        u['target'] = _incident_target(u['area'], u['room_number'])
    return {'urgent': urgent, **dict(counts)}
