"""Manual read-only verification of the transferred internal Basecamp setup.

No token output, raw provider payload persistence, or Basecamp write commands.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "work/basecamp-tools/bin/basecamp"
ENV = dict(os.environ)
ENV.update(
    XDG_CONFIG_HOME=str(ROOT / "work/private-connections/config"),
    XDG_CACHE_HOME=str(ROOT / "work/private-connections/cache"),
    BASECAMP_PROFILE="hamle-gamechangers",
)
for key in ("BASECAMP_TOKEN", "BASECAMP_NO_KEYRING"):
    if ENV.get(key):
        raise SystemExit("Unexpected credential override; stopped")


def read(*args):
    result = subprocess.run(
        [str(CLI), *args, "--account", "5898865", "--json"],
        env=ENV, capture_output=True, text=True, timeout=60,
    )
    if result.returncode or "fallback" in result.stderr.lower():
        raise SystemExit("Read failed or unsafe credential backend; stopped")
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise SystemExit("Invalid provider response; stopped") from None
    if payload.get("ok") is not True:
        raise SystemExit("Provider rejected read; stopped")
    return payload["data"]


status = read("auth", "status", "--check")
assert status.get("storage") == "keyring"
assert status.get("authenticated") is True
assert status.get("valid") is True
assert status.get("user_email") == "hamle@gamechangers.com.tr"
assert str(status.get("account_id")) == "5898865"

person = read("people", "show", "me")
assert str(person.get("id")) == "53358100"
assert person.get("email_address") == "hamle@gamechangers.com.tr"

project = read("projects", "show", "49184053")
assert str(project.get("id")) == "49184053"
assert project.get("name") == "Hamle — İç Deneme"
assert project.get("clients_enabled") is False

people = read("people", "list", "--in", "49184053", "--all")
assert isinstance(people, list)
member_ids = sorted(str(p["id"]) for p in people)
assert member_ids == ["47470759", "53358100"]

todo = read("todos", "show", "10391849167", "--no-comments")
assert str(todo.get("id")) == "10391849167"
assert str(todo.get("bucket", {}).get("id")) == "49184053"

evidence = {
    "verified_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).isoformat(),
    "device": "new_mac", "verification_kind": "read_only",
    "authenticated": True, "credential_storage": "keyring", "token_valid": True,
    "email": person["email_address"], "account_id": "5898865",
    "oauth_login_identity_id": "28787068", "account_person_id": "53358100",
    "identity_note": "OAuth login identity and account person ID are distinct namespaces.",
    "provider_scope": status.get("scope"),
    "broad_provider_scope_user_approved": True,
    "project_id": "49184053", "project_name": project["name"],
    "clients_enabled": False, "project_member_ids": member_ids,
    "unknown_members": False, "existing_test_todo_read": "10391849167",
    "todo_visible_to_clients": todo.get("visible_to_clients"),
    "basecamp_content_writes": 0, "paid_model_calls": 0,
}
out = ROOT / "outputs/new-mac-2026-10-10/basecamp-read-verification.json"
out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(evidence, ensure_ascii=False, indent=2))
