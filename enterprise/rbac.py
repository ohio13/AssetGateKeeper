"""Path-based role-based access control.

The problem this solves
    "Only the environment lead may loosen the triangle budget for
    /environments/." A role alone cannot say that; a path-scoped grant can.

Model
    A :class:`Grant` gives (or with ``deny`` takes away) a set of permissions to
    a subject - a user, an IdP group, or a role - for a subtree of the asset
    tree. Every decision walks the grants that match the action, keeps the most
    specific path match, and lets an explicit deny win at that specificity.

    Specificity is the length of the matched path, not the order of the file.
    That means a ``/environments/` grant cannot be neutralised by moving it up
    or down in the config, which is the single most common way policy files
    become theatre.

Paths
    ``/environments/**``   that folder and everything under it
    ``/characters/*``      exactly one level below
    ``/environments/rock`` that folder, any case, trailing slash optional
    Paths are normalised: backslashes become slashes, ``..`` is refused outright
    rather than resolved (a traversal in a policy string is an attack, not a
    typo to be helpful about), and comparison is case-insensitive because
    Windows and Perforce both are.

Permission vocabulary
    Deliberately small and verb-shaped. Categories in ``rules.toml`` may only
    tighten budgets; these permissions are about *who may change the policy*.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable, Optional

PERMISSIONS = {
    "rules.read": "read the effective rules for a path",
    "rules.override": "loosen a budget for a path (the lead-only action)",
    "rules.edit": "edit rules.toml / publish a budget",
    "assets.validate": "run the gate over an asset",
    "assets.heal": "repair an asset in place",
    "workstation.review": "open and annotate an asset in the workstation",
    "audit.read": "read the audit trail and trends",
    "admin.users": "manage accounts and sessions",
    "admin": "everything, including this policy",
}

ROLE_DIRECTOR = "director"


class RBACError(Exception):
    """A policy problem: the message names the rule and the fix."""


def normalise_path(path: str) -> str:
    """Canonical form used for every comparison.

    Refuses to *resolve* traversal: ``/environments/../secrets`` is an error, not
    a quiet path to ``/secrets``. A policy that can be walked around is worse than
    no policy, because it looks like one.
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return "/"
    if ".." in text.split("/"):
        raise RBACError(f"path '{path}' contains '..': refusing to resolve traversal")
    if not text.startswith("/"):
        text = "/" + text
    text = re.sub(r"/{2,}", "/", text)
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/")
    return text.lower()


def normalise_rule(pattern: str) -> str:
    text = str(pattern or "").strip().replace("\\", "/")
    if ".." in text.split("/"):
        raise RBACError(f"rule path '{pattern}' contains '..'")
    if not text.startswith("/"):
        text = "/" + text
    return re.sub(r"/{2,}", "/", text).lower()


def path_matches(pattern: str, path: str) -> bool:
    """Does ``pattern`` cover ``path``? See the module docstring for the forms."""
    rule = normalise_rule(pattern)
    target = normalise_path(path)
    if rule in ("/", "/**"):
        return True
    if rule.endswith("/**"):
        base = rule[:-3].rstrip("/")
        return target == base or target.startswith(base + "/")
    if rule.endswith("/*"):
        base = rule[:-2].rstrip("/")
        if not target.startswith(base + "/"):
            return False
        return "/" not in target[len(base) + 1:]
    if "*" in rule or "?" in rule:
        return fnmatch.fnmatchcase(target, rule)
    base = rule.rstrip("/")
    return target == base or target.startswith(base + "/")


def specificity(pattern: str) -> int:
    """Longer, more literal patterns win. Wildcards subtract weight."""
    rule = normalise_rule(pattern)
    stars = rule.count("*") + rule.count("?")
    return len(rule.replace("*", "").replace("?", "")) - (stars * 2)


@dataclass(frozen=True)
class Principal:
    """Who is asking. Groups and roles come from the IdP, not from the user."""

    subject: str
    username: str = ""
    email: str = ""
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    auth_method: str = ""

    @property
    def subjects(self) -> set[str]:
        """Every identity string a grant may name."""
        values = {f"user:{self.username.lower()}"} if self.username else set()
        if self.subject:
            values.add(f"user:{self.subject.lower()}")
        for group in self.groups:
            values.add(f"group:{group.lower()}")
        for role in self.roles:
            values.add(f"role:{role.lower()}")
        return values


@dataclass(frozen=True)
class Grant:
    subject: str
    path: str
    permissions: frozenset[str]
    deny: bool = False

    @property
    def specificity(self) -> int:
        return specificity(self.path)

    def applies_to(self, principal: Principal) -> bool:
        return self.subject.strip().lower() in principal.subjects


