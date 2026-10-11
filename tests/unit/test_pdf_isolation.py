"""Local PDF libraries run in a bounded child, never in the durable worker."""

import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

from oam.config import load_config
from oam.contracts.resources import ResourceMediaType
from oam.resources.originals import OriginalDescriptor
from oam.resources.pdf_isolated import inspect_pdf, render_page
from oam.resources.types import ResourceLimitError, ResourceParseError

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "conformance/fixtures/medical/bilingual"
LIMITS = load_config(ROOT / "configs/oam.yaml").config.resource


def test_three_page_text_pdf_is_inspected_in_child_without_vision():
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.PDF, (FIXTURES / "en-allergy-native.pdf").read_bytes()
    )

    inspected = inspect_pdf(original, LIMITS)

    assert [page.page_number for page in inspected] == [1, 2, 3]
    assert all(page.needs_vision is False for page in inspected)
    assert "Dust mite" in inspected[0].text
    assert "Horse dander" in inspected[2].text


def test_scanned_page_is_rendered_in_child_with_bounded_pixels():
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.PDF, (FIXTURES / "en-scanned.pdf").read_bytes()
    )

    inspected = inspect_pdf(original, LIMITS)
    image, pixels = render_page(original, 1, LIMITS, remaining_pixels=LIMITS.max_total_pixels)

    assert inspected[0].text == ""
    assert inspected[0].needs_vision is True
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert 0 < pixels <= LIMITS.max_pixels_per_image


@pytest.mark.parametrize("width,height", [(100.25, 50.75), (1000, 1)])
def test_pdf_child_downscales_before_rendering_and_counts_actual_pixels(width, height):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    output = BytesIO()
    writer.write(output)
    original = OriginalDescriptor.from_bytes(ResourceMediaType.PDF, output.getvalue())
    limits = LIMITS.model_copy(update={"max_pixels_per_image": 100})

    image, pixels = render_page(original, 1, limits, remaining_pixels=100)

    with Image.open(BytesIO(image)) as decoded:
        assert decoded.width * decoded.height == pixels
        assert 0 < pixels <= 100
        assert decoded.width > decoded.height

    with pytest.raises(ResourceLimitError):
        render_page(original, 1, limits, remaining_pixels=pixels - 1)


def test_hung_pdf_child_becomes_parse_failure_without_fallback(monkeypatch):
    from oam.resources import pdf_isolated

    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.PDF, (FIXTURES / "en-native.pdf").read_bytes()
    )

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic PDF child", 0.1)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(ResourceParseError, match="timed out"):
        pdf_isolated.inspect_pdf(original, LIMITS)


def test_crashed_or_oversized_pdf_child_result_fails_without_fallback(monkeypatch):
    from oam.resources import pdf_isolated

    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.PDF, (FIXTURES / "en-native.pdf").read_bytes()
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], -9, b"", b""),
    )
    with pytest.raises(ResourceParseError, match="child failed"):
        pdf_isolated.inspect_pdf(original, LIMITS)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"x" * 80_001, b""),
    )
    with pytest.raises(ResourceLimitError, match="transfer limit"):
        pdf_isolated.inspect_pdf(original, LIMITS.model_copy(update={"max_bytes": 20_000}))
