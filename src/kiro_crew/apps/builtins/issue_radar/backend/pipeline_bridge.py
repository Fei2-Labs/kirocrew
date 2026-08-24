"""Bridge an external pipeline's event log onto the crew ledger's stage model.

WHAT THIS IS FOR. Kiro Crew's own GitHub automation runs as a set of scheduled
scripts that append structured events to their own audit log: ``scan``,
``triage``, ``implement_start``, ``pr_opened``, ``pr_green``, ``implement_done``
and so on. That log records WHAT HAPPENED. It does not record WHERE EACH ITEM
IS, and those are different questions: a reader can reconstruct the history from
it, but nothing can answer "which stage is issue #123 in, and how long has it sat
there" without replaying every line and knowing what each event implies.

The crew ledger already answers exactly that question — an ordered phase spine
(:data:`~crew_store.PHASES`), one current phase per work item, one appended line
per transition, and terminal exits off the spine. The Auto Triage Pipeline board
draws that shape. So the gap between "an automation that works" and "an
automation you can see" is not a missing UI and not a missing ledger: it is one
missing layer of semantics on events that are already being written.

This module is that layer. It takes an event log plus a STAGE TABLE — a mapping
from the pipeline's own event names to phases on the spine — and replays it into
the crew ledger. The board then draws the pipeline with no frontend change at
all, because it is reading the shape it already knows.

WHY A TABLE AND NOT A WALK. The mapping is data, declared by whoever configures
the pipeline, not a chain of ``if`` branches in here. That is the whole point of
the exercise: a second pipeline with different stage names ships a different
table and needs no code in this module. :data:`GH_AUTOFIX_STAGES` is the first
such table and describes Kiro Crew's own automation; it is an example of the
contract, not a privileged case.

WHAT THIS DELIBERATELY DOES NOT DO.

* It registers no route and no MCP tool. It is a library plus a ``main`` for
  script use, so it adds nothing to any always-on surface and costs an agent
  session no context.
* It never writes to the source log. Replay is one-directional, so a bridge that
  is wrong can be re-run after a fix without having corrupted its input.
* It does not invent a phase. An event whose name is absent from the table is
  skipped and counted, never guessed at — a table that is missing an event is a
  reported gap rather than a lane that quietly drifts to the wrong column.

WHAT A HISTORICAL REPLAY CANNOT GIVE YOU. The ledger stamps its own clock, so a
replay of a log written over days produces lanes whose STAGES are right and whose
DWELL is not: every transition lands at replay time, and the board's "how long
has this sat here" reads as minutes. That is a property of replaying history, not
a defect to patch here — passing the source event's ``ts`` through would let a
caller backdate the ledger, which is a write the ledger deliberately does not
offer. The target shape is a LIVE bridge: the pipeline calls :func:`record_stage`
as each event happens, and then the dwell is real because the clock is. Replay
exists to make an existing log drawable at all, and to prove the table before
anything is wired live.

ONE CREW PER WORK ITEM, AND WHY. ``crew_store`` refuses a second item entering
an editing phase within one crew, because a crew is one worker and a worker
edits one thing at a time. The automation this bridges runs its fix sessions
CONCURRENTLY — several items can legitimately be in ``implementing`` at once —
so mapping the whole pipeline onto a single crew would hit that refusal on the
second concurrent item. Each session is an independent worker on one item, so
the faithful mapping is one crew per work item. The board draws one lane per
work item either way, and the per-crew editing cap then reports one editing item
per crew, which is the truth rather than a fabricated conflict.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from kiro_crew import hooks, platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform.context import redact_via_context

from . import crew_store


class BridgeError(RuntimeError):
    """A stage table or an event log that cannot be honoured as given."""


#: One stage-table entry: the phase an event moves the item to, and the ledger
#: event kind to record it under. ``kind`` must be one of
#: :data:`crew_store.EVENT_KINDS` — the ledger's vocabulary is closed, and a
#: bridge is not the place to widen it.
StageRule = tuple[str, str]

#: Kiro Crew's own GitHub automation, as a table. The keys are that pipeline's
#: audit event names (see its ``gh_audit`` module's docstring); the values say
#: which phase the item has reached and under which ledger kind to record it.
#:
#: ``cleanup`` is absent on purpose: it is housekeeping that runs after an item
#: has already reached a terminal phase, so binding it to a phase would move a
#: finished lane. ``scan`` maps to ``selected``, which the store treats as
#: local-only and never publishes.
GH_AUTOFIX_STAGES: Mapping[str, StageRule] = {
    "scan": ("selected", "claim"),
    "label": ("claimed", "claim"),
    "triage": ("claimed", "investigate"),
    "implement_queued": ("claimed", "claim"),
    "answer": ("awaiting-reply", "reply"),
    "implement_start": ("implementing", "implement"),
    "implement_resume": ("implementing", "implement"),
    "push": ("implementing", "implement"),
    "rebase_push": ("implementing", "implement"),
    # Both spellings appear in the live log (`pr_opened` 83, `pr_open` 2). The
    # drift is exactly why the mapping is data: a table absorbs it, a chain of
    # `if` branches in code would have to be edited to notice it.
    "pr_opened": ("awaiting-ci", "ci"),
    "pr_open": ("awaiting-ci", "ci"),
    "side_fix_pr_opened": ("awaiting-ci", "ci"),
    "review_round": ("addressing-review", "review"),
    "review_fix_round": ("addressing-review", "review"),
    "review_findings_fixed": ("addressing-review", "review"),
    "review_done": ("awaiting-ci", "review"),
    "gates_green": ("awaiting-ci", "ci"),
    "pr_green": ("awaiting-merge", "merge"),
    "review_ready": ("awaiting-merge", "review"),
    "implement_done": ("resolved", "merge"),
    "implement_fail": ("handed-back", "handback"),
    "escalate": ("awaiting-reply", "reply"),
    "skip": ("skipped", "skip"),
    "yield": ("yielded", "yield"),
    "stand_down": ("yielded", "yield"),
    "blocked_on_precondition": ("awaiting-reply", "reply"),
    "blocked_on_dependency": ("awaiting-reply", "reply"),
}


#: The label every bridge-created crew carries. It is DOCUMENTATION -- it tells a
#: human reading the crew list where that lane came from -- and deliberately NOT
#: the ownership test. Every field ``crew_store._validated_crew_patch`` accepts is
#: user-editable, ``labels`` included, so a label can be assigned to any crew from
#: the ordinary editor: treating it as authorization would let someone name a crew
#: ``<pipeline>#<n>``, add this label, and have the bridge adopt and rewrite their
#: work item and ledger. Ownership lives in :func:`_owned_ids` instead, a file only
#: this module writes.
_BRIDGE_LABEL = "pipeline-bridge"

#: Crew ids this module created, per repo. Kept beside the crews it describes so a
#: deleted data root takes its ownership record with it, and written only here --
#: which is the whole point: an editable crew FIELD cannot express ownership, so
#: the record has to live outside the surface the crew editor can reach.
_OWNED_FILE = "pipeline-bridge-owned.json"


def _owned_path(owner: str, repo: str, root: Path | None) -> Path:
    return crew_store.crews_dir(owner, repo, root) / _OWNED_FILE


def _owned_ids(owner: str, repo: str, root: Path | None) -> dict[str, str]:
    """``{crew name: crew id}`` for the crews this module created.

    A malformed or hand-edited index degrades to "own nothing" rather than
    raising: the safe direction is to refuse a name we cannot prove we own, which
    surfaces as a refusal for that item instead of adopting a stranger's crew.
    """
    path = _owned_path(owner, repo, root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _is_reclaimable_orphan(
    owner: str, repo: str, crew: Mapping[str, Any], root: Path | None
) -> bool:
    """Whether an unowned same-named crew is safely this module's to reclaim.

    Three conditions, and all three matter. The label says it was probably ours.
    Inertness says nothing has been driving it. NO WORK ITEMS is the one that makes
    reclaiming safe rather than merely likely-correct: the harm the ownership check
    prevents is overwriting a crew that holds real state, and an empty crew holds
    none -- so a forged match wins an attacker nothing, while any crew with a
    single work item is still refused.
    """
    if _BRIDGE_LABEL not in (crew.get("labels") or []):
        return False
    if any(crew.get(flag) is not False for flag in _INERT_CREW):
        return False
    try:
        items = crew_store.list_work_items(owner, repo, str(crew["id"]), root)
    except (crew_store.CrewStoreError, OSError):
        return False
    return not items


def _remember_owned(owner: str, repo: str, root: Path | None, name: str, crew_id: str) -> None:
    """Add one entry under an exclusive lock.

    The lock is NOT the same question as the one declined for crew creation. There,
    ``crew_store`` already serializes name-uniqueness inside its own lock, so a
    second lock would duplicate an authority this module does not hold. Here the
    file IS this module's, so its read-modify-write is this module's to make
    atomic: without the lock two concurrent recorders both read, both write, and
    one entry is lost -- after which that item's next stage is refused and its lane
    stalls.
    """
    path = _owned_path(owner, repo, root)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            owned = _owned_ids(owner, repo, root)
            owned[name] = crew_id
            atomic_write(path, json.dumps(owned, indent=2))


#: The crew fields a bridge crew must NEVER inherit from ``_DEFAULT_CREW``.
#:
#: A crew record is not inert. ``crew_runtime`` drives any crew for which
#: ``unattended and is_live(crew)`` holds, and the store's defaults are
#: ``enabled: True, unattended: True, auto_merge: True,
#: auto_resolve_conflicts: True``. So creating a crew with only a name — which is
#: all a drawing surface needs — mints a worker that will be picked up, run
#: auto-approved, and allowed to merge and resolve conflicts on the repository.
#: Replaying a log of N items would mint N of them.
#:
#: A bridge crew exists so a lane can be DRAWN. It must never be drivable, and
#: that has to hold by construction rather than by the caller remembering: these
#: flags are applied on every create in this module and pinned by a test.
_INERT_CREW: dict[str, bool] = {
    "enabled": False,
    "unattended": False,
    "auto_merge": False,
    "auto_resolve_conflicts": False,
}


def _safe_text(value: Any, fallback: str) -> str:
    """Ledger text for one event: redacted first, then truncated.

    The source log is arbitrary external input written by whatever automation
    owns it, and its detail fields carry URLs and command output — so a
    credential can reach this function. The ledger line is served back to the
    dashboard's work log, which makes this an egress site, so it routes through
    the canonical redaction shim.

    Redact BEFORE truncating: truncating first can cut a token in half and leave
    a fragment the redactor no longer recognises, storing part of the secret.
    """
    text = str(value if value not in (None, "") else fallback)
    return redact_via_context(text)[:200]


def _printable(name: str) -> str:
    """An event name safe to write to a terminal.

    Event names come from an external log, and ``main`` prints them. A name
    carrying ANSI or OSC escapes would be INTERPRETED by the operator's terminal
    rather than shown -- so the report becomes a way for whatever wrote that log
    to drive the terminal of whoever inspects it. ``ascii()`` renders escapes as
    their literal source form, which is also what an operator needs to see to
    recognise a hostile line.
    """
    return ascii(redact_via_context(name))[1:-1][:120]


def coverage_report(
    events: Iterable[Mapping[str, Any]],
    table: Mapping[str, StageRule],
) -> dict[str, Any]:
    """Which of a log's event names the table maps, and which it does not.

    Configuring a pipeline means writing its table, and the failure mode is a
    stage-bearing event nobody mapped: the lane silently stops advancing while
    the automation keeps working, which reads as a broken board rather than an
    incomplete table. This turns that into a list. It writes nothing and is safe
    to run against a live log.

    ``unmapped`` is ordered by volume because that is the order worth reading:
    the highest-count unmapped name is either the most important gap or — as with
    this pipeline's ``scan_skip_claimed`` — the clearest evidence that the event
    is housekeeping and belongs unmapped.
    """
    mapped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for rec in events:
        kind = str(rec.get("event") or rec.get("kind") or "").strip()
        if not kind:
            continue
        bucket = mapped if kind in table else unmapped
        bucket[kind] = bucket.get(kind, 0) + 1
    return {
        "mapped": dict(sorted(mapped.items(), key=lambda kv: -kv[1])),
        "unmapped": dict(sorted(unmapped.items(), key=lambda kv: -kv[1])),
        "mapped_events": sum(mapped.values()),
        "unmapped_events": sum(unmapped.values()),
    }


def validate_stage_table(table: Mapping[str, StageRule]) -> None:
    """Refuse a table that names a phase or kind the ledger does not have.

    Checked up front rather than at the first offending event: a replay that
    dies halfway leaves a partially-populated board, which is worse than one
    that refuses to start, because the half is indistinguishable from a real
    pipeline that stopped there.
    """
    if not table:
        raise BridgeError("a stage table needs at least one entry")
    for event, rule in table.items():
        if not isinstance(rule, tuple) or len(rule) != 2:
            raise BridgeError(f"stage rule for {event!r} must be (phase, kind)")
        phase, kind = rule
        if phase not in crew_store.PHASES:
            raise BridgeError(f"stage rule for {event!r} names unknown phase {phase!r}")
        if kind not in crew_store.EVENT_KINDS:
            raise BridgeError(f"stage rule for {event!r} names unknown event kind {kind!r}")


def read_event_log(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read a JSONL event log. Returns ``(events, unparseable_line_count)``.

    The count is the point of the tuple. An append interrupted mid-write leaves a
    line with no newline, and the NEXT append concatenates onto it -- so one
    malformed line can swallow a real transition, and skipping it silently makes
    a lane show a stale phase with nothing to say why. Returning the count lets
    every caller report the loss, which is what turns a silent gap into a visible
    one.

    Malformed lines are still SKIPPED rather than fatal. A torn line must not hide
    the history in front of it -- the same discipline ``crew_store.read_events``
    applies to its own ledger -- and refusing the whole log over one corrupt byte
    would make a 3,000-line pipeline undrawable for a defect that cost it a single
    transition. Visibility is the fix here; refusal would be a worse trade.

    The path is operator-supplied (``main`` takes it as an argument), so the read
    goes through ``hooks.safe_read_file_bytes`` rather than ``Path.read_text``:
    that is the centralized ``is_sensitive_path`` enforcement, and it canonicalizes
    via realpath and opens with ``O_NOFOLLOW``, so a path -- or a symlink -- aimed
    at a credential store is refused before any byte is read instead of ending up
    in a ledger line. A new read path that skips the central gate is a bypass even
    when the operator owns the machine.

    A log that outgrows the reader's safety cap raises
    :class:`hooks.FileTooLargeError` from that helper, which is re-raised here as
    a :class:`BridgeError` naming the file: an append-only log grows without
    bound, so this is a state every long-lived pipeline reaches, and an operator
    running the script deserves a sentence rather than a traceback.
    """
    try:
        raw = hooks.safe_read_file_bytes(str(path))
    except hooks.FileTooLargeError as exc:
        raise BridgeError(f"{path} is too large to replay in one pass: {exc}") from exc
    if raw is None:
        return [], 0
    text = raw.decode("utf-8", errors="replace")
    out: list[dict[str, Any]] = []
    unparseable = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            # ValueError COVERS json.JSONDecodeError (it is a subclass -- naming
            # both trips this repo's redundancy ratchet, see #5287), and the wider
            # clause is required rather than merely tidy: a bare integer token
            # longer than CPython's int/str conversion limit makes json.loads
            # raise a PLAIN ValueError from its internal int(), and deeply nested
            # JSON raises RecursionError, which is not a ValueError at all. Either
            # one would escape this loop, pass main's refusal guard untouched, and
            # abort the whole read as a traceback -- losing every transition over
            # one hostile line, the invariant this function exists to hold.
            # _item_number guards the same limit on its string path; this is the
            # sibling path.
            unparseable += 1
            continue
        if isinstance(rec, dict):
            out.append(rec)
        else:
            unparseable += 1
    return out, unparseable


