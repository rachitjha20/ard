"""Central policy engine.

Every operation, from every interface (UI, REST, CLI, MCP, workflows), is
authorised here before it runs. Interfaces differ only in the scopes they are
granted; they never carry their own rules.

Text found inside documents is data. Nothing in this module reads document
content, so a PDF cannot alter a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionKind(str, Enum):
    READ = "read"
    WRITE = "write"  # creates new versions/documents, never removes anything
    DESTRUCTIVE = "destructive"  # hides or removes user-visible data
    ADMIN = "admin"  # changes application behaviour


@dataclass(frozen=True)
class ActionSpec:
    name: str
    kind: ActionKind
    audited: bool = True
    # Sensitive actions are disabled while the application is in safe mode.
    sensitive: bool = False


_ACTIONS: dict[str, ActionSpec] = {}


def _register(name: str, kind: ActionKind, *, audited: bool = True, sensitive: bool = False) -> None:
    _ACTIONS[name] = ActionSpec(name, kind, audited, sensitive)


# Reads are not audited individually; they would drown the audit log.
for _name in (
    "document.list",
    "document.read",
    "document.download",
    "system.read",
    "job.read",
    "audit.read",
):
    _register(_name, ActionKind.READ, audited=False)

_register("document.export", ActionKind.READ)
_register("document.import", ActionKind.WRITE)
_register("document.update", ActionKind.WRITE)
_register("document.restore", ActionKind.WRITE)
_register("document.revert", ActionKind.WRITE, sensitive=True)
_register("document.merge", ActionKind.WRITE, sensitive=True)
_register("document.split", ActionKind.WRITE, sensitive=True)
_register("document.operate", ActionKind.WRITE, sensitive=True)
_register("document.trash", ActionKind.DESTRUCTIVE)
_register("job.cancel", ActionKind.WRITE)
_register("system.configure", ActionKind.ADMIN)
_register("audit.verify", ActionKind.READ)


# Scopes granted to each interface by default. MCP is read-only until the user
# grants more; plugins and workflows start with nothing.
_READ = {ActionKind.READ}
_ALL = {ActionKind.READ, ActionKind.WRITE, ActionKind.DESTRUCTIVE, ActionKind.ADMIN}
DEFAULT_SCOPES: dict[str, set[ActionKind]] = {
    "ui": _ALL,
    "api": _ALL,
    "cli": _ALL,
    "mcp": _READ,
    "workflow": _READ,
    "plugin": set(),
    "system": _ALL,
}


@dataclass(frozen=True)
class Actor:
    name: str = "local-user"
    interface: str = "ui"
    # Set when the user has explicitly confirmed a destructive action.
    approved: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    action: ActionSpec | None = None
    requires_approval: bool = False


class PolicyDenied(Exception):
    def __init__(self, decision: Decision, action: str) -> None:
        super().__init__(decision.reason)
        self.decision = decision
        self.action = action


@dataclass
class PolicyEngine:
    scopes: dict[str, set[ActionKind]] = field(default_factory=lambda: {k: set(v) for k, v in DEFAULT_SCOPES.items()})
    safe_mode: bool = False
    safe_mode_reason: str | None = None

    def enter_safe_mode(self, reason: str) -> None:
        self.safe_mode = True
        self.safe_mode_reason = reason

    def leave_safe_mode(self) -> None:
        self.safe_mode = False
        self.safe_mode_reason = None

    def evaluate(self, actor: Actor, action: str, *, document_trashed: bool = False) -> Decision:
        spec = _ACTIONS.get(action)
        if spec is None:
            # Unknown actions are denied: nothing runs without a declared policy.
            return Decision(False, f"Action '{action}' is not registered with the policy engine.")

        granted = self.scopes.get(actor.interface)
        if granted is None:
            return Decision(False, f"Interface '{actor.interface}' is not recognised.", spec)
        if spec.kind not in granted:
            return Decision(
                False,
                f"The {actor.interface} interface does not have the '{spec.kind.value}' scope needed for {action}.",
                spec,
            )

        if self.safe_mode and (spec.sensitive or spec.kind in (ActionKind.DESTRUCTIVE, ActionKind.ADMIN)):
            if action != "system.configure":
                return Decision(
                    False,
                    "ARD is in safe mode, so editing is disabled. Viewing and exporting still work. "
                    f"Reason: {self.safe_mode_reason}",
                    spec,
                )

        if document_trashed and spec.kind is ActionKind.WRITE and action != "document.restore":
            return Decision(False, "This document is in the trash. Restore it before changing it.", spec)

        if spec.kind is ActionKind.DESTRUCTIVE and not actor.approved:
            return Decision(
                False,
                f"{action} needs explicit confirmation from the user.",
                spec,
                requires_approval=True,
            )

        return Decision(True, "allowed", spec)

    def authorize(self, actor: Actor, action: str, *, document_trashed: bool = False) -> ActionSpec:
        decision = self.evaluate(actor, action, document_trashed=document_trashed)
        if not decision.allowed:
            raise PolicyDenied(decision, action)
        assert decision.action is not None
        return decision.action
