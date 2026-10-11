"""Synthetic-only state machine. No provider actions or SDK imports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Callable, Protocol, TypeVar

PROJECT = "gamechangers-hamle"
NAMESPACE = "hamle-synthetic-probe-v1"
SCHEMA = 1
SPEC = "fixed-synthetic-sum-1-through-100-v1"
DEADLINE = datetime(2026, 10, 12, 20, 59, 59, tzinfo=timezone.utc)
T = TypeVar("T")


class Refused(Exception):
    """A safe, static reason suitable for logs."""


class Tx(Protocol):
    def get(self, key: str) -> dict | None: ...
    def create(self, key: str, data: dict) -> None: ...
    def put(self, key: str, data: dict) -> None: ...


class Store(Protocol):
    def transact(self, callback: Callable[[Tx], T]) -> T: ...


@dataclass(frozen=True)
class Config:
    project: str
    namespace: str
    database: str
    run_id: str

    def validate(self) -> None:
        if self.project != PROJECT or self.namespace != NAMESPACE:
            raise Refused("project_or_namespace_not_allowed")
        if not re.fullmatch(r"(?:\(default\)|hamle-synthetic)", self.database):
            raise Refused("database_not_allowed")
        if not re.fullmatch(r"synthetic-[a-z0-9-]{1,48}", self.run_id):
            raise Refused("synthetic_run_id_required")

    @property
    def run_key(self) -> str:
        self.validate()
        return "run-" + hashlib.sha256(self.run_id.encode("ascii")).hexdigest()


def authorize_network(config: Config, opt_in: str, now: datetime) -> None:
    config.validate()
    if opt_in != "yes":
        raise Refused("explicit_firestore_opt_in_required")
    if now.tzinfo is None or now.astimezone(timezone.utc) > DEADLINE:
        raise Refused("spending_authorization_expired")


def valid_control(control: dict | None) -> dict:
    if (not isinstance(control, dict) or type(control.get("schema")) is not int
            or control.get("schema") != SCHEMA
            or type(control.get("enabled")) is not bool
            or type(control.get("generation")) is not int
            or control["generation"] < 0):
        raise Refused("control_missing_or_invalid")
    return control


def valid_run(run: dict) -> dict:
    if (not isinstance(run, dict) or type(run.get("schema")) is not int
            or run.get("schema") != SCHEMA or run.get("spec") != SPEC
            or type(run.get("generation")) is not int or run["generation"] < 0
            or type(run.get("attempts")) is not int or run["attempts"] != 1
            or not isinstance(run.get("owner"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", run["owner"])
            or run.get("state") not in ("started", "completed", "interrupted")):
        raise Refused("existing_run_invalid")
    if run["state"] == "completed":
        if type(run.get("synthetic_result")) is not int or run["synthetic_result"] != 5050:
            raise Refused("existing_run_invalid")
    elif "synthetic_result" in run:
        raise Refused("existing_run_invalid")
    return run


def initialize(store: Store) -> str:
    def operation(tx: Tx) -> str:
        control = tx.get("control")
        if control is not None:
            valid_control(control)
            return "already_initialized"
        tx.create("control", {"schema": SCHEMA, "enabled": False, "generation": 0})
        return "initialized_stopped"
    return store.transact(operation)


def set_enabled(store: Store, enabled: bool) -> str:
    if type(enabled) is not bool:
        raise Refused("invalid_control_action")
    def operation(tx: Tx) -> str:
        control = valid_control(tx.get("control"))
        # Every arm/stop fences earlier workers, including stop followed by arm.
        tx.put("control", {"schema": SCHEMA, "enabled": enabled,
                           "generation": control["generation"] + 1})
        return "armed" if enabled else "stopped"
    return store.transact(operation)


def claim(store: Store, config: Config, owner: str) -> str:
    config.validate()
    if not re.fullmatch(r"[a-f0-9]{32}", owner):
        raise Refused("invalid_owner")
    def operation(tx: Tx) -> str:
        control = valid_control(tx.get("control"))
        run = tx.get(config.run_key)
        if not control["enabled"]:
            return "stopped"
        if run is not None:
            valid_run(run)
            if run.get("state") == "completed":
                return "duplicate_completed"
            # Started, interrupted, malformed, or unknown runs are never reclaimed.
            return "existing_run_held"
        tx.create(config.run_key, {
            "schema": SCHEMA, "spec": SPEC, "state": "started",
            "owner": owner, "generation": control["generation"], "attempts": 1,
        })
        return "claimed"
    return store.transact(operation)


def complete(store: Store, config: Config, owner: str) -> str:
    config.validate()
    def operation(tx: Tx) -> str:
        control = valid_control(tx.get("control"))
        run = tx.get(config.run_key)
        valid_run(run)
        if run.get("owner") != owner or run.get("state") != "started":
            raise Refused("completion_owner_or_state_mismatch")
        if not control["enabled"] or run.get("generation") != control["generation"]:
            tx.put(config.run_key, {**run, "state": "interrupted"})
            return "stopped_or_fenced"
        # The sole result is fixed synthetic arithmetic, inside the same commit.
        tx.put(config.run_key, {**run, "state": "completed", "synthetic_result": 5050})
        return "completed"
    return store.transact(operation)


def run_once(store: Store, config: Config, owner: str) -> str:
    result = claim(store, config, owner)
    if result != "claimed":
        return result
    return complete(store, config, owner)
