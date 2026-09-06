# Kubernetes deployment

Manifests for running the Stockroom API and its Redis cache on any conformant
cluster (EKS, GKE, kind, minikube). The Terraform under `infra/terraform`
provisions the managed AWS pieces — RDS, Cognito, the S3 receipt bucket, ECR —
and these manifests run the workload itself.

## Layout

| File | Purpose |
|---|---|
| `00-namespace.yaml` | `stockroom` namespace |
| `01-configmap.yaml` | non-secret settings (CORS, cache TTL, AWS region) |
| `02-secret.example.yaml` | template for the database URL and Cognito ids — **do not commit real values** |
| `03-redis.yaml` | single-replica Redis with a `Deployment` + `ClusterIP` `Service` |
| `04-api.yaml` | API `Deployment` (2 replicas) + `Service` |
| `05-hpa.yaml` | `HorizontalPodAutoscaler`, 2–6 replicas on CPU |
| `06-ingress.yaml` | TLS ingress routing `/api` and `/health` to the API service |

## Apply

```bash
# 1. Create the real secret from your own values (never commit it)
kubectl create namespace stockroom
kubectl -n stockroom create secret generic stockroom-secrets \
  --from-literal=DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/stockroom' \
  --from-literal=COGNITO_USER_POOL_ID='us-east-1_xxxxxxxxx' \
  --from-literal=COGNITO_APP_CLIENT_ID='xxxxxxxxxxxxxxxxxxxxxxxxxx'

# 2. Apply everything else
kubectl apply -f infra/k8s/

# 3. Watch the rollout
kubectl -n stockroom rollout status deployment/stockroom-api
```

## Verifying the cache is live

`/health` reports which cache backend the pod actually bound to. With Redis
reachable it returns `"backend": "redis"`; if Redis is down the API stays up
and reports `"backend": "memory"` instead of failing readiness.

```bash
kubectl -n stockroom port-forward svc/stockroom-api 8000:80
curl -s localhost:8000/health | jq .cache
# { "backend": "redis", "ttl_seconds": 60, "hits": 0, "misses": 0, ... }
```

## Notes on the design

- **Redis is a cache, not a store.** It runs with `--maxmemory-policy allkeys-lru`
  and no persistence; losing it costs a recomputation, never data.
- **The API declares no `redis` readiness dependency** on purpose — the cache
  degrades to in-process memory rather than taking the service down.
- `readinessProbe` and `livenessProbe` both hit `/health`, which touches no
  database, so a slow query never restarts a healthy pod.
- Resource requests are deliberately small; raise them from real load data
  rather than guessing.
