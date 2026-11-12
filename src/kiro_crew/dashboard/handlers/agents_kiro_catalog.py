"""The kiro-cli ``--list-models`` catalog fetch behind ``GET /api/models``.

:mod:`kiro_crew.dashboard.handlers.agents` binds these names and is the import path
callers use; this plain module sits outside the owner composition, as
``agents_cc_model_rows`` does.
"""

from __future__ import annotations

import asyncio
import json
import dataclasses
import logging
import os
import subprocess

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.acp_backends import ACP_BACKEND_KIRO, model_registry_namespace
from kiro_crew.agent_sdk import host_auth
from kiro_crew.config.loader_credentials import inject_kiro_cli_api_key
from kiro_crew.agent_sdk.drivers.acp import (
    is_auth_failure_output,
    resolve_kiro_bin_for_spawn,
    resolve_ssh_auth_sock,
)
from kiro_crew.dashboard.chat_utils import is_deprecated_model
from kiro_crew.dashboard.kiro_readiness import KIRO_PREREQUISITE_REQUIRED_CODE
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.platform_compat import kill_and_reap
from kiro_crew.sandbox import configured_sandbox_mode, scrub_agent_subprocess_env


logger = logging.getLogger(__name__)


_MODEL_LIST_STDERR_TAIL_CHARS = 1000
# How long one GET /api/models waits for a catalog before answering the degraded
# 503. Unchanged: a remote hub still budgets its whole cold path against this
# bound (see api_models), so raising it would move
# DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS with it.
_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS: float = 10.0
# How long the SHARED background catalog fetch may run, decoupled from any one
# request. On a host where a cold `kiro-cli --list-models` spawn always exceeds
# the request bound, killing it at that bound meant no call ever finished, so
# every 8s poll started another doomed cold spawn and the picker never left
# `auto`. The fetch now outlives the request that started it: the request still
# answers 503 after _LIST_MODELS_SUBPROCESS_TIMEOUT_SECS, but the spawn keeps
# running under this ceiling and the next poll is served from its result.
_LIST_MODELS_BACKGROUND_TIMEOUT_SECS: float = 90.0
# A cached catalog younger than this is served straight away; an older one is
# served straight away too and refreshed behind the reply (stale-while-
# revalidate). The catalog changes rarely, so a few minutes stale costs nothing
# the per-request entitlement narrowing does not already correct.
_LIST_MODELS_CATALOG_TTL_SECS: float = 300.0
# A fetch that spawned kiro-cli and failed (signed out, non-zero exit, timeout,
# bad output) is remembered this long, and every poll inside the window gets the
# same 503 without a new spawn or a new WARNING. The dashboard re-polls about
# every 8s while the list is degraded, so a signed-out CLI cost one spawn and one
# WARNING per poll (81 of each in 16 minutes on one gateway). 30s holds a failing
# CLI to two spawns a minute, while a user who has just signed in waits at most
# 30s for the picker to fill. A success clears the entry at once.
_LIST_MODELS_FAILURE_BACKOFF_SECS: float = 30.0
# The cache is the one place ``--list-models`` output is held past the request,
# so retention is bounded before it is stored. Row count and identifying-field
# length reuse the repo's ONE shared admission bound
# (``model_registry.ADVERTISED_MODELS_MAX_IDS`` /
# ``ADVERTISED_MODEL_ID_MAX_CHARS``) so the picker cannot serve an id the window
# / advertised stores refused — an over-long id is REFUSED (the whole row
# skipped), never truncated, matching ``admit_catalog_rows``. This cap bounds
# only the non-identifying extra fields a row may carry (the shared admission
# does not speak to those): each is clamped to the id-length bound and a row
# keeps at most this many.
_CATALOG_CACHE_MAX_FIELDS_PER_ROW = 64



_CATALOG_UNAVAILABLE_CODE = "model_list_unavailable"


