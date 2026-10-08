from dataclasses import FrozenInstanceError
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.html import parse_html
from oam.resources.images import inspect_images
from oam.resources.originals import OriginalDescriptor
from oam.resources.pdf import native_text_usable, parse_pdf
from oam.resources.types import ResourceLimitError, ResourceParseError


def _limits(**overrides: int) -> ResourceConfig:
    values = {
        "max_output_tokens": 4096,
        "max_bytes": 1_024,
        "url_fetch_timeout_seconds": 30,
        "pdf_child_timeout_seconds": 30,
        "max_pdf_pages": 2,
        "max_images": 2,
        "max_pixels_per_image": 100,
        "max_total_pixels": 150,
    }
    values.update(overrides)
    return ResourceConfig(**values)


def _image_bytes(width: int, height: int, image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color=(17, 91, 140)).save(output, format=image_format)
    return output.getvalue()


def _pdf_bytes(page_texts: tuple[str, ...]) -> bytes:
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_original_descriptor_is_immutable_and_verifies_exact_bytes() -> None:
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.HTML,
        b"<p>Synthetic result: 140 mmol/L</p>",
    )

    assert original.byte_length == len(original.content)
    assert len(original.sha256) == 64
    with pytest.raises(FrozenInstanceError):
        original.content = b"changed"  # type: ignore[misc]


def test_html_parser_returns_stable_ordered_block_locators_and_plain_text() -> None:
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.HTML,
        (
            b"<html><script>ignore()</script><h1>Lab report</h1>"
            b"<p>Sodium <b>140</b> mmol/L</p>"
            b"<ul><li>Status: synthetic</li></ul></html>"
        ),
    )

    parsed = parse_html(original, _limits())
    repeated = parse_html(original, _limits())

    assert [(block.locator, block.text) for block in parsed.blocks] == [
        ("html:block:0001", "Lab report"),
        ("html:block:0002", "Sodium 140 mmol/L"),
        ("html:block:0003", "Status: synthetic"),
    ]
    assert parsed == repeated
    assert "ignore" not in parsed.text


def test_html_parser_preserves_available_fields_for_passage_discovery() -> None:
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.HTML,
        b"<html><head><title>Allergy report</title></head><body><h1>Results</h1>"
        b"<p>Peanut allergy absent</p><table><caption>Allergens</caption>"
        b"<tr><td>Peanut</td><td>none</td></tr></table></body></html>",
    )

    blocks = parse_html(original, _limits()).blocks

    assert blocks[1].text == "Peanut allergy absent"
    assert (blocks[1].title, blocks[1].heading, blocks[1].caption) == (
        "Allergy report",
        "Results",
        None,
    )
    assert blocks[3].text == "Peanut none"
    assert blocks[3].caption == "Allergens"


def test_html_parser_rejects_oversize_bytes_before_parsing() -> None:
    original = OriginalDescriptor.from_bytes(ResourceMediaType.HTML, b"<" * 11)

    with pytest.raises(ResourceLimitError, match="byte limit.*10"):
        parse_html(original, _limits(max_bytes=10))


def test_pdf_parser_extracts_page_text_and_optional_render_metadata() -> None:
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.PDF,
        _pdf_bytes(("Synthetic sodium 140 mmol/L", "Synthetic glucose 92 mg/dL")),
    )

    parsed = parse_pdf(
        original,
        _limits(max_bytes=10_000),
        include_render_metadata=True,
    )

    assert parsed.page_count == 2
    assert [(page.locator, page.page_number, page.text) for page in parsed.pages] == [
        ("pdf:page:0001", 1, "Synthetic sodium 140 mmol/L"),
        ("pdf:page:0002", 2, "Synthetic glucose 92 mg/dL"),
    ]
    assert parsed.pages[0].rendering is not None
    assert parsed.pages[0].rendering.width_pixels == 612
    assert parsed.pages[0].rendering.height_pixels == 792


