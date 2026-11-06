"""Background launcher for the hotel app (started from the Windows Startup folder).

- Runs app.py with no console window (this file is opened with pythonw.exe).
- Restarts it automatically if it ever stops unexpectedly.
- Writes its output to logs/server.log, rotating at 5 MB (keeps 3 old files),
  since every request is logged and the file would otherwise grow forever.
- Does nothing if the app is already running on port 8000, so a second
  start (double-click, re-login) can't launch a duplicate.
"""
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8000
LOG_DIR = os.path.join(APP_DIR, 'logs')
RESTART_DELAY_SECONDS = 5


def already_running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', PORT)) == 0


def make_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, 'server.log'), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    log = logging.getLogger('hotel')
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    return log


def main():
    if already_running():
        return
    log = make_logger()
    python = os.path.join(os.path.dirname(sys.executable), 'python.exe')
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', FLASK_DEBUG='0')
    while True:
        log.info(time.strftime('=== %Y-%m-%d %H:%M:%S  starting app.py ==='))
        proc = subprocess.Popen(
            [python, 'app.py'], cwd=APP_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in proc.stdout:
            log.info(line.rstrip('\n'))
        code = proc.wait()
        log.info(time.strftime(f'=== %Y-%m-%d %H:%M:%S  app.py stopped (exit code {code}); '
                               f'restarting in {RESTART_DELAY_SECONDS}s ==='))
        time.sleep(RESTART_DELAY_SECONDS)
        if already_running():   # something else took the port meanwhile
            return


if __name__ == '__main__':
    main()