class _CatalogUnavailable(Exception):
    """A degraded catalog fetch outcome that maps to one 503 body.

    The fetch runs in a shared background task that cannot return an HTTP
    response, so each degraded outcome (binary unresolved, timeout, non-zero
    exit, empty / invalid output) raises this carrying its error message, and
    the request path renders a dict-literal 503 body from it — one 503 per
    outcome. The message is carried, not a response dict, so the single render
    site stays a dict literal the error-code ratchet can scan.

    ``backoff`` says whether the outcome is held in the cache's negative entry
    (see :data:`_LIST_MODELS_FAILURE_BACKOFF_SECS`). It is true for every outcome
    of a spawn that ran. An unresolved binary spawns nothing and resolves on its
    own shortly after start, so it is retried on the next poll.

    ``code`` is the machine tag the 503 body carries beside ``error`` so a client
    tells one degraded cause from another, the same contract the sibling 503s on
    this endpoint already carry (``model_list_revalidating``,
    ``model_list_sandbox_unavailable``). It defaults to
    :data:`_CATALOG_UNAVAILABLE_CODE` for the ordinary degraded outcomes (binary
    unresolved, timeout, non-zero exit, empty / invalid output), which the
    dashboard renders as its generic config-load notice; the expired / signed-out
    login path passes :data:`KIRO_PREREQUISITE_REQUIRED_CODE` instead, which the
    dashboard renders as the one actionable "run ``kiro-cli login``" message.
    """

    def __init__(self, error: str, *, backoff: bool = True, code: str = _CATALOG_UNAVAILABLE_CODE):
        super().__init__(error)
        self.error = error
        self.backoff = backoff
        self.code = code


def _bounded_catalog(models: list[dict]) -> list[dict]:
    """Clamp a parsed catalog to what the cache may safely retain.

    The retention point (:data:`_catalog_cache`) holds the list past the request,
    so it is bounded here before it is stored, through the SAME admission the
    window / advertised stores use — ``model_registry.ADVERTISED_MODELS_MAX_IDS``
    rows, each identifying field (``model_name`` / ``model_id``) at most
    ``ADVERTISED_MODEL_ID_MAX_CHARS`` — so the cache cannot serve an id those
    stores refused. An over-long id is REFUSED (its whole row skipped), never
    truncated, matching :func:`model_registry.admit_catalog_rows`'s own
    invariant; the picker reads the name, so a sliced name would name a different
    model or none.

    Each remaining scalar field (str / int / float / bool / None) is kept, with
    strings clamped to the id-length bound and a row holding at most
    :data:`_CATALOG_CACHE_MAX_FIELDS_PER_ROW`; a non-scalar field (nested object
    or array) is dropped — no reader of the cached list consumes one, and it is
    the unbounded edge the retention bound exists to close. Row ORDER is
    preserved (readers treat catalog order as meaningful). Rows and fields
    discarded for any reason are counted and logged once per snapshot.
    """
    max_rows = model_registry.ADVERTISED_MODELS_MAX_IDS
    max_chars = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
    bounded: list[dict] = []
    dropped_rows = 0
    dropped_fields = 0
    for row in models:
        if len(bounded) >= max_rows:
            dropped_rows += 1
            continue
        if not isinstance(row, dict):
            dropped_rows += 1
            continue
        # Refuse (skip) a row whose identifying field is over-long, rather than
        # slicing it into an id no store retained.
        ident = row.get("model_name") or row.get("model_id")
        if isinstance(ident, str) and len(ident) > max_chars:
            dropped_rows += 1
            continue
        kept: dict = {}
        for key, value in row.items():
            if len(kept) >= _CATALOG_CACHE_MAX_FIELDS_PER_ROW:
                dropped_fields += 1
                continue
            # The KEY is a retained string too: refuse (skip) an over-long one
            # rather than cache it, so no retained string is unbounded.
            if isinstance(key, str) and len(key) > max_chars:
                dropped_fields += 1
                continue
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                kept[key] = value
            elif isinstance(value, str):
                kept[key] = value[:max_chars]
            else:
                # Non-scalar (nested dict/list): dropped — unbounded and unread.
                dropped_fields += 1
        bounded.append(kept)
    if dropped_rows or dropped_fields:
        logger.warning(
            "api_models: catalog cache dropped %d row(s) and %d field(s) over the "
            "retention bound (max_rows=%d, max_id_chars=%d)",
            dropped_rows,
            dropped_fields,
            max_rows,
            max_chars,
        )
    return bounded


