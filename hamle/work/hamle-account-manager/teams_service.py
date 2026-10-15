"""Hamle Teams service: a small always-on Cloud Run SERVICE for the Teams bot (separate from the 15-minute jobs).

  POST /api/messages   Bot Framework activities (v11_teams.TeamsDesk.handle)
  GET  /health         200 'ok' (no store, no secret; /healthz is reserved by Cloud Run)

WSGI app for the gunicorn already in the image:
  gunicorn --bind :$PORT --workers 1 --threads 4 'teams_service:create_app()'
Answers to @mentions run on one background thread after the HTTP reply (Bot Framework expects a fast 200), so the
service runs with CPU always allocated and min-instances 1. Nothing is built at import: the desk (store, secrets,
config) is built on the first request and only when client-watch-config.json teams.enabled is true.
Local smoke without Cloud: python3 teams_service.py (wsgiref on $PORT, desk_factory must be injected by the caller).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
from datetime import datetime, timezone

MAX_BODY = 256 * 1024


class Background:
    """One daemon thread, bounded queue; a full queue drops the job (the mention stays 'queued' in the store)."""
    def __init__(self, size=100):
        self.jobs = queue.Queue(maxsize=size)
        self.thread = None
        self.lock = threading.Lock()

    def _loop(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                pass  # TeamsDesk.answer records its own outcome
            finally:
                self.jobs.task_done()

    def __call__(self, job):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name="teams-answers", daemon=True)
                self.thread.start()
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            pass


def build_live_desk(run_later):
    """Existing loaders only: Firestore operations store, Secret Manager, catalog, client-watch config."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    import v11_client_watch as cw
    import v11_teams as teams
    from cloud_store import FirestoreBackend, OperationsStore
    from v11_basecamp_reader import SessionGet
    from v11_chain import BasecampCommentWriter
    from v11_chain_live import _memo, load_catalog
    from v11_daily_digest import load_brand_cards
    if os.environ.get("HAMLE_ALLOW_FIRESTORE_OPERATIONS") != "yes" or \
            os.environ.get("HAMLE_PROJECT_ID") != "gamechangers-hamle" or \
            os.environ.get("HAMLE_FIRESTORE_DATABASE") != "hamle-operations":
        raise teams.TeamsError("cloud_scope_invalid")
    cfg = cw.validate_config(cw.load_config())
    catalog = load_catalog()
    settings = teams.validate_teams(cfg.get("teams"), catalog)
    if settings is None:
        raise teams.TeamsError("teams_disabled")
    ops = OperationsStore(FirestoreBackend(opt_in="yes"))
    secrets = provider.GoogleSecretManager()
    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    jobs = todo_writer = None
    if cfg["mode"] == "reply":
        import v11_client_job as cj
        from v11_client_reply import validate_reply
        jobs = cj.validate_jobs(cfg.get("jobs"), catalog, validate_reply(cfg["reply"], catalog))
        if jobs is not None:
            todo_writer = cj.JobTodoWriter(catalog, jobs, token_provider=token)

    def basecamp_get(url, conditional):
        session = provider._session()
        try:
            return SessionGet(session, token)(url, conditional)
        finally:
            session.close()
    return teams.TeamsDesk(
        ops, settings, clock=lambda: datetime.now(timezone.utc),
        auth=teams.BotAuth(settings["app_id"], fetch_json=teams.https_json()),
        model=teams.teams_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)}, catalog),
        sender=teams.ConnectorSender(settings, secret_loader=lambda: provider.teams_bot_secret(secrets)),
        jobs=jobs, todo_writer=todo_writer, comment_writer=BasecampCommentWriter(catalog, token_provider=token),
        basecamp_get=basecamp_get, brands=cw.brand_index(catalog, load_brand_cards()), run_later=run_later)


def create_app(desk_factory=None):
    background = Background()
    box, lock = {}, threading.Lock()
    factory = desk_factory or build_live_desk

    def desk():
        with lock:
            if "desk" not in box:
                box["desk"] = factory(background)
            return box["desk"]

    def respond(start, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
                   413: "Payload Too Large", 503: "Service Unavailable"}
        start(f"{status} {reasons.get(status, 'Error')}", [("Content-Type", "application/json; charset=utf-8"),
                                                          ("Content-Length", str(len(body))),
                                                          ("Cache-Control", "no-store")])
        return [body]

    def app(env, start):
        path, method = env.get("PATH_INFO") or "/", env.get("REQUEST_METHOD") or "GET"
        if path in ("/health", "/healthz"):
            return respond(start, 200, {"status": "ok"})
        if path != "/api/messages":
            return respond(start, 404, {"status": "not_found"})
        if method != "POST":
            return respond(start, 405, {"status": "method_not_allowed"})
        try:
            length = int(env.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return respond(start, 413, {"status": "rejected", "reason": "body_too_large"})
        body = env["wsgi.input"].read(length) if length else b""
        headers = {k[5:].replace("_", "-").lower(): v for k, v in env.items() if k.startswith("HTTP_")}
        try:
            current = desk()
        except Exception as exc:
            reason = str(getattr(exc, "code", None) or type(exc).__name__)[:80]
            return respond(start, 503, {"status": "held", "reason": reason})
        status, payload = current.handle(headers, body)
        if status >= 400:  # reason codes only (auth_..., body_...): no message text, no token
            print(json.dumps({"event": "teams_request_refused", "status": status,
                              "reason": str(payload.get("reason") or payload.get("status"))[:80]}), file=sys.stderr)
        return respond(start, status, payload)
    return app


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    port = int(os.environ.get("PORT", "8080"))
    print(json.dumps({"status": "listening", "port": port}), file=sys.stderr)
    make_server("", port, create_app()).serve_forever()
