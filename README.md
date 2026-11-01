# caddy-gateway-sync

Opérateur Kubernetes qui réconcilie les HTTPRoutes d'un Gateway public avec le
reverse proxy **Caddy** d'un OPNsense (plugin **os-caddy**).

Une HTTPRoute attachée au listener HTTP du Gateway → un reverse proxy Caddy
(`hostname`, TLS Let's Encrypt DNS-01) qui proxifie vers le VIP du Gateway.
Supprimer la route retire le routage Caddy (finalizer). Le DNS (CNAME vers
l'edge) est géré séparément par external-dns.

```
HTTPRoute (parentRef public/http + hostname)
  ├─ external-dns ────────► CNAME <hostname> -> <edge-host> (Cloudflare)
  └─ caddy-gateway-sync ──► API os-caddy ──► Caddy :
        reverse proxy  : <hostname>, TLS Let's Encrypt (DNS-01)
        handle         : reverse_proxy -> <gateway VIP>:80 (Host préservé)
```

## Modèle de synchronisation

- **Push** côté cluster : watch de l'API server (kopf), aucun polling.
- **Refresh interval** côté OPNsense (`RECONCILE_INTERVAL`, 900 s par défaut) :
  os-caddy ne peut pas émettre de webhook — c'est le seul moyen de détecter une
  dérive faite à la main dans la GUI Caddy.

## Garde-fous

- **Marqueur d'ownership** dans `description` : `[gitops] httproute:<ns>/<name>`
  (configurable via `MARKER_PREFIX`). Seules ces entrées sont modifiées/supprimées.
- **Adoption** : une entrée manuelle de même domaine existante est marquée, pas
  dupliquée (Caddy refuse un Caddyfile avec deux sites de même adresse).
- **Suppression** : `delHandle` puis `delReverseProxy` (safeDelete du plugin).
- **Un seul reload** (`/api/caddy/service/reconfigure`) par passage. Un reload
  qui échoue laisse Caddy sur l'ancienne config.
- Sans credentials, le pod reste en veille (`sleep infinity`) sans rien toucher.

## Configuration (env)

| Variable | Défaut | Rôle |
|---|---|---|
| `OPNSENSE_URL` | — | Base de l'API OPNsense (ex. `https://10.10.100.254:8443`) |
| `OPNSENSE_API_KEY` / `OPNSENSE_API_SECRET` | — | Clé API (privilège minimal : Services > Caddy, Reverse Proxy, Diagnostics) |
| `OPNSENSE_TLS_INSECURE` | `1` | `0` = vérifier le certificat TLS de l'API |
| `GATEWAY_NAMESPACE` / `GATEWAY_NAME` | `gateway-system` / `public` | Gateway à observer |
| `LISTENER_HTTP` | `http` | Listener HTTP du Gateway |
| `CADDY_UPSTREAM_HOST` / `CADDY_UPSTREAM_PORT` | `10.10.16.38` / `80` | VIP du Gateway, cible des reverse proxies |
| `RECONCILE_INTERVAL` | `900` | Refresh de dérive, en secondes |
| `MARKER_PREFIX` | `[gitops] httproute:` | Marqueur d'ownership dans `description` |
| `PLAN_ONLY` | `0` | `1` = log le diff sans rien écrire |

## Déploiement

1. Créer le namespace, user + clé API OPNsense à privilège minimal, règle pf
   (`pass in` src LAN → This Firewall, port API).
2. Sceller/poser le Secret :

   ```yaml
   apiVersion: v1
   kind: Secret
   metadata:
     name: caddy-gateway-sync
     namespace: gateway-system
   stringData:
     OPNSENSE_URL: https://<opnsense>:8443
     OPNSENSE_API_KEY: <key>
     OPNSENSE_API_SECRET: <secret>
   ```

   L'annotation `reloader.stakater.com/auto` redémarre le pod quand le Secret change
   (uniquement s'il existait déjà au déploiement — sinon `kubectl rollout restart`).
3. `kubectl apply -k .` (ou l'inclure dans ArgoCD comme source kustomize ; le
   Secret reste à sceller à part).

## Gotchas API OPNsense (os-caddy)

- **GET avec `Content-Type: application/json` sans corps = 400 "Invalid JSON syntax"**
  → ne poser le header que s'il y a un body.
- **POST sans corps = 400** → envoyer `{}`.
- Les endpoints vivent sous `/api/caddy/*` (pas `/api/frr/*` ni `/api/quagga/*`).
