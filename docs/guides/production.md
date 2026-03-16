# Production Monitoring & Deployment Automation

This guide covers practical patterns for integrating api-shield into the scripts and pipelines that keep your production systems healthy.

---

## Monitoring scripts

### Poll route health via the REST API

The `ShieldAdmin` REST API is JSON over HTTP, so any monitoring tool that can make an HTTP request can query it. No `shield` CLI install needed on the monitoring host.

```bash
#!/usr/bin/env bash
# check-routes.sh — exit 1 if any route is unexpectedly disabled

SHIELD_URL="${SHIELD_SERVER_URL:-http://localhost:8000/shield}"
TOKEN="${SHIELD_TOKEN}"

routes=$(curl -sf \
  -H "X-Shield-Token: $TOKEN" \
  "$SHIELD_URL/api/routes")

if [ $? -ne 0 ]; then
  echo "ERROR: Could not reach ShieldAdmin at $SHIELD_URL" >&2
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
*/5 * * * * /opt/scripts/check-routes.sh >> /var/log/shield-monitor.log 2>&1
```

---

### Python monitoring script

```python
#!/usr/bin/env python3
"""monitor_routes.py — check api-shield route states and alert on anomalies."""

import os
import sys
import httpx

SHIELD_URL = os.environ.get("SHIELD_SERVER_URL", "http://localhost:8000/shield")
TOKEN = os.environ["SHIELD_TOKEN"]

ALERT_ON = {"disabled", "maintenance"}   # statuses that warrant an alert


def fetch_routes() -> list[dict]:
    resp = httpx.get(
        f"{SHIELD_URL}/api/routes",
        headers={"X-Shield-Token": TOKEN},
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

api-shield fires webhooks on every state change. Wire a Slack webhook to get instant alerts without polling:

```python
from shield.core.engine import ShieldEngine
from shield.core.webhooks import SlackWebhookFormatter

engine = ShieldEngine()
engine.add_webhook(
    url=os.environ["SLACK_WEBHOOK_URL"],
    formatter=SlackWebhookFormatter(),
)

# Or a generic JSON endpoint (e.g. PagerDuty Events API v2)
engine.add_webhook(url=os.environ["PAGERDUTY_WEBHOOK_URL"])
```

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

Webhook failures are non-blocking; they are logged and never affect the request path.

---

## Deployment automation

### Pre/post deploy maintenance pattern

The safest deployment pattern: enable maintenance before the deploy, run migrations, then re-enable routes.

```bash
#!/usr/bin/env bash
# deploy.sh
set -euo pipefail

SHIELD_URL="${SHIELD_SERVER_URL:-http://localhost:8000/shield}"

shield_cmd() {
  shield --server-url "$SHIELD_URL" "$@"
}

echo "==> Enabling global maintenance..."
shield_cmd global enable \
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
shield_cmd global disable

echo "==> Deploy complete."
```

---

### Route-level rolling deploy

For zero-downtime deploys where only specific routes need to go offline:

```bash
#!/usr/bin/env bash
# rolling-deploy.sh
set -euo pipefail

shield maintenance "POST:/orders" \
  --reason "Order service upgrade — ETA 10 minutes" \
  --start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --end "$(date -u -d '+10 minutes' +%Y-%m-%dT%H:%M:%SZ)"

# ... deploy only the orders service ...
docker compose up -d --no-deps --build orders

# Wait for readiness
until curl -sf http://localhost:8001/health; do sleep 2; done

shield enable "POST:/orders"
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
      SHIELD_SERVER_URL: ${{ secrets.SHIELD_SERVER_URL }}

    steps:
      - uses: actions/checkout@v4

      - name: Install shield CLI
        run: pip install "api-shield[cli]"

      - name: Authenticate with ShieldAdmin
        run: shield login ${{ secrets.SHIELD_USER }} --password ${{ secrets.SHIELD_PASS }}

      - name: Enable global maintenance
        run: |
          shield global enable \
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
        run: shield global disable

      - name: Verify routes
        run: |
          shield status
          # fail the workflow if any route is unexpectedly disabled
          shield status | grep -qv DISABLED || exit 1
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
                    shield --server-url $SHIELD_SERVER_URL \
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
  name: shield-enable-routes
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: shield-cli
          image: python:3.13-slim
          command:
            - sh
            - -c
            - |
              pip install -q "api-shield[cli]"
              shield login $SHIELD_USER --password $SHIELD_PASS
              shield enable GET:/payments
              shield global disable
          env:
            - name: SHIELD_SERVER_URL
              value: "http://api-svc/shield"
            - name: SHIELD_USER
              valueFrom:
                secretKeyRef: { name: shield-creds, key: username }
            - name: SHIELD_PASS
              valueFrom:
                secretKeyRef: { name: shield-creds, key: password }
```

---

### Scheduled maintenance via cron + CLI

For recurring maintenance windows (nightly jobs, weekly DB vacuums):

```bash
# crontab — every Sunday 02:00–04:00 UTC
0 2 * * 0 shield schedule GET:/reports \
  --start "$(date -u +\%Y-\%m-\%dT02:00:00Z)" \
  --end   "$(date -u +\%Y-\%m-\%dT04:00:00Z)" \
  --reason "Weekly report rebuild"
```

Or schedule programmatically from Python:

```python
import asyncio
from datetime import datetime, UTC, timedelta
from shield.core.engine import ShieldEngine
from shield.core.models import MaintenanceWindow

async def schedule_nightly(engine: ShieldEngine) -> None:
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

SHIELD_URL = os.environ.get("SHIELD_SERVER_URL", "http://localhost:8000/shield")
TOKEN = os.environ["SHIELD_TOKEN"]
LOOKBACK = timedelta(minutes=15)

resp = httpx.get(
    f"{SHIELD_URL}/api/audit?limit=50",
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
| `SHIELD_SERVER_URL` | CLI, monitoring scripts | Base URL of the `ShieldAdmin` mount point |
| `SHIELD_TOKEN` | Monitoring scripts (direct API calls) | Bearer token from `shield login` |
| `SHIELD_BACKEND` | App server | Backend type: `memory`, `file`, `redis` |
| `SHIELD_ENV` | App server | Current environment name (`dev`, `staging`, `production`) |
| `SHIELD_REDIS_URL` | App server | Redis connection URL for `RedisBackend` |
| `SHIELD_FILE_PATH` | App server | JSON file path for `FileBackend` |
