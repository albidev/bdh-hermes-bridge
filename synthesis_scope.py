"""Fail-closed admission gate for session/room synthesis vault routing.

Background
----------
Synthesis is a *write* path: it can create notes in a vault. The previous
recovery path resolved a vault from the transcript text through the semantic
vault router overlay (``vault_router.suggest_vault``). That is a substring
match over an operator-maintained concept index, so any session that merely
*mentions* a client's vocabulary was routed into that client's vault — even
when the session was served by an unrelated profile and contained no client
work at all.

Topic is not authority. The actor is. This module authorises a synthesis
target from **who served the session / who is in the room**, never from what
the text talks about:

1. an explicit room registry entry (``room_vaults.json``), provided no member
   profile contradicts it;
2. the serving profile name, matched against configured profile prefixes;
3. the room's non-default member profiles, when they agree on one vault;
4. otherwise -> ``None``: no synthesis. There is deliberately no fallback.

The policy file is local and gitignored; mappings are operator config, not
repository content. Absent or invalid policy means "synthesize nothing",
which is the safe direction for a write path.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = "synthesis-policy.local.json"
POLICY_ENV = "BDH_SYNTHESIS_POLICY_FILE"
ROOM_REGISTRY_ENV = "BDH_ROOM_VAULT_REGISTRY"

# A room with no vault in the registry is not a synthesis scope by itself; the
# member-profile rule still applies, but only for profiles the operator listed.
_DEFAULT_PROFILE_NAMES = frozenset({"", "default", "none"})


@dataclass(frozen=True)
class SynthesisPolicy:
    """Authorised profile-prefix -> vault mappings plus room-registry opt-in."""

    version: int = 1
    profile_vaults: Mapping[str, str] = field(default_factory=dict)
    allow_room_registry: bool = True
    room_registry_path: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.profile_vaults) or (self.allow_room_registry and bool(self.room_registry_path))


_DISABLED = SynthesisPolicy(profile_vaults={}, allow_room_registry=False)


def _policy_path() -> Path:
    """Resolve the policy file independently of the process working directory.

    A daemon's CWD is not a stable contract, so the fallback is anchored to
    this module's directory. ``BDH_SYNTHESIS_POLICY_FILE`` overrides it.
    """
    raw = os.environ.get(POLICY_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / DEFAULT_POLICY_PATH


def _registry_path(policy: SynthesisPolicy) -> Path | None:
    raw = os.environ.get(ROOM_REGISTRY_ENV, "").strip() or policy.room_registry_path
    if not raw:
        return None
    return Path(raw).expanduser()


def load_policy() -> SynthesisPolicy:
    """Load the local synthesis policy; disabled unless explicitly valid."""
    path = _policy_path()
    if not path.exists():
        logger.debug("[synthesis-scope] policy missing at %s — synthesis disabled", path)
        return _DISABLED
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("[synthesis-scope] invalid policy (%s) — synthesis disabled", exc)
        return _DISABLED
    if not isinstance(data, dict):
        logger.warning("[synthesis-scope] policy must be a JSON object — synthesis disabled")
        return _DISABLED

    raw_map = data.get("profile_vaults") or {}
    if not isinstance(raw_map, dict):
        logger.warning("[synthesis-scope] profile_vaults must be an object — ignored")
        raw_map = {}

    profile_vaults: dict[str, str] = {}
    for prefix, vault in raw_map.items():
        prefix_text = str(prefix or "").strip().casefold()
        vault_text = str(vault or "").strip()
        if not prefix_text or not vault_text:
            continue
        profile_vaults[prefix_text] = vault_text

    return SynthesisPolicy(
        version=int(data.get("version") or 1),
        profile_vaults=profile_vaults,
        allow_room_registry=bool(data.get("allow_room_registry", True)),
        room_registry_path=str(data.get("room_registry_path") or "").strip(),
    )


def load_room_registry(policy: SynthesisPolicy) -> dict[str, str]:
    """Load the explicit room_id -> vault registry, if configured."""
    if not policy.allow_room_registry:
        return {}
    path = _registry_path(policy)
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(room_id): str(vault).strip()
        for room_id, vault in data.items()
        if str(room_id).strip() and str(vault or "").strip()
    }


def vault_for_profile(profile_name: Any, policy: SynthesisPolicy) -> str | None:
    """Map a serving profile name to a vault by configured prefix."""
    name = str(profile_name or "").strip().casefold()
    if name in _DEFAULT_PROFILE_NAMES:
        return None
    for prefix, vault in policy.profile_vaults.items():
        if name == prefix or name.startswith(f"{prefix}-"):
            return vault
    return None


def _member_profiles(members: Sequence[Any] | None) -> list[str]:
    profiles: list[str] = []
    for member in members or ():
        if isinstance(member, Mapping):
            value = member.get("profile")
        else:
            value = member
        name = str(value or "").strip().casefold()
        if name and name not in _DEFAULT_PROFILE_NAMES:
            profiles.append(name)
    return profiles


def resolve_synthesis_vault(
    *,
    session_profile: Any = None,
    room_id: Any = None,
    room_members: Sequence[Any] | None = None,
    policy: SynthesisPolicy | None = None,
    registry: Mapping[str, str] | None = None,
) -> str | None:
    """Return the authorised vault for a synthesis target, or ``None``.

    ``None`` means "do not synthesize". Callers must never substitute a
    semantic/textual guess for a missing authorisation.
    """
    active = policy if policy is not None else load_policy()
    if not active.configured:
        return None

    if registry is not None:
        # The policy decides whether the room registry is an authority at all.
        rooms = dict(registry) if active.allow_room_registry else {}
    else:
        rooms = load_room_registry(active)
    member_vaults = {
        vault
        for vault in (vault_for_profile(name, active) for name in _member_profiles(room_members))
        if vault
    }
    # A room with any unauthorised (default) participant is not a client scope:
    # its transcript carries non-client traffic, so it is never synthesized.
    mixed_room = _has_default_member(room_members)

    room_key = str(room_id or "").strip()
    if room_key and room_key in rooms and not mixed_room:
        registry_vault = rooms[room_key]
        # The serving profile wins when it is itself authorised, so a single
        # profile's work is never labelled with another vault's identity.
        serving = vault_for_profile(session_profile, active)
        if serving is not None:
            if member_vaults and registry_vault not in member_vaults:
                logger.warning(
                    "[synthesis-scope] room %s registry=%s contradicts member profiles %s — skipped",
                    room_key, registry_vault, sorted(member_vaults),
                )
                return None
            return serving
        # No authorised serving profile (e.g. a driverless room): the registry
        # entry is the authorisation, but only while no member profile says
        # otherwise. Ambiguity is never resolved by guessing.
        if member_vaults and member_vaults != {registry_vault}:
            logger.warning(
                "[synthesis-scope] room %s member profiles %s disagree with registry=%s — skipped",
                room_key, sorted(member_vaults), registry_vault,
            )
            return None
        return registry_vault

    serving = vault_for_profile(session_profile, active)
    if serving is not None:
        return serving

    if len(member_vaults) == 1 and not mixed_room:
        return next(iter(member_vaults))

    return None


def _has_default_member(members: Sequence[Any] | None) -> bool:
    """True when a room has any non-authorised (default) participant.

    A mixed room is not a client scope: its transcript contains non-client
    traffic, so it must not be synthesized into the client's vault.
    """
    for member in members or ():
        value = member.get("profile") if isinstance(member, Mapping) else member
        if str(value or "").strip().casefold() in _DEFAULT_PROFILE_NAMES:
            return True
    return False
