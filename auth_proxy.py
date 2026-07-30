"""OpenHost front proxy for the Unciv multiplayer server.

UncivServer is a headless HTTP API that Unciv game clients talk to;
there is no browser login page and no user session to mint.  So this
sidecar does NOT do an SSO auto-login dance (unlike the Pattern A/B/D
apps).  Its jobs are:

  * Serve ``/_healthz`` locally with a static 200 so OpenHost's readiness
    probe flips to "alive" the moment the proxy binds, without waiting on
    (or flapping during) the UncivServer JVM cold start.

  * Serve a small, owner-only landing page at ``/`` (and ``/index.html``)
    that tells the zone owner exactly which URL to paste into their Unciv
    client's "Server address" box.  UncivServer itself returns 404 at
    ``/``, so this is pure UX we add at the seam.  This page is reachable
    only by the owner because ``/`` is NOT in openhost.toml's
    ``public_paths`` — anonymous visitors get bounced to OpenHost SSO.

  * Transparently forward the game API (``/isalive``, ``/files/*``,
    ``/auth``, and the ``/chat`` WebSocket) to UncivServer on loopback
    127.0.0.1:8081.  These paths ARE public (see openhost.toml) because
    the Unciv game clients that call them cannot perform OpenHost's
    browser zone_auth flow — friends join by pointing their client at
    the server URL, exactly as with any self-hosted Unciv server.

Security model: the game API is public, which is inherent to how Unciv
multiplayer works (every self-hosted Unciv server is reachable by the
clients that use it).  UncivServer's optional per-user "auth v1"
(enabled by default here) lets each player set a password that guards
writes to *their own* save slot; see the README.  We add defensive
hardening at this seam: we strip any client-supplied
``X-OpenHost-*`` trust headers before forwarding upstream, and we cap
request bodies.

Implementation is adapted from openhost-lila/auth_proxy.py (same
transparent-forward + local-health + WebSocket-tunnel machinery),
minus the auto-login logic which Unciv does not need.
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import AbstractSet, Iterable

# Trust headers a hostile client could try to forge.  UncivServer does
# not read them, but we strip them defensively so nothing downstream can
# ever be fooled by a client-supplied value.
ALWAYS_STRIP_HEADERS = frozenset(
    h.lower() for h in ("X-OpenHost-Is-Owner", "X-OpenHost-User")
)

# Hop-by-hop headers (RFC 9110 §7.6.1) plus framing headers we rebuild
# ourselves at the proxy seam.
HOP_BY_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Host",
        "Content-Length",
    )
)

CLIENT_READ_TIMEOUT_SECONDS = 60

# Unciv game saves can be a few MiB of JSON.  32 MiB is comfortably
# more than any single save.
MAX_BODY_BYTES = 32 * 1024 * 1024

HEALTH_PATH = "/_healthz"

# Paths the owner-landing page lives at.  Everything else is forwarded.
LANDING_PATHS = frozenset(("/", "/index.html"))

logging.basicConfig(
    level=os.environ.get("AUTH_PROXY_LOG_LEVEL", "INFO"),
    format="[unciv-proxy] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("unciv_proxy")


def _strip_headers(
    headers: Iterable[tuple[str, str]], drop: AbstractSet[str]
) -> list[tuple[str, str]]:
    drop_lower = {h.lower() for h in drop}
    return [(k, v) for k, v in headers if k.lower() not in drop_lower]


def _landing_html(server_url: str) -> bytes:
    """Owner-facing setup page.  ``server_url`` is the public base URL
    the owner should paste into Unciv → Options → Multiplayer.
    """
    safe = server_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Unciv multiplayer server</title>"
        "<style>"
        "body{background:#1c1b1a;color:#e8e6e3;font-family:system-ui,sans-serif;"
        "max-width:640px;margin:3rem auto;padding:0 1.2rem;line-height:1.55}"
        "h1{font-size:1.5rem}code,.url{background:#2b2a28;padding:.2em .45em;"
        "border-radius:6px;font-family:ui-monospace,monospace;word-break:break-all}"
        ".url{display:inline-block;font-size:1.05rem;margin:.3rem 0}"
        "ol{padding-left:1.2rem}li{margin:.5rem 0}a{color:#7cb3ff}"
        ".muted{color:#a8a4a0;font-size:.92rem}"
        "</style></head><body>"
        "<h1>Your Unciv multiplayer server is running</h1>"
        "<p>Point your Unciv game client at this server to play online "
        "multiplayer with friends.</p>"
        "<p>Server address:</p>"
        f"<p><span class=\"url\">{safe}</span></p>"
        "<h2>How to use it</h2>"
        "<ol>"
        "<li>In Unciv, open <code>Main Menu → Options → Multiplayer</code>.</li>"
        "<li>Set <b>Server address</b> to the URL above and click "
        "<b>Check connection to server</b> — you should see "
        "<b>Success!</b></li>"
        "<li>Share the same URL with your friends so their clients use the "
        "same server.</li>"
        "<li>Start an <code>Online multiplayer</code> game and share the "
        "game ID, as usual.</li>"
        "</ol>"
        "<p class=\"muted\">This page is only visible to you (the zone "
        "owner). The game API itself is reachable by anyone with the URL "
        "so friends' clients can connect — the same as any self-hosted "
        "Unciv server. Players can set a per-save password in Unciv "
        "(Multiplayer options) to protect their own games.</p>"
        "</body></html>"
    ).encode("utf-8")


class UncivProxyHandler(BaseHTTPRequestHandler):
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8081

    def log_message(self, format: str, *args) -> None:  # noqa: A002, N802
        log.info("%s - " + format, self.address_string(), *args)

    # All HTTP verbs funnel through _dispatch.
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _safe_send_error(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except OSError as exc:
            log.debug("client disconnected before error response: %s", exc)

    def _path_only(self) -> str:
        return self.path.split("?", 1)[0]

    def _dispatch(self) -> None:
        try:
            self.connection.settimeout(CLIENT_READ_TIMEOUT_SECONDS)
        except OSError:
            pass

        path = self._path_only()

        # Health probe: answered locally, never forwarded.
        if path == HEALTH_PATH:
            self._serve_health()
            return

        # WebSocket upgrade (/chat): tunnel straight through.
        upgrade = self.headers.get("Upgrade", "").lower().strip()
        if upgrade == "websocket":
            self._proxy_websocket()
            return

        # Owner landing page.
        if self.command == "GET" and path in LANDING_PATHS:
            self._serve_landing()
            return
        if self.command == "HEAD" and path in LANDING_PATHS:
            self._serve_landing(head_only=True)
            return

        # Everything else → forward to UncivServer.
        self._proxy()

    def _serve_health(self) -> None:
        body = b"ok\n"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during health response: %s", exc)

    def _public_base_url(self) -> str:
        """Reconstruct the public base URL from the OpenHost router's
        forwarded headers, falling back to a sensible placeholder.
        """
        host = self.headers.get("X-Forwarded-Host", "").strip()
        if not host:
            host = self.headers.get("Host", "").strip()
        proto = self.headers.get("X-Forwarded-Proto", "https").strip() or "https"
        if not host:
            return "https://<your-unciv-app-url>"
        return f"{proto}://{host}"

    def _serve_landing(self, head_only: bool = False) -> None:
        body = _landing_html(self._public_base_url())
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if not head_only and self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during landing response: %s", exc)

    def _proxy_websocket(self) -> None:
        """Tunnel a WebSocket upgrade (/chat) through to UncivServer.

        We copy bytes in both directions after relaying the handshake.
        """
        cleaned_headers = _strip_headers(self.headers.items(), ALWAYS_STRIP_HEADERS)
        try:
            up = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=15
            )
        except OSError as exc:
            log.warning("ws: upstream connect failed: %s", exc)
            self._safe_send_error(502, "Bad Gateway")
            return

        up_file = None
        try:
            try:
                request = f"{self.command} {self.path} HTTP/1.1\r\n"
                for k, v in cleaned_headers:
                    request += f"{k}: {v}\r\n"
                request += "\r\n"
                up.sendall(request.encode("latin-1"))
            except OSError as exc:
                log.warning("ws: upstream send failed: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return

            up_file = up.makefile("rb")
            status_line = up_file.readline()
            if not status_line:
                self._safe_send_error(502, "Bad Gateway")
                return
            try:
                self.wfile.write(status_line)
            except OSError:
                return

            while True:
                line = up_file.readline()
                if not line:
                    return
                try:
                    self.wfile.write(line)
                except OSError:
                    return
                if line in (b"\r\n", b"\n"):
                    break

            import threading

            def _copy(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass

            t1 = threading.Thread(target=_copy, args=(self.connection, up), daemon=True)
            t2 = threading.Thread(target=_copy, args=(up, self.connection), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            if up_file is not None:
                try:
                    up_file.close()
                except OSError:
                    pass
            try:
                up.close()
            except OSError:
                pass

    def _proxy(self) -> None:
        cleaned_headers = _strip_headers(
            self.headers.items(), HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS
        )
        # Always emit a Host header upstream (RFC 9112).  UncivServer does
        # not Origin-check, so a loopback Host is fine.
        cleaned_headers.append(("Host", f"{self.upstream_host}:{self.upstream_port}"))

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower().strip()
        if transfer_encoding and transfer_encoding != "identity":
            self._safe_send_error(501, "Transfer-Encoding not supported")
            return

        body: bytes | None = None
        content_length_header = self.headers.get("Content-Length")
        if content_length_header:
            try:
                length = int(content_length_header)
            except ValueError:
                self._safe_send_error(400, "invalid Content-Length")
                return
            if length < 0:
                self._safe_send_error(400, "negative Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._safe_send_error(413, "request body too large")
                return
            if length > 0:
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    log.info("client read error: %s", exc)
                    self._safe_send_error(400, "request body read failed")
                    return
                if len(body) != length:
                    log.info(
                        "short read: expected %d bytes, got %d", length, len(body)
                    )
                    self._safe_send_error(400, "incomplete request body")
                    return
            else:
                body = b""
        elif self.command in ("POST", "PUT", "PATCH", "DELETE"):
            body = b""

        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=120
        )
        try:
            try:
                conn.putrequest(
                    self.command,
                    self.path,
                    skip_host=True,
                    skip_accept_encoding=True,
                )
                for key, value in cleaned_headers:
                    conn.putheader(key, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(message_body=body)
                upstream = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream error: %s", exc)
                self._serve_cold_start_placeholder()
                return

            try:
                payload = upstream.read(MAX_BODY_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream read error: %s", exc)
                self._serve_cold_start_placeholder()
                try:
                    upstream.close()
                except Exception as close_exc:  # noqa: BLE001
                    log.debug("upstream.close() raised: %s", close_exc)
                return
            try:
                upstream.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("upstream.close() raised (ignored): %s", exc)
            if len(payload) > MAX_BODY_BYTES:
                log.warning(
                    "upstream response exceeded %d bytes; returning 502",
                    MAX_BODY_BYTES,
                )
                self._safe_send_error(502, "upstream response too large")
                return

            reason = upstream.reason or ""
            try:
                self.send_response(upstream.status, reason)
                for key, value in upstream.getheaders():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except OSError as exc:
                log.debug("client disconnected mid-response: %s", exc)
        finally:
            conn.close()

    def _serve_cold_start_placeholder(self) -> None:
        """During cold start (UncivServer JVM still booting), return a
        503 so the Unciv client retries rather than seeing a raw error.
        """
        body = b"Unciv server is starting; please retry shortly.\n"
        try:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", "3")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            log.debug("client disconnected during cold-start placeholder: %s", exc)


class IPv4ThreadingServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


def _port_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer: {exc}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name}={raw!r} is out of range (1-65535)")
    return port


def main() -> int:
    try:
        listen_port = _port_from_env("AUTH_PROXY_LISTEN_PORT", 8080)
        upstream_port = _port_from_env("AUTH_PROXY_UPSTREAM_PORT", 8081)
    except ValueError as exc:
        log.error("invalid port configuration: %s", exc)
        return 1

    upstream_host = os.environ.get("AUTH_PROXY_UPSTREAM_HOST", "127.0.0.1").strip()

    UncivProxyHandler.upstream_host = upstream_host
    UncivProxyHandler.upstream_port = upstream_port

    try:
        server = IPv4ThreadingServer(("0.0.0.0", listen_port), UncivProxyHandler)
    except OSError as exc:
        log.error(
            "failed to bind listener on 0.0.0.0:%d: %s", listen_port, exc
        )
        return 1
    log.info(
        "listening on 0.0.0.0:%d -> %s:%d", listen_port, upstream_host, upstream_port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
