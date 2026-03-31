# Production Monitoring & Deployment Automation

This guide covers practical patterns for integrating waygate into the scripts and pipelines that keep your production systems healthy.

---

## Monitoring scripts

### Poll route health via the REST API

The `WaygateAdmin` REST API is JSON over HTTP, so any monitoring tool that can make an HTTP request can query it. No `waygate` CLI install needed on the monitoring host.

```bash
#!/usr/bin/env bash
# check-routes.sh — exit 1 if any route is unexpectedly disabled

WAYGATE_URL="${WAYGATE_SERVER_URL:-http://localhost:8000/waygate}"
TOKEN="${WAYGATE_TOKEN}"

routes=$(curl -sf \
  -H "X-Waygate-Token: $TOKEN" \
  "$WAYGATE_URL/api/routes")

if [ $? -ne 0 ]; then
  echo "ERROR: Could not reach WaygateAdmin at $WAYGATE_URL" >&2
  exit 1
fi

# Alert on any DISABLED route (adapt jq filter to your alert threshold)
disabled=$(echo "$routes" | jq -r '.[] | select(.status == "disabled") | .path')

if [ -n "$disabled" ]; then
  echo "ALERT: The following routes are disabled:"
  echo "$disabled"
  exit 1
fi

echo "OK: all routes nominal"
```

Run this from cron, Datadog, or any scheduler:

```cron
*/5 * * * * /opt/scripts/check-routes.sh >> /var/log/waygate-monitor.log 2>&1
```

---

### Python monitoring script

```python
#!/usr/bin/env python3
"""monitor_routes.py — check waygate route states and alert on anomalies."""

import os
import sys
import httpx

WAYGATE_URL = os.environ.get("WAYGATE_SERVER_URL", "http://localhost:8000/waygate")
TOKEN = os.environ["WAYGATE_TOKEN"]

ALERT_ON = {"disabled", "maintenance"}   # statuses that warrant an alert


def fetch_routes() -> list[dict]:
    resp = httpx.get(
        f"{WAYGATE_URL}/api/routes",
        headers={"X-Waygate-Token": TOKEN},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    try:
        routes = fetch_routes()
    except httpx.HTTPError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2   # unknown — monitoring system treats as warning

    alerts = [r for r in routes if r["status"] in ALERT_ON]

    if alerts:
        for r in alerts:
            print(f"ALERT  {r['status'].upper():<12} {r['path']}  reason={r.get('reason', '')!r}")
        return 1

    print(f"OK  {len(routes)} route(s) nominal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

### Webhook alerting (Slack / PagerDuty)

waygate fires webhooks on every state change — enable, disable, maintenance on/off. Webhook delivery always originates from the process that owns the engine where state mutations happen. Where you register them depends on your deployment mode.

#### Embedded mode (single service)

Register directly on the engine before mounting `WaygateAdmin`:

```python
from waygate import WaygateEngine
from waygate import SlackWebhookFormatter
from waygate.fastapi import WaygateAdmin

engine = WaygateEngine()
engine.add_webhook(
    url=os.environ["SLACK_WEBHOOK_URL"],
    formatter=SlackWebhookFormatter(),
)
engine.add_webhook(url=os.environ["PAGERDUTY_WEBHOOK_URL"])

admin = WaygateAdmin(engine=engine, auth=("admin", os.environ["WAYGATE_PASS"]))
app.mount("/waygate", admin)
```

#### Waygate Server mode (multi-service)

State mutations happen on the **Waygate Server**, not on SDK clients. Build the engine explicitly so you can call `add_webhook()` on it before passing it to `WaygateAdmin`:

```python
# waygate_server.py
import os
from waygate import WaygateEngine
from waygate import RedisBackend
from waygate import SlackWebhookFormatter
from waygate.fastapi import WaygateAdmin

engine = WaygateEngine(backend=RedisBackend(os.environ["REDIS_URL"]))
engine.add_webhook(
    url=os.environ["SLACK_WEBHOOK_URL"],
    formatter=SlackWebhookFormatter(),
)
engine.add_webhook(url=os.environ["PAGERDUTY_WEBHOOK_URL"])

