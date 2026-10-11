"""Mac-side, read-only Basecamp readback for catalog brand_routes (never run by tests or the cloud).

Default mode is READ-ONLY: for every brand route it GETs the project, its people and
its todoset's todolists, then prints the facts the catalog needs (clients_enabled,
Hamle/SM/rep/Ahmet membership, each list's visible_to_clients) and a proposed patch.
It never edits the catalog file; a human copies the values after review.

  BASECAMP_ACCESS_TOKEN=... python3 brand_routes_readback.py [--brand wat_mobilite]

Optional, explicit write (one brand per call, needs both flags):
  BASECAMP_ACCESS_TOKEN=... python3 brand_routes_readback.py --brand rabam --create-hamle-list --confirm-create
creates ONE todolist "Hamle İşleri" in that brand's todoset (only if no list with that name
exists), then reads it back and reports visible_to_clients. If it comes back client-visible
the route must stay unverified (hide it in Basecamp first, re-run the readback).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ORIGIN = "https://3.basecampapi.com"
HAMLE_LIST_NAME = "Hamle İşleri"
CATALOG = Path(__file__).resolve().parents[2] / "outputs/gamechangers-internal/gamechangers-internal-catalog.json"


def http_get_factory(token):
    def call(url, body=None):
        req = urllib.request.Request(url, method="POST" if body is not None else "GET",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "User-Agent": "Hamle brand readback (ahmet@gamechangers.com.tr)"})
        with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 (fixed https origin)
            return json.loads(response.read(2_000_000) or b"null")
    return call


def readback(catalog, key, call):
    """Read-only facts for one brand route. call(url) -> parsed JSON."""
    account = catalog["basecamp"]["account_id"]
    hamle = catalog["basecamp"]["hamle_person_id"]
    ahmet = catalog["brand_routing"]["principal_person_id"]
    route = catalog["brand_routes"][key]
    pid = route["project_id"]
    base = f"{ORIGIN}/{account}"
    project = call(f"{base}/projects/{pid}.json")
    people = {p.get("id") for p in call(f"{base}/projects/{pid}/people.json") or []}
    todoset = next((d for d in project.get("dock") or [] if d.get("name") == "todoset"), {})
    ts = todoset.get("id")
    lists = call(f"{base}/buckets/{pid}/todosets/{ts}/todolists.json") if ts else []
    wanted = {k: v for k, v in (route.get("lists") or {}).items() if v}
    seen = {row.get("id"): row for row in lists or []}
    facts = {
        "brand_key": key, "project_id": pid, "project_name_from_basecamp": project.get("name"),
        "clients_enabled": project.get("clients_enabled"), "todoset_id": ts,
        "hamle_member": hamle in people,
        "members_ok": {"sm": route.get("sm_person_id") in people, "rep": route.get("rep_person_id") in people,
                       "ahmet": ahmet in people},
        "lists": {kind: {"id": lid, "exists": lid in seen, "active": (seen.get(lid) or {}).get("status") == "active",
                         "visible_to_clients": (seen.get(lid) or {}).get("visible_to_clients")}
                  for kind, lid in wanted.items()},
        "all_lists": [{"id": r.get("id"), "name": r.get("name"), "visible_to_clients": r.get("visible_to_clients")}
                      for r in lists or []],
    }
    ok = (facts["hamle_member"] and all(facts["members_ok"].values()) and type(facts["clients_enabled"]) is bool
          and wanted.get("social") and wanted.get("project")
          and all(v["exists"] and v["active"] and v["visible_to_clients"] is False for v in facts["lists"].values()))
    facts["proposed_patch"] = {"todoset_id": ts, "clients_enabled": facts["clients_enabled"],
                               "lists_visible_to_clients": False if ok else None, "hamle_member": facts["hamle_member"],
                               "verified": bool(ok)}
    return facts


def create_hamle_list(catalog, key, call):
    account = catalog["basecamp"]["account_id"]
    pid = catalog["brand_routes"][key]["project_id"]
    facts = readback(catalog, key, call)
    if any(r["name"] == HAMLE_LIST_NAME for r in facts["all_lists"]):
        return {"created": False, "reason": "already_exists", "facts": facts}
    if not facts["todoset_id"] or not facts["hamle_member"]:
        return {"created": False, "reason": "todoset_or_membership_missing", "facts": facts}
    row = call(f"{ORIGIN}/{account}/buckets/{pid}/todosets/{facts['todoset_id']}/todolists.json",
               {"name": HAMLE_LIST_NAME, "description": "Hamle tarafından açılan ekip içi işler (müşteriye görünmez)."})
    back = call(f"{ORIGIN}/{account}/buckets/{pid}/todolists/{row['id']}.json")
    return {"created": True, "list_id": back.get("id"), "visible_to_clients": back.get("visible_to_clients"),
            "use_as_route_list": back.get("visible_to_clients") is False}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--brand", action="append")
    ap.add_argument("--create-hamle-list", action="store_true")
    ap.add_argument("--confirm-create", action="store_true")
    args = ap.parse_args(argv)
    token = os.environ.get("BASECAMP_ACCESS_TOKEN")
    if not token:
        ap.error("BASECAMP_ACCESS_TOKEN is required (never stored by this script)")
    if args.create_hamle_list and not (args.confirm_create and args.brand and len(args.brand) == 1):
        ap.error("--create-hamle-list needs exactly one --brand and --confirm-create")
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    call = http_get_factory(token)
    if args.create_hamle_list:
        print(json.dumps(create_hamle_list(catalog, args.brand[0], call), ensure_ascii=False, indent=2))
        return 0
    keys = args.brand or sorted(catalog["brand_routes"])
    out = {}
    for key in keys:
        try:
            out[key] = readback(catalog, key, lambda url: call(url))
        except Exception as exc:  # report and continue; never guesses a value
            out[key] = {"error": type(exc).__name__}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
