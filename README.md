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
- **Release** power-cycles the device back to a known state, whatever the test result
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

A GitHub Actions job on a self-hosted runner in the lab (the same three commands work in Bamboo or
Bitbucket Pipelines):

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
| `labforge/sim.py` | Simulated ARM sensors with Redfish BMCs and fault injection |
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

## Development

```bash
pytest
```

## Milestones

| Milestone | Status | Goal |
|---|---|---|
| **M0 Platform foundation** | ✅ Done (v0.1.0) | IaC, plan/apply, drivers, monitoring, agent, web console, IaC workspace |
| **M1 Build & test lab onboarding** | 🚧 In progress | Device pools, build machines, runners and cache services all managed as code |
| **M2 Device test rigs (on-device CI)** | 🚧 Core done | CI checks out a device, flashes it, runs on-device tests, then returns and restores it |
| **M3 Alerting & on-call** | ⏳ Planned | Push problems to the right person via Slack / Teams / WhatsApp / SMS |
| **M4 Build farm capacity & speed** | ⏳ Planned | Short queues, warm caches, build time that is measured and optimised |
| **M5 Security, compliance & GitOps** | ⏳ Planned | Safe multi-user operation and CRA traceability |
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
- [ ] Build-machine monitoring: free disk space (the most common build-machine failure), Docker disk usage, runner process online
- [ ] Deploy the platform on an always-on server in the lab with the API token enabled
- [ ] Agents on every device and build machine; devices that cannot run an agent (embedded firmware) are probed via ping and an HTTP version endpoint
- [ ] **Acceptance**: every lab resource shows "up" on the overview, with no drift and no lost agents, for one week straight

### M2 Device test rigs (on-device CI)

QEMU proves the ARM binary runs; it does not prove the product works on the device.

- [x] Device checkout / return API: borrow a device from a pool by label and lock it, with timeout-based auto-return
- [x] Pre-test health check (boot, network, version endpoint); a failure is recorded as an **infrastructure failure**, not a test failure
- [x] On return, always power-cycle the device back to a known state, whether the tests passed or failed
- [x] Flash a firmware build onto a leased device and verify it runs exactly that build (version + sha256)
- [ ] Device-specific flashing (vendor update tools, PXE) and restoring a known-good **image** on return (not just a power cycle)
- [x] Self-healing through the BMC: a device that fails its health check is power-cycled and re-checked; quarantined after two failed health checks
- [ ] Hard reset through a **PDU** for devices without a BMC
- [x] CLI and REST API any CI system can call (`labforge rig checkout / release`), with a GitHub Actions example
- [ ] Ready-made Bamboo and Bitbucket Pipelines steps
- [x] BuildPulse integration: `buildpulse device-test` records checkout → flash → verify → test → release in `report.json` with device, lease, firmware version and sha256; `buildpulse triage` separates `infrastructure` from `test` failures
- [x] Rigs page: pool state, holders, lease expiry, quarantine reasons, lease history
- [ ] Rig trends: device utilisation, queue time, failure causes (code / device / network); flaky tests tracked separately

### M3 Alerting & on-call

- [ ] Notification channels (pluggable, one adapter per channel):
  - **Slack** (primary): actionable buttons to acknowledge, silence or quarantine a device; messages link to the run and include a failure summary
  - **Microsoft Teams**: Incoming Webhooks / Workflows
  - **WhatsApp**: Meta WhatsApp Cloud API or Twilio, for after-hours on-call
  - **SMS**: Twilio as the last-resort fallback (iMessage has no public sending API, so SMS covers phones)
  - **PagerDuty / Opsgenie**: on-call rotations and escalation; plus email and generic webhooks
- [ ] Routing rules by environment tier (dev / test / staging / demo / prod) and severity, e.g. build machine disk full → Slack channel; device pool availability below threshold → on-call engineer via WhatsApp
- [ ] Deduplication, grouping, silence windows and recovery notifications to prevent alert storms
- [ ] Prometheus metrics at `/metrics` with Grafana dashboards; historical trends and availability reports

### M4 Build farm capacity & speed

- [ ] Build agents managed as IaC, scaling ephemeral Docker build agents on demand
- [ ] Ingest BuildPulse `report.json` files to track per-stage timings, cache hit rates and queue times
- [ ] Monitor capacity and hit rate of the remote cache services (Conan remote, sccache) so every runner starts warm
- [ ] Surface the slowest pipelines and stages, with optimisation suggestions

### M5 Security, compliance & GitOps

- [ ] IaC lives in Bitbucket / GitHub: a pull request posts the plan as a comment; merging applies it
- [ ] Single sign-on (OIDC / LDAP), per-environment RBAC, audit log
- [ ] Per-agent tokens; secrets moved to a secrets manager such as Vault
- [ ] **CRA traceability**: record the firmware version currently on each device and link it to that version's SBOM from BuildPulse, so when a new CVE is published the affected devices and versions are known immediately, supporting the 24-hour reporting process
- [ ] PostgreSQL and multi-instance deployment

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
