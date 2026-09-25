"""Public synthetic bilingual report assets retain the same measurable facts."""

import hashlib
import json
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from oam.config import load_config
from oam.contracts.resources import ResourceMediaType
from oam.resources.html import parse_html
from oam.resources.originals import OriginalDescriptor
from oam.resources.pdf import parse_pdf

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "conformance/fixtures/medical/bilingual"


def test_public_bilingual_reports_keep_equivalent_facts_across_formats():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    limits = load_config(ROOT / "configs/oam.yaml").config.resource

    assert manifest["synthetic"] is True
    assert manifest["contains_real_person_data"] is False
    assert manifest["subject_code"] == "SYN-001"
    assert manifest["collected_on"] == "2026-08-14"
    for language, labels in (
        ("en", ("Sodium", "Glucose", "Potassium")),
        ("zh", ("钠", "葡萄糖", "钾")),
    ):
        assets = manifest["languages"][language]
        originals = {}
        for name in ("html", "native_pdf", "scanned_pdf", "image"):
            entry = assets[name]
            content = (FIXTURES / entry["file"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
            originals[name] = content

        html = parse_html(
            OriginalDescriptor.from_bytes(ResourceMediaType.HTML, originals["html"]), limits
        )
        native = parse_pdf(
            OriginalDescriptor.from_bytes(ResourceMediaType.PDF, originals["native_pdf"]),
            limits,
        )
        scanned = parse_pdf(
            OriginalDescriptor.from_bytes(ResourceMediaType.PDF, originals["scanned_pdf"]),
            limits,
        )
        assert scanned.page_count == 1
        assert scanned.pages[0].text == ""
        with Image.open(BytesIO(originals["image"])) as image:
            assert image.format == "PNG"
            assert image.width >= 800
            assert image.height >= 400
        for label, value in zip(labels, ("140 mmol/L", "92 mg/dL", "4.2 mmol/L"), strict=True):
            expected = f"{label} {value}"
            assert expected in html.text
            assert expected in native.pages[0].text
        assert "SYN-001" in html.text and "SYN-001" in native.pages[0].text
        assert "2026-08-14" in html.text and "2026-08-14" in native.pages[0].text


def test_allergy_report_has_three_native_text_pages_and_many_equivalent_rows():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    limits = load_config(ROOT / "configs/oam.yaml").config.resource
    for language, first_label, last_label in (
        ("en", "Dust mite", "Horse dander"),
        ("zh", "尘螨", "马皮屑"),
    ):
        entry = manifest["languages"][language]["allergy_pdf"]
        content = (FIXTURES / entry["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        parsed = parse_pdf(OriginalDescriptor.from_bytes(ResourceMediaType.PDF, content), limits)
        assert parsed.page_count == 3
        assert [page.page_number for page in parsed.pages] == [1, 2, 3]
        assert all(page.text for page in parsed.pages)
        combined = " ".join(page.text for page in parsed.pages)
        assert first_label in combined and last_label in combined
        assert combined.count("kUA/L") == 27
        assert all("SYN-001" in page.text for page in parsed.pages)
        with pdfium.PdfDocument(content) as document:
            for page in document:
                try:
                    assert all(
                        item.type == pdfium.raw.FPDF_PAGEOBJ_TEXT for item in page.get_objects()
                    )
                finally:
                    page.close()
