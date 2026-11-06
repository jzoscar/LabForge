"""Who is asking, and may they?

API calls authenticate with `X-LabForge-Token`: the master token (LABFORGE_API_TOKEN, admin) or a
named token from `labforge token create` (role, environment scope, optionally bound to one agent's
host). With no master token and no named tokens, the API is open (development).

The web UI sits behind an SSO proxy (oauth2-proxy, Authentik, ... in front of OIDC or LDAP) that sets
a user header (LABFORGE_AUTH_HEADER) and optionally a groups header. An AccessPolicy in the IaC
workspace maps users and groups to roles. Without an auth header configured, the UI is open.

Roles: viewer < operator (rigs, alerts, dry-runs, build ingest) < editor (IaC files) < admin (real
apply, tokens). `agent` tokens can only send agent reports, as the host they are bound to.
"""

from __future__ import annotations

import fnmatch
import getpass
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from fastapi import HTTPException, Request
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from labforge.models import ApiToken, AuditEvent, utcnow

RANK = {"viewer": 0, "operator": 1, "editor": 2, "admin": 3}
ROLES = [*RANK, "agent"]


@dataclass
class Principal:
    name: str
    role: str
    envs: list[str] = field(default_factory=lambda: ["*"])
    subject: str | None = None  # agent tokens: host address glob
    via: str = "anonymous"  # anonymous | master | token | sso | cli

    def can(self, role: str) -> bool:
        if role == "agent":
            return self.role == "agent" or RANK.get(self.role, -1) >= RANK["operator"]
        return RANK.get(self.role, -1) >= RANK[role]

    def covers(self, env: str) -> bool:
        return any(fnmatch.fnmatchcase(env, pattern) for pattern in self.envs)

    def may_report_as(self, address: str) -> bool:
        return self.role != "agent" or (self.subject is not None and fnmatch.fnmatchcase(address, self.subject))


ANONYMOUS = Principal("anonymous", "admin")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(engine: Engine, name: str, role: str, envs: list[str] | None = None,
                 subject: str | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if role == "agent" and not subject:
        raise ValueError("agent tokens need a subject: the host address they report as")
    token = "lf_" + secrets.token_urlsafe(32)
    with Session(engine) as s:
        if s.exec(select(ApiToken).where(ApiToken.name == name)).first():
            raise ValueError(f"a token named {name!r} already exists")
        s.add(ApiToken(name=name, token_hash=hash_token(token), prefix=token[:7], role=role,
                       envs=",".join(envs or ["*"]), subject=subject))
        s.commit()
    return token


def revoke_token(engine: Engine, name: str) -> None:
    with Session(engine) as s:
        row = s.exec(select(ApiToken).where(ApiToken.name == name, ApiToken.revoked_at == None)).first()  # noqa: E711
        if not row:
            raise LookupError(f"no active token named {name!r}")
        row.revoked_at = utcnow()
        s.add(row)
        s.commit()


def _api_auth_required(request: Request) -> bool:
    if request.app.state.settings.api_token:
        return True
    with Session(request.app.state.engine) as s:
        return s.exec(select(ApiToken).where(ApiToken.revoked_at == None).limit(1)).first() is not None  # noqa: E711


def api_principal(request: Request) -> Principal:
    token = request.headers.get("X-LabForge-Token")
    master = request.app.state.settings.api_token
    if token:
        if master and hmac.compare_digest(token, master):
            return Principal("master-token", "admin", via="master")
        with Session(request.app.state.engine) as s:
            row = s.exec(select(ApiToken).where(ApiToken.token_hash == hash_token(token),
                                                ApiToken.revoked_at == None)).first()  # noqa: E711
            if row:
                row.last_used_at = utcnow()
                s.add(row)
                s.commit()
                return Principal(f"token:{row.name}", row.role, row.envs.split(","), row.subject, "token")
        raise HTTPException(401, "invalid or revoked X-LabForge-Token")
    if _api_auth_required(request):
        raise HTTPException(401, "invalid or missing X-LabForge-Token")
    return ANONYMOUS


def requires(role: str):
    """FastAPI dependency: the caller must hold `role`; the principal is kept on request.state."""

    def dependency(request: Request) -> Principal:
        who = api_principal(request)
        if not who.can(role):
            raise HTTPException(403, f"{who.name} ({who.role}) may not do this; needs {role}")
        request.state.principal = who
        return who

    return dependency


def web_principal(request: Request) -> Principal:
    settings = request.app.state.settings
    if not settings.auth_header:
        return ANONYMOUS
    user = request.headers.get(settings.auth_header)
    if not user:
        raise HTTPException(401, "sign in through the SSO proxy")
    groups = [g.strip() for g in (request.headers.get(settings.groups_header or "") or "").split(",") if g.strip()]
    role, envs = _role_for(request, user, groups)
    return Principal(user, role, envs, via="sso")


def _role_for(request: Request, user: str, groups: list[str]) -> tuple[str, list[str]]:
    from labforge.iac.loader import load_access

    names = {user, f"user:{user}", *(f"group:{g}" for g in groups)}
    best, envs = request.app.state.settings.sso_default_role, ["*"]
    for policy in load_access(request.app.state.settings.workspace().root):
        for b in policy.spec.bindings:
            if names & set(b.subjects) and RANK[b.role] >= RANK.get(best, -1):
                best, envs = b.role, b.envs
    return best, envs


def web_require(request: Request, role: str) -> Principal:
    who = web_principal(request)
    if not who.can(role):
        raise HTTPException(403, f"{who.name} ({who.role}) may not do this; needs {role}")
    request.state.principal = who
    return who


def require_envs(who: Principal, envs: list[str]) -> None:
    denied = [e for e in envs if not who.covers(e)]
    if denied:
        raise HTTPException(403, f"{who.name} may not change environment(s) {', '.join(denied)}")


def audit(engine: Engine, who: Principal | str, action: str, target: str = "", detail: str = "",
          source: str = "web") -> None:
    actor = who if isinstance(who, str) else who.name
    with Session(engine) as s:
        s.add(AuditEvent(actor=actor, action=action, target=target[:300], detail=detail[:1000], source=source))
        s.commit()


def cli_actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli"
