"""ESC/POS rendering and sending of kitchen/bar tickets ("comandas").

Standard library only, so the hotel's print agent (print_agent.py) can use it
on any PC with plain Python — no database or web libraries needed. Raw
ESC/POS over TCP port 9100 is what Epson, Star, Bixolon, Xprinter and most
80 mm receipt printers accept.
"""
import socket
import textwrap

LINE_WIDTH = 48          # characters per line in Font A on 80 mm paper
SEND_TIMEOUT_SECONDS = 5

ESC_INIT = b'\x1b@'
ESC_CODEPAGE_850 = b'\x1bt\x02'      # PC850: covers á é í ó ú ñ ¿ ¡
ESC_ALIGN_LEFT = b'\x1ba\x00'
ESC_ALIGN_CENTER = b'\x1ba\x01'
ESC_BOLD_ON = b'\x1bE\x01'
ESC_BOLD_OFF = b'\x1bE\x00'
GS_SIZE_NORMAL = b'\x1d!\x00'
GS_SIZE_TALL = b'\x1d!\x01'          # double height, normal width
GS_SIZE_BIG = b'\x1d!\x11'           # double height and width
GS_FEED_AND_CUT = b'\n\n\n\n\x1dV\x42\x00'


def _text(s):
    return str(s).encode('cp850', errors='replace')


def build_ticket(payload):
    """Renders one queued ticket as ESC/POS bytes."""
    if payload.get('kind') == 'document':
        return build_document(payload['lines'])
    out = [ESC_INIT, ESC_CODEPAGE_850, ESC_ALIGN_CENTER, ESC_BOLD_ON, GS_SIZE_BIG,
           _text(payload['station_label'].upper()), b'\n']
    if payload.get('is_addition'):
        out += [GS_SIZE_TALL, _text('** ADICIONAL **'), b'\n']
    out += [GS_SIZE_NORMAL, ESC_BOLD_OFF]

    if payload.get('table_number'):
        account = payload.get('account_name') or f"Cuenta {payload.get('sub_account') or 1}"
        where = f"MESA {payload['table_number']} - {account}"
    elif payload.get('room_number'):
        where = f"HAB. {payload['room_number']} - {payload.get('guest_name') or ''}".rstrip(' -')
        if payload.get('guest_type') == 'Desayuno incluido':
            where = f'DESAYUNO · {where}'
    else:
        where = payload.get('guest_type') or 'Para llevar'
        if payload.get('account_name'):
            where = f"{where} - {payload['account_name']}"   # e.g. Propietarios - Familia Pérez
    out += [GS_SIZE_TALL, ESC_BOLD_ON, _text(where), b'\n', ESC_BOLD_OFF, GS_SIZE_NORMAL]
    out += [_text(f"Orden {payload['receipt_number']}  ·  {payload['created_at']}"), b'\n']
    out += [_text(f"Mesero: {payload.get('server_name') or '-'}"), b'\n']
    out += [ESC_ALIGN_LEFT, _text('-' * LINE_WIDTH), b'\n']

    # Quantity and name in tall bold type so they read at arm's length.
    out += [GS_SIZE_TALL, ESC_BOLD_ON]
    for item in payload['items']:
        prefix = f"{item['quantity']:>2} x "
        lines = textwrap.wrap(item['name'], LINE_WIDTH - len(prefix)) or ['']
        out += [_text(prefix + lines[0]), b'\n']
        for cont in lines[1:]:
            out += [_text(' ' * len(prefix) + cont), b'\n']
        # Sides, doneness and the waiter's note, in normal type under the dish.
        if item.get('details'):
            out += [ESC_BOLD_OFF, GS_SIZE_NORMAL]
            for detail in item['details']:
                for i, part in enumerate(textwrap.wrap(detail, LINE_WIDTH - 6) or ['']):
                    out += [_text(('    > ' if i == 0 else '      ') + part), b'\n']
            out += [GS_SIZE_TALL, ESC_BOLD_ON]
    out += [ESC_BOLD_OFF, GS_SIZE_NORMAL, _text('-' * LINE_WIDTH), b'\n']
    out += [GS_FEED_AND_CUT]
    return b''.join(out)


# Bills and summaries at reception use a narrower column than the comandas,
# leaving a margin on both sides of the 80 mm roll.
DOC_WIDTH = 42


def build_document(lines):
    """Renders a reception document. Each line is one of:
    {'t': text, 'a': 'c' (centered) | absent, 'b': bold} — wrapped text;
    {'l': left, 'r': right, 'b': bold} — left text with an amount flush right;
    {'rule': 1} — a dashed separator."""
    out = [ESC_INIT, ESC_CODEPAGE_850]
    for ln in lines:
        out.append(ESC_BOLD_ON if ln.get('b') else ESC_BOLD_OFF)
        if ln.get('rule'):
            out += [ESC_ALIGN_LEFT, _text('-' * DOC_WIDTH), b'\n']
        elif 'l' in ln:
            right = str(ln.get('r') or '')
            wrapped = textwrap.wrap(str(ln['l']), DOC_WIDTH - len(right) - 1) or ['']
            out += [ESC_ALIGN_LEFT, _text(wrapped[0].ljust(DOC_WIDTH - len(right)) + right), b'\n']
            out += [x for cont in wrapped[1:] for x in (_text('  ' + cont), b'\n')]
        else:
            out.append(ESC_ALIGN_CENTER if ln.get('a') == 'c' else ESC_ALIGN_LEFT)
            for part in textwrap.wrap(str(ln.get('t') or ''), DOC_WIDTH) or ['']:
                out += [_text(part), b'\n']
    out += [ESC_BOLD_OFF, ESC_ALIGN_LEFT, GS_FEED_AND_CUT]
    return b''.join(out)


def build_test_ticket(station_label):
    return b''.join([
        ESC_INIT, ESC_CODEPAGE_850, ESC_ALIGN_CENTER, ESC_BOLD_ON, GS_SIZE_BIG,
        _text(station_label.upper()), b'\n', GS_SIZE_NORMAL, ESC_BOLD_OFF,
        _text('Prueba de impresión'), b'\n',
        _text('Arenas Beach Hotel'), b'\n',
        _text('Si puede leer esto (con tildes: á é í ó ú ñ),'), b'\n',
        _text('la impresora está lista.'), b'\n',
        GS_FEED_AND_CUT,
    ])


def send(host, port, data):
    with socket.create_connection((host, port), timeout=SEND_TIMEOUT_SECONDS) as conn:
        conn.sendall(data)


def friendly_error(host, port, exc):
    if isinstance(exc, socket.timeout):
        return f'La impresora {host}:{port} no respondió (¿apagada o sin red?)'
    if isinstance(exc, ConnectionRefusedError):
        return f'La impresora {host}:{port} rechazó la conexión (revise la IP y el puerto)'
    if isinstance(exc, socket.gaierror):
        return f'No se encontró la dirección {host}'
    return f'No se pudo imprimir en {host}:{port}: {exc}'