#: Largest item number the bridge will accept, and the digit budget that bounds
#: the CONVERSION as well as the value. Issue and PR numbers are small monotonic
#: counters, so anything past this is a malformed or hostile log line.
#:
#: The digit cap matters independently: ``int()`` raises on a digit string past
#: CPython's int/str conversion limit, so a value check alone runs too late --
#: the conversion has already thrown. Bounding the string first is what makes the
#: guard reachable.
_MAX_ITEM_NUMBER = 10_000_000
_MAX_ITEM_DIGITS = 9


def _item_number(rec: Mapping[str, Any]) -> int | None:
    """The issue or PR this event is about, or None when it is about neither.

    Accepts either key because the two script families that write these logs
    disagree: the issue pipeline stamps ``issue`` and the PR pipeline stamps
    ``pr``. Reading both here is what lets one table serve both without either
    script changing.

    Never raises. Every rejection path returns None so one crafted line cannot
    abort a replay that has already written earlier items -- the half-populated
    board this module exists to avoid. Two traps make that harder than it looks:

    * ``str.isdigit()`` is TRUE for characters ``int()`` refuses -- ``"\u2460"``,
      ``"\u00b2"`` and every other Unicode category-No digit -- so the test must
      be ``isdecimal()``, which is exactly what ``int()`` accepts.
    * a digit string longer than CPython's int/str conversion limit raises inside
      ``int()`` itself, so the length is checked BEFORE converting.
    """
    for key in ("issue", "pr", "number"):
        val = rec.get(key)
        if isinstance(val, bool):
            continue
        num: int | None = None
        if isinstance(val, int):
            num = val
        elif isinstance(val, str):
            text = val.strip().lstrip("#")
            if text.isdecimal() and len(text) <= _MAX_ITEM_DIGITS:
                num = int(text)
        if num is not None and 0 < num <= _MAX_ITEM_NUMBER:
            return num
    return None


