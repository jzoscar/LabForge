"""Human-readable views of IaC: per-environment resource summaries, dependency
graphs (Mermaid) and a field reference generated from the schema itself."""

from __future__ import annotations

import re
from dataclasses import dataclass

from labforge.iac.planner import DesiredResource, flatten
from labforge.iac.spec import (
    Bmc, Connection, Container, EnvironmentDoc, HealthCheck, Host, Listener, LoadBalancer, Metadata, Monitor,
    Network, Platform, Probe, Subnet, TerraformRef,
)

KIND_ORDER = ["network", "host", "loadbalancer", "container"]


@dataclass
class ResourceView:
    kind: str
    name: str
    address: str
    summary: str
    details: list[str]
    depends_on: list[str]


@dataclass
class EnvView:
    name: str
    tier: str
    description: str
    resources: list[ResourceView]
    mermaid: str
    labels: dict[str, str]
    kinds: dict[str, int]  # resource count per kind
    arm: int  # hosts on ARM
    windows: int  # Windows hosts

    def by_kind(self, kind: str) -> list[ResourceView]:
        return [r for r in self.resources if r.kind == kind]


def _summary(r: DesiredResource) -> tuple[str, list[str]]:
    s = r.spec
    if r.kind == "network":
        subnets = s.get("subnets", [])
        details = [f"{x['name']} {x['cidr']}" + (f" · vlan {x['vlan']}" if x.get("vlan") else "") for x in subnets]
        return f"{s['type']} {s['cidr']} · {len(subnets)} subnet(s) · {s['driver']}", details
    if r.kind == "host":
        p, c = s["platform"], s.get("connection", {})
        details = []
        if s.get("bmc"):
            details.append(f"BMC {s['bmc']['type']} {s['bmc']['address']}" + (f" · power {s['power']}" if s.get("power") else ""))
        if s.get("bootstrap"):
            details.append(f"{len(s['bootstrap'])} bootstrap command(s)")
        if s.get("monitor", {}).get("agent"):
            details.append("agent expected")
        return f"{s['kind']} · {p['os']}/{p['arch']} · {s.get('address') or 'no address'} · {c.get('type', 'ssh')}", details
    if r.kind == "loadbalancer":
        ports = ", ".join(str(ls["port"]) for ls in s["listeners"])
        return f"{s['driver']} · {s.get('vip') or 'no vip'} :{ports} → {len(s.get('targets', []))} target(s)", \
            [f"target {t}" for t in s.get("targets", [])]
    details = [f"port {p}" for p in s.get("ports", [])] + [f"secret env {k}" for k in s.get("secretEnv", {})]
    return f"{s['image']} on {s['host']}", details