@dataclasses.dataclass
class _CatalogCache:
    """The last good ``--list-models`` catalog, kept in memory only.

    One slow success is enough: once ``models`` is set, every request is served
    from it (after per-request entitlement narrowing) while a stale entry is
    refreshed behind the reply. A gateway restart drops it, so the first request
    after a restart pays the cold start again — the catalog is not persisted,
    which keeps a downgraded account's stale rows from surviving a restart.

    ``task`` single-flights the fetch: concurrent polls (and the 8s self-heal
    loop) share one spawn rather than each starting its own cold start.

    ``failure`` is the negative entry: the 503 message of the last failed fetch,
    and ``failed_at`` when it ended. Inside :data:`_LIST_MODELS_FAILURE_BACKOFF_SECS`
    the request path answers that 503 instead of spawning again. Only the message
    is held, not the exception, so no traceback frame outlives the fetch. A
    successful fetch clears it. ``failure_code`` rides alongside it so a held
    auth-expiry 503 keeps its :data:`KIRO_PREREQUISITE_REQUIRED_CODE` tag through
    the backoff window rather than decaying to a generic body on the next poll.
    """

    models: list[dict] | None = None
    fetched_at: float = 0.0
    task: "asyncio.Task[list[dict]] | None" = None
    failure: str | None = None
    failure_code: str = _CATALOG_UNAVAILABLE_CODE
    failed_at: float = 0.0

    def recent_failure(self) -> tuple[str, str] | None:
        """A fetch that failed inside the backoff window: its 503 message and code.

        Returns ``None`` once the window has elapsed (the next poll re-spawns), so
        a non-``None`` answer is always within backoff. The code is the second
        element -- the generic ``model_list_unavailable`` for an ordinary degraded
        outcome, or ``kiro_prerequisite_required`` for an expired login -- so the
        request path renders the same body the failing fetch would have.
        """
        if self.failure is None:
            return None
        from kiro_crew.dashboard.handlers import agents as _agents

        if _agents.time.monotonic() - self.failed_at >= _LIST_MODELS_FAILURE_BACKOFF_SECS:
            return None
        return self.failure, self.failure_code


_catalog_cache = _CatalogCache()