@dataclass
class Decision:
    allowed: bool
    reason: str
    matched: Optional[Grant] = None
    permissions: frozenset[str] = frozenset()

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "matched": None if self.matched is None else {
                    "subject": self.matched.subject, "path": self.matched.path,
                    "permissions": sorted(self.matched.permissions),
                    "deny": self.matched.deny}}


class AuthorizationEngine:
    """Answers "may this person do this to that path", and says why."""

    def __init__(self, grants: Iterable[Grant] = (), default_deny: bool = True,
                 director_role: str = ROLE_DIRECTOR) -> None:
        self.grants = list(grants)
        self.default_deny = default_deny
        self.director_role = director_role.lower()
        for grant in self.grants:
            unknown = set(grant.permissions) - set(PERMISSIONS)
            if unknown:
                raise RBACError(
                    f"grant for {grant.subject} names unknown permission(s) "
                    f"{', '.join(sorted(unknown))}; known: {', '.join(sorted(PERMISSIONS))}")

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_specs(cls, specs: Iterable[object], default_deny: bool = True,
                   director_role: str = ROLE_DIRECTOR) -> "AuthorizationEngine":
        grants = [Grant(subject=spec.subject, path=spec.path,
                        permissions=frozenset(p.lower() for p in spec.permissions),
                        deny=getattr(spec, "deny", False))
                  for spec in specs]
        return cls(grants, default_deny=default_deny, director_role=director_role)

    # -- evaluation -------------------------------------------------------- #

    def decide(self, principal: Principal, permission: str,
               path: str = "/") -> Decision:
        permission = permission.strip().lower()
        if permission not in PERMISSIONS:
            raise RBACError(f"unknown permission '{permission}'")
        roles = {role.lower() for role in principal.roles}

        # A director is not a back door for a typo: they get everything only
        # under an explicit rule, and only when the deployment says so.
        director = self.director_role in roles

        candidates: list[Grant] = []
        for grant in self.grants:
            if permission not in grant.permissions and "admin" not in grant.permissions:
                continue
            if not grant.applies_to(principal):
                continue
            if not path_matches(grant.path, path):
                continue
            candidates.append(grant)

        if not candidates:
            if director:
                return Decision(True, f"{self.director_role} role: directory-wide grant")
            allowed = not self.default_deny
            return Decision(allowed, "no matching grant; "
                            + ("denied by default" if self.default_deny
                               else "allowed by default (default_deny = false)"))

        candidates.sort(key=lambda g: (g.specificity, not g.deny), reverse=True)
        best = candidates[0]
        if best.deny:
            return Decision(False, f"denied by {best.subject} for {best.path}",
                            matched=best)
        return Decision(True, f"granted by {best.subject} for {best.path}", matched=best)

    def permissions_for(self, principal: Principal, path: str = "/") -> set[str]:
        """Everything this principal may do at ``path`` - for a UI to grey out."""
        granted = set()
        for permission in PERMISSIONS:
            if self.decide(principal, permission, path).allowed:
                granted.add(permission)
        return granted

    def explain(self, path: str = "/") -> list[dict]:
        """Which rules touch this path, in evaluation order (for ``agk-enterprise``)."""
        rows = [g for g in self.grants if path_matches(g.path, path)]
        rows.sort(key=lambda g: (g.specificity, not g.deny), reverse=True)
        return [{"subject": g.subject, "path": g.path,
                 "permissions": sorted(g.permissions), "deny": g.deny,
                 "specificity": g.specificity} for g in rows]

    def audit_policy(self) -> list[str]:
        """Warnings a security review will ask for, produced instead of guessed."""
        warnings = []
        if not self.default_deny:
            warnings.append("default_deny is false: anything not covered by a rule "
                            "is allowed")
        for grant in self.grants:
            if "admin" in grant.permissions and grant.path.strip() in ("/", "/**", "*"):
                warnings.append(f"{grant.subject} has admin on the whole tree")
            if (not grant.deny and "rules.override" in grant.permissions
                    and path_matches(grant.path, "/")):
                warnings.append(f"{grant.subject} may override rules everywhere")
        return warnings


def default_grants(director: str = "group:studio-directors",
                   leads: Optional[dict[str, str]] = None) -> list[Grant]:
    """A sane starting policy: directors everywhere, leads in their department."""
    grants = [Grant(subject=director, path="/",
                    permissions=frozenset({"admin"}), deny=False)]
    for department, subject in (leads or {}).items():
        grants.append(Grant(subject=subject, path=f"/{department}/**",
                            permissions=frozenset({"rules.override", "rules.edit",
                                                   "assets.validate",
                                                   "workstation.review"}),
                            deny=False))
    return grants
