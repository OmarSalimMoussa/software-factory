"""The script the provisioner feeds to GlitchTip's Django shell, and the reader of its answer.

The script arrives on standard input, so the owner's password never shows in a process list. It runs in one
transaction and ends with one marker line: the result as JSON, or the reason it refused.
"""

from __future__ import annotations

import json
from typing import Any

RESULT = "FACTORY-RESULT "
REFUSED = "FACTORY-REFUSED "
TOKEN_LABEL = "factory-read"
# Every read flag of GlitchTip 6.2.6's APIToken.scopes (apps/api_tokens/models.py), and no write or admin flag.
READ_SCOPES = ("project:read", "team:read", "event:read", "org:read", "member:read")

_BODY = """
from django.contrib.auth import get_user_model
from django.db import transaction
from apps.api_tokens.models import APIToken
from apps.organizations_ext.models import Organization, OrganizationUserRole
from apps.projects.models import Project
from apps.teams.models import Team


def refuse(reason):
    print(REFUSED + reason)
    raise SystemExit(3)


def project_dsn(org, team, slug):
    project = Project.objects.filter(organization=org, slug=slug).first()
    if project is None:
        project = Project.objects.create(organization=org, slug=slug, name=slug)
        if project.slug != slug:
            refuse(f"GlitchTip named the new project {project.slug!r}, not {slug!r}; a deleted one may hold the name")
    project.teams.add(team)
    key = project.projectkey_set.filter(is_active=True).first()
    if key is None:
        key = project.projectkey_set.create()
    return key.get_dsn()


def organization(name):
    org = Organization.objects.filter(slug=name).first()
    if org is None:
        refuse(f"GlitchTip has no organization {name!r}: run the set-up, which makes it")
    team = Team.objects.filter(organization=org, slug=name).first()
    if team is None:
        refuse(f"GlitchTip's organization {name!r} has no team {name!r}: run the set-up, which makes it")
    return org, team


def owner(email, password):
    users = get_user_model()
    user = users.objects.filter(email=email).first()
    if user is None:
        return users.objects.create_superuser(email, password)
    if not user.check_password(password):
        user.set_password(password)
        user.save(update_fields=["password"])
    return user


def bootstrap():
    name = PARAMS["org"]
    user = owner(PARAMS["owner_email"], PARAMS["owner_password"])
    org = Organization.objects.filter(slug=name).first()
    if org is None:
        org = Organization.objects.create(name=name, slug=name)
        if org.slug != name:
            refuse(f"GlitchTip named the organization {org.slug!r}, not {name!r}; pick another product name")
    member = org.organization_users.filter(user=user).first()
    if member is None:
        member = org.add_user(user, role=OrganizationUserRole.OWNER)
    team, _ = Team.objects.get_or_create(organization=org, slug=name)
    team.members.add(member)
    token = APIToken.objects.filter(user=user, label=PARAMS["token_label"]).first()
    if token is None:
        token = APIToken.objects.create(user=user, label=PARAMS["token_label"])
    if sorted(token.get_scopes()) != sorted(PARAMS["scopes"]):
        token.scopes = 0
        for scope in PARAMS["scopes"]:
            setattr(token.scopes, scope, True)
        token.save(update_fields=["scopes"])
    return {"token": token.token, "factory_dsn": project_dsn(org, team, PARAMS["factory_project"])}


def workspace():
    org, team = organization(PARAMS["org"])
    return {"dsns": {name: project_dsn(org, team, slug) for name, slug in PARAMS["projects"].items()}}


with transaction.atomic():
    ANSWER = {"bootstrap": bootstrap, "workspace": workspace}[PARAMS["action"]]()
print(RESULT + json.dumps(ANSWER))
"""


def script(params: dict[str, Any]) -> str:
    """The whole script for one action, its parameters embedded as a JSON string literal."""
    head = (
        "import json\n"
        f"RESULT = {RESULT!r}\n"
        f"REFUSED = {REFUSED!r}\n"
        f"PARAMS = json.loads({json.dumps(params)!r})\n"
    )
    return head + _BODY


def bootstrap_params(org: str, owner_email: str, owner_password: str, factory_project: str) -> dict[str, Any]:
    return {
        "action": "bootstrap",
        "org": org,
        "owner_email": owner_email,
        "owner_password": owner_password,
        "token_label": TOKEN_LABEL,
        "scopes": list(READ_SCOPES),
        "factory_project": factory_project,
    }


def workspace_params(org: str, projects: dict[str, str]) -> dict[str, Any]:
    """``projects`` maps each errors name to its GlitchTip project slug."""
    return {"action": "workspace", "org": org, "projects": projects}


def answer(stdout: str) -> dict[str, Any] | str:
    """The script's result, or the reason it refused, read from the shell's output (which may hold other lines)."""
    for line in stdout.splitlines():
        if line.startswith(REFUSED):
            return line[len(REFUSED) :]
        if line.startswith(RESULT):
            try:
                found = json.loads(line[len(RESULT) :])
            # telemetry: surfaced — a result line that is not JSON is the reason `answer` returns
            except ValueError:
                return "GlitchTip's result line is not JSON"
            if isinstance(found, dict):
                return found
    return "GlitchTip's shell printed no result line"
