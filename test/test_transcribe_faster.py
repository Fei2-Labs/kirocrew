"""The ``faster`` (faster-whisper) STT provider, and the hallucination filter.

Two things under test, and they are separable:

* The provider — dispatch, availability, and the fact that it needs neither a
  subprocess nor the system ffmpeg the CLI providers depend on.
* The hallucination filter — pure text logic applied to every Whisper-family
  provider. It matters because transcripts here go to agents: a hallucinated
  sign-off becomes a meeting note, and a phrase repeated forty times becomes
  forty note lines.

``faster_whisper`` is not installed (it is an on-demand runtime, not a declared
extra), so the library itself is always patched. That is the same situation CI is
in, which is the point.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.loader import (
    _VALID_STT_MODELS,
    _VALID_STT_PROVIDERS,
    SttConfig,
    _validated_stt_model,
)
from kiro_crew.dashboard.handlers.core import (
    _STT_MODEL_SIZES,
    _build_stt_install_script,
    _stt_prereq_commands,
)
from kiro_crew.transcribe import (
    _WHISPER_FAMILY_PROVIDERS,
    _collapse_repeated_phrases,
    _faster_whisper_model,
    _is_boilerplate_line,
    _run_faster_whisper_sync,
    filter_hallucinations,
    is_available,
    transcribe_audio,
)


@contextmanager
def _library_absent():
    """Simulate faster-whisper being uninstalled.

    Patching the cached class alone is not enough since the lazy helper retries
    the import — on a dev machine that happens to have the library, the retry
    would succeed and the "absent" test would silently test presence. Poisoning
    ``sys.modules`` makes the retry raise ImportError everywhere.
    """
    with patch.dict(sys.modules, {"faster_whisper": None}):
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            yield


@pytest.fixture(autouse=True)
def _clear_fw_model_cache():
    """Isolate the per-(model, device) instance cache between tests.

    The cache is a module global keyed on config values most tests share
    (turbo/cpu), so without clearing, one test's MagicMock model leaks into the
    next test's dispatch and every assertion after the first tests the cache,
    not the code.
    """
    from kiro_crew import transcribe

    transcribe._FW_MODEL_CACHE.clear()
    yield
    transcribe._FW_MODEL_CACHE.clear()


def _fake_model(text_segments: list[str]) -> MagicMock:
    """A stand-in for ``faster_whisper.WhisperModel`` yielding *text_segments*."""
    model = MagicMock()
    model.transcribe.return_value = (
        iter([MagicMock(text=t) for t in text_segments]),
        MagicMock(),
    )
    return model


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestProviderRegistration:
    def test_faster_is_a_valid_provider(self):
        assert "faster" in _VALID_STT_PROVIDERS

    def test_faster_is_in_the_whisper_family(self):
        # Which is what subjects it to the hallucination filter.
        assert "faster" in _WHISPER_FAMILY_PROVIDERS

    def test_transcribe_is_not_in_the_whisper_family(self):
        # AWS Transcribe uses a different decoder and does not produce these
        # artefacts, so filtering it could only ever delete real speech.
        assert "transcribe" not in _WHISPER_FAMILY_PROVIDERS


# ---------------------------------------------------------------------------
# Model enum
# ---------------------------------------------------------------------------


class TestModelEnum:
    def test_turbo_remains_the_default(self):
        assert SttConfig().model == "turbo"

    def test_every_size_is_accepted(self):
        for model in _VALID_STT_MODELS:
            assert _validated_stt_model(model) == model

    @pytest.mark.parametrize("offmenu", ["tiny.en", "base.en", "small.en", "medium.en", "large-v2"])
    def test_offmenu_string_models_pass_through_with_a_warning(self, offmenu):
        # openai-whisper legitimately accepts names outside the dashboard's size
        # menu; a hand-edited config holding one must NOT be silently coerced to
        # turbo — that would remove a real capability the old loader allowed.
        assert _validated_stt_model(offmenu) == offmenu

    def test_unknown_string_passes_through_rather_than_coercing(self):
        # Providers degrade safely per-recording on a bad name (logged, non-fatal),
        # so the loader's job is to warn, not to rewrite the user's config.
        assert _validated_stt_model("large-v9") == "large-v9"

    @pytest.mark.parametrize("bad", ["", None, 42, ["small"]])
    def test_non_string_or_empty_model_falls_back_instead_of_raising(self, bad):
        # A mangled config field must not stop the Gateway from starting, and a
        # non-string cannot be handed to any provider at all.
        assert _validated_stt_model(bad) == "turbo"

    def test_dashboard_offers_a_size_for_every_valid_model(self):
        # `_STT_MODEL_SIZES` is the dashboard's PUT allowlist, so a model the config
        # loader accepts but this dict omits would be silently rejected by the API.
        assert set(_STT_MODEL_SIZES) == set(_VALID_STT_MODELS)

    def test_every_size_is_human_readable(self):
        for model, size in _STT_MODEL_SIZES.items():
            assert size.startswith("~"), model
            assert size.endswith(("MB", "GB")), model


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_available_when_the_library_imports(self):
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", MagicMock()):
            assert is_available(cfg) is True

    def test_unavailable_when_the_library_is_missing(self):
        cfg = SttConfig(enabled=True, provider="faster")
        with _library_absent():
            assert is_available(cfg) is False

    def test_does_not_probe_for_ffmpeg(self):
        # faster-whisper decodes in-process through PyAV's bundled FFmpeg, so the
        # system binary is irrelevant. Probing for it would make availability depend
        # on something this provider never calls.
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", MagicMock()):
            with patch("kiro_crew.transcribe.ensure_ffmpeg_in_path") as ensure:
                assert is_available(cfg) is True
        ensure.assert_not_called()

    def test_disabled_beats_available(self):
        cfg = SttConfig(enabled=False, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", MagicMock()):
            assert is_available(cfg) is False

    def test_available_from_disk_before_anything_has_imported_it(self):
        # The regression this guards: a plain restart of an already-installed
        # gateway begins with an empty cache. While availability was a cached read,
        # Settings reported faster-whisper missing until something happened to run a
        # transcription. Locating the library on the import path answers correctly
        # from the first request.
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            with patch("importlib.util.find_spec", return_value=MagicMock()) as find:
                assert is_available(cfg) is True
        find.assert_called_once_with("faster_whisper")

    def test_never_imports_the_library(self):
        # This function runs on the event loop (config GET, Slack voice) and
        # importing faster_whisper links CTranslate2's native extension
        # synchronously, stalling every gateway task. The import must therefore be
        # unreachable from here whatever the cache holds — asserting "answers
        # correctly" is not enough, since the wrong implementation also answers
        # correctly and merely blocks the loop while doing it.
        cfg = SttConfig(enabled=True, provider="faster")

        def _fail_on_import(*_args, **_kwargs):
            pytest.fail("is_available imported faster_whisper on the event loop")

        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            with patch("importlib.util.find_spec", return_value=MagicMock()):
                with patch("importlib.import_module", _fail_on_import):
                    assert is_available(cfg) is True

    def test_a_broken_install_reports_unavailable_rather_than_raising(self):
        # A half-removed install can leave the name in sys.modules with no spec, so
        # find_spec raises instead of answering. This runs on the loop serving
        # /api/config/stt, where an exception is a 500 rather than a verdict.
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            with patch("importlib.util.find_spec", side_effect=ValueError("no spec")):
                assert is_available(cfg) is False


class TestLazyImportRetry:
    def test_helper_retries_the_import_after_an_on_demand_install(self):
        # The Settings install lands the library in this interpreter AFTER module
        # load cached None. Without a retry, the button reports "Done" while
        # availability stays False until a gateway restart.
        sentinel = MagicMock()
        fake_module = MagicMock(WhisperModel=sentinel)
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            with patch.dict(sys.modules, {"faster_whisper": fake_module}):
                assert _faster_whisper_model() is sentinel

    def test_helper_returns_none_while_the_library_is_absent(self):
        with _library_absent():
            assert _faster_whisper_model() is None

    def test_helper_prefers_the_cached_class(self):
        cached = MagicMock()
        with patch("kiro_crew.transcribe._FasterWhisperModel", cached):
            assert _faster_whisper_model() is cached


class TestModelMemoization:
    def test_same_model_and_device_constructs_once(self):
        # Constructing a WhisperModel re-loads and re-quantizes the weights;
        # concurrent recordings each holding a copy compounds to RAM exhaustion.
        model_cls = MagicMock(return_value=_fake_model(["one"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            _run_faster_whisper_sync("/tmp/b.wav", "turbo", "cpu")
        assert model_cls.call_count == 1

    def test_distinct_keys_get_distinct_instances(self):
        model_cls = MagicMock(side_effect=lambda *a, **k: _fake_model(["x"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            _run_faster_whisper_sync("/tmp/a.wav", "small", "cpu")
        assert model_cls.call_count == 2

    def test_switching_models_evicts_the_previous_instance(self):
        # SINGLE-SLOT on purpose: keeping every size ever selected resident
        # would accumulate multi-GB native models and OOM a small gateway host.
        from kiro_crew import transcribe

        model_cls = MagicMock(side_effect=lambda *a, **k: _fake_model(["x"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            _run_faster_whisper_sync("/tmp/a.wav", "large-v3", "cpu")
            assert list(transcribe._FW_MODEL_CACHE) == [("large-v3", "cpu")]
            # Switching BACK constructs again — correctness over reload cost.
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            assert list(transcribe._FW_MODEL_CACHE) == [("turbo", "cpu")]
        assert model_cls.call_count == 3

    def test_a_failed_construction_is_not_cached(self):
        # One bad load (e.g. interrupted download) must not poison every later
        # recording with a cached broken instance or a cached None.
        model_cls = MagicMock(side_effect=RuntimeError("load failed"))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None
        ok_cls = MagicMock(return_value=_fake_model(["recovered"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", ok_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") == "recovered"


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class TestRunFasterWhisperSync:
    def test_joins_segment_text(self):
        model_cls = MagicMock(return_value=_fake_model([" Hello ", "world. ", "  "]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") == "Hello world."

    def test_quantises_to_int8_on_the_configured_device(self):
        # int8 is what makes CPU inference fast enough to be usable on a
        # meeting-length recording.
        model_cls = MagicMock(return_value=_fake_model(["hi"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "small", "cuda")
        model_cls.assert_called_once_with("small", device="cuda", compute_type="int8")

    def test_empty_output_is_none_not_empty_string(self):
        model_cls = MagicMock(return_value=_fake_model(["   ", ""]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None

    def test_returns_none_when_the_library_is_missing(self):
        with _library_absent():
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None

    def test_an_inference_failure_is_logged_not_raised(self):
        # Same contract as every other provider: one bad recording must not take a
        # caller down.
        model_cls = MagicMock(side_effect=RuntimeError("model load failed"))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None


class TestDispatch:
    @pytest.mark.asyncio
    async def test_faster_provider_is_dispatched(self, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster", model="small", device="cpu")
        model_cls = MagicMock(return_value=_fake_model(["Real speech here."]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            result = await transcribe_audio(str(audio), cfg)
        assert result == "Real speech here."

    @pytest.mark.asyncio
    async def test_does_not_shell_out_or_need_ffmpeg(self, tmp_path):
        # The reason this provider is worth having: no binary discovery at all.
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        model_cls = MagicMock(return_value=_fake_model(["ok"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            with patch("kiro_crew.transcribe.ensure_ffmpeg_in_path") as ensure:
                with patch("kiro_crew.transcribe._run_whisper_cli") as cli:
                    assert await transcribe_audio(str(audio), cfg) == "ok"
        ensure.assert_not_called()
        cli.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_library_returns_none(self, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        with _library_absent():
            assert await transcribe_audio(str(audio), cfg) is None

    @pytest.mark.asyncio
    async def test_hallucinated_output_becomes_none(self, tmp_path):
        # The whole point of the filter being inside transcribe_audio: a recording of
        # silence must come back as "no transcript", not as boilerplate for an agent
        # to write into the meeting notes.
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        model_cls = MagicMock(return_value=_fake_model(["Subtitles by Amara.org."]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert await transcribe_audio(str(audio), cfg) is None


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------


class TestBoilerplateDetection:
    @pytest.mark.parametrize(
        "line",
        [
            "Subtitles by",
            "subtitles by amara.org.",
            "  Subtitled by!  ",
            "Captioned by.",
            "Subtitles by Amara.org",
        ],
    )
    def test_detects_boilerplate(self, line):
        assert _is_boilerplate_line(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "Let's ship the recording change on Friday.",
            "The copyright review is blocked on legal.",
            "I said goodbye to the old design.",
            # A sentence CONTAINING a boilerplate phrase is not boilerplate: these
            # are real speech on the normal path, and deleting them is silent
            # content loss (the blocking finding this rule exists to prevent).
            "Thanks for joining today's standup, let's start with Priya.",
            "I really do thank you for watching over the rollout last week.",
            "The transcript is available in the shared drive for everyone.",
            "See you next time we meet in Boston, bring the roadmap.",
            # Even ONE extra word must spare the sentence — "Thanks for joining,
            # everyone." is a normal meeting opener, not an artefact.
            "Thanks for joining, everyone.",
            "Thanks for watching this, team.",
            # Ordinary-speech phrases were REMOVED from the list entirely: a
            # dictated farewell or rights notice is plausible real speech even
            # as a complete utterance, so it must never be filtered.
            "Goodbye.",
            "goodbye",
            "Copyright",
            "All rights reserved.",
            "Thanks for listening.",
            "Thanks for joining.",
            "See you next time!",
            "The transcript is available.",
            # Sign-offs and subscribe CTAs were REMOVED from the list: each is a
            # sentence someone recording a demo or dictating a video script says
            # out loud, and a whole-transcript match discarded the recording.
            "Thank you for watching.",
            "Thanks for watching!",
            "Please subscribe.",
            "Like and subscribe.",
            "Please like and subscribe.",
            "Don't forget to subscribe.",
            "Hit the bell.",
            "Click the subscribe button.",
            "See you in the next video.",
        ],
    )
    def test_keeps_real_speech(self, line):
        assert _is_boilerplate_line(line) is False

    def test_all_phrases_require_a_whole_line_match(self):
        # Substring or word-count-proximity matching deletes real sentences that
        # merely mention (or lightly extend) a phrase — the cases above.
        assert _is_boilerplate_line("Transcribed by") is True
        assert _is_boilerplate_line("We kept transcribed by in the caption doc") is False

    def test_every_listed_phrase_is_a_caption_artefact(self):
        # LIST DISCIPLINE, tightened: an entry must be attribution text a caption
        # track carries ABOUT ITSELF, not merely "video-flavoured". The looser
        # caption-domain rule is what admitted "thank you for watching" and
        # "hit the bell" — sentences a human genuinely records, which the
        # whole-transcript path then deleted. Required direction:
        import re as _re

        attribution_markers = _re.compile(r"subtitle|caption|transcri|translat|amara|mooji")
        from kiro_crew.transcribe import _WHISPER_BOILERPLATE as phrases

        for phrase in phrases:
            assert attribution_markers.search(phrase), (
                f"'{phrase}' is not caption self-attribution — it may be real"
                " dictated speech, so it must not be on the filter list"
            )

    def test_no_listed_phrase_is_a_spoken_sign_off(self):
        # Forbidden direction, and the half that actually holds the line: the
        # required-marker test above passes for "subscribe to my subtitles too",
        # so the vocabulary of speech a presenter utters is banned outright. This
        # fails if a future edit re-adds any entry of the deleted class.
        import re as _re

        speech_markers = _re.compile(r"watch|subscribe|bell|video|thank|see you|like and")
        from kiro_crew.transcribe import _WHISPER_BOILERPLATE as phrases

        for phrase in phrases:
            assert not speech_markers.search(phrase), (
                f"'{phrase}' reads as something a speaker says on a recording;"
                " filtering it can delete the only words a transcript had"
            )

    def test_known_artefact_variants_are_listed_as_full_phrases(self):
        # "Subtitles by Amara.org" is the canonical artefact shape; it matches by
        # being IN the phrase list, not by loosening the match rule.
        assert _is_boilerplate_line("Subtitles by Amara.org") is True
        assert _is_boilerplate_line("Subtitles by the Amara.org community") is True


class TestCollapseRepeatedPhrases:
    def test_collapses_a_long_run_to_one(self):
        text = " ".join(["Thank you."] * 12)
        assert _collapse_repeated_phrases(text) == "Thank you."

    def test_leaves_a_short_run_alone(self):
        # Real emphasis reaches well past two — "No. No. No." is ordinary
        # insistence, and even five repeats is plausible counted speech. Only
        # dozens-long runs are the Whisper artefact.
        for n in range(2, 6):
            text = " ".join(["No."] * n)
            assert _collapse_repeated_phrases(text) == text, n

    def test_only_consecutive_runs_collapse(self):
        # The same sentence recurring later in a meeting is ordinary speech.
        text = "Okay. Next item. Okay."
        assert _collapse_repeated_phrases(text) == "Okay. Next item. Okay."

    def test_preserves_surrounding_speech(self):
        run = " ".join(["Uh huh."] * 8)
        text = f"We start now. {run} Then we ship."
        assert _collapse_repeated_phrases(text) == "We start now. Uh huh. Then we ship."

    def test_single_sentence_is_untouched(self):
        assert _collapse_repeated_phrases("Just one sentence") == "Just one sentence"


class TestFilterHallucinations:
    def test_empty_input_is_returned_as_is(self):
        assert filter_hallucinations("") == ""

    def test_real_speech_survives_intact(self):
        text = "We agreed to ship on Friday. Priya owns the rollout."
        assert filter_hallucinations(text) == text

    def test_a_fully_hallucinated_transcript_becomes_empty(self):
        # Which the caller turns into None. An empty string is the honest answer for
        # a recording of silence.
        assert filter_hallucinations("Subtitles by Amara.org. Transcribed by.") == ""

    def test_strips_boilerplate_but_keeps_the_meeting(self):
        text = "Priya owns the rollout. Subtitles by Amara.org. We ship Friday."
        assert filter_hallucinations(text) == "Priya owns the rollout. We ship Friday."

    def test_a_dictated_sign_off_survives_whole(self):
        """The GPT 5.6 blocking finding, pinned.

        Each of these is a complete sentence a human records — a demo outro, a
        dictated video script — and each was previously deleted by an exact
        whole-sentence match. When it was the entire transcript the filter
        returned "", which ``transcribe_audio`` turns into ``None``: the only
        words the recording held, gone, with a log line as the sole trace.
        """
        for text in (
            "Thank you for watching.",
            "Thanks for watching!",
            "Please subscribe.",
            "Don't forget to subscribe.",
            "Hit the bell.",
            "See you in the next video.",
        ):
            assert filter_hallucinations(text) == text, text

    def test_handles_both_artefacts_together(self):
        run = " ".join(["Okay."] * 10)
        text = f"{run} Ship it. Transcribed by."
        assert filter_hallucinations(text) == "Okay. Ship it."


class TestFilterHallucinationsVisibility:
    """The filter is the one step that can delete words the speaker said.

    A silent deletion is indistinguishable from the model never having heard the
    words, so every removal has to leave a trace an operator can find after the
    fact. These tests pin what the trace says, not merely that one exists.
    """

    def test_dropped_boilerplate_is_named_in_the_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="kiro_crew.transcribe"):
            filter_hallucinations("Priya owns the rollout. Subtitles by Amara.org.")
        assert "dropped 1 boilerplate line(s)" in caplog.text
        assert "Subtitles by Amara.org." in caplog.text

    def test_collapsed_repetitions_are_counted_but_not_quoted(self, caplog):
        # A repeated sentence is ordinary speech; its text belongs in the
        # transcript, not in the log, so only the count is recorded.
        with caplog.at_level(logging.INFO, logger="kiro_crew.transcribe"):
            filter_hallucinations(" ".join(["Ship the thing."] * 8))
        assert "collapsed 7 repeated sentence(s)" in caplog.text
        assert "Ship the thing" not in caplog.text

    def test_discarding_the_whole_transcript_warns(self, caplog):
        # The caller turns "" into None and the recording is gone with no other
        # trace, so this case is a warning rather than an info line.
        with caplog.at_level(logging.INFO, logger="kiro_crew.transcribe"):
            assert filter_hallucinations("Subtitles by Amara.org. Transcribed by.") == ""
        assert "discarded the entire transcript" in caplog.text
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_an_untouched_transcript_logs_nothing(self, caplog):
        # Every recording passes through here. A line per transcription would bury
        # the removals this logging exists to surface.
        with caplog.at_level(logging.INFO, logger="kiro_crew.transcribe"):
            filter_hallucinations("We agreed to ship on Friday.")
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Install path
# ---------------------------------------------------------------------------


class TestInstallScript:
    def test_installs_into_the_gateways_own_interpreter(self):
        # The library is imported IN-PROCESS by kiro_crew.transcribe, so the one
        # environment that matters is sys.executable's. A system python's
        # user-site would be invisible here — and inside a venv pip refuses
        # `--user` outright — so the script must target the gateway interpreter
        # and must not pass `--user`.
        script = _build_stt_install_script("faster")
        assert "pip install -q faster-whisper" in script
        assert "--user" not in script
        assert sys.executable in script

    def test_does_not_probe_for_a_system_python(self):
        # The $PY probe belongs to the CLI providers, whose binary any python can
        # own. Probing here risks installing into an interpreter the gateway
        # never imports from.
        assert "for py in" not in _build_stt_install_script("faster")

    def test_does_not_install_ffmpeg(self):
        # It is not needed, and installing it would make the button slower and more
        # failure-prone for no benefit.
        script = _build_stt_install_script("faster")
        assert "brew install ffmpeg" not in script
        assert "openai-whisper" not in script

    def test_documents_the_windows_arm_gap(self):
        # CTranslate2 publishes no wheel there, so the install cannot succeed and the
        # script should say why rather than fail opaquely.
        assert "Windows on ARM" in _build_stt_install_script("faster")

    def test_includes_the_path_prelude(self):
        # A brew-installed python3 is common on macOS, and the gateway's inherited
        # PATH does not contain the Homebrew prefix.
        assert "brew shellenv" in _build_stt_install_script("faster")

    def test_emits_the_progress_line_the_status_parser_matches(self):
        # `_stt_install_status` keys the `installing_faster` step off this exact text.
        assert "Installing faster-whisper" in _build_stt_install_script("faster")

    def test_requires_no_manual_prerequisites(self):
        assert _stt_prereq_commands("faster") == []

    def test_a_non_importable_install_fails_the_script(self):
        # pip can report success while the package is unusable — a CTranslate2
        # wheel whose native extension will not load is the common case. The
        # verification import therefore has to be able to fail the script: while
        # its failure was swallowed into a "check install" note, the script exited
        # 0 and the caller reported the provider ready.
        script = _build_stt_install_script("faster")
        assert "check install" not in script
        assert "is not importable" in script
        assert "exit 1" in script.split("Installing faster-whisper")[1]


# ---------------------------------------------------------------------------
# Inference bulkhead + timeout
# ---------------------------------------------------------------------------


class TestInferenceExecutor:
    """Inference runs on its OWN pool, not the PTY-teardown one.

    A started ``run_in_executor`` future cannot be cancelled, so a wedged model load
    (or a first-run multi-GB weight download inside the library's constructor) holds
    its worker until the process exits. On ``subprocess_executor`` that would consume
    one of the eight workers whose whole purpose is absorbing a teardown storm — the
    recovery path would be starved by the thing it recovers from.
    """

    def test_transcribe_binds_the_stt_pool(self):
        from kiro_crew import executors, transcribe

        assert transcribe.stt_executor is executors.stt_executor

    def test_stt_pool_is_distinct_from_the_teardown_pool(self):
        from kiro_crew import executors

        assert executors.stt_executor() is not executors.subprocess_executor()

    def test_stt_pool_threads_are_identifiable_in_a_stack_dump(self):
        from kiro_crew import executors

        assert executors.stt_executor()._thread_name_prefix == "mc-stt"

    def test_pool_is_bounded_because_each_worker_holds_a_model(self):
        # The worker count is a MEMORY ceiling, not just a CPU one: every in-flight
        # call keeps a fully quantised model resident (up to ~GBs for large-v3).
        from kiro_crew import executors

        assert executors.stt_executor()._max_workers == 2

    @pytest.mark.asyncio
    async def test_inference_is_submitted_to_the_stt_pool(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="probe-stt")
        try:
            model_cls = MagicMock(return_value=_fake_model(["ok"]))
            with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
                with patch("kiro_crew.transcribe.stt_executor", return_value=pool) as chosen:
                    assert await transcribe_audio(str(audio), cfg) == "ok"
            chosen.assert_called_once()
        finally:
            pool.shutdown(wait=True)


class TestInferenceTimeout:
    """``stt.timeout_secs`` bounds the faster path like it bounds the CLI providers.

    Before this, the future was unbounded: a wedged inference left the dictation
    request hanging with no ceiling at all.
    """

    @pytest.mark.asyncio
    async def test_a_wedged_inference_returns_none_instead_of_hanging(self, tmp_path):
        import threading

        from kiro_crew import transcribe

        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster", timeout_secs=1)
        release = threading.Event()

        def _wedged(*_a, **_k):
            # Bounded so a failed assertion cannot leak a thread for the whole run;
            # the test releases it explicitly below.
            release.wait(timeout=30)
            return "arrived too late"

        try:
            with patch("kiro_crew.transcribe._run_faster_whisper_sync", _wedged):
                assert await transcribe.transcribe_audio(str(audio), cfg) is None
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_the_timeout_is_logged_as_releasing_the_caller_only(self, tmp_path, caplog):
        """The log line must not imply the work was cancelled.

        ``asyncio.wait_for`` cannot interrupt a running thread, so the inference (or
        the download it is stuck in) continues and its worker stays occupied. An
        operator reading "timed out" would otherwise assume the slot was freed.
        """
        import threading

        from kiro_crew import transcribe

        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster", timeout_secs=1)
        release = threading.Event()

        def _wedged(*_a, **_k):
            release.wait(timeout=30)
            return None

        try:
            with caplog.at_level("ERROR", logger="kiro_crew.transcribe"):
                with patch("kiro_crew.transcribe._run_faster_whisper_sync", _wedged):
                    await transcribe.transcribe_audio(str(audio), cfg)
        finally:
            release.set()
        assert "timed out" in caplog.text
        assert "cannot be cancelled" in caplog.text

    @pytest.mark.asyncio
    async def test_a_prompt_transcription_is_unaffected(self, tmp_path):
        # The bound must not clip normal work: the same generous default every other
        # provider uses applies here.
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        model_cls = MagicMock(return_value=_fake_model(["Real speech here."]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert await transcribe_audio(str(audio), cfg) == "Real speech here."
