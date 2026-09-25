"""Regenerate small, synthetic English and Chinese report fixtures.

Use ``uv run --with reportlab python generate.py --font /path/to/NotoSansSC.ttf``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parent
DATE = "2026-08-14"
SUBJECT = "SYN-001"
VALUES = ("140 mmol/L", "92 mg/dL", "4.2 mmol/L")
REPORTS = {
    "en": (
        "Synthetic laboratory report",
        "Subject",
        "Collected",
        ("Sodium", "Glucose", "Potassium"),
    ),
    "zh": ("合成化验报告", "编号", "采样日期", ("钠", "葡萄糖", "钾")),
}
ALLERGENS = (
    ("Dust mite", "尘螨"),
    ("Cat dander", "猫皮屑"),
    ("Dog dander", "狗皮屑"),
    ("Birch pollen", "桦树花粉"),
    ("Timothy grass", "梯牧草"),
    ("Ragweed", "豚草"),
    ("Mold mix", "霉菌混合物"),
    ("Oak pollen", "橡树花粉"),
    ("Cedar pollen", "雪松花粉"),
    ("Peanut", "花生"),
    ("Egg white", "蛋清"),
    ("Cow milk", "牛奶"),
    ("Wheat", "小麦"),
    ("Soybean", "大豆"),
    ("Shrimp", "虾"),
    ("Walnut", "核桃"),
    ("Almond", "杏仁"),
    ("Sesame", "芝麻"),
    ("Latex", "乳胶"),
    ("Cockroach", "蟑螂"),
    ("Rye grass", "黑麦草"),
    ("Mugwort", "艾蒿"),
    ("Alternaria", "链格孢霉"),
    ("Cladosporium", "枝孢霉"),
    ("Penicillium", "青霉"),
    ("Aspergillus", "曲霉"),
    ("Horse dander", "马皮屑"),
)


def html_report(title: str, subject_label: str, date_label: str, labels: tuple[str, ...]) -> bytes:
    rows = "\n".join(
        f"<tr><td>{label}</td><td>{value}</td></tr>"
        for label, value in zip(labels, VALUES, strict=True)
    )
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8"><title>'
        f"{title}</title></head><body><h1>{title}</h1>"
        f"<p>{subject_label} {SUBJECT}</p><p>{date_label} {DATE}</p>"
        f"<table>{rows}</table></body></html>\n"
    ).encode()


def native_pdf(
    title: str,
    subject_label: str,
    date_label: str,
    labels: tuple[str, ...],
    *,
    vector_table: bool = False,
) -> bytes:
    output = BytesIO()
    canvas = Canvas(output, pagesize=(612, 792), invariant=1, pageCompression=1)
    canvas.setTitle(title)
    canvas.setFont("FixtureNoto", 20)
    canvas.drawString(52, 720, title)
    canvas.setFont("FixtureNoto", 12)
    for y, line in zip(
        (672, 646, 590, 560, 530),
        (
            f"{subject_label} {SUBJECT}",
            f"{date_label} {DATE}",
            *(f"{label} {value}" for label, value in zip(labels, VALUES, strict=True)),
        ),
        strict=True,
    ):
        canvas.drawString(52, y, line)
    if vector_table:
        canvas.setStrokeColorRGB(0.5, 0.5, 0.5)
        for y in (615, 585, 555, 525, 495):
            canvas.line(48, y, 560, y)
        for x in (48, 300, 560):
            canvas.line(x, 495, x, 615)
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def allergy_pdf(language: str, subject_label: str, date_label: str) -> bytes:
    output = BytesIO()
    canvas = Canvas(output, pagesize=(612, 792), invariant=1, pageCompression=1)
    canvas.setTitle("Synthetic allergy panel" if language == "en" else "合成过敏原检测报告")
    sections = (
        ("Environmental allergens", "环境过敏原"),
        ("Food allergens", "食物过敏原"),
        ("Additional allergens", "其他过敏原"),
    )
    for page_number, section in enumerate(sections, start=1):
        canvas.setFont("FixtureNoto", 20)
        canvas.drawString(
            52, 726, "Synthetic allergy panel" if language == "en" else "合成过敏原检测报告"
        )
        canvas.setFont("FixtureNoto", 11)
        canvas.drawString(52, 684, f"{subject_label} {SUBJECT}")
        canvas.drawString(52, 660, f"{date_label} {DATE}")
        canvas.drawString(
            52,
            630,
            f"Page {page_number} of 3" if language == "en" else f"第 {page_number} 页，共 3 页",
        )
        canvas.setFont("FixtureNoto", 14)
        canvas.drawString(52, 590, section[0 if language == "en" else 1])
        canvas.setFont("FixtureNoto", 11)
        for row_on_page, (english, chinese) in enumerate(
            ALLERGENS[(page_number - 1) * 9 : page_number * 9]
        ):
            index = (page_number - 1) * 9 + row_on_page
            value = (index * 17 % 95 + 5) / 100
            label = english if language == "en" else chinese
            canvas.drawString(
                60, 550 - row_on_page * 43, f"{index + 1:02d}. {label} {value:.2f} kUA/L"
            )
        canvas.drawString(
            52,
            84,
            "Fictional values; no clinical interpretation."
            if language == "en"
            else "数值为虚构示例，不作临床解读。",
        )
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def report_image(
    font: Path, title: str, subject_label: str, date_label: str, labels: tuple[str, ...]
) -> Image.Image:
    image = Image.new("RGB", (1200, 650), "white")
    draw = ImageDraw.Draw(image)
    heading = ImageFont.truetype(str(font), 40)
    body = ImageFont.truetype(str(font), 27)
    draw.text((64, 42), title, font=heading, fill="#111827")
    draw.text((64, 126), f"{subject_label} {SUBJECT}", font=body, fill="#111827")
    draw.text((64, 176), f"{date_label} {DATE}", font=body, fill="#111827")
    draw.line((64, 238, 1136, 238), fill="#9ca3af", width=2)
    for index, (label, value) in enumerate(zip(labels, VALUES, strict=True)):
        y = 274 + index * 84
        draw.text((80, y), label, font=body, fill="#111827")
        draw.text((620, y), value, font=body, fill="#111827")
        draw.line((64, y + 56, 1136, y + 56), fill="#d1d5db", width=1)
    return image


def generate(font: Path) -> None:
    if not font.is_file():
        raise FileNotFoundError(font)
    pdfmetrics.registerFont(TTFont("FixtureNoto", str(font)))
    manifest = {
        "synthetic": True,
        "contains_real_person_data": False,
        "subject_code": SUBJECT,
        "collected_on": DATE,
        "languages": {},
    }
    for language, (title, subject_label, date_label, labels) in REPORTS.items():
        image = report_image(font, title, subject_label, date_label, labels)
        png_output = BytesIO()
        image.save(png_output, format="PNG", optimize=True)
        scan_output = BytesIO()
        image.save(scan_output, format="PDF", resolution=144.0)
        assets = {
            "html": (
                f"{language}-report.html",
                html_report(title, subject_label, date_label, labels),
            ),
            "native_pdf": (
                f"{language}-native.pdf",
                native_pdf(title, subject_label, date_label, labels),
            ),
            "scanned_pdf": (f"{language}-scanned.pdf", scan_output.getvalue()),
            "image": (f"{language}-report.png", png_output.getvalue()),
            "allergy_pdf": (
                f"{language}-allergy-native.pdf",
                allergy_pdf(language, subject_label, date_label),
            ),
        }
        if language == "en":
            assets["vector_table_pdf"] = (
                "en-vector-table.pdf",
                native_pdf(title, subject_label, date_label, labels, vector_table=True),
            )
        manifest["languages"][language] = {}
        for kind, (name, content) in assets.items():
            (ROOT / name).write_bytes(content)
            manifest["languages"][language][kind] = {
                "file": name,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True, help="Local Noto Sans SC font file")
    generate(parser.parse_args().font)
