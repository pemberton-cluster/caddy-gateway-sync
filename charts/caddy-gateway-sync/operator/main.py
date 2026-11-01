#!/usr/bin/env python3
"""Reconcile HTTPRoutes of a public Gateway with an OPNsense Caddy reverse proxy.

Sync model mirrors ArgoCD: the cluster side is pure push (watches the API server,
no polling), while the OPNsense side is refreshed periodically (RECONCILE_INTERVAL)
because os-caddy cannot emit webhooks — this is the equivalent of ArgoCD's refresh
interval, which catches drift made by hand in the Caddy GUI.

Ownership: only Caddy entries whose `description` starts with MARKER_PREFIX are
modified or deleted. An existing unmarked entry for the same hostname is ADOPTED
(marked), never duplicated: Caddy refuses to load a Caddyfile with two sites for
the same address. Adoption rewrites the entry and deletes it on cleanup, so it
requires explicit intent: the ADOPT_ANNOTATION annotation set to "true" on the
HTTPRoute (entries already owned by the operator are re-marked without it).

Public route convention: parentRef -> Gateway GATEWAY_NAME/GATEWAY_NAMESPACE with
sectionName `http` (or no sectionName). A route attached only to an https-* listener
is not reachable through the :80 upstream and is therefore not synced.
"""

import base64
import json
import logging
import os
import ssl
import threading
import urllib.request

import kopf
from kubernetes import client as k8s, config as k8s_config

log = logging.getLogger("caddy-gateway-sync")

GATEWAY_NAMESPACE = os.environ.get("GATEWAY_NAMESPACE", "gateway-system")
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "public")
LISTENER_HTTP = os.environ.get("LISTENER_HTTP", "http")
UPSTREAM_HOST = os.environ.get("CADDY_UPSTREAM_HOST", "10.10.16.38")
UPSTREAM_PORT = os.environ.get("CADDY_UPSTREAM_PORT", "80")
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "900"))
PLAN_ONLY = os.environ.get("PLAN_ONLY", "0") == "1"

MARKER_PREFIX = os.environ.get("MARKER_PREFIX", "[gitops] httproute:")
FINALIZER = os.environ.get("CADDY_FINALIZER", "caddy-gateway-sync/httproute")
# Adopting an unmarked Caddy entry requires this annotation = "true" on the
# claiming HTTPRoute: adoption rewrites the entry now and deletes it when the
# route goes away, so an unmarked entry must never be claimed silently.
ADOPT_ANNOTATION = os.environ.get("CADDY_ADOPT_ANNOTATION", "caddy-gateway-sync/adopt")

OPNSENSE_URL = os.environ.get("OPNSENSE_URL", "").rstrip("/")
API_KEY = os.environ.get("OPNSENSE_API_KEY", "")
API_SECRET = os.environ.get("OPNSENSE_API_SECRET", "")

# API credentials travel as Basic auth over this connection: verification stays
# on unless explicitly disabled. OPNSENSE_TLS_CA trusts a private CA;
# OPNSENSE_TLS_INSECURE=1 is an explicit opt-out.
_tls = ssl.create_default_context()
if os.environ.get("OPNSENSE_TLS_INSECURE", "0") == "1":
    _tls.check_hostname = False
    _tls.verify_mode = ssl.CERT_NONE
elif os.environ.get("OPNSENSE_TLS_CA"):
    _tls.load_verify_locations(os.environ["OPNSENSE_TLS_CA"])

# Serializes OPNsense writes + reload: never two concurrent reconfigures.
_lock = threading.Lock()

_custom = None


def _custom_api():
    """Lazily created client: in-cluster config is not loaded at import time."""
    global _custom
    if _custom is None:
        k8s_config.load_incluster_config()
        _custom = k8s.CustomObjectsApi()
    return _custom


# --- OPNsense API client ------------------------------------------------------

def _opnsense(method, path, body=None):
    # The OPNsense API requires valid JSON: a POST without a body must carry {}.
    if body is None and method == "POST":
        body = {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OPNSENSE_URL + path, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20, context=_tls) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _search_all(path):
    """searchBase paginates: query again if `rows` < `total`."""
    out = _opnsense("GET", path)
    rows = out.get("rows") or []
    total = int(out.get("total") or len(rows))
    if len(rows) < total:
        sep = "&" if "?" in path else "?"
        rows = (_opnsense("GET", f"{path}{sep}rowCount={total}").get("rows")) or []
    return rows


