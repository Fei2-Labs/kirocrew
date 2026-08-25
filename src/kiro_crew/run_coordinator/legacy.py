"""Read-only import of pre-coordinator subagent run folders."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from .models import (
    CoordinatorDecision,
    LegacyRunImport,
    ObservedState,
    RunCoordinator,
    RunOutcome,
)

logger = logging.getLogger(__name__)

_SOURCE_VERSION = "legacy-state-v1"
_MAX_TASK_CHARS = 1000
_MAX_ERROR_CHARS = 2000
_MAX_LEGACY_JSON_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LegacyImportReport:
    imported: int = 0
    existing: int = 0
    corrupt: int = 0


def _redact(value: str) -> str:
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


def _number(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError("legacy timestamp is out of range") from exc
        if not math.isfinite(number):
            raise ValueError("legacy timestamp must be finite")
        return number
    return default


class LegacyRunImporter:
    """Import known legacy fields while leaving source folders byte-identical."""

    def __init__(
        self,
        coordinator: RunCoordinator,
        *,
        root: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._coordinator = coordinator
        self._root = root
        self._clock = clock

    def _base(self) -> Path:
        return self._root if self._root is not None else data_home() / "subagents"

    def _folders(self) -> list[Path]:
        base = self._base()
        try:
            return [
                path
                for path in sorted(base.iterdir())
                if path.is_dir() and not is_link_or_junction(path)
            ]
        except (FileNotFoundError, OSError):
            return []

    def _safe_folder(self, run_id: str) -> Path:
        if (
            not run_id
            or run_id in (".", "..")
            or ".." in run_id
            or "/" in run_id
            or "\\" in run_id
            or "\0" in run_id
        ):
            raise ValueError("invalid legacy run id")
        base = self._base().resolve()
        folder = (base / run_id).resolve()
        if folder.parent != base or is_link_or_junction(base / run_id):
            raise ValueError("legacy run folder escapes its root")
        return folder

    @staticmethod
    def _valid_id(run_id: object, folder: Path) -> str:
        if not isinstance(run_id, str) or run_id != folder.name:
            raise ValueError("state id does not match its folder")
        return run_id

    @staticmethod
    def _read_object(path: Path) -> dict[str, object]:
        raw = safe_read_file_bytes_nolink(
            str(path),
            within_root=str(path.parent.parent),
            max_bytes=_MAX_LEGACY_JSON_BYTES,
        )
        if raw is None:
            raise ValueError("legacy record is not a safe regular file")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("legacy record must be an object")
        return value

    def _request(self, folder: Path) -> LegacyRunImport:
        folder = self._safe_folder(folder.name)
        state = self._read_object(folder / "state.json")
        run_id = self._valid_id(state.get("id"), folder)
        tombstone_path = folder / "tombstone.json"
        tombstone = self._read_object(tombstone_path) if tombstone_path.exists() else None
        result_file = folder / "result.txt"
        result_path = str(result_file) if result_file.exists() else ""
        started = _number(state.get("started"), self._clock())
        updated = _number(state.get("updated_at"), started)
        task = _redact(str(state.get("task") or ""))[:_MAX_TASK_CHARS]
        agent = _redact(str(state.get("agent") or ""))
        conversation = str(state.get("conversation_key") or "")
        if tombstone is None:
            return LegacyRunImport(
                run_id=run_id,
                parent_session="",
                agent=agent,
                task=task,
                conversation_key=conversation,
                observed_state=ObservedState.RUNNING,
                outcome=None,
                result_path=result_path,
                error="",
                created_at=started,
                updated_at=updated,
                terminal_at=None,
                source_version=_SOURCE_VERSION,
            )

        died = _number(tombstone.get("died"), updated)
        raw_outcome = str(tombstone.get("outcome") or "")
        try:
            outcome = RunOutcome(raw_outcome)
        except ValueError:
            outcome = (
                RunOutcome.COMPLETED
                if tombstone.get("cause") == "delivered"
                else RunOutcome.INTERRUPTED
            )
        error = _redact(
            str(tombstone.get("detail") or tombstone.get("cause") or "gateway restart")
        )[:_MAX_ERROR_CHARS]
        return LegacyRunImport(
            run_id=run_id,
            parent_session="",
            agent=agent,
            task=task,
            conversation_key=conversation,
            observed_state=ObservedState.TERMINAL,
            outcome=outcome,
            result_path=result_path,
            error=error,
            created_at=started,
            updated_at=died,
            terminal_at=died,
            source_version=_SOURCE_VERSION,
            # Legacy routing fields are agent-writable evidence, not authority.
            # Import terminal history without manufacturing a pending delivery
            # that could inject into an attacker-selected session.
            event_type="",
            destination="",
            payload_json="",
            delivery_state=None,
        )

    async def import_all(self) -> LegacyImportReport:
        imported = 0
        existing = 0
        corrupt = 0
        folders = await asyncio.to_thread(self._folders)
        for folder in folders:
            try:
                request = await asyncio.to_thread(self._request, folder)
                result = await self._coordinator.import_legacy(request)
                if result.decision is CoordinatorDecision.APPLIED:
                    imported += 1
                elif result.decision is CoordinatorDecision.UNCHANGED:
                    existing += 1
                else:
                    corrupt += 1
                    logger.warning("legacy subagent import skipped for %s", folder.name)
            except (
                FileTooLargeError,
                OSError,
                RecursionError,
                TypeError,
                UnicodeDecodeError,
                ValueError,
            ):
                corrupt += 1
                logger.warning("legacy subagent import skipped for %s", folder.name)
        return LegacyImportReport(imported=imported, existing=existing, corrupt=corrupt)
