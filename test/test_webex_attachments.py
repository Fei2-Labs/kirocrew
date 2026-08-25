"""Tests for kiro_crew.webex.attachments — inbound file ingest.

Everything channel-neutral (caps, classification, signature sniffing, temp-file
ownership, the SEL audit) belongs to ``messaging/attachments.py`` and is tested
there. What is Webex-specific and tested here: an opaque content URL becomes a
described :class:`Attachment` via a HEAD probe, and a probe that fails still
yields an entry so the failure surfaces to the user instead of the file
disappearing.
"""

from __future__ import annotations

import pytest

from kiro_crew.webex.attachments import outbound_mimetype, to_attachments


class FakeClient:
    """A client whose HEAD answers are scripted per URL."""

    def __init__(self, answers: dict[str, tuple[str, str, int]] | None = None) -> None:
        self.answers = answers or {}
        self.head_calls: list[str] = []
        self.downloads: list[tuple[str, str]] = []

    async def head_content(self, url: str) -> tuple[str, str, int]:
        self.head_calls.append(url)
        return self.answers.get(url, ("", "", 0))

    async def download_content(self, url: str, dest: str) -> None:
        self.downloads.append((url, dest))


class TestToAttachments:
    @pytest.mark.asyncio
    async def test_a_probe_describes_the_file(self) -> None:
        url = "https://webexapis.com/v1/contents/C1"
        client = FakeClient({url: ("report.pdf", "application/pdf", 2048)})

        [attachment] = await to_attachments(client, (url,))

        assert attachment.name == "report.pdf"
        assert attachment.mimetype == "application/pdf"
        assert attachment.size == 2048
        assert attachment.url == url

    @pytest.mark.asyncio
    async def test_a_failed_probe_still_yields_an_entry(self) -> None:
        """Silence is the wrong failure here.

        The shared ingest turns an unusable attachment into a visible rejection
        line. Dropping it instead would leave the user believing the agent saw
        their file, which is worse than an error.
        """
        url = "https://webexapis.com/v1/contents/C1"

        [attachment] = await to_attachments(FakeClient(), (url,))

        assert attachment.name == "attachment"
        assert attachment.url == url

    @pytest.mark.asyncio
    async def test_order_is_preserved(self) -> None:
        # The prompt reads the attachments in the order the user attached them.
        urls = tuple(f"https://webexapis.com/v1/contents/C{i}" for i in range(4))
        client = FakeClient({u: (f"f{i}.txt", "text/plain", 1) for i, u in enumerate(urls)})

        names = [a.name for a in await to_attachments(client, urls)]

        assert names == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]

    @pytest.mark.asyncio
    async def test_no_urls_makes_no_requests(self) -> None:
        client = FakeClient()
        assert await to_attachments(client, ()) == []
        assert client.head_calls == []

    @pytest.mark.asyncio
    async def test_a_hostile_filename_cannot_steer_the_temp_path(self) -> None:
        """The name comes from a Content-Disposition header, so it is untrusted.

        ``safe_suffix`` keeps exactly one leading dot (a suffix needs one) and
        strips every other non-alphanumeric character, so no separator, traversal
        segment or NUL can reach the path a temp file is created at.
        """
        url = "https://webexapis.com/v1/contents/C1"
        client = FakeClient({url: ("../../etc/passwd", "text/plain", 10)})

        [attachment] = await to_attachments(client, (url,))

        hint = attachment.suffix_hint
        assert hint.startswith(".") and hint.count(".") == 1
        assert hint[1:].isalnum()
        for hostile in ("/", "\\", "..", "\x00"):
            assert hostile not in hint


class TestOutboundMimetype:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/tmp/chart.png", "image/png"),
            ("/tmp/a.jpg", "image/jpeg"),
            ("/tmp/notes.txt", "text/plain"),
        ],
    )
    def test_a_known_extension_maps_to_its_type(self, path: str, expected: str) -> None:
        assert outbound_mimetype(path) == expected

    def test_an_unknown_extension_falls_back_to_binary(self) -> None:
        """A wrong guess costs a preview, not the delivery.

        Webex previews only a handful of types; everything else still uploads, so
        a generic binary type is the right fallback rather than a refusal.
        """
        assert outbound_mimetype("/tmp/thing.qqq") == "application/octet-stream"

    def test_only_the_basename_is_consulted(self) -> None:
        # A directory called "x.png" must not decide the type of a file that is not.
        assert outbound_mimetype("/tmp/x.png/data.txt") == "text/plain"