def _crew_name_for(pipeline: str, number: int) -> str:
    """The crew standing in for the worker that handled this item.

    One per item, for the reason the module docstring gives: the source pipeline
    works several items at once and the store's editing cap is per crew.
    """
    return f"{pipeline}#{number}"


def record_stage(
    owner: str,
    repo: str,
    number: int,
    event: str,
    *,
    pipeline: str,
    table: Mapping[str, StageRule],
    text: str = "",
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Record ONE stage transition as it happens. The live path.

    This is the shape a pipeline should call — at the moment it emits its own
    event — because then the ledger's clock is the event's clock and the board's
    dwell is real. :func:`replay` is the same mapping applied to history, and
    exists to prove a table before anything is wired live.

    Returns ``None`` when the table does not name ``event``: an unmapped event is
    not an error, it is housekeeping. Raises :class:`BridgeError` for a table
    that names a phase or kind the ledger does not have, and lets
    ``crew_store``'s own refusals surface — a caller recording a stage the store
    rejects wants to know, not to have it swallowed.
    """
    validate_stage_table(table)
    rule = table.get(str(event).strip())
    if rule is None:
        return None
    phase, ledger_kind = rule
    crew_id = _ensure_crew(owner, repo, pipeline, int(number), root)
    return crew_store.commit_work_progress(
        owner,
        repo,
        crew_id,
        int(number),
        {"phase": phase},
        ledger_kind,
        _safe_text(text, str(event)),
        root=root,
    )


def _ensure_crew(owner: str, repo: str, pipeline: str, number: int, root: Path | None) -> str:
    """The crew id standing in for this item's worker, created on first use.

    Reuse is keyed on the STAMP, not on the name. A name-only match would adopt a
    crew a human happens to have called ``<pipeline>#<number>`` and then overwrite
    its work item and ledger -- the bridge would corrupt real crew state it did
    not create. So a bridge crew carries :data:`_BRIDGE_LABEL` in its ``labels``,
    only a record carrying that label is ever reused, and a same-named crew
    without it is refused loudly rather than silently taken over.
    """
    name = _crew_name_for(pipeline, number)
    owned = _owned_ids(owner, repo, root)
    crews = {
        str(c.get("id")): c for c in crew_store.list_crews(owner, repo, root, include_retired=True)
    }

    # Resolve by ID, never by the crew's CURRENT name. The index key is derived
    # from pipeline+number and so is stable, but a crew's name is editable: a
    # rename in the Crew Editor would make a name match fail, and the bridge would
    # mint a SECOND crew for the same item and split its history in two.
    owned_id = owned.get(name)
    if owned_id and owned_id in crews:
        return owned_id

    # Nothing owned under this key. A crew already wearing the name is either a
    # stranger's -- refuse rather than adopt -- or an ORPHAN this module created and
    # then failed to record, because `create_crew` and `_remember_owned` are two
    # writes and a full disk can land between them. The store has no delete
    # (`retire_crew` keeps the name reserved on purpose, since a retired crew's
    # name still appears in its work log and its GitHub comments), so a rollback
    # cannot free the name and the orphan would block that item forever.
    #
    # So an orphan is RECLAIMED, under conditions that make reclaiming safe: it
    # must carry the bridge label, be fully inert, and hold NO work items. The harm
    # the ownership check exists to prevent is rewriting a crew that has real
    # state; an empty inert crew has none to lose, which is why a forgery matching
    # this shape costs nothing while a genuine crew is still protected.
    for crew in crews.values():
        if str(crew.get("name") or "") != name:
            continue
        if _is_reclaimable_orphan(owner, repo, crew, root):
            _remember_owned(owner, repo, root, name, str(crew["id"]))
            return str(crew["id"])
        raise BridgeError(
            f"a crew named {name!r} already exists and was not created by the "
            "bridge - rename it or replay under a different --pipeline name"
        )
    try:
        crew = crew_store.create_crew(
            owner, repo, {"name": name, "labels": [_BRIDGE_LABEL], **_INERT_CREW}, root
        )
    except crew_store.CrewStoreError as exc:
        # THE serialization point, and it is the store's, not ours. `create_crew`
        # checks name-uniqueness and writes inside one exclusive file lock, so a
        # concurrent replay that slipped past this function's own preflight loses
        # the race HERE and is told so. Re-raising as a refusal is what makes the
        # populated-root guard safe under concurrency without a second lock: the
        # loser refuses that item, the summary reports it, and the command exits
        # nonzero. A lock of our own would duplicate an authority we do not hold
        # and could not make atomic with the store's write anyway.
        raise BridgeError(f"lost a race creating crew {name!r}: {exc}") from exc
    _remember_owned(owner, repo, root, name, str(crew["id"]))
    return str(crew["id"])


def replay(
    owner: str,
    repo: str,
    events: Iterable[Mapping[str, Any]],
    *,
    pipeline: str,
    table: Mapping[str, StageRule],
    root: Path | None = None,
) -> dict[str, Any]:
    """Replay an event log into the crew ledger. Returns a counted summary.

    Events are applied in the order given, so the last stage an item reached is
    the phase it ends on — the ledger's own rule that the live phase is the
    record's, not the furthest column touched. An event the table does not name
    is counted in ``skipped_kinds`` and applied to nothing.

    Idempotence is the source log's to provide, not this function's: replaying
    the same log twice writes the same phases (the final state is a function of
    the events) but appends the ledger lines again. Callers that re-run against
    a growing log should replay into a fresh root, which is what the script
    entry point does.
    """
    validate_stage_table(table)
    # ONE operation, guard and loop together. The precondition below reads state
    # that the loop then writes, so checking and writing under separate locks is a
    # TOCTOU: two replays both see an empty root, and the loser can reclaim the
    # winner's freshly-created crew in the window before it holds a work item --
    # after which both write interleaved transitions into one lane and its dwell is
    # nonsense. `record_stage` needs no such lock: it is a single event whose only
    # shared write is the ownership index, which has its own.
    lock_path = _replay_lock_path(owner, repo, pipeline, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            return _replay_locked(owner, repo, events, pipeline=pipeline, table=table, root=root)


def _replay_lock_path(owner: str, repo: str, pipeline: str, root: Path | None) -> Path:
    """One lock per (repo, pipeline) -- the scope the precondition itself uses.

    Per pipeline rather than per repo because two DIFFERENT pipelines drawing on one
    board is legitimate and must not serialize against each other; repo-wide rather
    than per item because the guard's question -- has this pipeline already been
    replayed here -- is repo-wide.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pipeline)[:64]
    return crew_store.crews_dir(owner, repo, root) / f"pipeline-bridge-replay-{safe}.lock"


