"""Resolve explicit task durations using the record's local creation date."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from app.store import tzinfo


NUMBER = r"[0-9零一二两三四五六七八九十]{1,3}"
DURATION = re.compile(
    rf"(?:花|用|给自己|留给自己)\s*(?P<spend>{NUMBER})\s*(?:个)?(?P<unit1>天|日|周|星期)(?:的)?(?:时间)?"
    rf"|(?P<within>{NUMBER})\s*(?:个)?(?P<unit2>天|日|周|星期)(?:之内|内|以后|之后|后)"
)


def count_days(value):
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip("一二两三四五六七八九", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if value in digits:
        return digits[value]
    if re.fullmatch(r"[一二两三四五六七八九]?十[一二三四五六七八九]?", value):
        tens, units = value.split("十")
        return digits.get(tens, 1) * 10 + digits.get(units, 0)
    return None


def duration_deadline(entry, quote, kind):
    """Narrow fallback, not a general natural-language date parser.

    An explicit personal task such as “花两天时间整理资料” means creation
    date + two calendar days. History, estimates, workdays, multiple durations
    and other start dates stay with the model/user rather than guessing.
    """
    if kind != "task" or re.search(
        r"已经|已花|花了|用了|昨天|前天|上周|过去|曾经|之前|原本|本来|不打算|不准备|取消|"
        r"大概|大约|约莫|预计|左右|至少|至多|可能|工作日|截止|最晚|月底|月初|年底|本周|这周|本月|下月|"
        r"(?:今天|今晚|明天|后天)(?:前|之前|完成|做完|交|提交)", quote
    ):
        return None
    matches = list(DURATION.finditer(quote))
    if len(matches) != 1:
        return None
    if re.search(rf"{NUMBER}\s*(?:-|~|～|至|到)\s*{NUMBER}", quote):
        return None
    if len(re.findall(rf"{NUMBER}\s*(?:个)?(?:天|日|周|星期)", quote)) != 1:
        return None
    match = matches[0]
    count = count_days(match["spend"] or match["within"])
    if not count or count > 365:
        return None
    # A range or approximation is not a fixed deadline.
    if re.search(r"[零一二两三四五六七八九十0-9几半多余到至~～-]", quote[match.end():match.end() + 1]):
        return None
    if re.search(r"\d{1,4}[年/月日]|下周|下个|周[一二三四五六日天末]|星期[一二三四五六日天]", quote):
        return None
    start = re.search(r"(?:从)?(?P<day>今天|明天|后天)(?:起|开始)", quote)
    if re.search(r"明天|后天", quote) and not start:
        return None
    try:
        anchor = datetime.fromisoformat(entry["created_at"]).astimezone(tzinfo(entry["timezone"])).date()
    except (KeyError, TypeError, ValueError):
        return None
    offset = {"今天": 0, "明天": 1, "后天": 2}[start["day"]] if start else 0
    days = count * (7 if (match["unit1"] or match["unit2"]) in ("周", "星期") else 1)
    try:
        due = anchor + timedelta(days=offset + days)
    except OverflowError:
        return None
    begin, end = match.span()
    if start:
        if start.end() > begin:
            return None
        begin = start.start()
    phrase = quote[begin:end]
    if len(phrase) > 200:
        return None
    return phrase, due.isoformat()
