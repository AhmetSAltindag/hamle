"""Loopback-only control panel for the local, non-executing Hamle review demo."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from typing import Any


TEST_SUBJECT = "HAMLE-IC-PROVA-20261010-YENIMAC-01"
COOKIE_NAME = "hamle_review_session"
MAX_BODY_BYTES = 256
MAX_HEADER_BYTES = 8192
TEMPLATE = Path(__file__).with_name("ui.html")


def public_projection(raw: dict[str, Any]) -> dict[str, Any]:
    """Never send message IDs, body, paths, fingerprints or arbitrary store fields."""
    control = raw.get("control", {})
    mode = control.get("mode")
    if mode not in {"review_open", "stopped", "takeover"}:
        raise ValueError("Unrecognized control mode")
    generation = control.get("generation")
    if type(generation) is not int or generation < 0:
        raise ValueError("Invalid generation")
    items = raw.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Invalid items")
    # This panel is deliberately constrained to a single, fixed internal demo.
    # Refuse incompatible store data rather than silently hiding extra work.
    if len(items) > 1 or raw.get("test_subject") != TEST_SUBJECT:
        raise ValueError("Unexpected demo scope")
    item = None
    if items:
        status = items[0].get("status")
        if status not in {"awaiting_human_review", "held_stale", "cancelled"}:
            raise ValueError("Unrecognized review state")
        if items[0].get("action_eligible") is not False:
            raise ValueError("Unexpected actionable item")
        item = {"subject": TEST_SUBJECT, "status": status, "action_eligible": False}
    if raw.get("demo_only") is not True or raw.get("external_actions_enabled") is not False:
        raise ValueError("Unexpected execution scope")
    return {
        "control": {"mode": mode, "generation": generation},
        "item_count": len(items),
        "item": item,
        "demo_only": True,
        "external_actions_enabled": False,
    }


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, store: Any):
        self.store = store
        self.csrf_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csp_nonce = secrets.token_urlsafe(24)
        # There is intentionally no configurable public bind address or fixed port.
        super().__init__(("127.0.0.1", 0), ReviewHandler)
        self.authority = f"127.0.0.1:{self.server_address[1]}"
        self.origin = f"http://{self.authority}"

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer
    server_version = "HamleLocalReview"
    sys_version = ""

    def log_message(self, format, *args):
        # No request, cookie, token, database path or body logging.
        pass

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def _headers_ok(self) -> bool:
        if len(self.headers) > 30 or sum(len(k) + len(v) for k, v in self.headers.items()) > MAX_HEADER_BYTES:
            self._error(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, "İstek çok büyük.")
            return False
        if self._single_header("Host") != self.server.authority:
            self._error(HTTPStatus.FORBIDDEN, "Yalnız yerel panel adresi kullanılabilir.")
            return False
        origin_values = self.headers.get_all("Origin", [])
        if origin_values and origin_values != [self.server.origin]:
            self._error(HTTPStatus.FORBIDDEN, "İstek kaynağı kabul edilmedi.")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error(HTTPStatus.FORBIDDEN, "İstek kaynağı kabul edilmedi.")
            return False
        return True

    def _session_ok(self) -> bool:
        cookie_header = self._single_header("Cookie")
        if cookie_header is None:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            supplied = cookie.get(COOKIE_NAME)
            return bool(supplied) and secrets.compare_digest(supplied.value, self.server.session_token)
        except (CookieError, TypeError, ValueError):
            return False

    def _reply(self, status: int, body: bytes, content_type: str, *, cookie: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", (
            "default-src 'none'; "
            f"script-src 'nonce-{self.server.csp_nonce}'; "
            f"style-src 'nonce-{self.server.csp_nonce}'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ))
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, status: int, value: dict):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._reply(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def do_GET(self):
        if not self._headers_ok():
            return
        if self.path == "/":
            page = TEMPLATE.read_text(encoding="utf-8")
            page = page.replace("__CSP_NONCE__", self.server.csp_nonce).replace("__SESSION_CSRF__", self.server.csrf_token)
            self._reply(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8", cookie=True)
        elif self.path == "/api/state":
            if not self._session_ok():
                self._error(HTTPStatus.FORBIDDEN, "Önce yerel paneli açın.")
                return
            self._read_state()
        elif self.path == "/api/control":
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Kontrol işlemi yalnız POST ile yapılabilir.")
        else:
            self._error(HTTPStatus.NOT_FOUND, "Sayfa bulunamadı.")

    def _read_state(self):
        try:
            result = public_projection(self.server.store.public_state())
        except Exception:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Yerel kayıt okunamadı. İşlem yetkisi kapalı.")
            return
        self._json(HTTPStatus.OK, result)

    def do_POST(self):
        if not self._headers_ok():
            return
        if self.path != "/api/control":
            self._error(HTTPStatus.NOT_FOUND, "Sayfa bulunamadı.")
            return
        token = self._single_header("X-CSRF-Token")
        if (
            self._single_header("Origin") != self.server.origin
            or not self._session_ok()
            or not token
            or not token.isascii()
            or not secrets.compare_digest(token, self.server.csrf_token)
        ):
            self._error(HTTPStatus.FORBIDDEN, "Panel oturumu doğrulanamadı. Sayfayı yenileyin.")
            return
        if self._single_header("Content-Type") != "application/json" or self.headers.get_all("Transfer-Encoding"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "İstek biçimi kabul edilmedi.")
            return
        length = self._single_header("Content-Length")
        if length is None or not length.isascii() or not length.isdecimal():
            self._error(HTTPStatus.LENGTH_REQUIRED, "İstek uzunluğu gerekli.")
            return
        if len(length) > 3 or not 0 < int(length) <= MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "İstek çok büyük veya boş.")
            return
        try:
            payload = self.rfile.read(int(length))
            if len(payload) != int(length):
                raise ValueError("Incomplete request body")
            # Duplicate JSON keys are also invalid; only one action is accepted.
            pairs = json.loads(payload.decode("utf-8"), object_pairs_hook=lambda p: p)
            if not isinstance(pairs, list) or len(pairs) != 1 or not isinstance(pairs[0], tuple):
                raise ValueError("Invalid object")
            key, action = pairs[0]
            if key != "action" or action not in ("stop", "takeover"):
                raise ValueError("Invalid action")
        except (ValueError, TypeError, TimeoutError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "Yalnız Durdur veya Ben devralıyorum işlemi kabul edilir.")
            return
        try:
            # Calls the local store directly. No external service/transport exists here.
            raw = self.server.store.stop() if action == "stop" else self.server.store.takeover()
            result = public_projection(raw)
        except Exception:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Kontrol sonucu doğrulanamadı. Durumu yenileyin.")
            return
        self._json(HTTPStatus.OK, result)

    def do_PUT(self):
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Yöntem desteklenmiyor.")

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_OPTIONS = do_PUT
    do_HEAD = do_PUT


def create_server(store: Any) -> ReviewServer:
    public_projection(store.public_state())
    return ReviewServer(store)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hamle yalnız yerel iç prova kontrol paneli")
    parser.add_argument("--db", required=True, type=Path, help="Önceden hazırlanmış yerel prova SQLite kaydı")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("Önceden hazırlanmış yerel prova kaydı gerekli; panel kayıt oluşturmaz.")
    from store import Store
    server = create_server(Store(args.db))
    print(f"Hamle yerel iç prova paneli: {server.origin}/", flush=True)
    print("İşlem yetkisi kapalı. Durdurmak için Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
