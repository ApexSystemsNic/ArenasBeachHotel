"""Web-layer protections: session secret, security headers / CSP, cross-site
request blocking, and simple in-memory rate limits.

The app runs as a single process on the hotel's own PC, so the rate limits
live in memory — a restart clears them, which is fine for their purpose
(slowing down password guessing and fake-booking floods from one device).
"""
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlsplit

from flask import g, jsonify, request, session

SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
_PUBLISHED_DEFAULT_KEY = 'dev-secret-key-change-in-production'


def load_secret_key():
    """The session cookie is signed with this key; anyone who knows it can
    forge a login as any user. Use SECRET_KEY from the environment if a real
    one is set, otherwise a random key generated once and kept in .secret_key
    next to the app (deleting that file just logs everyone out)."""
    env_key = os.environ.get('SECRET_KEY')
    if env_key and env_key != _PUBLISHED_DEFAULT_KEY:
        return env_key
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, encoding='utf-8') as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, 'w', encoding='utf-8') as f:
        f.write(key)
    return key


class RateLimiter:
    """Sliding-window counter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, q, now):
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, key):
        """Seconds until `key` may try again, or 0 if it isn't blocked."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            self._prune(q, now)
            if len(q) < self.limit:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def hit(self, key):
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            self._prune(q, now)
            q.append(now)

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


# 5 wrong passwords per 15 minutes, counted both per device and per username,
# so one phone can't guess many accounts and many phones can't gang up on one.
login_failures = RateLimiter(limit=5, window=15 * 60)
# A real guest books a handful of stays at most; this stops a script from
# filling every room with fake reservations.
public_bookings = RateLimiter(limit=5, window=60 * 60)
# Overall ceiling for requests that need no login (booking site, live events).
public_requests = RateLimiter(limit=300, window=60)


_LOOPBACK = {'127.0.0.1', '::1'}
# Set TRUST_PROXY_HEADERS=1 only where the app sits behind a proxy that
# always sets these headers (Railway); on the hotel PC they could be forged.
TRUST_PROXY_HEADERS = os.environ.get('TRUST_PROXY_HEADERS') == '1'


def client_ip():
    """The device to count limits against. Visitors coming through the
    Cloudflare tunnel all reach the app from this PC (127.0.0.1), so without
    this they would share one counter: five typos by any tester would lock
    out everyone, and five web bookings would block the whole internet for
    an hour. cloudflared runs on this PC and passes the visitor's real address
    in CF-Connecting-IP, which Cloudflare sets itself (a visitor can't forge
    it). The header is only believed on loopback connections, so a device on
    the hotel network can't fake it to dodge the limits."""
    remote = request.remote_addr or 'unknown'
    if TRUST_PROXY_HEADERS:
        # Hosted (Railway, with Cloudflare in front): every request arrives
        # from the platform's proxy, so the real visitor is in the headers.
        # Cloudflare's own header first, else the address the proxy appended.
        forwarded = (request.headers.get('CF-Connecting-IP')
                     or (request.headers.get('X-Forwarded-For') or '').split(',')[-1]).strip()
        return forwarded or remote
    if remote in _LOOPBACK:
        forwarded = (request.headers.get('CF-Connecting-IP') or '').strip()
        if forwarded:
            return forwarded
    return remote


def too_many(retry_after, message):
    resp = jsonify({'error': message})
    resp.status_code = 429
    resp.headers['Retry-After'] = str(retry_after)
    return resp


def _minutes(seconds):
    return max(1, round(seconds / 60))


def check_login_allowed(username):
    wait = max(login_failures.retry_after(('ip', client_ip())),
               login_failures.retry_after(('user', username.lower())))
    if wait:
        return too_many(wait, f'Demasiados intentos fallidos. Intente de nuevo en {_minutes(wait)} minuto(s).')
    return None


def record_login_failure(username):
    login_failures.hit(('ip', client_ip()))
    login_failures.hit(('user', username.lower()))


def record_login_success(username):
    login_failures.reset(('ip', client_ip()))
    login_failures.reset(('user', username.lower()))


def check_public_booking_allowed():
    wait = public_bookings.retry_after(client_ip())
    if wait:
        return too_many(wait, f'Demasiadas reservaciones desde este dispositivo. Intente de nuevo en {_minutes(wait)} minuto(s).')
    public_bookings.hit(client_ip())
    return None


def _same_origin(url):
    return urlsplit(url).netloc == request.host


def _cross_site_write():
    """A browser always sends Origin (or at least Referer) on a cross-site
    POST, so a write naming another site is a forgery attempt."""
    origin = request.headers.get('Origin')
    if origin:
        return origin == 'null' or not _same_origin(origin)
    referer = request.headers.get('Referer')
    return bool(referer) and not _same_origin(referer)


def init_app(app):
    app.secret_key = load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        # Browsers won't attach the login cookie to POSTs coming from another
        # website, which is the main defence against cross-site request forgery.
        SESSION_COOKIE_SAMESITE='Lax',
        # HTTPS-only cookie where the app is served over HTTPS (Railway). The
        # hotel PC is reached over plain http on the local network, where a
        # Secure cookie would never be sent back and nobody could log in.
        SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '1' if TRUST_PROXY_HEADERS else '0') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    @app.before_request
    def _guard():
        g.csp_nonce = secrets.token_urlsafe(16)

        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and _cross_site_write():
            return jsonify({'error': 'Solicitud rechazada: origen no permitido'}), 403

        if 'user_id' not in session and request.endpoint != 'static':
            wait = public_requests.retry_after(client_ip())
            if wait:
                return too_many(wait, 'Demasiadas solicitudes. Intente de nuevo en un momento.')
            public_requests.hit(client_ip())
        return None

    @app.context_processor
    def _inject_nonce():
        return {'csp_nonce': getattr(g, 'csp_nonce', '')}

    @app.after_request
    def _headers(resp):
        nonce = getattr(g, 'csp_nonce', '')
        # Only scripts this server rendered (carrying this request's nonce) or
        # the Tailwind CDN may run, so text a guest types into the booking form
        # can never execute in a staff member's browser even if it slips into
        # the page as HTML.
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'same-origin'
        resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if request.path.startswith('/api/'):
            # Keep guest/folio data out of the browser cache on shared devices.
            resp.headers.setdefault('Cache-Control', 'no-store')
        return resp
