"""Declarative IaC schema (YAML, camelCase keys).

One document = one environment (dev / test / rnd / staging / demo / prod) containing
networks (VPC/VLAN/physical), load balancers, hosts (bare-metal boards, VMs,
servers - Linux or Windows, ARM or x86) and containers.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic.alias_generators import to_camel

NAME = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
Tier = Literal["dev", "test", "rnd", "staging", "demo", "prod"]


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class TerraformRef(Model):
    module: str  # path to a terraform module, relative to the IaC file that declares it
    vars: dict[str, Any] = {}


# --- networks -------------------------------------------------------------


class Subnet(Model):
    name: str = Field(pattern=NAME)
    cidr: str
    gateway: str | None = None
    vlan: int | None = Field(default=None, ge=1, le=4094)

    @model_validator(mode="after")
    def _check(self) -> Subnet:
        net = ipaddress.ip_network(self.cidr, strict=True)
        if self.gateway and ipaddress.ip_address(self.gateway) not in net:
            raise ValueError(f"gateway {self.gateway} is outside subnet {self.cidr}")
        return self


class Network(Model):
    name: str = Field(pattern=NAME)
    type: Literal["vpc", "vlan", "physical"] = "vpc"
    cidr: str
    subnets: list[Subnet] = []
    driver: Literal["record", "terraform"] = "record"
    terraform: TerraformRef | None = None
    labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Network:
        net = ipaddress.ip_network(self.cidr, strict=True)
        seen: set[str] = set()
        for s in self.subnets:
            if s.name in seen:
                raise ValueError(f"duplicate subnet {s.name}")
            seen.add(s.name)
            sub = ipaddress.ip_network(s.cidr)
            if sub.version != net.version or not sub.subnet_of(net):
                raise ValueError(f"subnet {s.name} ({s.cidr}) is outside {self.cidr}")
        if self.driver == "terraform" and not self.terraform:
            raise ValueError("driver terraform requires a terraform block")
        return self


# --- hosts ----------------------------------------------------------------


class Platform(Model):
    os: Literal["linux", "windows", "macos"]
    arch: Literal["arm64", "amd64", "armv7", "riscv64"] = "amd64"
    distro: str | None = None
    version: str | None = None


class Bmc(Model):
    """Out-of-band management controller of a physical machine."""

    type: Literal["redfish", "ipmi"] = "redfish"
    address: str
    credentials_ref: str  # secret holding "user:password"
    verify_tls: bool = False
    system_id: str | None = None  # Redfish system id; defaults to the first system


class Connection(Model):
    type: Literal["ssh", "winrm", "none"] = "ssh"
    user: str | None = None
    port: int | None = None
    key_ref: str | None = None  # ssh: secret holding a private key *path*
    password_ref: str | None = None  # winrm: secret holding the password
    transport: Literal["ntlm", "kerberos", "basic", "credssp"] = "ntlm"
    https: bool = False


class Probe(Model):
    type: Literal["ping", "tcp", "http"]
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str = "/"
    https: bool = False
    timeout: float = 3.0

    @model_validator(mode="after")
    def _check(self) -> Probe:
        if self.type == "tcp" and not self.port:
            raise ValueError("tcp probe requires port")
        return self

    @property
    def label(self) -> str:
        if self.type == "ping":
            return "ping"
        if self.type == "tcp":
            return f"tcp:{self.port}"
        return f"{'https' if self.https else 'http'}:{self.port or ''}{self.path}"


class Thresholds(Model):
    """Per-host limits for agent metrics; unset fields use the defaults (95 / 95 / 90 / 85)."""

    cpu: float | None = Field(default=None, ge=0, le=100)
    mem: float | None = Field(default=None, ge=0, le=100)
    disk: float | None = Field(default=None, ge=0, le=100)
    temp: float | None = None


class Monitor(Model):
    agent: bool = False  # expect reports from the labforge agent
    probes: list[Probe] | None = None  # None -> [ping] when the host has an address
    processes: list[str] = []  # must be running, as reported by the agent (e.g. actions-runner)
    thresholds: Thresholds = Thresholds()

    @model_validator(mode="after")
    def _check(self) -> Monitor:
        if self.processes and not self.agent:
            raise ValueError("process checks need agent: true")
        return self


class Host(Model):
    name: str = Field(pattern=NAME)
    kind: Literal["baremetal", "vm", "cloud"] = "baremetal"
    platform: Platform
    address: str | None = None
    subnet: str | None = None  # "<network>/<subnet>"
    bmc: Bmc | None = None
    power: Literal["on", "off"] | None = None  # desired power state (requires bmc)
    connection: Connection = Connection()
    bootstrap: list[str] = []  # idempotent commands; run on create and when changed
    monitor: Monitor = Monitor()
    on_destroy: Literal["deregister", "poweroff"] = "deregister"
    labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Host:
        if self.power and not self.bmc:
            raise ValueError("power requires a bmc block")
        if self.on_destroy == "poweroff" and not self.bmc:
            raise ValueError("onDestroy: poweroff requires a bmc block")
        if self.bootstrap:
            if not self.address:
                raise ValueError("bootstrap requires address")
            if self.connection.type == "none":
                raise ValueError("bootstrap requires an ssh or winrm connection")
        if self.connection.type == "winrm" and not self.connection.password_ref:
            raise ValueError("winrm connection requires passwordRef")
        return self


# --- load balancers -------------------------------------------------------


class Listener(Model):
    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "http", "https"] = "tcp"
    target_port: int | None = Field(default=None, ge=1, le=65535)


class HealthCheck(Model):
    protocol: Literal["tcp", "http"] = "tcp"
    port: int = Field(ge=1, le=65535)
    path: str = "/"


class LoadBalancer(Model):
    name: str = Field(pattern=NAME)
    network: str | None = None
    vip: str | None = None
    listeners: list[Listener] = Field(min_length=1)
    targets: list[str] = []  # host names
    health_check: HealthCheck | None = None
    driver: Literal["record", "haproxy", "terraform"] = "record"
    host: str | None = None  # haproxy: the host that runs haproxy
    terraform: TerraformRef | None = None
    labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> LoadBalancer:
        if self.driver == "haproxy" and not self.host:
            raise ValueError("driver haproxy requires host")
        if self.driver == "terraform" and not self.terraform:
            raise ValueError("driver terraform requires a terraform block")
        return self


# --- containers -----------------------------------------------------------


class Container(Model):
    name: str = Field(pattern=NAME)
    host: str
    image: str
    ports: list[str] = []  # docker -p syntax
    env: dict[str, str] = {}
    secret_env: dict[str, str] = {}  # VAR -> secret ref; never written to argv or logs
    volumes: list[str] = []
    command: list[str] | None = None
    restart: Literal["no", "always", "unless-stopped", "on-failure"] = "unless-stopped"
    labels: dict[str, str] = {}


# --- environment document -------------------------------------------------


class Metadata(Model):
    name: str = Field(pattern=NAME)
    tier: Tier = "dev"
    description: str = ""
    labels: dict[str, str] = {}


class EnvironmentSpec(Model):
    networks: list[Network] = []
    load_balancers: list[LoadBalancer] = []
    hosts: list[Host] = []
    containers: list[Container] = []


class EnvironmentDoc(Model):
    api_version: Literal["labforge/v1"]
    kind: Literal["Environment"]
    metadata: Metadata
    spec: EnvironmentSpec = EnvironmentSpec()
    # File the document was loaded from; relative paths (terraform modules) resolve against it.
    _source: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _references(self) -> EnvironmentDoc:
        s = self.spec
        errors: list[str] = []
        for section in ("networks", "load_balancers", "hosts", "containers"):
            names = [item.name for item in getattr(s, section)]
            for dup in sorted({n for n in names if names.count(n) > 1}):
                errors.append(f"duplicate {section} name {dup!r}")

        nets = {n.name: n for n in s.networks}
        hosts = {h.name: h for h in s.hosts}

        for h in s.hosts:
            if not h.subnet:
                continue
            net_name, _, sub_name = h.subnet.partition("/")
            net = nets.get(net_name)
            sub = next((x for x in net.subnets if x.name == sub_name), None) if net else None
            if not sub:
                errors.append(f"host {h.name}: unknown subnet {h.subnet!r}")
            elif h.address and _is_ip(h.address) and ipaddress.ip_address(h.address) not in ipaddress.ip_network(sub.cidr):
                errors.append(f"host {h.name}: address {h.address} is outside subnet {h.subnet} ({sub.cidr})")

        haproxy_hosts: set[str] = set()
        for lb in s.load_balancers:
            if lb.network and lb.network not in nets:
                errors.append(f"loadBalancer {lb.name}: unknown network {lb.network!r}")
            for t in lb.targets:
                if t not in hosts:
                    errors.append(f"loadBalancer {lb.name}: unknown target host {t!r}")
                elif not hosts[t].address:
                    errors.append(f"loadBalancer {lb.name}: target {t!r} has no address")
            if lb.host:
                if lb.host not in hosts:
                    errors.append(f"loadBalancer {lb.name}: unknown host {lb.host!r}")
                elif lb.driver == "haproxy" and hosts[lb.host].connection.type != "ssh":
                    errors.append(f"loadBalancer {lb.name}: haproxy host must use an ssh connection")
            if lb.driver == "haproxy" and lb.host:
                if lb.host in haproxy_hosts:
                    errors.append(f"loadBalancer {lb.name}: host {lb.host!r} already runs another haproxy LB")
                haproxy_hosts.add(lb.host)

        for c in s.containers:
            h = hosts.get(c.host)
            if not h:
                errors.append(f"container {c.name}: unknown host {c.host!r}")
            elif h.connection.type == "winrm":
                errors.append(f"container {c.name}: host {c.host!r} uses winrm; docker needs ssh or a local host")
            elif h.connection.type == "ssh" and not h.address:
                errors.append(f"container {c.name}: host {c.host!r} has no address")

        if errors:
            raise ValueError("; ".join(errors))
        return self


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


KIND_MODELS: dict[str, type[Model]] = {
    "network": Network,
    "loadbalancer": LoadBalancer,
    "host": Host,
    "container": Container,
}