def _replay_locked(
    owner: str,
    repo: str,
    events: Iterable[Mapping[str, Any]],
    *,
    pipeline: str,
    table: Mapping[str, StageRule],
    root: Path | None = None,
) -> dict[str, Any]:
    """:func:`replay`'s body, run inside the per-pipeline replay lock."""
    # Scoped to THIS pipeline, not to bridge crews in general. The precondition
    # exists to stop the SAME log being replayed twice into the same root; a
    # different pipeline's lanes are a legitimate neighbour on one repo's board,
    # which is the whole point of a board that folds every crew in the repo.
    # Filtering on the label alone refused pipeline B just because pipeline A was
    # already drawn there.
    prefix = f"{pipeline}#"
    # Resolved through the ownership index's IDS, so a renamed crew still counts as
    # this pipeline's and a stranger's same-named crew still does not.
    owned_ids = {
        crew_id
        for name, crew_id in _owned_ids(owner, repo, root).items()
        if name.startswith(prefix)
    }
    existing = [
        crew
        for crew in crew_store.list_crews(owner, repo, root, include_retired=True)
        if str(crew.get("id")) in owned_ids
    ]
    if existing:
        # Enforced, not merely documented. Replay APPENDS ledger lines and the
        # store stamps its own clock, so a second pass over a grown log gives
        # historical transitions fresh timestamps: the timeline gains duplicate
        # and backward segments and every dwell reading is wrong. A caveat in a
        # docstring does not stop that; refusing before the first write does.
        # `record_stage` is deliberately NOT gated this way -- it is incremental
        # by design, one event as it happens, which is the correct way to keep
        # adding to a populated root.
        raise BridgeError(
            f"{len(existing)} crews for pipeline {pipeline!r} already exist in this "
            "root; replay would append duplicate transitions and corrupt their "
            "dwell. Replay into a fresh root, or use record_stage to add events "
            "incrementally."
        )
    crews: dict[int, str] = {}
    applied = 0
    skipped_kinds: dict[str, int] = {}
    no_number = 0
    refused: list[str] = []

    for rec in events:
        kind = str(rec.get("event") or rec.get("kind") or "").strip()
        rule = table.get(kind)
        if rule is None:
            if kind:
                skipped_kinds[kind] = skipped_kinds.get(kind, 0) + 1
            continue
        number = _item_number(rec)
        if number is None:
            no_number += 1
            continue

        phase, ledger_kind = rule
        crew_id = crews.get(number)
        if crew_id is None:
            try:
                crew_id = _ensure_crew(owner, repo, pipeline, number, root)
            except BridgeError as exc:
                # A name collision with a crew the bridge did not create is a
                # refusal for THAT item only. Aborting the loop would leave every
                # item written so far on the board and the rest missing, which is
                # the half-populated state this module exists to avoid.
                refused.append(f"#{number}: {exc}")
                continue
            crews[number] = crew_id

        text = _safe_text(rec.get("detail") or rec.get("text"), kind)
        try:
            crew_store.commit_work_progress(
                owner,
                repo,
                crew_id,
                number,
                {"phase": phase},
                ledger_kind,
                text,
                root=root,
            )
        except crew_store.CrewStoreError as exc:
            # The store's refusals are the contract working, not a bug to route
            # around: report them so a wrong table is visible, and keep going so
            # one bad item cannot cost the whole replay.
            refused.append(f"#{number} -> {phase}: {exc}")
            continue
        applied += 1

    return {
        "pipeline": pipeline,
        "items": len(crews),
        "applied": applied,
        "skipped_kinds": skipped_kinds,
        "events_without_item": no_number,
        "refused": refused,
    }


