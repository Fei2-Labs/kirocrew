"""Bring-your-own-key (BYOK) credential store for BYOK-capable ACP backends.

Some ACP backends can authenticate to a model provider with the operator's own
API key instead of the backend's built-in subscription — GitHub Copilot CLI's
``--acp`` mode is the first (see :data:`kiro_crew.acp.types.ACP_BACKENDS_BYOK`).
Those backends read the key from their own process environment (``OPENAI_API_KEY``,
``ANTHROPIC_API_KEY``, ...), so BYOK here is a small, owner-only key store that is
injected into the child's environment at spawn time and nowhere else.

Design constraints this module upholds:

- **The values are live credentials.** The store lives at ``<data-home>/byok.json``
  with owner-only mode (:func:`platform_compat.restrict_to_owner`), and that leaf
  is in :data:`security._CREW_SECRET_LEAVES`, so the agent's own file/bash tools
  can neither read nor overwrite it. A key never rides in ``os.environ`` on the
  gateway; it is read from disk and placed only in the spawned child's ``env``.
- **Keys become environment variable names**, so a name must be a valid env-var
  identifier. An invalid name is rejected on write and skipped on read rather
  than reaching a child process as a malformed variable.
- **The file wins over an inherited variable.** When BYOK is configured for a
  backend, the operator's explicit choice is authoritative, so injection
  overwrites any same-named variable already in the environment snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import MutableMapping

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

BYOK_FILENAME = "byok.json"
_STORE_VERSION = 1

# A stored key becomes an environment variable name for the child process, so it
# must be a POSIX-portable identifier: a letter or underscore followed by letters,
# digits, or underscores. Rejecting anything else on write keeps a name with an
# ``=``, whitespace, or a NUL out of the child's environ, where it could split a
# variable or be silently dropped.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Common provider variables, surfaced by the CLI as hints only. NOT an allowlist:
# a backend may read any variable the provider documents, so an unlisted name is
# accepted as long as it is a valid identifier.
KNOWN_KEY_HINTS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
)


class ByokError(ValueError):
    """A BYOK key name or value failed validation."""


def is_valid_key_name(name: str) -> bool:
    """True when *name* is a usable environment variable name."""
    return bool(_ENV_NAME_RE.match(name))


def byok_path() -> Path:
    """Absolute path to the BYOK store under the data home.

    Uses :func:`config.data_home` (resolve-only) rather than ``config_dir`` so a
    read on the spawn path does not trigger the start-of-process maintenance
    sweep. Imported lazily to keep this module off the heavy config import graph.
    """
    from kiro_crew.config import data_home

    return data_home() / BYOK_FILENAME


def load_byok_keys() -> dict[str, str]:
    """Return the stored ``name -> value`` mapping, or ``{}`` on any problem.

    Never raises: a missing file, unreadable file, malformed JSON, or unexpected
    shape all yield an empty mapping so the spawn path degrades to "no BYOK keys"
    rather than failing to launch. Entries with an invalid name or a non-string
    value are dropped individually and logged (without the value).
    """
    path = byok_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("BYOK store unreadable at %s", path, exc_info=True)
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("BYOK store at %s is not valid JSON; ignoring", path)
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        return {}
    result: dict[str, str] = {}
    for name, value in keys.items():
        if not isinstance(name, str) or not is_valid_key_name(name):
            logger.warning("BYOK store: dropping entry with invalid key name")
            continue
        if not isinstance(value, str):
            logger.warning("BYOK store: dropping non-string value for %s", name)
            continue
        result[name] = value
    return result


def save_byok_keys(keys: MutableMapping[str, str]) -> None:
    """Atomically write *keys* to the owner-only store.

    Validates every name and value before touching disk (so a bad entry cannot
    leave a half-written file), writes via a temp file + ``os.replace`` so a
    concurrent reader never sees a partial JSON, and locks the result to the
    owner. An empty mapping is written as an empty store rather than deleting the
    file, keeping the owner-only mode invariant on the path.
    """
    validated: dict[str, str] = {}
    for name, value in keys.items():
        if not is_valid_key_name(name):
            raise ByokError(
                f"invalid BYOK key name {name!r}: must be a valid environment "
                f"variable name ([A-Za-z_][A-Za-z0-9_]*)"
            )
        if not isinstance(value, str):
            raise ByokError(f"BYOK value for {name} must be a string")
        validated[name] = value

    path = byok_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _STORE_VERSION, "keys": validated}
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        try:
            platform_compat.restrict_to_owner(tmp)
        except OSError:
            logger.warning("could not restrict BYOK store permissions", exc_info=True)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    # os.replace preserves the temp file's mode on POSIX, but re-assert on the
    # final path so a pre-existing world-readable file (from an older write) is
    # tightened too.
    try:
        platform_compat.restrict_to_owner(path)
    except OSError:
        logger.warning("could not restrict BYOK store permissions", exc_info=True)


def set_byok_key(name: str, value: str) -> None:
    """Add or replace a single BYOK key, preserving the rest of the store."""
    if not is_valid_key_name(name):
        raise ByokError(
            f"invalid BYOK key name {name!r}: must be a valid environment "
            f"variable name ([A-Za-z_][A-Za-z0-9_]*)"
        )
    keys = load_byok_keys()
    keys[name] = value
    save_byok_keys(keys)


def remove_byok_key(name: str) -> bool:
    """Delete one key. Returns ``True`` if it existed, ``False`` otherwise."""
    keys = load_byok_keys()
    if name not in keys:
        return False
    del keys[name]
    save_byok_keys(keys)
    return True


def clear_byok_keys() -> None:
    """Remove every stored key, leaving an empty owner-only store."""
    save_byok_keys({})


def inject_byok_env(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Merge the stored BYOK keys into *env*, the file value winning.

    Called only for a backend in :data:`ACP_BACKENDS_BYOK`. The operator
    configured these keys specifically for this backend, so a stored value
    overwrites any inherited same-named variable in the environment snapshot. An
    empty value is skipped so an accidentally-blank entry does not shadow a real
    inherited key. Mutates *env* in place and returns it. Blocking file IO: call
    via ``asyncio.to_thread`` from async paths.
    """
    for name, value in load_byok_keys().items():
        if value:
            env[name] = value
    return env
