"""A name may not be claimed by both a shipped builtin and the app registry.

``list_registry`` deduplicates by NAME, and the precedence is fixed: builtin and
seed entries enter first, then user-configured registries, and a later row whose
name is already taken is skipped with **no diagnostic anywhere** -- no error, no
log line, no "an entry was hidden" notice.

So the moment an app is added under a name the other side already claims, the
losing entry stops appearing in the store on every machine running that wheel.
Its author cannot see why, and the only remedy is another release. This must fail
in review, while it is still one line in a diff, rather than after it ships.

The check is a set intersection, so it catches the collision from EITHER
direction: an app added to the registry under a builtin's name, or a builtin
renamed onto a name the registry already publishes.

The seed (``app-registry.json``) is the right thing to compare against rather
than the live catalog: a catalog ``git`` row is only kept when the seed or an
external registry also names it, so the seed is the offline-complete list of
third-party names that are actually installable, and reading it keeps this gate
deterministic and network-free.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.apps import registry
from kiro_crew.apps.discovery import _get_builtins_dir, discover_builtin_apps


def _seed_third_party_names() -> set[str]:
    """Names the bundled seed publishes, read straight from the shipped file."""
    path: Path = registry._REGISTRY_FILE
    assert path.is_file(), f"bundled seed missing at {path}"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(rows, list), "bundled seed must be a JSON array"
    return {r["name"] for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)}


def _shipped_builtin_names() -> set[str]:
    """Names this wheel's ``builtins/`` dir declares.

    Deliberately NOT ``execution.builtin_app_names()``: that consults
    ``installed.json`` to withdraw trust, so its answer depends on what the
    machine running the test happens to have installed. A gate must read the
    tree, not the desk.
    """
    return {
        app["name"]
        for app in discover_builtin_apps(_get_builtins_dir())
        if isinstance(app.get("name"), str)
    }


def test_builtin_and_registry_names_never_collide() -> None:
    builtins = _shipped_builtin_names()
    seeded = _seed_third_party_names()
    assert builtins, "no builtin apps discovered -- the gate would pass vacuously"
    assert seeded, "no seed rows read -- the gate would pass vacuously"

    collisions = sorted(builtins & seeded)
    assert not collisions, (
        f"name(s) {collisions} are claimed BOTH by a shipped builtin and by "
        f"{registry._REGISTRY_FILE.name}. Whichever side this change added, the result "
        "is the same: list_registry dedupes by name, the builtin/seed side wins, and "
        "the losing entry vanishes from the store on every machine with no error, no "
        "log line, and no notice to its author. Pick a different name, or remove the "
        "other claim deliberately in this same change."
    )
