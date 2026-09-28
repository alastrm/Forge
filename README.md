# Forge

> Self-hosted deployment platform control plane for a single server, built entirely with the Python standard library.

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()
[![Tests](https://img.shields.io/badge/tests-123%20passing-brightgreen.svg)]()

Forge is a minimalist single-node deployment control plane and PaaS. It provides a persistent state machine, asynchronous background deployments, continuous state reconciliation, zero-rebuild rollbacks, and an automated Traefik reverse proxy.

---

## Performance & Zero-Downtime Benchmarks

All performance claims and zero-downtime behaviors are measured empirically under sustained synthetic load rather than assumed. Benchmarks were conducted on a single host (Intel Core i5-13450HX, Docker Engine on Linux kernel, Traefik v3 reverse proxy).

### 1. High-Throughput Stress Profile (`autocannon`, 100 Connections)
Under continuous HTTP Keep-Alive saturation (100 concurrent connections, 45 seconds):
- **Sustained Throughput**: **~4,000 req/s** (average: **3,995.05 req/s**, peak: 4,947 req/s; **180,000 total requests** served).
- **Typical Latency**: **1–2 ms** (P50: 2 ms, P97.5: 14 ms, average: 24.2 ms).
- **Latency Tail under Parallel Deployment**: Only **~1 in 100 requests exceeded 1 second** (P99: 1,023 ms, Max: 8.8s). This tail latency occurred strictly during the momentary CPU and I/O spike of concurrent `docker build` and container initialization while sustaining 4k RPS.
- **Reliability**: **0 connection timeouts, 0 socket resets, 0 non-2xx responses (100.00% 2xx)**.

### 2. Blue-Green Rollout Race Condition & Empirical Fix
During concurrent load tests (~300 RPS, 10 worker threads, 83k requests), an ingress race condition was discovered in naive Blue-Green cutovers:
- **Before Fix**: **1 out of 7 rollouts dropped requests (HTTP 502 Bad Gateway)**. Because Traefik's dynamic file-provider has an inotify reload debounce (50–200 ms), stopping the old container immediately upon writing new configuration severed in-flight keep-alive sockets before the proxy routed to the new container.
- **Engineered Fix**:
  1. *Active Ingress Convergence*: The control plane issues synthetic probes directly through the ingress (`Host: <app>.localhost` on port 80) until the proxy confirms `200 OK` from the new upstream container.
  2. *Graceful Connection Draining*: A 2.0-second drain window keeps the old container serving established keep-alive sockets before issuing `SIGTERM`.
  3. *In-Flight Reconciler Protection*: A thread-safe in-flight registry guards transitional `STOPPING` containers against premature state failure by the background reconciler.
- **Verification Across 20 Consecutive Rollouts**:
  - Run suite: **20 consecutive live rollouts back-to-back** (`v14` ➔ `v34`) under uninterrupted traffic.
  - Total requests handled: **235,828**.
  - Dropped requests: **0 (0.00%)**.
  - All 20 previous containers cleanly transitioned to `STOPPED` in SQLite.

*For detailed test harnesses, methodology, and latency distributions, see [benchmarks/REPORT.md](benchmarks/REPORT.md).*

---

## Architecture

```text
┌──────────────┐       HTTP REST API       ┌────────────────────────────────────────────────────────┐
│  Forge CLI   │ ────────────────────────► │                      Forge Server                      │
└──────────────┘ (Bearer Auth / 127.0.0.1) │                                                        │
                                           │  ┌───────────────────────┐  ┌───────────────────────┐  │
                                           │  │   Background Queue    │  │   State Reconciler    │  │
                                           │  └──────────┬────────────┘  └──────────┬────────────┘  │
                                           │             │                          │               │
                                           │             ▼                          ▼               │
                                           │  ┌──────────────────────────────────────────────────┐  │
                                           │  │                Deployment Engine                 │  │
                                           │  │  (State Machine: PENDING ➔ BUILDING ➔ ACTIVE)   │  │
                                           │  └──────────┬──────────────────────────┬────────────┘  │
                                           │             │                          │               │
                                           │             ▼                          ▼               │
                                           │  ┌───────────────────────┐  ┌───────────────────────┐  │
                                           │  │ SQLite State Storage  │  │  Runtime Abstraction  │  │
                                           │  │ (WAL, Foreign Keys)   │  │    (Docker Engine)    │  │
                                           │  └───────────────────────┘  └──────────┬────────────┘  │
                                           └────────────────────────────────────────┼───────────────┘
                                                                                    │
                                             ┌──────────────────────────────────────┴───────────────┐
                                             │ Docker Engine & Traefik Routing                      │
                                             │                                                      │
                                             │  Incoming Traffic ──► [Traefik Proxy (:80)]          │
                                             │                              │                       │
                                             │          ┌───────────────────┴──────────────────┐    │
                                             │          ▼                                      ▼    │
                                             │ ┌──────────────────┐                  ┌──────────────────┐
                                             │ │  Candidate (v2)  │                  │   Active (v1)    │
                                             │ │  (Health Check)  │                  │(Serving Traffic) │
                                             │ └──────────────────┘                  └──────────────────┘
                                             └──────────────────────────────────────────────────────┘
```

---

## Core Capabilities

- **Zero External Dependencies**: Built 100% on the Python standard library (`sqlite3`, `http.server`, `urllib`, `threading`, `queue`, `subprocess`). No Celery, Redis, or heavy frameworks required.
- **Client-Server Architecture**: The CLI is a thin HTTP client. All lifecycle operations are coordinated by the `forge server` daemon.
- **Explicit Finite State Machine**: SQLite-backed deployment lifecycle with strict transitions: `PENDING` ➔ `BUILDING` ➔ `STARTING` ➔ `HEALTH_CHECKING` ➔ `ACTIVE` ➔ `STOPPING` ➔ `STOPPED` (or `FAILED` / `ROLLED_BACK`).
- **Asynchronous Job Queue**: API deployment requests return HTTP 202 immediately with a deployment ID while background workers execute image compilation and container management.
- **Continuous State Reconciliation**: The `Reconciler` loop compares SQLite desired state against actual Docker state:
  - Cleans up incomplete deployments and candidate containers after server restarts.
  - Detects dead or stopped active containers and records state drift events.
  - Removes orphaned containers with cooperative cancellation.
- **Zero-Rebuild Rollbacks**: Instant rollbacks using pre-recorded revision image tags without recompilation.
- **Live Log Streaming**: Real-time log streaming via HTTP (`forge logs <app> -f`) with zero-leak secret scrubbing on the fly.
- **In-Container Health Checks**: Validates health endpoints directly inside container network namespaces via `docker exec`, avoiding exposed host ports.
- **Automated Traefik Routing**: Manages an isolated bridge network (`forge-net`) and Traefik reverse proxy with dynamic labels.

---

## Baseline Security Hardening

> **Disclaimer**: Forge provides baseline configuration hardening and protections against operator error and trivial threats, but does **not** provide virtualization-level isolation or security guarantees. Deployed containers share the host Linux kernel.

### Threat Model & Boundaries
1. **Trusted Operator**: Host administrator running `forge server`.
2. **Deployed Application**: Untrusted application code inside containers.
3. **Remote API Client**: Interacts with the Control Plane via REST API.
4. **Docker Daemon**: Privileged root-equivalent service on the host.
5. **Traefik Ingress**: Routes incoming HTTP traffic based on labels.

### Hardening Controls
- **Zero-Leak Secret Handling**: Secrets are never passed via CLI arguments (`-e KEY=VAL`, which leaks in `ps aux` and shell history). Direct `KEY=VALUE` arguments in `forge env set` are strictly forbidden. Secrets must be entered interactively via secure prompt (`getpass`), loaded from a protected file (`--file`), or synced directly during `forge deploy`. Environment files on the host are written with `0600` permissions and immediately deleted after container creation.
- **Masked API Responses**: `GET /api/v1/applications/{id}` returns metadata and `is_set: true` indicators, never plaintext secrets. Events never store secret values.
- **Timing-Safe Authentication**: API Bearer tokens are validated using constant-time comparison (`hmac.compare_digest`).
- **Localhost Binding**: Forge Server strictly binds to `127.0.0.1`.
- **Docker Hardening Flags**: All containers run with `--cap-drop=ALL`, `--cap-add=NET_BIND_SERVICE`, `--security-opt=no-new-privileges:true`, `--pids-limit=150`, `--memory=512m`, and `--net=forge-net`. Mounting `/var/run/docker.sock` into application containers is blocked.
- **Deterministic Candidate Lifecycle**: Candidates start with `--restart=no` so failed startup does not cause restart loops before the control plane can inspect and clean them.

---

## Quickstart

### Prerequisites
- Python 3.10+
- Docker Engine (running)

### Installation
Clone the repository:
```bash
git clone https://github.com/alastrm/Forge.git
cd Forge
pip install -e .
```

### 1. Start the Forge Server
In a terminal, start the Forge control plane daemon:
```bash
forge server --port 8000
```

### 2. Deploy an Application
Deploy from inside your project directory (zero arguments) or pass a path:
```bash
cd ./test-app
forge deploy

# Or deploy from anywhere with overrides:
forge deploy ./test-app --app my-custom-app --port 8080 --env-file .env
```

Forge automatically:
- Resolves app name: `--app` ➔ `forge.json["app_name"]` ➔ normalized directory name.
- Sets local routing domain: `--domain` ➔ `forge.json["domain"]` ➔ `{app_name}.localhost`.
- Resolves container port: `--port` ➔ `forge.json["container_port"]` ➔ `EXPOSE` from `Dockerfile` ➔ `8000`.
- Injects environment variables: automatically syncs `.env` (or `--env-file`) with zero-leak secret scrubbing.
- Reports live progress, and prints container diagnostic output automatically if a build or startup fails.

The CLI submits the deployment asynchronously, streams status updates, and reports completion:
```text
Deploying application 'test-app' to Forge control plane...
Deployment enqueued: dep-14e9fbc7
Waiting for deployment to complete...
Status: BUILDING
Status: STARTING
Status: HEALTH_CHECKING
Status: ACTIVE

Deployment successful! Active container: test-app-a2bc81e
```

Your service is now available through Traefik at `http://test-app.localhost`.

### 3. CLI Commands
```bash
# Manage applications & deployment
forge deploy                 # Deploy current directory
forge deploy ./path/to/app   # Deploy specific path
forge app list
forge status my-api

# Manage environment variables & secrets (KEY=VALUE in CLI is strictly forbidden)
forge env set my-api API_KEY             # Interactive prompt without echo
forge env set my-api --file .env.secrets # Read from secure file
cat secrets.env | forge env set my-api   # Pipe from stdin

# Inspect deployments and logs
forge deployments my-api
forge logs my-api --tail 50
forge logs my-api -f         # Stream logs live in real-time

# Fast rollback to previous active version
forge rollback my-api

# Clean up dangling images to reclaim disk space
forge prune
```

---

## Configuration

Place a `forge.json` in your application root directory:
```json
{
  "app_name": "my-service",
  "domain": "my-service.localhost",
  "container_port": 8000,
  "health_check_path": "/health"
}
```

### Environment Variables
Variables defined in `.env` are securely injected into the container via temporary permission-isolated files:
```env
APP_ENV=production
DATABASE_URL=postgres://user:pass@db:5432/app
```

---

## Testing

Forge contains an architectural test suite of 105 tests covering state machine transitions, background queues, reconciler recovery, security validations, and real Docker integration:

```bash
# Run unit and integration tests
python -m unittest discover tests
```

---

## License

This project is licensed under the [MIT License](LICENSE).