async def _fetch_kiro_catalog() -> list[dict]:
    """Spawn ``kiro-cli --list-models`` once and return the catalog rows.

    Calls the module-level ``resolve_kiro_bin_for_spawn`` /
    ``resolve_ssh_auth_sock`` wrappers directly. They are imported from
    ``kiro_crew.agent_sdk.drivers.acp`` — the agent-SDK surface, not a forbidden
    root — so this background worker reaches the spawn helpers without adding an
    ACP-layer edge (see ``scripts/check_agent_sdk_boundary.py``).

    Returns the deprecated-stripped list, each row under the ``model_id`` its
    session advertises (see ``wire_row`` below), and seeds the window/advertised caches —
    the UNFILTERED-then-deprecated-stripped list, i.e. the response body before
    per-request entitlement narrowing. Raises :class:`_CatalogUnavailable` for a
    degraded outcome (binary unresolved, timeout, non-zero exit, empty / invalid
    output) and lets :class:`SandboxUnavailableError` propagate; the request path
    renders each as its own 503.

    Bounded by :data:`_LIST_MODELS_BACKGROUND_TIMEOUT_SECS`, NOT the request wait,
    so a cold start that outlasts one poll still finishes and warms the cache for
    the next one.
    """
    from kiro_crew.dashboard.handlers import agents as _agents
    from kiro_crew.env import augmented_path  # noqa: F811

    kiro_bin = await resolve_kiro_bin_for_spawn()
    if not kiro_bin:
        # Degraded (binary not resolved yet), NOT a genuine "zero models" result.
        # 503 so the client retries instead of caching an empty list — a cached
        # [] renders an empty picker that only a manual page refresh recovers from.
        raise _CatalogUnavailable("kiro binary not resolved", backoff=False)
    argv = [kiro_bin, "chat", "--list-models", "--format", "json", "--no-interactive"]
    # Mirror AcpClient._spawn() sandbox: wrap_argv + env + process isolation.
    # Note: AcpClient._spawn() is for interactive ACP sessions (stdin/stdout
    # pipes); this is a one-shot read-only command, so we replicate the
    # sandbox setup directly.  See the security-controls rule.
    #
    # The configured tier is passed EXPLICITLY rather than left to
    # wrap_argv's "auto" parameter default, so this endpoint can never ask
    # for stricter isolation than the chat spawn of the same binary. It
    # matters wherever the operator set agent.sandbox="off" (deferring
    # isolation to kiro-cli's own internal sandbox): the one-shot and chat
    # path must have one posture. The explicit Kiro classification also
    # makes the shipped "auto" tier work on Windows via Kiro's built-in
    # sandbox instead of answering 503 on every 8s poll.
    #
    # OFF the loop: `configured_sandbox_mode()` stats (and on a cache miss
    # re-reads + revalidates) config.json, and `wrap_argv` -> `detect_backend`
    # can cold-probe the backend with a synchronous
    # `subprocess.run(..., timeout=5)`. This runs behind the request, but a
    # blocking call on the event loop still stalls chat, cron and the liveness
    # heartbeat on exactly the host where the probe is slowest. Both reads run
    # in the worker, so the mode is resolved there too rather than passed in.
    #
    # A remote hub proxying this endpoint budgets its WHOLE cold path (the
    # sandbox detection, the list-models subprocess, and the up to
    # _READ_PATH_PROBE_DEADLINE_SECS the read-path entitlement revalidation
    # waits in _entitled_kiro_models) via
    # DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS in
    # kiro_crew/instances/constants.py — 5 + 10 + 3 < 20. The per-request wait
    # (_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS) is what that budget sizes against,
    # NOT the longer background ceiling; growing the request wait means moving
    # that constant with it.
    argv, cleanup = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _agents._wrap_list_models_argv, argv
    )
    argv = _agents.cgroup_scope_argv(argv)  # cgroup DoS ceiling
    try:
        env = {**os.environ}
        env["PATH"] = augmented_path(env.get("PATH", ""))
        # OFF the loop: the resolver globs /tmp/ssh-*/agent.* and stats
        # every hit, so its latency scales with the /tmp entry count. Its
        # sibling wrapper's contract states it must never run on the event loop.
        await asyncio.to_thread(resolve_ssh_auth_sock, env)
        # The Docker entrypoint removes credentials from the long-lived
        # gateway environment.  This fixed-argv child is the official
        # kiro-cli and KIRO_API_KEY is its own model credential, so settle
        # the same single key the interactive ACP spawn receives.  Keep
        # the protected .env read off the gateway loop.
        await asyncio.to_thread(inject_kiro_cli_api_key, env)
        env = scrub_agent_subprocess_env(env)
        # Supervised so the call ends whatever it leaves behind. A kiro-cli
        # launcher wrapper can start a ~140-thread credential helper for each
        # call and leave it running; the self-heal loop re-polls every 8s while
        # degraded, so on a gateway on that path every poll leaked one until
        # the agent cgroup ran out of pids.
        proc = await _agents.spawn_supervised_oneshot(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_LIST_MODELS_BACKGROUND_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            # The whole group, while the supervisor still leads it: killing
            # only the leader would leave the command and its helpers running.
            await kill_and_reap(proc)
            # A cold CLI spawn exceeded even the background ceiling. 503 so the
            # client keeps its last-good list and polls again rather than caching
            # a successful empty result.
            logger.warning("api_models: --list-models timed out; returning 503")
            raise _CatalogUnavailable("model list timed out")
    finally:
        if cleanup and callable(cleanup):
            cleanup()

    if proc.returncode != 0:
        from kiro_crew.platform import redact_via_context  # noqa: F811

        stderr_tail = stderr.decode(errors="replace").strip()
        stderr_tail = redact_via_context(stderr_tail)[-_MODEL_LIST_STDERR_TAIL_CHARS:]
        logger.warning(
            "api_models: --list-models exited %s: %s; returning 503",
            proc.returncode,
            stderr_tail or "<no stderr>",
        )
        # Classify the ONE non-zero exit that is not a degraded backend blip: an
        # expired / signed-out kiro-cli login. ``--list-models`` fails that way
        # mid-session once the token lapses, and the generic 503 below rendered as
        # the dashboard's "Failed to load config." — pointing the user at retry /
        # switch-model, neither of which can fix auth. The readiness gate above
        # cannot catch an EXPIRED login: its latch was verified true at startup
        # and is stale here. ``is_auth_failure_output`` is the SAME classifier the
        # ACP error-frame and spawn-stderr paths already use (401 / 403, expiry
        # wording, rejected bearer, the named service exceptions), so this adds no
        # new auth vocabulary. The stderr is already redacted above, so no token
        # reaches this branch or the error carried out of it.
        if is_auth_failure_output(stderr_tail):
            raise _CatalogUnavailable(
                host_auth.signed_out_message(ACP_BACKEND_KIRO),
                code=KIRO_PREREQUISITE_REQUIRED_CODE,
            )
        raise _CatalogUnavailable("model list command failed")

    if not stdout.strip():
        logger.warning("api_models: --list-models returned empty output; returning 503")
        raise _CatalogUnavailable("model list returned empty output")

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "api_models: --list-models returned invalid JSON (%s); returning 503",
            exc,
        )
        raise _CatalogUnavailable("model list returned invalid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        logger.warning("api_models: --list-models returned an invalid payload; returning 503")
        raise _CatalogUnavailable("model list returned an invalid payload")
    models = data["models"]
    # Seed the central window authority from kiro's authoritative structured
    # 'context_window_tokens' field (keyed by model_id/model_name). This is
    # the ONE place these rows enter the system; every other consumer (the
    # ACP backfill, the context-budget scaler, the live meter) then resolves
    # through model_registry.model_window() rather than re-reading kiro. The
    # in-memory update is synchronous (cheap dict mutation); only the disk
    # persist is offloaded to an executor so the event loop never blocks on
    # filesystem I/O (no blocking call on the event loop).
    #
    # This fork keeps kiro's bare-dotted ids as the picker WIRE FORMAT
    # (guarded by _model_rejected_reason / api_chat_slot_model, which rejects
    # canonical registry keys the ACP CLI can't accept). The upstream
    # registry-key canonicalization is deliberately NOT ported — it is
    # incompatible with this fork's _model_rejected_reason guard. The window
    # seeding above uses kiro's authoritative context_window_tokens to give
    # the backfill real GPT/DeepSeek/Qwen windows, independent of the
    # wire-format choice.
    #
    # The same rows also warm the ``acp`` advertised-model cache, kiro's
    # VOCABULARY: which ids are kiro's own, so model_scope can tell a pin
    # chosen for another harness from one chosen here before any session
    # exists (the chip and the provider factory judge from the cache; the
    # wire holds the live list). ONE admission feeds both caches
    # (refresh_kiro_catalog), so an id the bound refuses gets no row in
    # either. Fed from the UNFILTERED catalog on purpose: a deprecated or
    # unentitled row is still a kiro id, and dropping it here would make
    # model_scope call a native pin foreign. Entitlement stays with the
    # live ``session/new`` list downstream (_entitled_kiro_models,
    # model_is_unusable) -- ``--list-models`` is a catalog and no reader
    # of this cache treats it as more. Sourced here rather than from any
    # ``session/new`` payload because the registry attributes that payload
    # to claude-agent-acp and a kiro session's list is scoped to the agent
    # that session started. In-memory updates on the loop, disk persists
    # off it.
    windows_changed, advertised_changed = model_registry.refresh_kiro_catalog(
        models, model_registry_namespace(ACP_BACKEND_KIRO)
    )
    if windows_changed:
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), model_registry.persist_kiro_windows
        )
    if advertised_changed:
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), model_registry.persist_advertised_models
        )

    def wire_row(row: dict) -> dict:
        # Every reader downstream treats a row's ``model_name`` as the model id:
        # the entitlement narrowing judges it against the live ``session/new``
        # ``availableModels`` ids, the pin validators compare it literally, and
        # the picker sends it to ``session/set_model``. kiro-cli prints a
        # ``model_id`` beside the name, and a model can carry a display name that
        # differs from its id. ``session/new`` advertises the ``model_id``, so a
        # row left under its printed name is never found among the advertised ids
        # and the picker hides a model the account can run. Such a row is served
        # under the ``model_id``, with the printed name kept as ``display_name``;
        # a row with no usable ``model_id`` is returned unchanged. Nested rather
        # than module-level: the facade's definitions are pinned by
        # ``test/test_dashboard_agents_composition_contract.py``.
        model_id = row.get("model_id")
        name = row.get("model_name")
        if not isinstance(model_id, str) or not model_id.strip() or model_id == name:
            return row
        label = name if isinstance(name, str) and name.strip() else model_id
        return {**row, "model_name": model_id, "display_name": row.get("display_name") or label}

    return [wire_row(m) for m in models if not is_deprecated_model(m.get("model_name", ""))]