waygate_app = WaygateAdmin(
    engine=engine,
    auth=("admin", os.environ["WAYGATE_PASS"]),
    secret_key=os.environ["WAYGATE_SECRET_KEY"],
)
```

!!! note
    SDK service apps (`WaygateSDK`) never fire webhooks. They only enforce state locally — all mutations and therefore all webhook triggers originate on the Waygate Server.

Webhook payload sent on every state change:

```json
{
  "event": "maintenance_on",
  "path": "GET:/payments",
  "reason": "DB migration",
  "timestamp": "2025-06-01T02:00:00Z",
  "state": { "path": "GET:/payments", "status": "maintenance", ... }
}
```

Webhook failures are non-blocking; they are logged and never affect the request path. On multi-node Waygate Server deployments (`RedisBackend`), Redis `SET NX` deduplication ensures only one node fires per event.

---

## Deployment automation

### Pre/post deploy maintenance pattern

The safest deployment pattern: enable maintenance before the deploy, run migrations, then re-enable routes.

```bash
#!/usr/bin/env bash
# deploy.sh
set -euo pipefail

WAYGATE_URL="${WAYGATE_SERVER_URL:-http://localhost:8000/waygate}"

waygate_cmd() {
  waygate --server-url "$WAYGATE_URL" "$@"
}

echo "==> Enabling global maintenance..."
waygate_cmd global enable \
  --reason "Deploying v$(cat VERSION) — back in ~5 minutes" \
  --exempt /health \
  --exempt GET:/readiness

echo "==> Running migrations..."
uv run alembic upgrade head

echo "==> Deploying new container..."
docker compose up -d --no-deps --build api

echo "==> Waiting for health check..."
until curl -sf http://localhost:8000/health; do sleep 2; done

echo "==> Disabling global maintenance..."
waygate_cmd global disable

echo "==> Deploy complete."
```

---

### Route-level rolling deploy

For zero-downtime deploys where only specific routes need to go offline:

```bash
#!/usr/bin/env bash
# rolling-deploy.sh
set -euo pipefail

waygate maintenance "POST:/orders" \
  --reason "Order service upgrade — ETA 10 minutes" \
  --start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --end "$(date -u -d '+10 minutes' +%Y-%m-%dT%H:%M:%SZ)"

# ... deploy only the orders service ...
docker compose up -d --no-deps --build orders

# Wait for readiness
until curl -sf http://localhost:8001/health; do sleep 2; done

waygate enable "POST:/orders"
echo "Orders service back online."
```

---

### GitHub Actions — deploy workflow

```yaml
# .github/workflows/deploy.yml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      WAYGATE_SERVER_URL: ${{ secrets.WAYGATE_SERVER_URL }}

    steps:
      - uses: actions/checkout@v4

      - name: Install waygate CLI
        run: pip install "waygate[cli]"

      - name: Authenticate with WaygateAdmin
        run: waygate login ${{ secrets.WAYGATE_USER }} --password ${{ secrets.WAYGATE_PASS }}

      - name: Enable global maintenance
        run: |
          waygate global enable \
            --reason "GitHub Actions deploy — commit ${{ github.sha }}" \
            --exempt /health

      - name: Run database migrations
        run: uv run alembic upgrade head

      - name: Deploy application
        run: |
          # your deploy command here
          kubectl set image deployment/api api=${{ env.IMAGE_TAG }}
          kubectl rollout status deployment/api --timeout=120s

      - name: Disable global maintenance
        if: always()   # run even if a previous step failed
        run: waygate global disable

      - name: Verify routes
        run: |
          waygate status
          # fail the workflow if any route is unexpectedly disabled
          waygate status | grep -qv DISABLED || exit 1