def main(argv: list[str] | None = None) -> int:
    """Replay a pipeline's log into a repo's ledger, or report a table's coverage.

    The operator-facing half of this module, and the reason it is a script rather
    than a route: populating a board from a log is something a person does
    deliberately, against a path only they know, and it must not become a
    request any session can make.

    ``--dry-run`` reports coverage and writes nothing, which is the order to do
    this in: read which of your event names the table maps, decide whether the
    unmapped ones are housekeeping, and only then replay.

    EVERY refusal in this module reaches the operator as one bounded, escaped
    line and a nonzero exit. That is enforced by wrapping the whole flow in a
    single guard rather than by catching at each raise site: three separate rounds
    of review found a NEW raise escaping ``main`` uncaught -- the reader, the
    crew-collision check, the populated-root precondition -- because a per-site
    catch has to be remembered again every time a refusal is added. One guard
    around the body cannot be forgotten.

    ``OSError`` is caught alongside :class:`BridgeError` because the destination
    is a filesystem the operator names: a read-only or full data root makes the
    crew store's mkdir/open/write raise, and that is an environment problem to
    report in a sentence, not a defect to show a stack trace for.
    """
    ap = argparse.ArgumentParser(prog="pipeline-bridge", description=main.__doc__)
    ap.add_argument("log", type=Path, help="JSONL event log to read (never written)")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pipeline", required=True, help="names the lanes' crews")
    ap.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Issue Radar data root. Replay REFUSES a root that already holds "
        "crews for this pipeline, because appending a second pass would give "
        "historical transitions new timestamps and corrupt every dwell reading.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report table coverage and write nothing",
    )
    args = ap.parse_args(argv)
    try:
        return _run(args)
    except (BridgeError, OSError) as exc:
        print(f"refused: {_printable(str(exc))}")
        return 1