def test_pdf_parser_rejects_page_limit_before_text_extraction() -> None:
    original = OriginalDescriptor.from_bytes(
        ResourceMediaType.PDF,
        _pdf_bytes(("first", "second")),
    )

    with pytest.raises(ResourceLimitError, match="page limit.*1"):
        parse_pdf(original, _limits(max_bytes=10_000, max_pdf_pages=1))


def test_pdf_text_layer_quality_distinguishes_legible_from_corrupt_text() -> None:
    assert native_text_usable("Sodium 140 mmol/L with one �")
    assert not native_text_usable("�\x00�\x00�\x00�\x00�\x00")
    assert not native_text_usable("")


def test_native_pdf_preserves_lines_indentation_and_usable_formatting() -> None:
    text = "Release procedure\n    run --safe\n\nException\nDo not restart."
    original = OriginalDescriptor.from_bytes(ResourceMediaType.PDF, _pdf_bytes((text,)))

    parsed = parse_pdf(original, _limits(max_bytes=10_000))

    assert parsed.pages[0].text == text
    assert native_text_usable("A\nB\nC\nD\nE\nF\nG\nH")
    assert native_text_usable("x\t1\ny\t2\nz\t3")
    assert not native_text_usable(" \n\t ")


def test_image_inspection_reports_format_dimensions_and_stable_locators() -> None:
    images = (
        OriginalDescriptor.from_bytes(ResourceMediaType.PNG, _image_bytes(10, 10)),
        OriginalDescriptor.from_bytes(
            ResourceMediaType.JPEG, _image_bytes(5, 5, image_format="JPEG")
        ),
    )

    parsed = inspect_images(images, _limits(max_bytes=10_000))

    assert [image.locator for image in parsed.images] == ["image:0001", "image:0002"]
    assert [image.format for image in parsed.images] == ["PNG", "JPEG"]
    assert [(image.width, image.height, image.pixels) for image in parsed.images] == [
        (10, 10, 100),
        (5, 5, 25),
    ]
    assert parsed.total_pixels == 125


def test_image_limits_reject_count_each_image_and_total_pixels() -> None:
    small = OriginalDescriptor.from_bytes(ResourceMediaType.PNG, _image_bytes(10, 10))
    second = OriginalDescriptor.from_bytes(ResourceMediaType.PNG, _image_bytes(6, 6))
    oversized = OriginalDescriptor.from_bytes(ResourceMediaType.PNG, _image_bytes(11, 10))

    with pytest.raises(ResourceLimitError, match="image count limit.*1"):
        inspect_images((small, second), _limits(max_bytes=10_000, max_images=1))
    with pytest.raises(ResourceLimitError, match="per-image pixel limit.*100"):
        inspect_images((oversized,), _limits(max_bytes=10_000))
    with pytest.raises(ResourceLimitError, match="total pixel limit.*120"):
        inspect_images(
            (small, second),
            _limits(max_bytes=10_000, max_total_pixels=120),
        )


def test_image_resource_rejects_aggregate_bytes_before_decoding() -> None:
    first = OriginalDescriptor.from_bytes(ResourceMediaType.PNG, _image_bytes(2, 2))
    second = OriginalDescriptor.from_bytes(ResourceMediaType.PNG, _image_bytes(2, 2))
    aggregate_limit = first.byte_length + second.byte_length - 1

    with pytest.raises(ResourceLimitError, match="aggregate byte limit"):
        inspect_images(
            (first, second),
            _limits(max_bytes=aggregate_limit),
        )


def test_image_inspection_normalizes_corrupt_payload_to_resource_parse_error() -> None:
    corrupt = bytearray(_image_bytes(2, 2))
    corrupt[-8] ^= 0xFF
    original = OriginalDescriptor.from_bytes(ResourceMediaType.PNG, bytes(corrupt))

    with pytest.raises(ResourceParseError, match="malformed or unreadable"):
        inspect_images((original,), _limits(max_bytes=10_000))
