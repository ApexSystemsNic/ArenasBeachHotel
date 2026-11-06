"""Automated database backups: an incremental snapshot every 2–3 days and a
full snapshot weekly.

Each backup is a complete, consistent snapshot of every table, written as
gzip-compressed JSON (read in one transaction, so it never mixes data from
before and after a concurrent change). Plain JSON needs no pg_dump binary,
so it works the same on the hotel PC and on Railway. Both kinds are full
snapshots — they're differentiated by *schedule and retention*:
incrementals run more often and are pruned sooner; the weekly full backup is
kept longer as the durable archive.

BACKUPS_DIR can point at persistent storage (e.g. a Railway volume); the
container's own disk is wiped on every deploy. Admins can also download any
backup from the Reportes y Respaldos screen.
"""
import glob
import gzip
import json
import os
from datetime import datetime

import psycopg

import database as db
import delivery

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUPS_DIR = os.environ.get('BACKUPS_DIR', os.path.join(BASE_DIR, 'backups'))
BACKUP_FORMAT = 'arenas-pms-backup'
BACKUP_EXTENSION = '.json.gz'

RETENTION = {
    'incremental': 8,   # ~2-3 weeks of incrementals at a 2-3 day cadence
    'full': 12,          # ~3 months of weekly full archives
}