```

!!! tip "Always disable on failure"
    Use `if: always()` on the disable step so maintenance mode is lifted even when the deploy fails. Pair it with a Slack webhook so the team is notified immediately.

---

### Kubernetes — pre/post deploy hooks

Use Kubernetes `lifecycle` hooks to tie maintenance mode to pod lifecycle:

```yaml
# k8s/deployment.yaml
spec:
  template:
    spec:
      containers:
        - name: api
          lifecycle:
            preStop:
              exec:
                command:
                  - sh
                  - -c
                  - |
                    waygate --server-url $WAYGATE_SERVER_URL \
                      maintenance GET:/payments \
                      --reason "Pod shutting down (rolling update)"
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 5
```

And a post-deploy Job to re-enable:

```yaml
# k8s/post-deploy-job.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: waygate-enable-routes
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: waygate-cli
          image: python:3.13-slim
          command:
            - sh
            - -c
            - |
              pip install -q "waygate[cli]"
              waygate login $WAYGATE_USER --password $WAYGATE_PASS
              waygate enable GET:/payments
              waygate global disable
          env:
            - name: WAYGATE_SERVER_URL
              value: "http://api-svc/waygate"
            - name: WAYGATE_USER
              valueFrom:
                secretKeyRef: { name: waygate-creds, key: username }
            - name: WAYGATE_PASS
              valueFrom:
                secretKeyRef: { name: waygate-creds, key: password }
```

---

### Scheduled maintenance via cron + CLI

For recurring maintenance windows (nightly jobs, weekly DB vacuums):

```bash
# crontab — every Sunday 02:00–04:00 UTC
0 2 * * 0 waygate schedule GET:/reports \
  --start "$(date -u +\%Y-\%m-\%dT02:00:00Z)" \
  --end   "$(date -u +\%Y-\%m-\%dT04:00:00Z)" \
  --reason "Weekly report rebuild"
```

Or schedule programmatically from Python:

```python
import asyncio
from datetime import datetime, UTC, timedelta
from waygate import WaygateEngine
from waygate import MaintenanceWindow

async def schedule_nightly(engine: WaygateEngine) -> None:
    now = datetime.now(UTC)
    tonight = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if tonight < now:
        tonight += timedelta(days=1)

    window = MaintenanceWindow(
        start=tonight,
        end=tonight + timedelta(hours=2),
        reason="Nightly data pipeline",
    )
    await engine.schedule_maintenance("GET:/reports", window=window)
    print(f"Scheduled maintenance: {window.start} → {window.end}")
```

---

## Audit log in monitoring pipelines

Pull the audit log to detect unexpected state changes (e.g. a route disabled by an unknown actor):

```python
#!/usr/bin/env python3
"""audit-sentinel.py — alert on unexpected route state changes."""

import httpx, os, sys
from datetime import datetime, UTC, timedelta

WAYGATE_URL = os.environ.get("WAYGATE_SERVER_URL", "http://localhost:8000/waygate")
TOKEN = os.environ["WAYGATE_TOKEN"]
LOOKBACK = timedelta(minutes=15)

resp = httpx.get(
    f"{WAYGATE_URL}/api/audit?limit=50",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=10,
)
resp.raise_for_status()

cutoff = datetime.now(UTC) - LOOKBACK
unexpected = [
    e for e in resp.json()
    if datetime.fromisoformat(e["timestamp"]) > cutoff
    and e["actor"] not in {"system", "deploy-bot", "alice", "bob"}
]

if unexpected:
    for e in unexpected:
        print(f"UNKNOWN ACTOR  {e['actor']}  {e['action']}  {e['path']}  {e['timestamp']}")
    sys.exit(1)

print("OK")
```

---

## Environment variable reference

| Variable | Used by | Description |
|---|---|---|
| `WAYGATE_SERVER_URL` | CLI, monitoring scripts | Base URL of the `WaygateAdmin` mount point |
| `WAYGATE_TOKEN` | Monitoring scripts (direct API calls) | Bearer token from `waygate login` |
| `WAYGATE_BACKEND` | App server | Backend type: `memory`, `file`, `redis` |
| `WAYGATE_ENV` | App server | Current environment name (`dev`, `staging`, `production`) |
| `WAYGATE_REDIS_URL` | App server | Redis connection URL for `RedisBackend` |
| `WAYGATE_FILE_PATH` | App server | JSON file path for `FileBackend` |
