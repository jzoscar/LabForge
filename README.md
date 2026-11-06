# LabForge

An infrastructure-as-code platform for orchestrating and monitoring the hardware behind
**dev / test / R&D / pre-production** environments. One Python project provides:

- **IaC engine**: declare environments in YAML → `plan` computes the diff → `apply` converges the real
  infrastructure (a Terraform-style state + plan model)
- **Backend**: FastAPI REST API (`/api/v1`, OpenAPI docs at `/docs`) with a background monitoring scheduler
- **Frontend**: a server-rendered web console (overview, resources, IaC workspace with an online editor, run history); no Node build
- **Agent**: a single-file collector that only needs `psutil`, so it can be copied straight onto
  **ARM Linux boards** and **Windows** test machines

## Positioning: the hardware lab behind the build pipeline

LabForge is aimed at build and test teams for embedded and industrial sensor products. Their code is
built with cross-compiled ARM GCC and with MSVC, and it ends up running on real devices: **a bad release
can stop a customer's production line**.

The build itself (compiling, caching, logs, automated triage, SBOMs) is handled by the sister project
[BuildPulse](https://github.com/jzoscar/BuildPulse). LabForge manages what sits behind it:

- **Device test rigs**: a pool of ARM sensor devices that CI checks out and locks before use, and that
  are restored to a known-good image afterwards
- **Build farm**: Linux Docker build hosts, Windows MSVC build machines, Bamboo remote agents, and
  Bitbucket Pipelines / GitHub Actions self-hosted runners
- **Shared infrastructure**: Conan remote (Artifactory), sccache remote cache, artifact storage, lab
  networks, load balancers

All of it is declared as code, converged with one command, and monitored continuously. The goal: when a
pipeline fails, know immediately **whether the code broke or the lab broke.**

### What a build & test team needs, and where LabForge covers it

| Need | Where LabForge covers it |
|---|---|
| Products run on real hardware; a bad release stops a production line | M2 device test rigs validate every release on real devices; M6 canary rollout and automatic rollback |
| ARM cross-compilation plus Windows MSVC | Device pools (ARM Linux) and build machines (Windows / Linux) are modelled per platform in IaC; the agent detects architecture drift |
| Docker build environments | Build hosts and cache services are managed as containers through IaC |
| Turn 40-minute builds into a coffee break | M4 build farm capacity and remote cache services, with queue time and cache hit rate made visible |
| "Fix, don't just file" | Failures are classified as infrastructure vs. code; device faults are quarantined and self-healed, code faults go to BuildPulse triage |
| Bamboo → Bitbucket Pipelines migration | Runners and agents are labelled by the hardware they reach; both CI systems borrow devices from LabForge through the same API |
| EU Cyber Resilience Act (CRA) | M5 firmware inventory per device, so a new CVE maps straight to affected devices and versions |
| Using AI in real work with production-grade output | Failure context (device ID, firmware, health checks, logs) is emitted as structured data an AI agent can triage directly |

## What it manages

| Resource | IaC section | Orchestration (driver) | Monitoring |
|---|---|---|---|
| Networks / VPC / VLAN / physical segments | `networks` | `record` (inventory) · `terraform` (delegate to an existing module) | Ping each subnet gateway |
| Load balancers | `loadBalancers` | `record` · `haproxy` (render config, push over SSH, validate, reload) · `terraform` | VIP listener ports plus a health check on every backend |
| Physical hardware (bare metal, ARM / x86) | `hosts` (`kind: baremetal`) | BMC power control (Redfish / IPMI) plus SSH / WinRM bootstrap commands | ping/tcp/http probes, BMC health/power/temperature, agent metrics |
| Windows machines (including Windows on ARM) | `hosts` (`platform.os: windows`) | PowerShell over WinRM | Same as above (WinRM/RDP ports, agent) |
| Linux servers / VMs / cloud hosts | `hosts` (`kind: vm/cloud`) | SSH bootstrap commands | Same as above |
| Containers | `containers` | Docker (local, or remote via `docker -H ssh://`) | `docker inspect` running state |

Additional monitoring:

- **Drift detection**: when the OS or CPU architecture reported by the agent differs from the IaC
  declaration (for example an ARM board re-imaged with an x86 image), the host is marked degraded
- **Thresholds**: CPU > 95%, memory > 95%, disk > 90%, temperature > 85 °C
- **Lost agents**: no report within `LABFORGE_AGENT_STALE_AFTER` seconds counts as a failure
- **Status events**: every transition between up / degraded / down / unknown is recorded

## Quick start (local demo, no real hardware needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # add the windows extra for WinRM targets: pip install -e '.[dev,windows]'
```

```bash
export LABFORGE_IAC_PATH=examples/local
labforge plan
labforge apply -y                 # the demo only records inventory; it touches no machines
labforge serve                    # open http://127.0.0.1:8000
```

In a second terminal, let this machine report as a piece of "test hardware":

```bash
labforge agent --env local --name this-mac
```

## Simulated lab (no hardware needed)

`labforge sim run` starts three simulated ARM sensors on this machine. Each has a device HTTP API
(`/version`, `/health`, firmware update) and a Redfish BMC (power state, reset, temperature), and faults
can be injected to reproduce what goes wrong on real rigs:

| Fault | What it models | What fixes it |
|---|---|---|
| `crash` | Firmware is dead, hardware is fine | A power cycle through the BMC |
| `hang` | Device stops answering in time | A power cycle through the BMC |
| `overheat` | Hardware problem (BMC reports Warning, 93 °C) | A person: it survives reboots |

```bash
labforge sim run                                   # terminal 1
export LABFORGE_SECRET_SIM_BMC=admin:admin
labforge apply infra/test/sim-lab.yaml -y          # real apply: power is checked over Redfish
labforge sim fault sim-sensor-02 overheat          # inject a fault, watch the overview / Rigs page
labforge sim status
```

## Device test rigs

Hosts labelled `pool: <name>` form a rig pool that CI borrows devices from (the **Rigs** page shows
every pool, who holds what, and why a device is quarantined).

- **Checkout** health-checks a device *before* handing it out, using the same probes as monitoring. If it
  fails, LabForge power-cycles it through the BMC and checks again ("fix, don't just file"); only a
  healthy device is handed out, otherwise the next one is tried
- **Two consecutive failed health checks quarantine** a device until a person returns it to the pool.
  These are infrastructure failures, never test failures; a job can also report `infra-failed`
- **Release** puts the device back to a known state, whatever the test result: its known-good firmware
  (`firmware.knownGood`) is flashed back if the job left another build on it, then it is power-cycled
- **Hard reset** goes through the BMC (Redfish / IPMI) or, for boards without one, the switched PDU
  outlet that feeds it (`pdu:`, HTTP or APC SNMP)
- **Flashing** follows each device's `firmware.method`: the device's firmware endpoint, or a command
  run on the LabForge server (vendor update tool, PXE re-image, serial flasher)
- **Queueing**: `checkout --wait 600` waits in line for a free device; wait times, results and
  per-device utilisation show up as 7-day trends on the Rigs page (`GET /api/v1/rigs/trends`)
- **Leases expire**, so a crashed job cannot hold a device forever
- Checkouts are serialized, so two jobs never get the same device

```bash
labforge rig list
labforge rig checkout --pool sim-sensor --holder "$CI_JOB_URL" --output env   # exit code 2: none available
labforge rig flash <lease-id> --version 1.5.0-g1a2b3c --artifact build/pulse   # install, wait until healthy
labforge rig check <lease-id> --expect-version 1.5.0-g1a2b3c --artifact build/pulse  # runs exactly this build?
labforge rig release <lease-id> --result passed|failed|infra-failed
```

`flash` sends the build's version and sha256 through LabForge, which keeps the device and BMC
credentials, so CI never needs them. [BuildPulse](https://github.com/jzoscar/BuildPulse)'s
`buildpulse device-test` wraps checkout → flash → verify → test → release as one recorded run, and its
triage reports a failed checkout or flash as `infrastructure`, not as a test failure.

REST: `GET /api/v1/rigs`, `POST /api/v1/rigs/checkout`, `POST /api/v1/rigs/leases/{id}/flash`,
`GET /api/v1/rigs/leases/{id}/device`, `POST /api/v1/rigs/leases/{id}/release`,
`POST /api/v1/rigs/leases/{id}/renew`, `GET /api/v1/rigs/leases`, `POST /api/v1/rigs/devices/{address}/unquarantine`.

A GitHub Actions job on a self-hosted runner in the lab (ready-made equivalents for
[Bitbucket Pipelines](deploy/ci/bitbucket-pipelines.yml) and [Bamboo specs](deploy/ci/bamboo-specs.yaml)
are in `deploy/ci/`):

```yaml
on-device-tests:
  runs-on: [self-hosted, linux, rig-sensor-a]
  needs: cross-arm64
  env:
    LABFORGE_SERVER: http://labforge.lab:8000
    LABFORGE_API_TOKEN: ${{ secrets.LABFORGE_API_TOKEN }}
  steps:
    - uses: actions/download-artifact@v4
      with: { name: firmware-arm64 }
    - name: Borrow a device
      run: labforge rig checkout --pool sensor-a --holder "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" --output env >> "$GITHUB_ENV"
    - name: Test on the device
      id: test
      run: ./run-device-tests --device "$LABFORGE_DEVICE_HTTP"
    - name: Return the device
      if: always()
      run: labforge rig release "$LABFORGE_LEASE_ID" --result ${{ steps.test.outcome == 'success' && 'passed' || 'failed' }}
```

## Alerting

An `AlertPolicy` document in the IaC workspace ([infra/alerting.yaml](infra/alerting.yaml)) declares
notification channels and routes. Alerts come from monitoring (resource down / degraded), quarantined
rig devices, and rig pools running short of usable devices.

- **Channels**: Slack (with Acknowledge / Silence 1h / Back to pool buttons), Microsoft Teams,
  WhatsApp (Meta Cloud API or Twilio), SMS (Twilio), PagerDuty, Opsgenie, e-mail and generic webhooks.
  Webhook URLs and tokens are secret refs, like everything else
- **Routes** match on env, tier, kind, severity and labels; first match wins unless `continue: true`
- One alert per condition, **one message per channel per sweep** (no alert storms), repeated after
  `repeatAfterMinutes` until acknowledged, a **recovery notice** when it clears, PagerDuty / Opsgenie
  alerts deduplicated and resolved by key
- **Silences** (glob on the subject, e.g. `staging.*`) mute notifications without hiding alerts
- The **Alerts** page shows what is firing, channels (with a *Send test* button), routes, silences,
  what was sent, and 7-day availability per resource (`GET /api/v1/reports/availability`)
- Slack buttons: create a Slack app, set its interactivity URL to
  `<LABFORGE_PUBLIC_URL>/api/v1/alerts/slack/actions` and `LABFORGE_SLACK_SIGNING_SECRET` to its
  signing secret; requests are verified before anything changes

**Prometheus / Grafana**: `GET /metrics` exposes resource status, agent metrics, probe results and
latency, firing alerts, rig devices and lease results. Scrape config and a dashboard are in
[deploy/grafana/](deploy/grafana/).

## Build farm speed

BuildPulse writes a `report.json` per run (stages, timings, cache state). Send them to LabForge after
each build:

```bash
labforge builds ingest .buildpulse/runs --runner "$RUNNER_NAME" [--queued <seconds waited>]
labforge builds stats
```

The **Builds** page (and `GET /api/v1/builds/stats`) shows, per pipeline: success rate, median and p90
duration with a trend, where the time goes per stage, compiler cache hit rate, cold vs warm dependency
time, and queue time; plus **suggestions derived from those numbers**, for example
*"Dependencies take 23s cold vs 2.7s warm (9x). Share a Conan remote so every runner starts warm."*

Build agents scale as code: a container with `replicas: N` runs `<name>-1 … <name>-N`; change N and
apply, and LabForge starts the new agents or removes the extra ones (see the build-farm starter).

## Security and access

- **API tokens**: `labforge token create ci-rigs --role operator --envs "test-*,sim-lab"` prints a token
  once (only its hash is stored); `token list`, `token revoke`. Roles: viewer < operator (rigs, alerts,
  dry-runs, build ingest) < editor (IaC files) < admin (real applies, tokens). A token can only change
  the environments it covers. `LABFORGE_API_TOKEN` is the master (admin) token. With no tokens at all
  the API is open, for development
- **Agent tokens** are bound to one host: `labforge token create arm-1 --role agent --subject dev.host.arm-node-01`
- **Web UI and SSO**: put LabForge behind an SSO proxy (oauth2-proxy, Authentik… in front of your
  OIDC or LDAP) and set `LABFORGE_AUTH_HEADER` (e.g. `X-Forwarded-Email`) and `LABFORGE_GROUPS_HEADER`.
  An `AccessPolicy` document in the IaC workspace maps users and groups to roles and environments:

  ```yaml
  apiVersion: labforge/v1
  kind: AccessPolicy
  metadata: { name: lab-access }
  spec:
    bindings:
      - { subjects: ["group:lab-admins"], role: admin }
      - { subjects: ["group:firmware"], role: operator, envs: ["dev", "test-*", "sim-lab"] }
  ```
- **Audit log** (`/audit`, `GET /api/v1/audit`): applies, IaC edits, rig operations, alert handling, tokens
- **Secrets**: `LABFORGE_SECRET_<REF>` first, then HashiCorp Vault when `VAULT_ADDR` / `VAULT_TOKEN` are set
  (KV v2 at `secret/data/labforge/<ref>`, field `value`)

## CRA traceability

Every flash and every monitoring read of a device's version endpoint records which firmware runs where.
Upload each release's SBOM (BuildPulse's `scan` writes CycloneDX and SPDX):

```bash
labforge cra sbom 1.5.0 .buildpulse/runs/<scan-run>/sbom.cdx.json
labforge cra affected zlib --below 1.3.1              # exit 1 when devices are affected
labforge cra affected zlib --below 1.3.1 --markdown   # report for the 24-hour early warning
```

The **Compliance** page shows the inventory, SBOM coverage, and the impact of a component version
range, including devices whose firmware has no SBOM (they cannot be ruled out).

## GitOps

```bash
labforge plan infra --remote https://labforge.lab --format markdown   # PR comment: plan against real state
labforge sync infra --remote https://labforge.lab --prune --apply      # on merge: mirror git, apply, wait
```

[deploy/gitops/iac-pipeline.yml](deploy/gitops/iac-pipeline.yml) wires both into GitHub Actions on a
self-hosted runner in the lab. Several LabForge instances can share one PostgreSQL database
(`pip install 'labforge[postgres]'`, `LABFORGE_DB_URL=postgresql+psycopg://…`): one is elected to run
monitoring and alerting, and applies and rig checkouts are serialized across them.

## Managing your lab

`infra/` declares a complete set of R&D environments, one folder per tier. Each environment's size
matches its purpose, and its `metadata.labels.scope` says what it is for (the web UI groups and filters
by tier and scope):

| Environment | File | Tier | Scope | What it is |
|---|---|---|---|---|
| `dev` | [infra/dev/dev.yaml](infra/dev/dev.yaml) | dev | module | One module on a developer's bench: a single ARM sensor board and a small server running the gateway service |
| `test-unit` | [infra/test/test-unit.yaml](infra/test/test-unit.yaml) | test | single | Device rig for module tests in CI: three ARM sensors with BMC power control and a rig controller (self-hosted runner) |
| `test-integration` | [infra/test/test-integration.yaml](infra/test/test-integration.yaml) | test | integration | The whole chain end to end: sensors, gateway + database behind HAProxy, Windows clients on x64 and ARM64 |
| `staging` | [infra/staging/staging.yaml](infra/staging/staging.yaml) | staging | pre-production | Mirrors production at smaller scale: VLAN per role, two gateway nodes behind an HAProxy edge, dedicated database host, sensor pool, Windows clients |
| `demo` | [infra/demo/demo.yaml](infra/demo/demo.yaml) | demo | showcase | A simplified production for customer demos: one cloud host runs every service; VPC and public load balancer come from Terraform modules; one sensor ships with the demo kit |

Addresses, hosts and images are placeholders. After filling in your own addresses, BMCs and accounts:

```bash
labforge validate infra           # schema + cross-reference checks (subnets inside the CIDR, LB targets exist, ...)
labforge plan infra               # show the diff only
labforge apply infra --dry-run    # print every ssh / ipmitool / docker / terraform command that would run
labforge apply infra              # apply for real (asks for confirmation)
labforge state                    # list every resource and its health
labforge destroy --env demo       # tear down one environment (physical machines are deregistered, never wiped)
```

### IaC workspace in the browser

The **IaC** page manages the folder at `LABFORGE_IAC_PATH` as a workspace:

- **Browse by tier and scope**: environments appear as cards in tabs (Dev / Test / Staging / Demo),
  filterable by scope; each card shows its size (networks, hosts, LBs, containers), ARM and Windows
  counts, labels and pending changes. Opening a card shows its resource table (what is declared, what it
  depends on) and a dependency graph. Files and the workspace plan have their own tabs
- **Edit online**: a YAML editor that validates as you type. Problems are marked on the exact line
  (schema errors, unknown fields, broken cross-references, duplicate environments across files), next to
  a live outline of the parsed structure and a **plan preview computed from the unsaved text**
- **Save safely**: saving is refused if someone else changed the file since you opened it; every save and
  delete keeps the previous version, which can be loaded back from the History tab
- **Create, import, export**: new files from starters (blank, device rig pool, build farm); import
  `.yaml` / `.yml` files or a `.zip` of a folder (each file is validated on import); export the
  workspace as a `.zip` or download a single file
- **Run**: dry-run apply a single environment file or the whole workspace and follow the run log; real applies from
  the browser need `LABFORGE_UI_APPLY=1`
- **Field reference** at `/iac/schema`, generated from the same schema used for validation, plus
  `/iac/schema.json` for completion in VS Code or any editor using yaml-language-server

Scripts and CI can do the same through the API: `GET/PUT/DELETE /api/v1/iac/files/{path}` (send the
digest you started from as `If-Match`), `POST /api/v1/iac/validate`, `GET /api/v1/iac/export`.

### Secrets

IaC files only hold reference names; values come from environment variables:
`credentialsRef: bmc-default` → `LABFORGE_SECRET_BMC_DEFAULT`.

| Reference | Format |
|---|---|
| `bmc.credentialsRef` | `user:password` |
| `connection.keyRef` | Path to an SSH private key |
| `connection.passwordRef` | WinRM password |
| `containers[].secretEnv` | Value of a container environment variable (never appears in argv or logs) |

`--dry-run` needs no secrets. The IPMI password is passed to `ipmitool -E` through the `IPMI_PASSWORD`
environment variable, so it never reaches the command line or the logs.

YAML files can also reference ordinary environment variables with `${VAR}` / `${VAR:-default}`.

### Guardrails

- Environments with `tier: prod` refuse changes unless `--allow-prod` is passed (API: `allow_prod: true`)
- An apply only affects the environments present in the loaded files; other environments' state is never deleted
- The web UI is dry-run only by default; real applies need `LABFORGE_UI_APPLY=1`
- With `LABFORGE_API_TOKEN` set, every write API call and agent report must send `X-LabForge-Token`
- Applies run one at a time within a server process

## IaC reference

```yaml
apiVersion: labforge/v1
kind: Environment
metadata: { name: dev, tier: dev }        # tier: dev | test | rnd | staging | demo | prod
spec:
  networks:
    - name: lab-vpc
      type: vpc                           # vpc | vlan | physical
      cidr: 10.20.0.0/16
      subnets: [ { name: arm, cidr: 10.20.1.0/24, gateway: 10.20.1.1, vlan: 101 } ]
      driver: record                      # record | terraform (+ terraform: {module, vars})
  hosts:
    - name: arm-node-01
      kind: baremetal                     # baremetal | vm | cloud
      platform: { os: linux, arch: arm64 } # os: linux|windows|macos  arch: arm64|amd64|armv7|riscv64
      address: 10.20.1.11
      subnet: lab-vpc/arm
      bmc: { type: redfish, address: 10.20.0.11, credentialsRef: bmc-default }   # redfish | ipmi
      power: "on"                         # desired power state (requires bmc)
      connection: { type: ssh, user: lab, keyRef: lab-ssh-key }                 # ssh | winrm | none
      bootstrap: [ "sudo apt-get install -y python3-psutil" ]  # idempotent; runs on create and when changed
      monitor:
        agent: true
        probes: [ { type: ping }, { type: tcp, port: 22 }, { type: http, port: 8080, path: /healthz } ]
      onDestroy: deregister               # deregister | poweroff
  loadBalancers:
    - name: test-api
      vip: 10.20.3.100
      driver: haproxy                     # record | haproxy | terraform
      host: svc-01                        # host that runs haproxy (over SSH)
      listeners: [ { port: 8080, protocol: http, targetPort: 8080 } ]
      targets: [ arm-node-01 ]
      healthCheck: { protocol: http, port: 8080, path: /healthz }
  containers:
    - name: test-db
      host: svc-01                        # a host with connection: none means the local docker daemon
      image: postgres:16
      ports: ["5432:5432"]
      env: { POSTGRES_DB: testdb }
      secretEnv: { POSTGRES_PASSWORD: test-db-password }
```

Unknown fields are rejected, so typos never get silently ignored. See [infra/staging/staging.yaml](infra/staging/staging.yaml)
for a full example.

## Deploying the agent

The agent is the single file [labforge/agent.py](labforge/agent.py). It only needs `psutil`, and
`--env` + `--name` match it to a host in the IaC.

**ARM / x86 Linux (systemd)**:

```bash
sudo mkdir -p /opt/labforge && sudo cp labforge/agent.py /opt/labforge/
sudo apt-get install -y python3-psutil          # or: pip install psutil
sudo cp deploy/agent/labforge-agent.service /etc/systemd/system/
sudo systemctl edit labforge-agent               # set LABFORGE_SERVER / LABFORGE_ENV / LABFORGE_HOST / LABFORGE_AGENT_TOKEN
sudo systemctl enable --now labforge-agent
```

**Windows (x64 and ARM64)**: run [deploy/agent/install-agent.ps1](deploy/agent/install-agent.ps1) from
an elevated PowerShell; it registers a scheduled task that starts at boot.

Temperatures: on Linux the agent reads `psutil` sensors and falls back to `/sys/class/thermal` on ARM
boards; on Windows, rely on the BMC (Redfish) for temperatures.

## Architecture

```
            ┌──────────── labforge serve ─────────────┐
 Browser ──▶ │ Web UI (Jinja2)   REST API /api/v1      │
 CLI ──────▶ │ service: plan / apply / runs            │
            │ IaC: loader → planner → applier          │──▶ ssh · winrm · redfish · ipmitool · docker · terraform
            │ monitor: probes + agent metrics + drift  │──▶ ping · tcp · http · BMC · docker inspect
            └───────────────┬─────────────────────────┘
                            │ SQLite (default) / PostgreSQL: state, runs, probe results, events, metrics
 Agent (ARM/Windows) ───────┘ POST /api/v1/agent/report
```

| Path | Purpose |
|---|---|
| `labforge/iac/spec.py` | IaC schema and validation |
| `labforge/iac/planner.py` | Desired vs. recorded state → ordered plan (topological order, deletes reversed) |
| `labforge/iac/applier.py` · `providers/` | Plan execution; one driver per resource type |
| `labforge/iac/workspace.py` · `inspect.py` | IaC files (safe paths, conflicts, history, import/export); parsed views and field reference |
| `labforge/monitor/` | Async probes, health sweeps, status aggregation |
| `labforge/api.py` · `web.py` · `web_iac.py` · `templates/` | Backend API and web pages |
| `labforge/rigs.py` · `web_rigs.py` | Device test rigs: checkout, health check, self-healing, quarantine, release |
| `labforge/alerting.py` · `notify.py` · `metrics.py` | Alert engine, channel adapters, Prometheus metrics |
| `labforge/builds.py` · `web_builds.py` | Build-run ingestion, pipeline statistics, suggestions |
| `labforge/auth.py` · `locks.py` · `cra.py` | Tokens, roles, SSO, audit; multi-instance locks; CRA traceability |
| `labforge/sim.py` | Simulated ARM sensors with Redfish BMCs, a switched PDU, and fault injection |
| `labforge/agent.py` | Host agent |

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `LABFORGE_DB_URL` | `sqlite:///labforge.db` | Switch to PostgreSQL for multiple users or instances |
| `LABFORGE_IAC_PATH` | `infra` | IaC file or directory |
| `LABFORGE_API_TOKEN` | none | Token for write API calls and agent reports |
| `LABFORGE_MONITOR` | `true` | Run health sweeps inside the server |
| `LABFORGE_MONITOR_INTERVAL` | `30` | Sweep interval (seconds) |
| `LABFORGE_AGENT_STALE_AFTER` | `120` | Seconds without a report before an agent counts as lost |
| `LABFORGE_RETENTION_HOURS` | `24` | Agent metric retention (events are kept 7× longer) |
| `LABFORGE_UI_APPLY` | `false` | Allow real applies from the web UI |
| `LABFORGE_UI_EDIT` | `true` | Allow editing, importing and deleting IaC files from the web UI |
| `LABFORGE_DATA_DIR` | `.labforge` | Generated files such as Terraform variable files |
| `LABFORGE_PUBLIC_URL` | `http://127.0.0.1:8000` | Base URL used for links in notifications |
| `LABFORGE_SLACK_SIGNING_SECRET` | none | Verifies Slack button clicks |
| `LABFORGE_AUTH_HEADER` / `LABFORGE_GROUPS_HEADER` | none | SSO proxy headers for the web UI's user and groups |
| `LABFORGE_SSO_DEFAULT_ROLE` | `viewer` | Role for signed-in users without a binding |
| `VAULT_ADDR` / `VAULT_TOKEN` | none | Read secrets from Vault KV v2 (`LABFORGE_VAULT_MOUNT`, `LABFORGE_VAULT_PREFIX`) |

## Development

```bash
pytest
```

## Milestones

| Milestone | Status | Goal |
|---|---|---|
| **M0 Platform foundation** | ✅ Done (v0.1.0) | IaC, plan/apply, drivers, monitoring, agent, web console, IaC workspace |
| **M1 Build & test lab onboarding** | 🚧 In progress | Device pools, build machines, runners and cache services all managed as code |
| **M2 Device test rigs (on-device CI)** | ✅ Done (simulated rigs) | CI checks out a device, flashes it, runs on-device tests, then returns and restores it |
| **M3 Alerting & on-call** | ✅ Done | Push problems to the right person via Slack / Teams / WhatsApp / SMS |
| **M4 Build farm capacity & speed** | ✅ Done | Short queues, warm caches, build time that is measured and optimised |
| **M5 Security, compliance & GitOps** | ✅ Done | Safe multi-user operation and CRA traceability |
| **M6 Production release guardrails** | ⏳ Planned | Releases for customer production lines pass real devices first, then roll out in stages |

### M0 Platform foundation ✅

- [x] IaC schema: environments / networks / load balancers / hosts / containers, with cross-reference validation
- [x] plan / apply / destroy, topological ordering, state storage, dry-run, production guardrail
- [x] Drivers: Redfish · IPMI · SSH · WinRM · Docker · HAProxy · Terraform
- [x] Monitoring: ping / TCP / HTTP / BMC / containers / LB backends / gateways, status-change events
- [x] Agent (ARM Linux / Windows), threshold alerts, architecture and OS drift detection
- [x] Web console + REST API, automated tests
- [x] IaC workspace: online editor with line-level validation, structure and plan preview, revision history, import / export, schema-generated field reference

### M1 Build & test lab onboarding 🚧

- [x] Full environment set declared: dev (module), test-unit (device rig), test-integration (full chain), staging (pre-production), demo (showcase)
- [x] Simulated device lab (`labforge sim`): ARM sensors with Redfish BMCs and fault injection, declared in `infra/test/sim-lab.yaml` and applied for real
- [ ] Declare the whole lab in IaC:
  - Device pools: ARM sensor devices grouped by product family (labelled with firmware version and serial number)
  - Build machines: Linux Docker build hosts, Windows MSVC build machines
  - CI executors: Bamboo remote agents, Bitbucket Pipelines / GitHub Actions self-hosted runners, labelled by hardware (e.g. `rig-sensor-a`)
  - Shared services: Conan remote (Artifactory), sccache remote cache, internal container registry
  - Networks: lab VLANs (management / devices / build) and the load balancers in front of devices
- [x] Build-machine monitoring: free disk space and Docker disk usage against per-host thresholds (`monitor.thresholds`), CI runner processes online (`monitor.processes`, agent `--process actions-runner`)
- [x] Deployment artifacts: `docker-compose.yml` (LabForge + PostgreSQL) and a systemd unit (`deploy/server/`)
- [ ] Deployed on an always-on server in the lab with the API token enabled *(needs the lab)*
- [ ] Agents on every device and build machine; devices that cannot run an agent are probed via ping and an HTTP version endpoint *(needs the lab)*
- [ ] **Acceptance**: every lab resource shows "up" on the overview, with no drift and no lost agents, for one week straight

### M2 Device test rigs (on-device CI)

QEMU proves the ARM binary runs; it does not prove the product works on the device.

- [x] Device checkout / return API: borrow a device from a pool by label and lock it, with timeout-based auto-return
- [x] Pre-test health check (boot, network, version endpoint); a failure is recorded as an **infrastructure failure**, not a test failure
- [x] On return, always power-cycle the device back to a known state, whether the tests passed or failed
- [x] Flash a firmware build onto a leased device and verify it runs exactly that build (version + sha256)
- [x] Device-specific flashing (`firmware.method`: device endpoint, or a command for vendor tools / PXE) and restoring the known-good firmware on return
- [x] Self-healing through the BMC: a device that fails its health check is power-cycled and re-checked; quarantined after two failed health checks
- [x] Hard reset through a **PDU** outlet (HTTP or APC SNMP) for devices without a BMC
- [x] CLI and REST API any CI system can call (`labforge rig checkout / release`), with a GitHub Actions example
- [x] Ready-made Bamboo specs and Bitbucket Pipelines steps (`deploy/ci/`) *(not yet run against a real Bamboo / Bitbucket)*
- [x] BuildPulse integration: `buildpulse device-test` records checkout → flash → verify → test → release in `report.json` with device, lease, firmware version and sha256; `buildpulse triage` separates `infrastructure` from `test` failures
- [x] Rigs page: pool state, holders, lease expiry, quarantine reasons, lease history
- [x] Rig trends: device utilisation, queue time (`checkout --wait`), results split into passed / failed / infra-failed
- [ ] Flaky-test tracking across runs *(needs per-test results from the on-device suite)*

### M3 Alerting & on-call

- [x] Notification channels (pluggable, one adapter per channel):
  - **Slack** (primary): actionable buttons to acknowledge, silence or quarantine a device; messages link to the run and include a failure summary
  - **Microsoft Teams**: Incoming Webhooks / Workflows
  - **WhatsApp**: Meta WhatsApp Cloud API or Twilio, for after-hours on-call
  - **SMS**: Twilio as the last-resort fallback (iMessage has no public sending API, so SMS covers phones)
  - **PagerDuty / Opsgenie**: on-call rotations and escalation; plus email and generic webhooks
- [x] Routing rules by environment tier (dev / test / staging / demo / prod) and severity, e.g. build machine disk full → Slack channel; device pool availability below threshold → on-call engineer via WhatsApp
- [x] Deduplication, grouping, silence windows and recovery notifications to prevent alert storms
- [x] Prometheus metrics at `/metrics` with a Grafana dashboard; availability reports
- [ ] Verified against live Slack / Teams / WhatsApp / Twilio / PagerDuty accounts *(payloads are tested against each provider's documented API; needs real credentials)*

### M4 Build farm capacity & speed

- [x] Build agents managed as IaC: `replicas` scales ephemeral Docker build agents up and down
- [x] Ingest BuildPulse `report.json` files to track per-stage timings, cache hit rates and queue times
- [x] Cache effectiveness from the build data: compiler cache hit rate, cold vs warm dependency time
- [ ] Capacity of the remote cache services themselves (Conan remote / sccache storage) *(needs those services; their hosts' disk is already covered by the agent)*
- [x] Surface the slowest pipelines and stages, with optimisation suggestions
- [ ] Autoscaling on queue length *(today: queue time shows up and the suggestion says to raise `replicas`)*

### M5 Security, compliance & GitOps

- [x] IaC lives in Bitbucket / GitHub: a pull request posts the plan (against the lab's real state) as a comment; merging syncs and applies it (`labforge plan --remote`, `labforge sync --apply`, `deploy/gitops/`)
- [x] Single sign-on through an SSO proxy (OIDC / LDAP behind oauth2-proxy or similar), per-environment RBAC from an `AccessPolicy`, audit log
- [x] Per-agent tokens bound to their host; named, scoped, revocable API tokens; secrets from Vault (KV v2) after the environment
- [x] **CRA traceability**: firmware inventory per device (from flashes and version endpoints), SBOM per firmware version, "which devices does this CVE reach" with a report for the 24-hour early warning
- [x] PostgreSQL and multi-instance deployment: database lease locks elect one monitor leader and serialize applies and rig checkouts; the test suite runs on PostgreSQL (`LABFORGE_TEST_DB_URL`)
- [ ] Wired to a real IdP and Vault *(needs your SSO and Vault; the protocols are covered by tests)*

### M6 Production release guardrails

- [ ] Release gate: a version must pass the device test rigs for every product family before it can ship
- [ ] Staged rollout: ship to one internal production-line station first (canary), widen after watching health metrics
- [ ] Automatic rollback to the known-good firmware when health metrics degrade
- [ ] Change approval, change windows, separate permissions and network isolation for production

## Getting started: from demo to a real lab (M1)

The steps below use the development environment as the example; device pools, build machines and runners
go into IaC the same way. Work through them in order; each step has a check you can verify.

**Step 1: Set up** (Python 3.11+)

```bash
git clone https://github.com/jzoscar/LabForge.git && cd LabForge
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,windows]'
pytest
```

✅ All tests pass.

**Step 2: Run the local demo** to get familiar with the console (see Quick start above).

✅ The browser shows `this-mac` up and `offline-board` down.

**Step 3: Inventory your devices and write the IaC.** Start with the development environment only, in
`infra/dev/dev.yaml`:

- `networks`: the lab address ranges and subnets (management / ARM / Windows / services)
- `hosts`: one entry per machine, with `platform.os` / `platform.arch`, `address`, a `bmc` block where
  there is one, and `connection`
- Remove what you do not need yet (load balancers, containers) and add it back later

```bash
labforge validate infra/dev/dev.yaml
```

✅ Prints `✓ dev [dev]: ...` with no errors.

**Step 4: Configure secrets** in the server's environment or a `.env` file; never put them in IaC files
or commit them to git.

```bash
export LABFORGE_SECRET_BMC_DEFAULT='admin:<bmc-password>'
export LABFORGE_SECRET_LAB_SSH_KEY="$HOME/.ssh/lab_ed25519"
export LABFORGE_SECRET_WIN_ADMIN='<windows-admin-password>'
```

**Step 5: Rehearse**

```bash
labforge plan infra/dev/dev.yaml
labforge apply infra/dev/dev.yaml --dry-run
```

✅ Every ssh / ipmitool / winrm / docker command listed is what you expect.

**Step 6: Deploy the platform** on an always-on lab server that can reach every device and BMC:

```bash
export LABFORGE_API_TOKEN=$(openssl rand -hex 24)   # keep it; the agents need it
export LABFORGE_IAC_PATH=infra
export LABFORGE_DB_URL=sqlite:////var/lib/labforge/labforge.db
labforge serve --host 0.0.0.0 --port 8000
```

Or as a container: `docker build -t labforge . && docker run -d -p 8000:8000 -v labforge-data:/data -e LABFORGE_API_TOKEN=... labforge`

✅ Other machines in the lab can open `http://<server>:8000`.

**Step 7: Install the agent on every ARM board and Windows machine** (see Deploying the agent);
`--name` must match the host name in the IaC.

✅ Every host on the overview shows CPU / memory / disk, and its agent column shows a report from a few seconds ago.

**Step 8: Apply for real**

```bash
labforge apply infra/dev/dev.yaml
```

✅ The run shows success, and `labforge state` lists every resource.

**Step 9: Accept and troubleshoot.** Watch it for a week; every dev resource should be up. If you see:

| Symptom | Fix |
|---|---|
| BMC column says "no BMC credentials" | Check that `LABFORGE_SECRET_*` is set in the **server process's** environment |
| Drift: `arch aarch64 != spec amd64` | The device's real architecture differs from the IaC; fix the IaC or investigate the device |
| Agent shows no reports / lost | Check the `--server` address, the token, and the firewall on port 8000 |
| Ping fails but the service works | The device blocks ICMP; switch `monitor.probes` to a `tcp` probe |

**Step 10: Expand** to the test environments in `infra/test/`, then `infra/staging/` and `infra/demo/`, and move on to M2.
