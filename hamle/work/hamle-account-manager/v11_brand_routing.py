"""Deterministic brand -> Basecamp project routing for the internal chain (flag-gated, fail-closed).

The internal catalog keeps its `basecamp` block (account, Hamle identity, internal
project 49184053) and its single internal route ("Hamle İşleri"). `brand_routing`
switches routing on; `brand_routes` maps a brand key to its own Basecamp project and
client-hidden todolists (read back on Ahmet's Mac). Nothing here guesses:

  * detection is an exact alias match (word boundaries, Turkish-aware lowercase) on the
    request subject + latest body (quoted text, e-mail addresses and URLs removed);
    exactly one brand -> that brand, zero or several -> internal fallback route;
  * a brand route is used only when it is verified, its project/lists/people are
    positive ids, every list was read back with visible_to_clients=false, clients_enabled
    is a known bool and the brand is not excluded; otherwise the internal route is used
    and the brand name goes into the task title;
  * per work, the existing modules (v11_intake_plan / v11_brand_task) receive a derived
    single-project catalog, so every existing guard (account, Hamle identity, list
    visible_to_clients=false readback, notify=false, profile-derived body) still applies.

No network, no credentials, no model calls.
"""
from __future__ import annotations

import copy
import re

ROUTING_KEYS = ("brand_routing", "brand_routes", "brand_routes_excluded")
MANAGER_ROLES = ("social_manager", "principal", "strategy_owner")
KINDS = ("graphic", "animation", "video", "text")
LIST_KINDS = ("social", "project", "motion")
SOCIAL_TERMS = ("post", "story", "stories", "reels", "reel", "carousel", "gönderi", "sosyal medya",
                "instagram", "tiktok", "linkedin")
MOTION_KINDS = ("animation", "video")


class RoutingError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise RoutingError(code)


def _pos(value):
    return type(value) is int and value > 0


# ------------------------------------------------------------------ detection
def normalize(text):
    text = text if isinstance(text, str) else ""
    return text.replace("İ", "i").replace("I", "ı").lower()


_QUOTE_START = re.compile(r"^\s*(on .{0,200} wrote:|.{0,200} tarihinde .{0,200} (şunu )?yazdı:|-{2,}\s*original message"
                          r"|-{2,}\s*orijinal ileti|from:\s|kimden:\s)", re.IGNORECASE)


def _clean(text):
    lines = []
    for line in (text if isinstance(text, str) else "").splitlines():
        if _QUOTE_START.match(line):
            break  # quoted earlier thread: never route on it
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    out = "\n".join(lines)[:20000]
    out = re.sub(r"\S+@\S+", " ", out)  # addresses (e.g. @gamechangers.com.tr signatures)
    return re.sub(r"(https?://|www\.)\S+", " ", out)