def _node_id(address: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", address)


def mermaid_graph(resources: list[DesiredResource]) -> str:
    lines = ["graph LR"]
    for r in resources:
        label = f"{r.name}<br/><small>{r.kind}</small>".replace('"', "'")
        lines.append(f'  {_node_id(r.address)}["{label}"]:::{r.kind}')
    for r in resources:
        lines += [f"  {_node_id(dep)} --> {_node_id(r.address)}" for dep in r.depends_on]
    lines += [
        "  classDef network fill:#dbe7ff,stroke:#2f6fde,color:#1d2330",
        "  classDef host fill:#e5f5ec,stroke:#1f8f4e,color:#1d2330",
        "  classDef loadbalancer fill:#fdf3e1,stroke:#b7791f,color:#1d2330",
        "  classDef container fill:#eee6fb,stroke:#7a4fd0,color:#1d2330",
    ]
    return "\n".join(lines)


def describe(docs: list[EnvironmentDoc]) -> list[EnvView]:
    out = []
    for doc in docs:
        resources = flatten(doc)
        resources.sort(key=lambda r: (KIND_ORDER.index(r.kind), r.name))
        views = []
        for r in resources:
            summary, details = _summary(r)
            views.append(ResourceView(r.kind, r.name, r.address, summary, details,
                                      [d.split(".", 2)[2] for d in r.depends_on]))
        hosts = doc.spec.hosts
        out.append(EnvView(
            doc.metadata.name, doc.metadata.tier, doc.metadata.description, views, mermaid_graph(resources),
            labels=dict(doc.metadata.labels),
            kinds={k: n for k in KIND_ORDER if (n := sum(1 for r in resources if r.kind == k))},
            arm=sum(1 for h in hosts if h.platform.arch in ("arm64", "armv7")),
            windows=sum(1 for h in hosts if h.platform.os == "windows"),
        ))
    return out


# --- schema reference -------------------------------------------------------

REFERENCE_MODELS = [
    ("Environment", "Top-level document; one per environment.", None),
    ("metadata", "Environment identity and tier.", Metadata),
    ("networks[]", "A VPC, VLAN or physical lab segment with its subnets.", Network),
    ("networks[].subnets[]", "A subnet inside a network.", Subnet),
    ("hosts[]", "A machine: bare-metal device, VM or cloud host; Linux, Windows or macOS.", Host),
    ("hosts[].platform", "OS and CPU architecture; the agent checks the machine against this (drift).", Platform),
    ("hosts[].bmc", "Out-of-band controller used for power control and hardware health.", Bmc),
    ("hosts[].connection", "How LabForge reaches the host for bootstrap commands.", Connection),
    ("hosts[].monitor", "What to check on the host.", Monitor),
    ("hosts[].monitor.probes[]", "An agentless probe.", Probe),
    ("loadBalancers[]", "A load balancer in front of hosts.", LoadBalancer),
    ("loadBalancers[].listeners[]", "A listening port.", Listener),
    ("loadBalancers[].healthCheck", "How backends are checked.", HealthCheck),
    ("containers[]", "A container run on a host's docker daemon.", Container),
    ("*.terraform", "Delegate a resource to an existing Terraform module.", TerraformRef),
]


def _section_for(title: str) -> str:
    return next((path for path, _, model in REFERENCE_MODELS if model and model.__name__ == title), title)


def _type_text(prop: dict, defs: dict) -> str:
    if "$ref" in prop:
        return "→ " + _section_for(defs.get(prop["$ref"].split("/")[-1], {}).get("title", "object"))
    if "enum" in prop:
        return " | ".join(str(v) for v in prop["enum"])
    if "const" in prop:
        return str(prop["const"])
    if "anyOf" in prop:
        return " | ".join(t for t in (_type_text(p, defs) for p in prop["anyOf"]) if t != "null") + " (optional)"
    t = prop.get("type", "any")
    if t == "array":
        return f"list of {_type_text(prop.get('items', {}), defs)}"
    if t == "object":
        return "map"
    return {"string": "string", "integer": "integer", "boolean": "boolean", "number": "number"}.get(t, t)


def schema_reference() -> list[dict]:
    sections = [{
        "path": "Environment", "description": "Top-level document; one per environment.",
        "fields": [
            {"name": "apiVersion", "type": "labforge/v1", "required": True, "default": ""},
            {"name": "kind", "type": "Environment", "required": True, "default": ""},
            {"name": "metadata", "type": "metadata", "required": True, "default": ""},
            {"name": "spec", "type": "networks, loadBalancers, hosts, containers", "required": False, "default": "{}"},
        ],
    }]
    for path, description, model in REFERENCE_MODELS[1:]:
        schema = model.model_json_schema(by_alias=True)
        defs = schema.get("$defs", {})
        required = set(schema.get("required", []))
        fields = []
        for name, prop in schema.get("properties", {}).items():
            default = prop.get("default", "")
            if default in ({}, []):
                default = "empty"
            elif isinstance(default, dict):
                default = "see section"
            fields.append({"name": name, "type": _type_text(prop, defs), "required": name in required,
                           "default": "" if default is None else str(default)})
        sections.append({"path": path, "description": description, "fields": fields})
    return sections


def json_schema() -> dict:
    """JSON Schema for editors (e.g. VS Code's YAML extension)."""
    schema = EnvironmentDoc.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "LabForge environment"
    return schema