def _run(args: Any) -> int:
    """``main``'s body, run inside its single refusal guard."""
    events, unparseable = read_event_log(args.log)
    if not events:
        print(f"no usable events in {_printable(str(args.log))}")
        return 1
    if unparseable:
        # Named, because a torn line can swallow a real transition and leave a
        # lane showing a stale phase -- the operator needs to know a gap exists.
        print(
            f"WARNING {unparseable} unparseable line(s) skipped; a torn append can "
            "swallow a transition, so a lane may show an earlier phase than it reached"
        )

    cov = coverage_report(events, GH_AUTOFIX_STAGES)
    print(f"read {len(events)} events")
    print(f"mapped   {cov['mapped_events']} across {len(cov['mapped'])} names")
    print(f"unmapped {cov['unmapped_events']} across {len(cov['unmapped'])} names")
    for name, count in list(cov["unmapped"].items())[:10]:
        print(f"  unmapped {count:6d}  {_printable(name)}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    summary = replay(
        args.owner,
        args.repo,
        events,
        pipeline=args.pipeline,
        table=GH_AUTOFIX_STAGES,
        root=args.root,
    )
    print(
        f"replayed {summary['applied']} transitions over {summary['items']} items; "
        f"{summary['events_without_item']} events carried no item"
    )
    for line in summary["refused"]:
        print(f"  refused {_printable(line)}")
    incomplete = bool(summary["refused"]) or bool(unparseable)
    if incomplete:
        # A knowingly-partial board must not exit 0. Both causes count: a refused
        # item is a missing lane, and a torn line may have swallowed a transition
        # so a lane can sit at an earlier phase than it reached. The output says
        # so either way, but a wrapper script reads the exit code -- reporting
        # success for an incomplete replay is how that goes unnoticed.
        #
        # It is an exit CODE and not a refusal to write, deliberately. A torn tail
        # is the EXPECTED state of a log a live process is appending to, so
        # refusing to replay whenever one exists would make this unusable against
        # the only input it really has. The operator gets the board, the warning,
        # and a truthful status.
        print(
            f"INCOMPLETE {len(summary['refused'])} item(s) refused, "
            f"{unparseable} unparseable line(s); the board may be partial"
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry
    raise SystemExit(main())
