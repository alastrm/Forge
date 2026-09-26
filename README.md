# Forge

> Self-hosted deployment platform control plane for a single server, built entirely with the Python standard library.

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()
[![Tests](https://img.shields.io/badge/tests-80%20passing-brightgreen.svg)]()

Forge is a minimalist single-node deployment control plane and PaaS. It provides a persistent state machine, asynchronous background deployments, continuous state reconciliation, zero-rebuild rollbacks, and an automated Traefik reverse proxy.

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
- **Zero-Leak Secret Handling**: Secrets are never passed via CLI arguments (`-e KEY=VAL`, which leaks in `ps aux`). Environment files are written with `0600` permissions and immediately deleted after container creation.
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
git clone https://github.com/madishkin/Forge.git
cd Forge
pip install -e .
```

### 1. Start the Forge Server
In a terminal, start the Forge control plane daemon:
```bash
forge server --port 8000
```

### 2. Deploy an Application
In another terminal, deploy an application containing a `Dockerfile`:
```bash
forge deploy ./test-app
```

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
# Manage applications
forge app create my-api --domain my-api.localhost --port 8000
forge app list
forge status my-api

# Inspect deployments and logs
forge deployments my-api
forge logs my-api --tail 50

# Fast rollback to previous active version
forge rollback my-api
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

Forge contains an architectural test suite of 80 tests covering state machine transitions, background queues, reconciler recovery, security validations, and real Docker integration:

```bash
# Run unit and integration tests
python -m unittest discover tests
```

---

## License

This project is licensed under the [MIT License](LICENSE).