def _matches(alias, haystack):
    alias = normalize(alias).strip()
    if not alias:
        return False
    return re.search(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", haystack) is not None


def _candidates(catalog):
    rows = []
    for key, route in sorted((catalog.get("brand_routes") or {}).items()):
        rows.append((key, route, False))
    for key, route in sorted((catalog.get("brand_routes_excluded") or {}).items()):
        rows.append((key, route, True))
    return rows


def detect_brand(catalog, subject, text):
    """-> {'status': 'single'|'none'|'ambiguous', 'brand_key', 'brand_name', 'excluded', 'matches'}"""
    subj = normalize(_clean(subject))
    body = normalize(_clean(text))
    matches = []
    for key, route, excluded in _candidates(catalog):
        if not isinstance(route, dict):
            continue
        aliases = [a for a in route.get("aliases") or [] if isinstance(a, str)]
        scope = route.get("match_scope", "subject_and_body")
        hay = subj if scope == "subject" else subj + "\n" + body
        if any(_matches(a, hay) for a in aliases):
            matches.append((key, route, excluded))
    out = {"status": "none", "brand_key": None, "brand_name": None, "excluded": False,
           "matches": [m[0] for m in matches]}
    if len(matches) == 1:
        key, route, excluded = matches[0]
        out.update(status="single", brand_key=key, brand_name=str(route.get("brand_name") or key)[:80],
                   excluded=excluded)
    elif len(matches) > 1:
        out["status"] = "ambiguous"
    return out


# ------------------------------------------------------------------ route validation
def enabled(catalog):
    return (catalog.get("brand_routing") or {}).get("enabled") is True


def _internal(catalog):
    bc = catalog.get("basecamp") or {}
    chain = catalog.get("chain") or {}
    routes = [r for r in catalog.get("routes", []) if r.get("service_id") == chain.get("default_service_id")
              and r.get("category_key") == chain.get("default_category_key")]
    _need(len(routes) == 1 and _pos(routes[0].get("list_id")) and _pos(bc.get("project_id")),
          "default_route_unconfirmed")
    return bc["project_id"], routes[0]


def _ahmet(catalog):
    pid = (catalog.get("brand_routing") or {}).get("principal_person_id")
    _need(_pos(pid) and str(pid) in (catalog.get("basecamp_people") or {}), "brand_routing_principal_missing")
    return pid


def route_problem(catalog, key):
    """None when the brand route is usable for a live task, else a reason code."""
    routes = catalog.get("brand_routes") or {}
    route = routes.get(key)
    if not isinstance(route, dict):
        return "brand_route_missing"
    if key in (catalog.get("brand_routes_excluded") or {}):
        return "brand_excluded"
    excluded_projects = {r.get("project_id") for r in (catalog.get("brand_routes_excluded") or {}).values()
                         if isinstance(r, dict)}
    internal_project = (catalog.get("basecamp") or {}).get("project_id")
    pid = route.get("project_id")
    if not _pos(pid) or pid in excluded_projects or pid == internal_project:
        return "brand_project_not_allowed"
    if sum(1 for r in routes.values() if isinstance(r, dict) and r.get("project_id") == pid) != 1:
        return "brand_project_ambiguous"
    if route.get("verified") is not True or not isinstance(route.get("verification_ref"), str):
        return "brand_route_unverified"
    lists = route.get("lists")
    if not (isinstance(lists, dict) and set(lists) <= set(LIST_KINDS) and _pos(lists.get("social"))
            and _pos(lists.get("project")) and all(v is None or _pos(v) for v in lists.values())
            and route.get("list_id") == lists["social"]):
        return "brand_list_unconfirmed"
    if type(route.get("clients_enabled")) is not bool:
        return "brand_clients_setting_unknown"
    if route.get("lists_visible_to_clients") is not False:
        return "brand_list_client_visibility_unconfirmed"  # incl. client-enabled projects (HAN Spaces, ya da)
    if route.get("hamle_member") is not True:
        return "hamle_not_member_of_brand_project"
    people = catalog.get("basecamp_people") or {}
    for field in ("sm_person_id", "rep_person_id"):
        if not _pos(route.get(field)) or str(route[field]) not in people:
            return "brand_" + field.replace("_person_id", "") + "_unknown"
    return None


def usable(catalog, key):
    return enabled(catalog) and route_problem(catalog, key) is None


def allowlist(catalog):
    """{project_id: {list_ids}}: internal route + every usable brand route (routing enabled only)."""
    try:
        project, route = _internal(catalog)
    except RoutingError:
        return {}  # no confirmed internal route: nothing is allowed (fail closed)
    out = {project: {route["list_id"]}}
    if enabled(catalog):
        for key, r in (catalog.get("brand_routes") or {}).items():
            if route_problem(catalog, key) is None:
                out[r["project_id"]] = {v for v in r["lists"].values() if _pos(v)}
    return out


def allowed_target(catalog, project_id, list_id):
    lists = allowlist(catalog).get(project_id)
    return lists is not None and list_id in lists


# ------------------------------------------------------------------ list choice
def list_bucket(deliverables):
    """Deterministic: social-media content -> social; motion -> motion; else -> project."""
    buckets = set()
    for d in deliverables or []:
        if not isinstance(d, dict):
            return "project"
        words = normalize(" ".join(str(d.get(k) or "") for k in ("id", "specification")))
        if any(_matches(t, words) for t in SOCIAL_TERMS):
            buckets.add("social")
        elif d.get("kind") in MOTION_KINDS:
            buckets.add("motion")
        else:
            buckets.add("project")
    return buckets.pop() if len(buckets) == 1 else "project"


def select_list(route, deliverables):
    lists = route["lists"]
    bucket = list_bucket(deliverables)
    return lists.get(bucket) or lists["project"]


# ------------------------------------------------------------------ derived per-work catalogs
def internal_catalog(catalog):
    cat = copy.deepcopy(catalog)
    for k in ROUTING_KEYS:
        cat.pop(k, None)
    return cat


def brand_catalog(catalog, key, list_id):
    """Single-project catalog for one usable brand route and one of its read-back lists."""
    _need(usable(catalog, key), "brand_route_not_usable")
    route = catalog["brand_routes"][key]
    _need(_pos(list_id) and list_id in route["lists"].values(), "brand_list_not_allowed")
    _, internal = _internal(catalog)
    ahmet, sm, rep = _ahmet(catalog), route["sm_person_id"], route["rep_person_id"]
    cat = internal_catalog(catalog)
    cat["version"] = f"{catalog['version']}+{key}"
    cat["routed_brand"] = {"brand_key": key, "brand_name": route["brand_name"],
                           "project_id": route["project_id"], "list_id": list_id,
                           "clients_enabled": route["clients_enabled"]}
    cat["basecamp"] = {"account_id": catalog["basecamp"]["account_id"], "project_id": route["project_id"],
                       "project_name": route["brand_name"], "customer_visible": False,
                       "clients_enabled": route["clients_enabled"], "verified": True,
                       "verification_ref": route["verification_ref"],
                       "hamle_person_id": catalog["basecamp"].get("hamle_person_id")}
    if _pos(route.get("todoset_id")):
        cat["basecamp"]["todoset_id"] = route["todoset_id"]
    r = copy.deepcopy(internal)
    r.pop("future_assignee_ids_by_kind", None)
    r.update(list_id=list_id, verified=True, verification_ref=route["verification_ref"],
             category_name="Hamle İşleri — " + route["brand_name"],
             list_name=next(k for k, v in route["lists"].items() if v == list_id),
             assignee_ids_by_kind={k: [sm] for k in r["allowed_kinds"]},
             producer_note="Brand route: the brand's social manager is assigned; rep + Ahmet subscribe.")
    cat["routes"] = [r]
    roles = {sm: {"social_manager"}, ahmet: {"principal"}}
    roles.setdefault(rep, set()).add("strategy_owner")
    for pid, person in cat["basecamp_people"].items():
        mine = roles.get(int(pid), set())
        person["agency_roles"] = sorted(mine)
        person["project_member"] = bool(mine)  # only SM/rep/Ahmet are bound to this brand project
        if int(pid) == sm:
            person["production_kinds"] = sorted(set(person.get("production_kinds") or []) | set(r["allowed_kinds"]))
        person["project_member_note"] = f"brand route {key}: project {route['project_id']} ({route['verification_ref']})"
    chain = cat.setdefault("chain", {})
    chain["quality_approver_basecamp_ids"] = sorted({sm, rep, ahmet})
    return cat


def work_catalog(catalog, work):
    """(brand_key | None, derived catalog) for a bound queue work; fail closed on drift."""
    project, route = _internal(catalog)
    if work.get("project_id") == project:
        _need(work.get("list_id") == route["list_id"], "work_list_not_allowed")
        return None, internal_catalog(catalog)
    keys = [k for k, r in (catalog.get("brand_routes") or {}).items()
            if isinstance(r, dict) and r.get("project_id") == work.get("project_id")]
    _need(len(keys) == 1, "work_project_not_in_catalog")
    _need(usable(catalog, keys[0]), "brand_route_no_longer_usable")
    return keys[0], brand_catalog(catalog, keys[0], work.get("list_id"))


def managers(cat):
    people = cat.get("basecamp_people", {})
    return sorted(int(k) for k, v in people.items() if set(v.get("agency_roles", [])) & set(MANAGER_ROLES))


def owner_person(catalog, key):
    """Brand owner to mention on an internal fallback task (SM, else rep), or None."""
    route = (catalog.get("brand_routes") or {}).get(key) or {}
    for field in ("sm_person_id", "rep_person_id"):
        if _pos(route.get(field)):
            return route[field]
    return None
