"""Minimal Server-Sent Events pub/sub so the public reservation site and the
front-desk dashboard push live updates to each other. Whenever a booking,
check-in, checkout, or cancellation happens anywhere, every connected client
(on either platform) is notified within seconds and re-fetches its own data —
that's the two-way real-time sync between the two front-ends.
"""
import json
import queue
import threading

_subscribers = []
_lock = threading.Lock()


def subscribe():
    q = queue.Queue()
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q):
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def broadcast(event_type, data=None):
    payload = json.dumps({'type': event_type, 'data': data or {}})
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        q.put(payload)


def stream(q):
    """Generator for a Flask Response — one per connected client."""
    try:
        yield 'retry: 2000\n\n'
        while True:
            try:
                payload = q.get(timeout=15)
                yield f'data: {payload}\n\n'
            except queue.Empty:
                yield ': keep-alive\n\n'
    finally:
        unsubscribe(q)