def _shared_catalog_fetch(gate_req: web.Request | None = None) -> "asyncio.Task[list[dict]]":
    """Return the in-flight catalog fetch, starting one if none is running.

    Single-flight: concurrent polls share one spawn. The task caches its result
    (and clears itself) when it finishes, so the next poll after a slow success
    is served straight from the cache rather than starting another cold start.
    A *gate_req* runs the sign-in gate right before the spawn (see api_models).
    """
    # Names the suite patches on the ``agents`` facade are resolved through it,
    # not bound here, so the patch target stays one module.
    from kiro_crew.dashboard.handlers import agents as _agents

    existing = _agents._catalog_cache.task
    if existing is not None and not existing.done():
        return existing

    async def _run() -> list[dict]:
        try:
            if gate_req is not None and await _agents.reject_if_kiro_unverified(gate_req) is not None:
                raise _CatalogUnavailable(  # nothing spawned, so no backoff entry
                    "kiro sign-in not verified; catalog refresh skipped",
                    backoff=False,
                    code=KIRO_PREREQUISITE_REQUIRED_CODE,
                )
            models = await _agents._fetch_kiro_catalog()
        except _CatalogUnavailable as exc:
            # Remember a failed spawn so the polls inside the backoff window
            # answer its 503 without spawning again (see recent_failure). The
            # code rides along so an expired-auth hold keeps its sign-in tag.
            if exc.backoff:
                _agents._catalog_cache.failure = exc.error
                _agents._catalog_cache.failure_code = exc.code
                _agents._catalog_cache.failed_at = _agents.time.monotonic()
            raise
        finally:
            # Release the single-flight slot the moment this attempt ends,
            # win or lose, so a failed fetch does not wedge every later poll.
            _agents._catalog_cache.task = None
        # Bound the retained list before it enters the one store that outlives
        # the request (see _bounded_catalog).
        bounded = _bounded_catalog(models)
        _agents._catalog_cache.models = bounded
        _agents._catalog_cache.fetched_at = _agents.time.monotonic()
        _agents._catalog_cache.failure = None
        _agents._catalog_cache.failure_code = _CATALOG_UNAVAILABLE_CODE
        return bounded

    task = asyncio.ensure_future(_run())
    _agents._catalog_cache.task = task
    # A stale-while-revalidate refresh is fire-and-forget: the request path
    # returns the cached list without awaiting this task, so a degraded refresh
    # (expired auth, background-ceiling timeout, SandboxUnavailableError) would
    # leave an unretrieved exception that crash_guard's asyncio handler logs as a
    # full crash record at the 8s poll cadence. Consume it here: a failed refresh
    # keeps the last-good cache and is a debug line, not a crash.
    task.add_done_callback(_consume_refresh_exception)
    return task


def _consume_refresh_exception(task: "asyncio.Task[list[dict]]") -> None:
    """Retrieve a finished refresh task's exception so it is never 'unhandled'.

    A cached catalog means a degraded background refresh is non-fatal — the
    request path already served the last-good list — so its exception is logged
    at debug and swallowed rather than left for ``Task.__del__`` to escalate.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("api_models: background catalog refresh failed (serving cached)", exc_info=exc)

