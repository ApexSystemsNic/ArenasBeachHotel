"""Kitchen and bar order tickets ("comandas") on network thermal printers.

Orders queue their tickets in the print_jobs table (database._queue_print_jobs).
A printer that is off or out of paper never blocks taking orders: the job
stays queued and is retried, and the POS shows a warning until it prints or
someone presses Reimprimir. Who actually sends the tickets depends on where
the app runs:

- On the hotel PC (default): a background thread here sends each one to its
  station's printer on the local network (tickets.py).
- In the cloud (PRINT_AGENT_TOKEN set): the server can't reach the hotel's
  printers, so the print agent (print_agent.py) running on a hotel PC polls
  /api/print-agent for due jobs, prints them and reports back. The token is
  the shared secret the agent sends.
"""
import json
import logging
import os
import threading
import time

import database as db
import tickets

log = logging.getLogger(__name__)

POLL_SECONDS = 3
AGENT_TOKEN = os.environ.get('PRINT_AGENT_TOKEN', '').strip()
AGENT_MODE = bool(AGENT_TOKEN)
# The agent polls every few seconds; silence longer than this means it is
# off (PC asleep, no internet) and tickets are not reaching the kitchen.
AGENT_OFFLINE_AFTER_SECONDS = 30

_wake = threading.Event()
_worker = None


def print_test(station):
    """Sends a test page. Raises ValueError with a readable message. In agent
    mode the request is handed to the agent, which prints it on its next poll.
    Returns 'sent' or 'agent'."""
    settings = db.get_printer_settings()
    host = settings.get(f'{station}_host')
    if not host:
        raise ValueError(f'No hay IP configurada para {db.STATION_LABELS_ES[station]}')
    if AGENT_MODE:
        if agent_offline():
            raise ValueError('El programa de impresión del hotel no está conectado; no se puede enviar la prueba.')
        db.set_setting('print_agent_test_station', station)
        return 'agent'
    try:
        tickets.send(host, settings['port'], tickets.build_test_ticket(db.STATION_LABELS_ES[station]))
    except OSError as e:
        raise ValueError(tickets.friendly_error(host, settings['port'], e))
    return 'sent'


def process_due_jobs():
    settings = db.get_printer_settings()
    # Once a printer fails this round, its other tickets wait for the next
    # retry instead of each sitting through the connect timeout — a dead
    # kitchen printer must not hold up the bar's tickets.
    down = {}
    for job in db.due_print_jobs():
        host = settings.get(f"{job['station']}_host")
        if not host:
            db.mark_print_job_failed(job['id'], f"No hay impresora configurada para {db.STATION_LABELS_ES[job['station']]}")
            continue
        if host in down:
            db.mark_print_job_failed(job['id'], down[host])
            continue
        try:
            tickets.send(host, settings['port'], tickets.build_ticket(json.loads(job['payload'])))
        except OSError as e:
            down[host] = tickets.friendly_error(host, settings['port'], e)
            if db.mark_print_job_failed(job['id'], down[host]):
                log.warning('Print job %s gave up: %s', job['id'], e)
            continue
        db.mark_print_job_printed(job['id'])


# ------------------------------------------------------------- Print agent

def agent_poll():
    """What the agent needs on each poll: printer addresses, the due jobs and
    any pending test page. Also records that the agent is alive."""
    db.set_setting('print_agent_seen_at', time.time())
    test_station = db.get_setting('print_agent_test_station')
    if test_station:
        db.set_setting('print_agent_test_station', None)
    settings = db.get_printer_settings()
    # The on/off switch is for the automatic comandas; bills at reception are
    # printed on request, so they go out either way.
    jobs = [j for j in db.due_print_jobs() if settings['enabled'] or j['station'] == 'reception']
    return {
        'printers': {'kitchen': settings['kitchen_host'], 'bar': settings['bar_host'],
                     'reception': settings['reception_host'], 'port': settings['port']},
        'jobs': [{'id': j['id'], 'station': j['station'], 'ticket': json.loads(j['payload'])} for j in jobs],
        'test': {'station': test_station, 'label': db.STATION_LABELS_ES[test_station]} if test_station else None,
    }


def agent_report(job_id, ok, error=None):
    if ok:
        db.mark_print_job_printed(job_id)
    elif db.mark_print_job_failed(job_id, error or 'Error desconocido en el programa de impresión'):
        log.warning('Print job %s gave up: %s', job_id, error)


def agent_offline():
    seen = db.get_setting('print_agent_seen_at')
    return seen is None or time.time() - float(seen) > AGENT_OFFLINE_AFTER_SECONDS


def status():
    """Stuck tickets per station, plus whether the agent has gone quiet while
    printing is on (then nothing prints, though no job has failed yet)."""
    result = db.print_status()
    result['agent_mode'] = AGENT_MODE
    result['agent_offline'] = AGENT_MODE and db.get_printer_settings()['enabled'] and agent_offline()
    return result


def notify():
    """Called after an order queues tickets, so they print now instead of on
    the next poll."""
    _wake.set()


def _run():
    while True:
        _wake.wait(POLL_SECONDS)
        _wake.clear()
        try:
            process_due_jobs()
        except Exception:
            log.exception('Print worker error')


def start_worker():
    """Call once per real running process (same guard as the scheduler). In
    agent mode the agent does the sending, so no worker runs here."""
    global _worker
    if AGENT_MODE:
        return None
    if _worker is None:
        _worker = threading.Thread(target=_run, name='print-worker', daemon=True)
        _worker.start()
    return _worker
