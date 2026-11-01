#!/usr/bin/env python3
"""Réconcilie les HTTPRoutes du Gateway `public` avec le reverse proxy Caddy de skillout.

Modèle ArgoCD : le côté cluster est du push pur (watch API server, aucun polling),
le côté OPNsense est rafraîchi périodiquement (RECONCILE_INTERVAL) car os-caddy
ne peut pas émettre de webhook — c'est l'équivalent du refresh interval d'ArgoCD,
qui couvre la dérive faite à la main dans la GUI Caddy.

Ownership : seules les entrées Caddy dont `description` commence par MARKER_PREFIX
sont modifiées ou supprimées. Une entrée existante de même domaine non marquée est
ADOPTÉE (marquée), jamais dupliquée : Caddy refuse de charger un Caddyfile avec
deux sites de même adresse.

Convention de route publique : parentRef -> Gateway GATEWAY_NAME/GATEWAY_NAMESPACE
avec sectionName `http` (ou sans sectionName). Une route attachée uniquement à un
listener https-* n'est pas joignable via l'upstream :80 et n'est donc pas synchronisée.
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
FINALIZER = "caddy.skillout.delta-net.ovh/httproute"

OPNSENSE_URL = os.environ.get("OPNSENSE_URL", "").rstrip("/")
API_KEY = os.environ.get("OPNSENSE_API_KEY", "")
API_SECRET = os.environ.get("OPNSENSE_API_SECRET", "")

_tls = ssl.create_default_context()
if os.environ.get("OPNSENSE_TLS_INSECURE", "1") == "1":
    _tls.check_hostname = False
    _tls.verify_mode = ssl.CERT_NONE

# Sérialise les écritures OPNsense + le reload : jamais deux reconfigure concurrents.
_lock = threading.Lock()

_custom = None


def _custom_api():
    """Client créé paresseusement : la config in-cluster n'est pas chargée à l'import."""
    global _custom
    if _custom is None:
        k8s_config.load_incluster_config()
        _custom = k8s.CustomObjectsApi()
    return _custom


# --- client API OPNsense ------------------------------------------------------

def _opnsense(method, path, body=None):
    # l'API OPNsense exige un JSON valide : un POST sans corps doit porter {}
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
    """searchBase pagine : on refait une requête si `rows` < `total`."""
    out = _opnsense("GET", path)
    rows = out.get("rows") or []
    total = int(out.get("total") or len(rows))
    if len(rows) < total:
        sep = "&" if "?" in path else "?"
        rows = (_opnsense("GET", f"{path}{sep}rowCount={total}").get("rows")) or []
    return rows


# --- modèle souhaité ----------------------------------------------------------

def _route_id(ns, name):
    return f"{ns}/{name}"


def _marker(route_id):
    return MARKER_PREFIX + route_id


def _is_managed(entry):
    return (entry.get("description") or "").startswith(MARKER_PREFIX)


def public_routes(items):
    """{hostname: route_id} pour les routes rattachées au listener http du Gateway public."""
    desired = {}
    for item in items:
        meta = item.get("metadata", {})
        if meta.get("deletionTimestamp"):
            continue  # en suppression : on ne la considère plus comme désirée
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
                    log.warning("hostname %s réclamé par %s et %s, on garde %s", h, prev, rid, rid)
                desired[h] = rid
            else:
                log.warning("hostname %s réclamé par %s et %s, on garde %s", h, prev, rid, prev)
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
    """Diff complet desired (cluster) vs live (Caddy), puis application. Idempotent."""
    items = _custom_api().list_cluster_custom_object(
        "gateway.networking.k8s.io", "v1", "httproutes").get("items") or []
    desired = public_routes(items)
    manages = {rid for rid in desired.values()}

    # Lifecycle : finalizer posé sur les routes gérées, retiré si la route sort du
    # périmètre (parentRef/hostname retiré) ou après nettoyage d'une suppression.
    for item in items:
        meta = item["metadata"]
        ns, name = meta["namespace"], meta["name"]
        fins = meta.get("finalizers") or []
        keep = (not meta.get("deletionTimestamp")) and _route_id(ns, name) in manages
        if not keep and FINALIZER in fins:
            log.info("route %s/%s : retrait du finalizer", ns, name)
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

    # 1. création / mise à jour / adoption
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
            # adopte une entrée manuelle de même domaine (jamais de doublon)
            log.info("[plan] adopt %s (%s) -> %s", hostname, existing["uuid"], rid)
            changes.append("adopt")
            if not PLAN_ONLY:
                _opnsense("POST", f"/api/caddy/reverse_proxy/setReverseProxy/{existing['uuid']}",
                          {"reverse": _reverse_fields(hostname, rid)})

        ours = [h for h in handles_by_reverse.get(existing["uuid"], [])
                if _handle_matches(h, existing["uuid"], rid)]
        if not ours:
            log.info("[plan] handle create %s", hostname)
            changes.append("handle")
            if not PLAN_ONLY:
                _opnsense("POST", "/api/caddy/reverse_proxy/addHandle",
                          {"handle": _handle_fields(existing["uuid"], rid)})

    # 2. suppression des entrées gérées devenues orphelines
    for rev in reverses:
        if not _is_managed(rev) or rev.get("FromDomain") in desired:
            continue
        log.info("[plan] delete %s (%s)", rev.get("FromDomain"),
                 (rev.get("description") or "")[len(MARKER_PREFIX):])
        changes.append("delete")
        if not PLAN_ONLY:
            # safeDelete : retirer les handles AVANT le reverse
            for h in handles_by_reverse.get(rev.get("uuid"), []):
                _opnsense("POST", f"/api/caddy/reverse_proxy/delHandle/{h['uuid']}")
            _opnsense("POST", f"/api/caddy/reverse_proxy/delReverseProxy/{rev['uuid']}")

    if changes and not PLAN_ONLY:
        out = _opnsense("POST", "/api/caddy/service/reconfigure")
        log.info("caddy reconfigure: %s", out)
    return changes


# --- points d'entrée kopf -----------------------------------------------------

@kopf.on.event("gateway.networking.k8s.io", "v1", "httproutes")
def on_route_event(**_):
    """Push : chaque événement watch déclenche un reconcile complet (modèle ArgoCD sync)."""
    with _lock:
        try:
            changes = reconcile()
        except Exception:
            log.exception("reconcile KO (le timer de dérive retentera)")
            return
    if changes:
        log.info("reconcile: %d changement(s) %s", len(changes), changes)


@kopf.timer("gateway.networking.k8s.io", "v1", "httproutes",
            interval=RECONCILE_INTERVAL, initial_delay=180)
def on_drift_refresh(**_):
    """Refresh interval : rattrape la dérive côté GUI Caddy et les échecs transitoires."""
    with _lock:
        try:
            reconcile()
        except Exception:
            log.exception("refresh de dérive KO")


if __name__ == "__main__":
    # NB : le warning "Not enough permissions to watch" au démarrage vient du scan
    # KopfPeering de kopf (403 sur les CRDs) — bénin, events + timer fonctionnent.
    kopf.run()