# --- desired model ------------------------------------------------------------

def _route_id(ns, name):
    return f"{ns}/{name}"


def _marker(route_id):
    return MARKER_PREFIX + route_id


def _is_managed(entry):
    return (entry.get("description") or "").startswith(MARKER_PREFIX)


def _adopt_allowed(item):
    return (item.get("metadata", {}).get("annotations") or {}).get(ADOPT_ANNOTATION) == "true"


def public_routes(items):
    """{hostname: route_id} for routes attached to the http listener of the public Gateway."""
    desired = {}
    for item in items:
        meta = item.get("metadata", {})
        if meta.get("deletionTimestamp"):
            continue  # being deleted: no longer desired
        ns = meta.get("namespace")
        rid = _route_id(ns, meta.get("name"))
        spec = item.get("spec", {})
        attached = False
        for p in spec.get("parentRefs") or []:
            if (p.get("group", "gateway.networking.k8s.io") == "gateway.networking.k8s.io"
                    and p.get("kind", "Gateway") == "Gateway"
                    and p.get("name") == GATEWAY_NAME
                    and (p.get("namespace") or ns) == GATEWAY_NAMESPACE
                    and p.get("sectionName") in (None, LISTENER_HTTP)):
                attached = True
        if not attached:
            continue
        for h in spec.get("hostnames") or []:
            prev = desired.get(h)
            if prev == rid:
                continue
            if prev is None or rid < prev:
                if prev is not None:
                    log.warning("hostname %s claimed by %s and %s, keeping %s", h, prev, rid, rid)
                desired[h] = rid
            else:
                log.warning("hostname %s claimed by %s and %s, keeping %s", h, prev, rid, prev)
    return desired


def _reverse_fields(hostname, rid):
    return {
        "enabled": "1",
        "FromDomain": hostname,
        "FromPort": "",
        "description": _marker(rid),
        "DnsChallenge": "1",
        "AccessLog": "0",
    }


def _handle_fields(reverse_uuid, rid):
    return {
        "enabled": "1",
        "reverse": reverse_uuid,
        "HandleType": "handle",
        "HandlePath": "",
        "HandleDirective": "reverse_proxy",
        "ToDomain": UPSTREAM_HOST,
        "ToPort": str(UPSTREAM_PORT),
        "description": _marker(rid),
    }


def _handle_matches(handle, reverse_uuid, rid):
    return (handle.get("reverse") == reverse_uuid
            and handle.get("HandleType") == "handle"
            and not handle.get("HandlePath")
            and handle.get("HandleDirective") == "reverse_proxy"
            and handle.get("ToDomain") == UPSTREAM_HOST
            and str(handle.get("ToPort") or "") == str(UPSTREAM_PORT))


# --- reconcile ----------------------------------------------------------------

def _patch_finalizers(ns, name, finalizers):
    _custom_api().patch_namespaced_custom_object(
        "gateway.networking.k8s.io", "v1", ns, "httproutes", name,
        {"metadata": {"finalizers": finalizers}})


