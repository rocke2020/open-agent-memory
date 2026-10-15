"""Deterministic query-time calendar windows for non-generative recall."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser

from oam.contracts.search import TimeWindow


@dataclass(frozen=True)
class QueryTime:
    window: TimeWindow | None
    expression: str | None
    status: str


_CJK_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})年(?:(\d{1,2})月(?:(\d{1,2})日?)?)?")
_CJK_DATETIME = re.compile(
    r"(?<!\d)((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(\d{1,2})分"
)
_ISO_CLOCK_CANDIDATE = re.compile(
    r"(?<![\w])(?:19|20)\d{2}-\d{1,2}-\d{1,2}[T ]"
    r"\d{1,2}:\d{1,2}(?::\d{1,2})?(?:Z|[+-]\d{2}:\d{2})?"
)
_ISO_DATETIME = re.compile(
    r"(?<![\w])(?:19|20)\d{2}-\d{1,2}-\d{1,2}[T ]"
    r"(?:[01]\d|2[0-3]):[0-5]\d(?P<second>:[0-5]\d)?(?:Z|[+-]\d{2}:\d{2})?(?!\d)"
)
_ISO_DATE = re.compile(r"(?<![\w])((?:19|20)\d{2})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?!\d)")
_ENGLISH_MONTH = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(?:(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+)?(?:19|20)\d{2}\b",
    re.IGNORECASE,
)
_ENGLISH_RELATIVE = re.compile(
    r"\b(?:today|yesterday|tomorrow|(?:last|this|next)\s+(?:week|month|year))\b", re.IGNORECASE
)
_DATEPARSER_RELATIVE = re.compile(
    r"\b\d{1,3}\s+(?:day|week|month|year)s?\s+ago\b|\d{1,3}(?:天|周|个月|年)前", re.IGNORECASE
)
_CJK_RELATIVE = re.compile(
    "再来週|先週|来週|今週|上周|下周|本周|上星期|下星期|这星期|一昨年|再来年|昨年|去年|今年|明年|"
    "上个月|这个月|下个月|先月|今月|来月|前天|昨天|今天|明天|后天|一昨日|昨日|今日|明日|明後日"
)


def _calendar_window(start: date, end: date, zone: ZoneInfo) -> TimeWindow:
    return TimeWindow(
        start=datetime(start.year, start.month, start.day, tzinfo=zone).astimezone(UTC),
        end=datetime(end.year, end.month, end.day, tzinfo=zone).astimezone(UTC),
    )


def _next_month(year: int, month: int) -> date:
    return date(year + (month == 12), 1 if month == 12 else month + 1, 1)


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous local query time")
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != value:
        raise ValueError("nonexistent local query time")
    return first


def _precise_window(value: datetime, *, seconds: bool) -> TimeWindow:
    start = value.astimezone(UTC)
    return TimeWindow(start=start, end=start + timedelta(seconds=1 if seconds else 60))


def _absolute(match: re.Match[str], zone: ZoneInfo) -> TimeWindow:
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    day = int(match.group(3)) if match.group(3) else None
    if day is not None:
        start = date(year, month, day)
        end = start + timedelta(days=1)
    elif month is not None:
        start = date(year, month, 1)
        end = _next_month(year, month)
    else:
        start, end = date(year, 1, 1), date(year + 1, 1, 1)
    return _calendar_window(start, end, zone)


def _relative(expression: str, today: date, zone: ZoneInfo) -> TimeWindow:
    name = " ".join(expression.lower().split())
    day_offsets = {
        "today": 0,
        "yesterday": -1,
        "tomorrow": 1,
        "今天": 0,
        "昨天": -1,
        "明天": 1,
        "前天": -2,
        "后天": 2,
        "今日": 0,
        "昨日": -1,
        "明日": 1,
        "一昨日": -2,
        "明後日": 2,
    }
    if name in day_offsets:
        start = today + timedelta(days=day_offsets[name])
        return _calendar_window(start, start + timedelta(days=1), zone)
    week_offsets = {
        "last week": -1,
        "this week": 0,
        "next week": 1,
        "再来週": 2,
        "上周": -1,
        "本周": 0,
        "下周": 1,
        "上星期": -1,
        "这星期": 0,
        "下星期": 1,
        "先週": -1,
        "今週": 0,
        "来週": 1,
    }
    if name in week_offsets:
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offsets[name])
        return _calendar_window(start, start + timedelta(weeks=1), zone)
    month_offsets = {
        "last month": -1,
        "this month": 0,
        "next month": 1,
        "上个月": -1,
        "这个月": 0,
        "下个月": 1,
        "先月": -1,
        "今月": 0,
        "来月": 1,
    }
    if name in month_offsets:
        month_index = today.year * 12 + today.month - 1 + month_offsets[name]
        year, zero_month = divmod(month_index, 12)
        start = date(year, zero_month + 1, 1)
        return _calendar_window(start, _next_month(year, zero_month + 1), zone)
    year_offsets = {
        "last year": -1,
        "this year": 0,
        "next year": 1,
        "一昨年": -2,
        "昨年": -1,
        "去年": -1,
        "今年": 0,
        "明年": 1,
        "再来年": 2,
    }
    year = today.year + year_offsets[name]
    return _calendar_window(date(year, 1, 1), date(year + 1, 1, 1), zone)


def infer_query_time(query: str, reference_at: datetime, timezone: str) -> QueryTime:
    """Resolve a date or calendar interval without disabling ordinary recall."""
    if reference_at.tzinfo is None or reference_at.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    zone = ZoneInfo(timezone)
    local_reference = reference_at.astimezone(zone)
    try:
        return _parse_query_time(query, local_reference, zone)
    except ValueError:
        return QueryTime(None, None, "invalid_time_expression")


def _parse_query_time(query: str, local_reference: datetime, zone: ZoneInfo) -> QueryTime:
    matches: list[tuple[int, int, str, TimeWindow]] = []
    calendar_spans: set[tuple[int, int]] = set()

    def append(match: re.Match[str], window: TimeWindow, *, calendar: bool = False) -> None:
        start, end = match.span()
        if any(
            existing_start <= start and end <= existing_end
            for existing_start, existing_end, _, _ in matches
        ):
            return
        matches[:] = [item for item in matches if not (start <= item[0] and item[1] <= end)]
        matches.append((start, end, match.group(), window))
        if calendar:
            calendar_spans.add((start, end))

    for match in _CJK_DATETIME.finditer(query):
        try:
            value = _localize(datetime(*(int(match.group(index)) for index in range(1, 6))), zone)
            append(match, _precise_window(value, seconds=False))
        except ValueError as exc:
            raise ValueError("invalid recognized query time") from exc
    for candidate in _ISO_CLOCK_CANDIDATE.finditer(query):
        if _ISO_DATETIME.fullmatch(candidate.group()) is None:
            raise ValueError("invalid recognized query time")
    for match in _ISO_DATETIME.finditer(query):
        try:
            value = datetime.fromisoformat(match.group().replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = _localize(value, zone)
            append(match, _precise_window(value, seconds=match.group("second") is not None))
        except ValueError as exc:
            raise ValueError("invalid recognized query time") from exc
    for pattern in (_CJK_DATE, _ISO_DATE):
        for match in pattern.finditer(query):
            try:
                append(match, _absolute(match, zone), calendar=True)
            except ValueError as exc:
                raise ValueError("invalid recognized query date") from exc
    for match in _ENGLISH_MONTH.finditer(query):
        parsed = dateparser.parse(
            match.group(),
            languages=["en"],
            settings={"RELATIVE_BASE": local_reference.replace(tzinfo=None)},
        )
        if parsed is None:
            raise ValueError("invalid recognized query date")
        start = date(parsed.year, parsed.month, parsed.day if match.group("day") else 1)
        end = (
            start + timedelta(days=1)
            if match.group("day")
            else _next_month(start.year, start.month)
        )
        append(match, _calendar_window(start, end, zone), calendar=True)
    for pattern in (_ENGLISH_RELATIVE, _CJK_RELATIVE):
        for match in pattern.finditer(query):
            append(match, _relative(match.group(), local_reference.date(), zone))
    for match in _DATEPARSER_RELATIVE.finditer(query):
        language = "zh" if re.search("[天周个月年前]", match.group()) else "en"
        parsed = dateparser.parse(
            match.group(),
            languages=[language],
            settings={"RELATIVE_BASE": local_reference.replace(tzinfo=None)},
        )
        if parsed is None:
            raise ValueError("invalid recognized query date")
        append(match, _calendar_window(parsed.date(), parsed.date() + timedelta(days=1), zone))
    if not matches:
        return QueryTime(None, None, "no_time_expression")
    ordered = sorted(matches)
    if len(ordered) == 2:
        left, right = ordered
        connector = query[left[1] : right[0]].strip().casefold()
        prefix = query[: left[0]]
        interval = connector in {"to", "-", "–", "—", "~", "〜", "到", "至"}
        interval |= connector == "and" and bool(re.search(r"\bbetween\s*$", prefix, re.I))
        interval |= connector == "から" and query[right[1] :].lstrip().startswith("まで")
        if interval:
            calendar = all((item[0], item[1]) in calendar_spans for item in ordered)
            comparison = bool(re.search(r"\bcompar(?:e|ed|ing)\s*$", prefix, re.I))
            if not calendar or comparison or left[3].start > right[3].start:
                return QueryTime(None, None, "ambiguous_time_expression")
            return QueryTime(
                TimeWindow(start=left[3].start, end=right[3].end),
                query[left[0] : right[1]],
                "inferred",
            )
    windows = {(window.start, window.end) for _, _, _, window in matches}
    if len(windows) != 1:
        return QueryTime(None, None, "ambiguous_time_expression")
    return QueryTime(matches[0][3], matches[0][2], "inferred")
