"""Print agent: runs on a PC at the hotel and prints the kitchen/bar tickets
("comandas") when the system itself runs in the cloud (Railway), which can't
reach the printers on the hotel network.

Every few seconds it asks the server for tickets waiting to print, sends
each one to its printer (same ESC/POS as before, see tickets.py) and reports
back whether it printed. If the PC or its internet goes down, the tickets
just wait on the server and the POS warns that printing is offline.

Setup on the hotel PC (Python 3 only, no extra packages):
  1. Copy print_agent.py and tickets.py into one folder.
  2. Next to them create print_agent.env with:
         SERVER_URL=https://your-domain.com
         PRINT_AGENT_TOKEN=<the same token set on the server>
  3. Run:  py print_agent.py      (or pythonw print_agent.py to hide the window;
     put a shortcut in the Windows Startup folder so it starts with the PC)

Printer IPs are configured in the web app (Panel -> Impresoras), not here.
"""
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

import tickets

HERE = os.path.dirname(os.path.abspath(__file__))
POLL_SECONDS = 3
MAX_BACKOFF_SECONDS = 60

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s  %(message)s',
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.path.join(HERE, 'print_agent.log'), encoding='utf-8')],
)
log = logging.getLogger('print_agent')


def load_config():
    config = {}
    path = os.path.join(HERE, 'print_agent.env')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()
    config.update({k: v for k, v in os.environ.items() if k in ('SERVER_URL', 'PRINT_AGENT_TOKEN')})
    missing = [k for k in ('SERVER_URL', 'PRINT_AGENT_TOKEN') if not config.get(k)]
    if missing:
        sys.exit(f'Falta configurar {", ".join(missing)} en print_agent.env')
    config['SERVER_URL'] = config['SERVER_URL'].rstrip('/')
    return config


class Server:
    def __init__(self, url, token):
        self.url, self.token = url, token

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'User-Agent': 'ArenasPrintAgent/1.0',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b'{}')

    def poll(self):
        return self._request('GET', '/api/print-agent/poll')

    def report(self, job_id, ok, error=None):
        self._request('POST', f'/api/print-agent/jobs/{job_id}', {'ok': ok, 'error': error})


def print_round(server, work):
    printers = work['printers']
    port = printers['port']
    test = work.get('test')
    if test:
        host = printers.get(test['station'])
        try:
            tickets.send(host, port, tickets.build_test_ticket(test['label']))
            log.info('Prueba impresa en %s (%s)', test['label'], host)
        except OSError as e:
            log.warning('Prueba en %s falló: %s', test['label'], tickets.friendly_error(host, port, e))

    # Once a printer fails this round, its other tickets wait for the next
    # retry instead of each sitting through the connect timeout.
    down = {}
    for job in work['jobs']:
        host = printers.get(job['station'])
        if not host:
            server.report(job['id'], False, f"No hay impresora configurada para {job['ticket'].get('station_label')}")
            continue
        if host in down:
            server.report(job['id'], False, down[host])
            continue
        try:
            tickets.send(host, port, tickets.build_ticket(job['ticket']))
        except OSError as e:
            down[host] = tickets.friendly_error(host, port, e)
            log.warning('Comanda %s no se imprimió: %s', job['id'], down[host])
            server.report(job['id'], False, down[host])
            continue
        server.report(job['id'], True)
        log.info('Comanda %s impresa en %s (orden %s)', job['id'], job['ticket'].get('station_label'),
                 job['ticket'].get('receipt_number'))


def main():
    config = load_config()
    server = Server(config['SERVER_URL'], config['PRINT_AGENT_TOKEN'])
    log.info('Programa de impresión iniciado — servidor %s', server.url)
    backoff = POLL_SECONDS
    connected = None
    while True:
        try:
            print_round(server, server.poll())
            if connected is not True:
                log.info('Conectado al servidor.')
            connected, backoff = True, POLL_SECONDS
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit('El servidor rechazó la clave (PRINT_AGENT_TOKEN). Revise que sea la misma del servidor.')
            log.warning('El servidor respondió con error %s; reintentando.', e.code)
            connected, backoff = False, min(backoff * 2, MAX_BACKOFF_SECONDS)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if connected is not False:
                log.warning('Sin conexión con el servidor (%s); reintentando.', e)
            connected, backoff = False, min(backoff * 2, MAX_BACKOFF_SECONDS)
        time.sleep(backoff)


if __name__ == '__main__':
    main()
