import base64
import json
from pathlib import Path

from oam.config import load_config
from oam.contracts.resources import ResourceMediaType
from oam.resources.html import parse_html
from oam.resources.images import inspect_images
from oam.resources.originals import OriginalDescriptor
from oam.resources.pdf import parse_pdf

REPOSITORY_ROOT = Path(__file__).parents[2]
UNIT_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "medical"
CONFORMANCE_FIXTURES = REPOSITORY_ROOT / "conformance" / "fixtures" / "medical"


def test_synthetic_medical_fixtures_cover_equivalent_html_pdf_and_image_inputs() -> None:
    manifest = json.loads((UNIT_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    limits = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config.resource

    html = parse_html(
        OriginalDescriptor.from_bytes(
            ResourceMediaType.HTML,
            (UNIT_FIXTURES / "synthetic-lab-report.html").read_bytes(),
        ),
        limits,
    )
    pdf = parse_pdf(
        OriginalDescriptor.from_bytes(
            ResourceMediaType.PDF,
            base64.b64decode(
                (UNIT_FIXTURES / "synthetic-lab-report.pdf.b64").read_text(encoding="ascii")
            ),
        ),
        limits,
    )
    images = inspect_images(
        (
            OriginalDescriptor.from_bytes(
                ResourceMediaType.PNG,
                base64.b64decode(
                    (UNIT_FIXTURES / "synthetic-lab-report.png.b64").read_text(encoding="ascii")
                ),
            ),
        ),
        limits,
    )

    assert manifest["synthetic"] is True
    assert manifest["contains_real_person_data"] is False
    for expected in ("140 mmol/L", "92 mg/dL", "2026-08-14"):
        assert expected in html.text
        assert expected in pdf.pages[0].text
    assert images.images[0].format == "PNG"
    assert images.images[0].width == 240
    assert images.images[0].height == 100


def test_conformance_fixture_copy_matches_unit_fixture_bytes() -> None:
    expected_names = {
        "manifest.json",
        "synthetic-lab-report.html",
        "synthetic-lab-report.pdf.b64",
        "synthetic-lab-report.png.b64",
    }

    assert {path.name for path in UNIT_FIXTURES.iterdir()} == expected_names
    assert {
        path.name for path in CONFORMANCE_FIXTURES.iterdir() if path.is_file()
    } == expected_names
    for name in expected_names:
        assert (UNIT_FIXTURES / name).read_bytes() == (CONFORMANCE_FIXTURES / name).read_bytes()
