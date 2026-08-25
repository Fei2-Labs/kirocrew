"""Tests for the table-driven string-field validation at the cron persistence
chokepoint (issue #5782).

``_build_job`` and ``_update_job_locked`` now iterate a single
``_CRON_STRING_FIELD_CAPS`` table to enforce type+length on every
caller-supplied string field. These tests lock that guarantee:

- All fields in the table reject non-string truthy values (type gate).
- All fields in the table reject strings exceeding their cap (length gate).
- Falsy values (None, "") in an update are no-ops (falsy-skip semantics).
- An anti-drift test enumerates CronJob str fields and asserts every
  persisted caller-supplied field is covered by the validation table.
- A typo guard asserts every name in the table is an actual CronJob field.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.cron import (
    CRON_VALIDATED_STRING_FIELDS,
    CronJob,
    CronService,
    _CRON_STRING_FIELD_CAPS,
)
from kiro_crew.validation import CHANNEL_MAX_LEN, MAX_CRON_MESSAGE, MAX_SHORT_STRING


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


# ── Per-field parametrize data ──
# Fields NOT already pinned by test_cron_name_cap.py / test_cron_message_cap.py.
# Each entry: (field_name, cap, oversize_value, non_string_value)
_NEWLY_VALIDATED_FIELDS: list[tuple[str, int, str, object]] = [
    ("channel", CHANNEL_MAX_LEN, "x" * (CHANNEL_MAX_LEN + 1), 42),
    ("thread_ts", 30, "x" * 31, ["ts"]),
    ("agent_id", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), 123),
    ("created_by", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), {"uid": 1}),
    ("folder_id", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), True),
    ("session_key", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), 99.9),
    ("model", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), []),
    ("command", 5000, "x" * 5001, 0xDEAD),
    ("script", 200, "x" * 201, ("t",)),
    ("timezone", 50, "x" * 51, 7),
]

_FIELD_IDS = [f[0] for f in _NEWLY_VALIDATED_FIELDS]


# ── Build-job (add_job) tests ──


class TestBuildJobFieldValidation:
    """_build_job rejects non-string and oversize values for each field."""

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_add_job_rejects_oversize(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="max length"):
            svc.add_job(
                name="j", message="m", every_secs=3600, **{field_name: oversize_value}
            )
        assert svc.list_jobs() == []

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_add_job_rejects_non_string(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            svc.add_job(
                name="j", message="m", every_secs=3600, **{field_name: non_string_value}
            )
        assert svc.list_jobs() == []

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_add_job_accepts_value_at_exact_cap(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        exact_value = "a" * cap
        job = svc.add_job(
            name="j", message="m", every_secs=3600, **{field_name: exact_value}
        )
        assert getattr(job, field_name) == exact_value


# ── Update-job tests ──


class TestUpdateJobFieldValidation:
    """_update_job_locked rejects non-string and oversize truthy values."""

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_update_job_rejects_oversize(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="max length"):
            svc.update_job(job.id, **{field_name: oversize_value})
        # Job unchanged
        reloaded = svc.list_jobs()[0]
        assert getattr(reloaded, field_name) == getattr(job, field_name)

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_update_job_rejects_non_string(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="must be a string"):
            svc.update_job(job.id, **{field_name: non_string_value})
        # Job unchanged
        reloaded = svc.list_jobs()[0]
        assert getattr(reloaded, field_name) == getattr(job, field_name)

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_update_job_accepts_value_at_exact_cap(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        exact_value = "a" * cap
        updated = svc.update_job(job.id, **{field_name: exact_value})
        assert updated is not None
        assert getattr(updated, field_name) == exact_value


# ── Falsy-skip semantics ──


class TestFalsySkipSemantics:
    """Falsy values (None, '') are no-ops in update and valid defaults in create."""

    @pytest.mark.parametrize("field_name", _FIELD_IDS)
    def test_update_with_none_is_noop(self, tmp_path, field_name):
        """Passing None for a field in update does not raise."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        # Should not raise - falsy values are skipped
        updated = svc.update_job(job.id, **{field_name: None})
        assert updated is not None

    @pytest.mark.parametrize("field_name", _FIELD_IDS)
    def test_update_with_empty_string_is_noop(self, tmp_path, field_name):
        """Passing '' for a field in update does not raise."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        # Should not raise - falsy values are skipped
        updated = svc.update_job(job.id, **{field_name: ""})
        assert updated is not None

    @pytest.mark.parametrize("field_name", _FIELD_IDS)
    def test_create_with_none_accepted_for_optional_fields(self, tmp_path, field_name):
        """Optional fields accept None at create time (falsy skip)."""
        svc = CronService(base_dir=tmp_path)
        # None is the default for optional fields; should not raise
        job = svc.add_job(name="j", message="m", every_secs=3600, **{field_name: None})
        assert job is not None


# ── Anti-drift: every persisted string field must be validated ──


class TestAntiDrift:
    """Structural guard: a new string field cannot ship without validation.

    Inspects the CronJob dataclass to enumerate all str-typed fields, removes
    the known set of runtime-only / non-caller-supplied fields, and asserts that
    every remaining field appears in CRON_VALIDATED_STRING_FIELDS.
    """

    # Fields excluded from the anti-drift assertion:
    # - id: generated internally by uuid.uuid4().hex[:8], never caller-supplied
    # - last_status: set only by the execution engine ("ok" | "error")
    # - last_error: set only by the execution engine on failure
    # - last_result: set only by set_run_result() during execution
    # - last_posted_hash: set by dedup logic when a Slack post is delivered
    # - last_failure_hash: set by dedup logic when a failure notification fires
    # - approval_mode: validated by a separate finite-set check, not length
    _RUNTIME_ONLY_FIELDS: frozenset[str] = frozenset(
        {
            "id",
            "last_status",
            "last_error",
            "last_result",
            "last_posted_hash",
            "last_failure_hash",
            "approval_mode",
        }
    )

    def test_all_persisted_string_fields_are_validated(self):
        """Every caller-supplied string field on CronJob has validation."""
        all_str_fields: set[str] = set()
        for f in dataclasses.fields(CronJob):
            # Accept str and str | None (Optional[str])
            annotation = f.type
            if annotation == "str" or annotation == "str | None":
                all_str_fields.add(f.name)

        # Remove runtime-only fields that are never caller-supplied
        persisted_caller_fields = all_str_fields - self._RUNTIME_ONLY_FIELDS

        missing = persisted_caller_fields - CRON_VALIDATED_STRING_FIELDS
        assert missing == set(), (
            f"String field(s) {sorted(missing)} on CronJob are not covered by "
            f"_CRON_STRING_FIELD_CAPS. Add them to the validation table or to "
            f"the _RUNTIME_ONLY_FIELDS exclusion set with a comment explaining why."
        )

    def test_validation_table_field_names_are_valid_cronjob_fields(self):
        """Every name in _CRON_STRING_FIELD_CAPS maps to an actual CronJob field.

        Catches typos in the table that would silently skip validation for the
        intended field.
        """
        cronjob_field_names = {f.name for f in dataclasses.fields(CronJob)}
        table_field_names = {name for name, _ in _CRON_STRING_FIELD_CAPS}

        invalid = table_field_names - cronjob_field_names
        assert invalid == set(), (
            f"Validation table contains field name(s) {sorted(invalid)} that do "
            f"not exist on CronJob. Fix the typo in _CRON_STRING_FIELD_CAPS."
        )

    def test_validated_fields_frozenset_matches_table(self):
        """CRON_VALIDATED_STRING_FIELDS exactly matches _CRON_STRING_FIELD_CAPS names."""
        table_names = frozenset(name for name, _ in _CRON_STRING_FIELD_CAPS)
        assert CRON_VALIDATED_STRING_FIELDS == table_names