def _backup_dir(kind):
    d = os.path.join(BACKUPS_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return d


def _snapshot():
    """Every table's columns and rows, read inside one transaction."""
    conn = db.get_connection()
    try:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        tables = {}
        for table in db.TABLES:
            cur = conn.execute(f'SELECT * FROM {table} ORDER BY 1')
            columns = [c.name for c in cur.description]
            tables[table] = {'columns': columns, 'rows': [list(r.values()) for r in cur.fetchall()]}
        return tables
    finally:
        conn.close()


def create_backup(kind, triggered_by='scheduled'):
    if kind not in ('incremental', 'full'):
        raise ValueError("kind must be 'incremental' or 'full'")

    target_dir = _backup_dir(kind)
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    file_name = f'arenas_beach_{kind}_{timestamp}{BACKUP_EXTENSION}'
    file_path = os.path.join(target_dir, file_name)

    payload = {
        'format': BACKUP_FORMAT,
        'version': 1,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'tables': _snapshot(),
    }
    tmp_path = file_path + '.tmp'
    with gzip.open(tmp_path, 'wt', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp_path, file_path)   # never leave a half-written backup behind

    file_size = os.path.getsize(file_path)
    cloud_result = delivery.sync_to_cloud_folder(file_path)

    run = db.create_backup_run(
        kind=kind, file_name=file_name, file_path=file_path, file_size_bytes=file_size,
        cloud_synced=cloud_result.get('synced', False), cloud_path=cloud_result.get('target_path'),
        triggered_by=triggered_by,
    )

    _prune_old_backups(kind)

    return {**run, 'cloud_result': cloud_result}


# ------------------------------------------------------------------ Restore

UPLOADS_DIR = os.path.join(BACKUPS_DIR, 'uploads')
SQLITE_HEADER = b'SQLite format 3\x00'
# Tables every real backup has; a file missing any of them is something else
# (or damaged) and must not replace the live data.
REQUIRED_TABLES = {'users', 'orders', 'order_items', 'menu_items', 'inventory_items', 'guests', 'activity_log'}


def _load_backup(path):
    """Reads and validates a backup file. Raises ValueError with a readable
    reason when it isn't a usable backup of this system."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        raise ValueError('No se pudo leer el archivo.')
    if head == SQLITE_HEADER:
        raise ValueError('Este respaldo (.db) es de la versión anterior del sistema y no se puede restaurar en esta.')
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, EOFError, ValueError):
        raise ValueError(f'El archivo no es un respaldo del sistema ({BACKUP_EXTENSION}) o está dañado.')
    if not isinstance(payload, dict) or payload.get('format') != BACKUP_FORMAT or not isinstance(payload.get('tables'), dict):
        raise ValueError('El archivo no es un respaldo del sistema Arenas Beach.')
    tables = payload['tables']
    missing = REQUIRED_TABLES - set(tables)
    if missing:
        raise ValueError('El respaldo está incompleto (faltan tablas: ' + ', '.join(sorted(missing)) + ').')
    for name, table in tables.items():
        if not isinstance(table, dict) or not isinstance(table.get('columns'), list) or not isinstance(table.get('rows'), list):
            raise ValueError(f'El respaldo está dañado (tabla {name}).')
    return payload


def _rows_as_dicts(table):
    cols = table['columns']
    return [dict(zip(cols, row)) for row in table['rows']]


def inspect_backup(path):
    """Checks that `path` is a healthy backup and summarizes what's in it, so
    the admin can confirm it's the right day before restoring. Raises
    ValueError with a readable reason otherwise."""
    tables = _load_backup(path)['tables']
    users = _rows_as_dicts(tables['users'])
    if not any(u.get('role') == 'admin' and u.get('is_active', 1) == 1 for u in users):
        raise ValueError('Este respaldo no tiene ninguna cuenta de administrador; nadie podría entrar.')
    orders = _rows_as_dicts(tables['orders'])
    activity = _rows_as_dicts(tables['activity_log'])
    return {
        'last_order_at': max((o['timestamp'] for o in orders if o.get('timestamp')), default=None),
        'last_activity_at': max((a['created_at'] for a in activity if a.get('created_at')), default=None),
        'orders': len(orders),
        'open_orders': sum(1 for o in orders if o.get('status') == 'open'),
        'guests': len(tables['guests']['rows']),
        'menu_items': len(tables['menu_items']['rows']),
        'inventory_items': len(tables['inventory_items']['rows']),
        'users': len(users),
        'file_size_bytes': os.path.getsize(path),
    }


def _timestamp_from_name(file_name):
    try:
        stem = file_name[:-len(BACKUP_EXTENSION)] if file_name.endswith(BACKUP_EXTENSION) else file_name
        stamp = '_'.join(stem.rsplit('_', 2)[-2:])
        return datetime.strptime(stamp, '%Y-%m-%d_%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, IndexError):
        return None


def resync_backup_runs():
    """The backup list lives inside the database, so after restoring an
    older backup it would forget the backups made since. Re-list every backup
    file still on disk that the table doesn't know about."""
    conn = db.get_connection(write=True)
    known = {r[0] for r in conn.execute('SELECT file_path FROM backup_runs')}
    for kind in ('incremental', 'full'):
        for path in sorted(glob.glob(os.path.join(_backup_dir(kind), f'arenas_beach_{kind}_*{BACKUP_EXTENSION}'))):
            if path in known:
                continue
            name = os.path.basename(path)
            conn.execute('''
                INSERT INTO backup_runs (kind, file_name, file_path, file_size_bytes, triggered_by, created_at)
                VALUES (?, ?, ?, ?, 'recuperado del disco', COALESCE(?, now_local()))
            ''', (kind, name, path, os.path.getsize(path), _timestamp_from_name(name)))
    conn.commit()
    conn.close()


def restore_from_file(path, restored_by_name):
    """Replaces all live data with the backup at `path`. A full backup of the
    current data is taken first, so a wrong restore can be undone by
    restoring that copy. Everything is replaced in one transaction: if any
    row fails, nothing changes. Returns the summary plus the safety copy's name."""
    summary = inspect_backup(path)
    tables = _load_backup(path)['tables']
    safety = create_backup('full', triggered_by=f'automático antes de restaurar ({restored_by_name})')

    conn = db.get_connection(write=True)
    try:
        conn.execute(f"TRUNCATE {', '.join(db.TABLES)} RESTART IDENTITY CASCADE")
        for table in db.TABLES:
            if table not in tables:
                continue
            live_cols = {r['column_name'] for r in conn.execute(
                'SELECT column_name FROM information_schema.columns WHERE table_name = ?', (table,))}
            # Columns a newer/older version doesn't have are skipped; missing
            # ones take their defaults.
            keep = [i for i, c in enumerate(tables[table]['columns']) if c in live_cols]
            if not keep or not tables[table]['rows']:
                continue
            cols = [tables[table]['columns'][i] for i in keep]
            conn.executemany(
                f'INSERT INTO {table} ({", ".join(cols)}) OVERRIDING SYSTEM VALUE '
                f'VALUES ({", ".join("?" for _ in cols)})',
                [[row[i] for i in keep] for row in tables[table]['rows']])
            if 'id' in cols:
                # Next new row continues after the restored ids.
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
        # Tickets still queued in the backup were for orders from back then;
        # the kitchen must not get them again now.
        conn.execute("UPDATE print_jobs SET status = 'printed', last_error = 'Omitida al restaurar un respaldo' "
                     "WHERE status != 'printed'")
        conn.execute("INSERT INTO activity_log (user_id, user_name, user_role, action, details) VALUES (NULL, ?, 'admin', 'database_restored', ?)",
                     (restored_by_name, f"Restauró la base de datos (última orden: {summary['last_order_at'] or '—'}). "
                                        f"Copia previa guardada como {safety['file_name']}"))
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise ValueError(f'El respaldo no se pudo cargar y no se cambió nada: {e}')
    finally:
        conn.close()

    db.init_db()   # re-seeds anything the backup lacked (e.g. the room roster)
    resync_backup_runs()
    return {**summary, 'safety_backup': safety['file_name']}


def _prune_old_backups(kind):
    keep = RETENTION.get(kind, 10)
    files = sorted(glob.glob(os.path.join(_backup_dir(kind), f'arenas_beach_{kind}_*{BACKUP_EXTENSION}')))
    for old_file in files[:-keep]:
        try:
            os.remove(old_file)
        except OSError:
            pass
