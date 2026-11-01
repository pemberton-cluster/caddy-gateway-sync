"""Tests for the caddy-gateway-sync operator.

Covers public_routes scoping/hostname conflicts and the reconcile diff loop
(stale handle cleanup, guarded adoption), with the OPNsense API and the
kubernetes client mocked. kopf and kubernetes are stubbed before importing
main.py: both are imported (and kopf decorators applied) at module load time.

Run with pytest (python -m pytest tests/) or unittest
(python -m unittest discover -s tests).
"""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

OPERATOR = Path(__file__).resolve().parents[1] / "charts" / "caddy-gateway-sync" / "operator"
sys.path.insert(0, str(OPERATOR))

# Deterministic import: main.py reads its configuration from the environment.
os.environ["GATEWAY_NAMESPACE"] = "gateway-system"
os.environ["GATEWAY_NAME"] = "public"
os.environ["LISTENER_HTTP"] = "http"
os.environ["CADDY_UPSTREAM_HOST"] = "10.10.16.38"
os.environ["CADDY_UPSTREAM_PORT"] = "80"
os.environ["OPNSENSE_TLS_INSECURE"] = "0"

# kopf and the kubernetes client may be absent on a dev machine: main.py only
# needs the decorators at import time, the API client is injected per test.
if "kopf" not in sys.modules:
    kopf = types.ModuleType("kopf")
    kopf.on = types.SimpleNamespace(
        event=lambda *a, **kw: (lambda fn: fn),
    )
    kopf.timer = lambda *a, **kw: (lambda fn: fn)
    sys.modules["kopf"] = kopf
if "kubernetes" not in sys.modules:
    client = types.ModuleType("kubernetes.client")
    client.CustomObjectsApi = object
    config = types.ModuleType("kubernetes.config")
    config.load_incluster_config = lambda: None
    kubernetes = types.ModuleType("kubernetes")
    kubernetes.client = client
    kubernetes.config = config
    sys.modules["kubernetes"] = kubernetes
    sys.modules["kubernetes.client"] = client
    sys.modules["kubernetes.config"] = config

import main  # noqa: E402


def route(ns, name, hostnames, section="http", deleted=False, annotations=None):
    meta = {"namespace": ns, "name": name}
    if deleted:
        meta["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    if annotations:
        meta["annotations"] = annotations
    ref = {"group": "gateway.networking.k8s.io", "kind": "Gateway",
           "name": "public", "namespace": "gateway-system"}
    if section is not None:
        ref["sectionName"] = section
    return {"metadata": meta, "spec": {"hostnames": hostnames, "parentRefs": [ref]}}


class _FakeApi:
    """Stand-in for CustomObjectsApi: lists fixed items, records patches."""

    def __init__(self, items):
        self.items = items
        self.patches = []

    def list_cluster_custom_object(self, *args):
        return {"items": self.items}

    def patch_namespaced_custom_object(self, *args):
        self.patches.append(args)


class PublicRoutesTest(unittest.TestCase):
    def test_hostname_conflict_keeps_lowest_route_id(self):
        items = [route("legacy", "web", ["app.example.com"]),
                 route("delta-net", "web", ["app.example.com"])]
        self.assertEqual(main.public_routes(items),
                         {"app.example.com": "delta-net/web"})

    def test_scope_filters(self):
        items = [
            route("default", "gone", ["del.example.com"], deleted=True),
            route("default", "tls", ["tls.example.com"], section="https"),
            route("default", "any", ["any.example.com"], section=None),
            route("default", "web", ["http.example.com"], section="http"),
        ]
        self.assertEqual(main.public_routes(items),
                         {"any.example.com": "default/any",
                          "http.example.com": "default/web"})


class ReconcileTest(unittest.TestCase):
    def _reconcile(self, items, reverses=(), handles=()):
        """Run reconcile() with the OPNsense API and k8s client mocked."""
        api = _FakeApi(items)
        calls = []

        def fake_opnsense(method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("addReverseProxy"):
                return {"uuid": "new-rev"}
            return {}

        def fake_search_all(path):
            return list(reverses) if "ReverseProxy" in path else list(handles)

        with mock.patch.object(main, "_custom_api", return_value=api), \
                mock.patch.object(main, "_opnsense", side_effect=fake_opnsense), \
                mock.patch.object(main, "_search_all", side_effect=fake_search_all):
            changes = main.reconcile()
        return changes, calls

    def test_stale_handle_deleted_before_new_one_added(self):
        rid = "default/web"
        rev = {"uuid": "rev1", "FromDomain": "app.example.com",
               "description": main._marker(rid)}
        stale = {"uuid": "h-old", "reverse": "rev1", "HandleType": "handle",
                 "HandlePath": "", "HandleDirective": "reverse_proxy",
                 "ToDomain": "10.0.0.1", "ToPort": "80"}
        items = [route("default", "web", ["app.example.com"])]

        changes, calls = self._reconcile(items, reverses=[rev], handles=[stale])

        self.assertIn("handle", changes)
        paths = [c[1] for c in calls]
        self.assertIn("/api/caddy/reverse_proxy/delHandle/h-old", paths)
        # the old backend must be gone before the new handle lands
        self.assertLess(paths.index("/api/caddy/reverse_proxy/delHandle/h-old"),
                        paths.index("/api/caddy/reverse_proxy/addHandle"))
        add = next(c for c in calls if c[1].endswith("addHandle"))
        self.assertEqual(add[2]["handle"]["ToDomain"], main.UPSTREAM_HOST)
        self.assertNotIn("/api/caddy/reverse_proxy/delReverseProxy/rev1", paths)
        self.assertIn("/api/caddy/service/reconfigure", paths)

    def test_matching_handle_kept_and_stale_sibling_removed(self):
        rid = "default/web"
        rev = {"uuid": "rev1", "FromDomain": "app.example.com",
               "description": main._marker(rid)}
        good = {"uuid": "h-ok", "reverse": "rev1", "HandleType": "handle",
                "HandlePath": "", "HandleDirective": "reverse_proxy",
                "ToDomain": main.UPSTREAM_HOST, "ToPort": main.UPSTREAM_PORT}
        stale = dict(good, uuid="h-old", ToDomain="10.0.0.1")
        items = [route("default", "web", ["app.example.com"])]

        changes, calls = self._reconcile(items, reverses=[rev], handles=[good, stale])

        self.assertEqual([c[1] for c in calls],
                         ["/api/caddy/reverse_proxy/delHandle/h-old",
                          "/api/caddy/service/reconfigure"])

    def test_manual_entry_requires_adopt_annotation(self):
        rev = {"uuid": "rev-manual", "FromDomain": "app.example.com",
               "description": "added by hand in the GUI"}

        # no annotation: the manual entry is left untouched
        changes, calls = self._reconcile(
            [route("default", "web", ["app.example.com"])], reverses=[rev])
        self.assertEqual(changes, [])
        self.assertEqual(calls, [])

        # explicit annotation: the entry is adopted (marked) and routed
        changes, calls = self._reconcile(
            [route("default", "web", ["app.example.com"],
                   annotations={main.ADOPT_ANNOTATION: "true"})], reverses=[rev])
        self.assertIn("adopt", changes)
        self.assertIn("handle", changes)
        self.assertIn("/api/caddy/reverse_proxy/setReverseProxy/rev-manual",
                      [c[1] for c in calls])
        self.assertIn("/api/caddy/reverse_proxy/addHandle", [c[1] for c in calls])


if __name__ == "__main__":
    unittest.main()