def reconcile():
    """Full diff desired (cluster) vs live (Caddy), then apply. Idempotent."""
    items = _custom_api().list_cluster_custom_object(
        "gateway.networking.k8s.io", "v1", "httproutes").get("items") or []
    desired = public_routes(items)
    manages = {rid for rid in desired.values()}
    items_by_rid = {_route_id(i["metadata"]["namespace"], i["metadata"]["name"]): i for i in items}

    # Lifecycle: finalizer set on managed routes, removed when a route leaves the
    # scope (parentRef/hostname removed) or after cleanup of a deletion.
    for item in items:
        meta = item["metadata"]
        ns, name = meta["namespace"], meta["name"]
        fins = meta.get("finalizers") or []
        keep = (not meta.get("deletionTimestamp")) and _route_id(ns, name) in manages
        if not keep and FINALIZER in fins:
            log.info("route %s/%s: removing finalizer", ns, name)
            if not PLAN_ONLY:
                _patch_finalizers(ns, name, [f for f in fins if f != FINALIZER])
        elif keep and FINALIZER not in fins:
            if not PLAN_ONLY:
                _patch_finalizers(ns, name, fins + [FINALIZER])

    reverses = _search_all("/api/caddy/reverse_proxy/searchReverseProxy")
    handles = _search_all("/api/caddy/reverse_proxy/searchHandle")
    handles_by_reverse = {}
    for h in handles:
        handles_by_reverse.setdefault(h.get("reverse"), []).append(h)

    changes = []

    # 1. create / update / adopt
    for hostname, rid in sorted(desired.items()):
        existing = next((r for r in reverses if (r.get("FromDomain") or "") == hostname), None)
        if existing is None:
            log.info("[plan] create %s -> %s (%s:%s)", hostname, rid, UPSTREAM_HOST, UPSTREAM_PORT)
            changes.append("create")
            if not PLAN_ONLY:
                out = _opnsense("POST", "/api/caddy/reverse_proxy/addReverseProxy",
                                {"reverse": _reverse_fields(hostname, rid)})
                _opnsense("POST", "/api/caddy/reverse_proxy/addHandle",
                          {"handle": _handle_fields(out["uuid"], rid)})
            continue

        if existing.get("description") != _marker(rid):
            if not _is_managed(existing) and not _adopt_allowed(items_by_rid.get(rid)):
                # Claiming a manual entry rewrites it now and deletes it when the
                # route goes away: require explicit intent, leave it untouched.
                log.warning("[plan] manual entry %s not adopted, set %s=\"true\" on %s to adopt it",
                            hostname, ADOPT_ANNOTATION, rid)
                continue
            # adopt a manual entry for the same hostname (never a duplicate)
            log.info("[plan] adopt %s (%s) -> %s", hostname, existing["uuid"], rid)
            changes.append("adopt")
            if not PLAN_ONLY:
                _opnsense("POST", f"/api/caddy/reverse_proxy/setReverseProxy/{existing['uuid']}",
                          {"reverse": _reverse_fields(hostname, rid)})

        rev_handles = handles_by_reverse.get(existing["uuid"], [])
        # Handles that no longer match must go (upstream change, adopted entry
        # with its own handle): leaving one keeps the old backend routed
        # alongside the new one.
        for h in rev_handles:
            if not _handle_matches(h, existing["uuid"], rid):
                log.info("[plan] handle delete %s (%s)", hostname, h["uuid"])
                changes.append("handle")
                if not PLAN_ONLY:
                    _opnsense("POST", f"/api/caddy/reverse_proxy/delHandle/{h['uuid']}")
        ours = [h for h in rev_handles if _handle_matches(h, existing["uuid"], rid)]
        if not ours:
            log.info("[plan] handle create %s", hostname)
            changes.append("handle")
            if not PLAN_ONLY:
                _opnsense("POST", "/api/caddy/reverse_proxy/addHandle",
                          {"handle": _handle_fields(existing["uuid"], rid)})

    # 2. delete managed entries that became orphans
    for rev in reverses:
        if not _is_managed(rev) or rev.get("FromDomain") in desired:
            continue
        log.info("[plan] delete %s (%s)", rev.get("FromDomain"),
                 (rev.get("description") or "")[len(MARKER_PREFIX):])
        changes.append("delete")
        if not PLAN_ONLY:
            # safeDelete: remove handles BEFORE the reverse proxy
            for h in handles_by_reverse.get(rev.get("uuid"), []):
                _opnsense("POST", f"/api/caddy/reverse_proxy/delHandle/{h['uuid']}")
            _opnsense("POST", f"/api/caddy/reverse_proxy/delReverseProxy/{rev['uuid']}")

    if changes and not PLAN_ONLY:
        out = _opnsense("POST", "/api/caddy/service/reconfigure")
        log.info("caddy reconfigure: %s", out)
    return changes


# --- kopf entrypoints ---------------------------------------------------------

@kopf.on.event("gateway.networking.k8s.io", "v1", "httproutes")
def on_route_event(**_):
    """Push: every watch event triggers a full reconcile (ArgoCD sync model)."""
    with _lock:
        try:
            changes = reconcile()
        except Exception:
            log.exception("reconcile failed (the drift timer will retry)")
            return
    if changes:
        log.info("reconcile: %d change(s) %s", len(changes), changes)


@kopf.timer("gateway.networking.k8s.io", "v1", "httproutes",
            interval=RECONCILE_INTERVAL, initial_delay=180)
def on_drift_refresh(**_):
    """Refresh interval: catches drift made in the Caddy GUI and transient failures."""
    with _lock:
        try:
            reconcile()
        except Exception:
            log.exception("drift refresh failed")


if __name__ == "__main__":
    # Note: the "Not enough permissions to watch" warning at startup comes from
    # kopf's KopfPeering CRD scan (403) — harmless, events + timer work fine.
    kopf.run()
