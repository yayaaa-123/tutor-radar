from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import html
import json
import re
from typing import Any


FIELD_RE = re.compile(r"[【《]\s*([^】》]+?)\s*[】》]\s*[：:]?\s*")
STANDARD_ORDER_RE = re.compile(r"^\s*订单\s*[：:]\s*([^\r\n]+)", re.MULTILINE)
BRACKET_ORDER_RE = re.compile(r"^\s*【\s*编号\s*】\s*[：:]\s*([^\r\n]+)", re.MULTILINE)
CODE_ORDER_RE = re.compile(r"^\s*(深圳[A-Za-z][^\r\n]*)", re.MULTILINE)
EMOJI_ORDER_RE = re.compile(
    r"^\s*[^\w\s\u4e00-\u9fff]{1,5}\s*([A-Za-z]*\d{5,}[A-Za-z0-9_-]*)\s*"
    r"[^\w\s\u4e00-\u9fff]{0,5}[^\r\n]*$",
    re.MULTILINE,
)
ORDER_RE = re.compile(
    r"^\s*(?:订单\s*[：:]\s*[^\r\n]+|【\s*编号\s*】\s*[：:]\s*[^\r\n]+|深圳[A-Za-z][^\r\n]*)",
    re.MULTILINE,
)
FIELD_ALIASES = (
    "编号", "地址", "地点", "辅导地点", "上课地址", "联系地址", "学员地址", "具体地址",
    "学科", "科目", "辅导内容", "年级科目", "辅导科目", "学员", "学生", "年级", "年玲",
    "学员情况", "学生情况", "年级性别", "学员年级", "学员性别", "性别", "情况", "成绩",
    "学员成绩", "要求", "教员要求", "老师要求", "教员", "时间", "课次", "上课时间",
    "时间次数", "时间安排", "次数", "薪酬", "薪资", "报价", "课酬", "课时报酬", "老师课费",
    "老师报酬", "老师薪水", "课费报酬", "价格", "课费", "报酬", "费用", "试课时间",
)
_FIELD_NAMES = "|".join(sorted((re.escape(name) for name in FIELD_ALIASES), key=len, reverse=True))
FIELD_LINE_RE = re.compile(
    rf"^\s*(?:\[[^\]\r\n]{{1,12}}\]\s*)?(?:[【《]\s*(?P<bracket>{_FIELD_NAMES})\s*[】》]\s*[：:]?\s*|"
    rf"(?P<plain>{_FIELD_NAMES})\s*[：:]\s*)",
    re.MULTILINE,
)
GENERIC_FIELD_LINE_RE = re.compile(
    rf"^\s*(?:\[[^\]\r\n]{{1,12}}\]\s*)?(?:"
    rf"[【《]\s*(?P<bracket>[^】》\r\n]{{1,24}})\s*[】》]\s*[：:]?\s*|"
    rf"(?P<plain>[\u4e00-\u9fffA-Za-z0-9_]{{1,16}})\s*[：:]\s*)",
    re.MULTILINE,
)
COMPACT_HEADER_RE = re.compile(r"^\s*【\s*[A-Za-z]\s*(?P<body>[^】]+)】", re.MULTILINE | re.IGNORECASE)
GENERIC_CODE_RE = re.compile(
    r"(?i)([A-Z]{1,8}\d{3,}[A-Z0-9_-]*|[A-Z]{1,5}深圳\d{3,}[A-Z0-9_-]*|"
    r"深圳\d{5,}[A-Z0-9_-]*|\d{6,}深圳)"
)
DISTRICTS = (
    "福田区",
    "罗湖区",
    "南山区",
    "盐田区",
    "宝安区",
    "龙岗区",
    "龙华区",
    "坪山区",
    "光明区",
    "大鹏新区",
)
DISTRICT_ALIASES = {district.removesuffix("区"): district for district in DISTRICTS if district.endswith("区")}
SUBJECTS = ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "政治")
CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
SCHEDULE_SIGNAL_RE = re.compile(
    r"(?:一|每)周\s*[一二两三四五六七1-7]\s*次|每(?:个)?星期\s*[一二两三四五六七1-7]\s*次|"
    r"周末|周[一二三四五六日天]|星期[一二三四五六日天]|每次(?:课)?(?:时长|课时)?|"
    r"一次(?:课)?(?:时长|课时)?|每节课|\d+(?:\.\d+)?\s*(?:小时|h)\s*/\s*次|"
    r"开学|秋季|春季|寒假|暑假|长期|短期",
    re.IGNORECASE,
)
PAY_SIGNAL_RE = re.compile(
    r"薪酬|薪资|课酬|课费|报酬|价格|费用|工资|"
    r"\d+(?:\.\d+)?(?:\s*[-~～—至到]\s*\d+(?:\.\d+)?)?\s*(?:元)?\s*/\s*(?:次|小时|时|h)",
    re.IGNORECASE,
)
REQUIREMENT_SIGNAL_RE = re.compile(
    r"(?:男|女)(?:老师|教师|大学生)|(?:老师|教员|教师)(?:要求|条件|性别)|"
    r"(?:要求|只要|限|优先|学历|院校|学校).{0,12}(?:老师|教师|教员|大学生|本科|研究生|985|211|一本|女|男)|"
    r"(?:985|211|一本|本科|研究生|院校).{0,8}(?:优先|要求|限)"
)


@dataclass(slots=True)
class TutorOrder:
    order_id: str
    tags: list[str] = field(default_factory=list)
    address: str | None = None
    district: str | None = None
    subjects: list[str] = field(default_factory=list)
    subject_text: str | None = None
    student_text: str | None = None
    student_gender: str | None = None
    current_level: str | None = None
    requirements_text: str | None = None
    teacher_gender_required: str | None = None
    teacher_gender_preferred: str | None = None
    teacher_identity_required: str | None = None
    schedule_text: str | None = None
    frequency_per_week: int | None = None
    duration_hours: float | None = None
    latest_end_time: str | None = None
    pay_text: str | None = None
    hourly_rate_min: float | None = None
    hourly_rate_max: float | None = None
    lesson_pay_min: float | None = None
    lesson_pay_max: float | None = None
    trial_time: str | None = None
    raw_fields: dict[str, str | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_text(text: str) -> str:
    text = html.unescape(text).replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _clean_value(value: str) -> str | None:
    cleaned = re.sub(r"(?<!\w)#(?=\S)", "", value)
    cleaned = re.sub(
        r"\n+(?:福田区|罗湖区|南山区|盐田区|宝安区|龙岗区|龙华区|坪山区|光明区|大鹏新区)\s*[：:].*$",
        "",
        cleaned,
        flags=re.DOTALL,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \n&#")
    return cleaned or None


def _canonical_field_name(label: str) -> str | None:
    compact = re.sub(r"[\s：:]", "", label)
    if compact in FIELD_ALIASES:
        return compact
    if any(token in compact for token in ("薪酬", "薪资", "课酬", "课费", "报酬", "工资", "价格", "费用", "报价")):
        return "薪酬"
    if any(token in compact for token in ("地址", "地点", "位置")):
        return "地址"
    if any(token in compact for token in ("科目", "学科", "辅导内容")):
        return "学科"
    if any(token in compact for token in ("学生", "学员", "年级性别", "学生情况", "学员情况")):
        return "学员"
    if any(token in compact for token in ("要求", "条件")) or compact in {"老师", "教师", "教员"}:
        return "要求"
    if any(token in compact for token in ("时间", "时长", "次数", "课次", "频次", "频率", "安排")):
        return "时间"
    if "成绩" in compact:
        return "成绩"
    if "试课" in compact:
        return "试课时间"
    return None


def _merge_field(fields: dict[str, str | None], name: str, value: str | None) -> None:
    if not value:
        return
    existing = fields.get(name)
    if not existing:
        fields[name] = value
    elif value not in existing:
        fields[name] = f"{existing} {value}"


def _split_fields(text: str) -> dict[str, str | None]:
    text = re.sub(
        r"(?<!^)(?<!\n)(?=[【《][^】》\r\n]{1,24}[】》])",
        "\n",
        text,
    )
    matches = list(GENERIC_FIELD_LINE_RE.finditer(text))
    fields: dict[str, str | None] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        label = (match.group("bracket") or match.group("plain")).strip()
        name = _canonical_field_name(label)
        if name:
            _merge_field(fields, name, _clean_value(text[match.end() : end]))
    return fields


def _extract_matching_lines(text: str, pattern: re.Pattern[str]) -> str | None:
    output: list[str] = []
    for line in text.splitlines():
        if _looks_like_header_line(line):
            continue
        if not pattern.search(line):
            continue
        match = GENERIC_FIELD_LINE_RE.match(line)
        value = _clean_value(line[match.end() :] if match else line)
        if value and value not in output:
            output.append(value)
    return " ".join(output) or None


def _merge_text(primary: str | None, fallback: str | None) -> str | None:
    if not primary:
        return fallback
    if not fallback or fallback in primary:
        return primary
    if primary in fallback:
        return fallback
    return f"{primary} {fallback}"


def _parse_order_header(text: str, fields: dict[str, str | None]) -> tuple[str, list[str]]:
    standard = STANDARD_ORDER_RE.search(text)
    bracket = BRACKET_ORDER_RE.search(text)
    coded = CODE_ORDER_RE.search(text)
    emoji_coded = EMOJI_ORDER_RE.search(text)
    numbered = fields.get("编号")
    family = re.search(r"^\s*深圳家教\s*[：:]\s*([^\r\n]+)", text, re.MULTILINE)
    generic = next((GENERIC_CODE_RE.search(line) for line in text.splitlines()[:5] if GENERIC_CODE_RE.search(line)), None)
    if numbered:
        header = numbered
    elif standard:
        header = standard.group(1).strip()
    elif bracket:
        header = bracket.group(1).strip()
    elif family:
        header = family.group(1).strip()
    elif coded:
        header = coded.group(1).strip()
    elif emoji_coded:
        header = emoji_coded.group(1).strip()
    elif generic:
        header = generic.group(1).strip()
    else:
        identity = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if fields else text
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"auto-{digest}", []
    clean_header = re.split(r"[#（(【]", header, maxsplit=1)[0].strip()
    order_id_match = re.match(r"(.+)", clean_header)
    if not order_id_match:
        raise ValueError("订单编号为空")
    order_id = re.sub(r"\s+", "", order_id_match.group(1))
    tags = [tag.strip() for tag in re.findall(r"#([^#\s]+)", header) if tag.strip()]
    return order_id, tags


def _field(fields: dict[str, str | None], *names: str) -> str | None:
    return next((fields.get(name) for name in names if fields.get(name)), None)


def _looks_like_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r"^(?:订单\s*[：:]|[【《]\s*编号\s*[】》]|深圳家教\s*[：:])", stripped):
        return True
    if FIELD_LINE_RE.match(line):
        return False
    if COMPACT_HEADER_RE.match(stripped):
        return True
    if EMOJI_ORDER_RE.match(stripped):
        return True
    return bool(GENERIC_CODE_RE.search(stripped)) and len(stripped) < 100


def _split_header_blocks(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        if _looks_like_header_line(line):
            offsets.append(position)
        position += len(line)
    starts = sorted(set(([0] if not offsets or offsets[0] > 0 else []) + offsets))
    if not starts:
        return [text]
    return [text[start : (starts[index + 1] if index + 1 < len(starts) else len(text))].strip() for index, start in enumerate(starts)]


def _split_pipe_orders(text: str) -> list[str]:
    district: str | None = None
    blocks: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^\s*##\s*(.+?)\s*$", line)
        if heading:
            district = next((item for item in DISTRICTS if item in heading.group(1)), None)
            continue
        row = re.match(r"^\s*\d+\s*[.、]\s*(.+)$", line)
        if not row:
            continue
        cells = [cell.strip() for cell in row.group(1).split("｜")]
        if len(cells) < 6:
            continue
        address = cells[0]
        if district and district not in address:
            address = f"{district}{address}"
        raw = "\n".join((
            f"订单：list-{hashlib.sha256(line.encode('utf-8')).hexdigest()[:16]}",
            f"【地址】：{address}", f"【年级科目】：{cells[1]}", f"【学生情况】：{cells[2]}",
            f"【时间安排】：{cells[3]}", f"【老师要求】：{cells[4]}", f"【薪酬】：{'｜'.join(cells[5:])}",
        ))
        blocks.append(raw)
    return blocks


def _split_numbered_fields(text: str) -> dict[str, str | None]:
    matches = list(re.finditer(r"^\s*([1-6])\s*[、,，]\s*", text, re.MULTILINE))
    if len(matches) < 4:
        return {}
    numbered: dict[str, str | None] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        numbered[match.group(1)] = _clean_value(text[match.end() : end])
    return {
        "地址": numbered.get("1"),
        "学员": numbered.get("2"),
        "学科": numbered.get("3"),
        "时间": numbered.get("4"),
        "要求": numbered.get("5"),
        "薪酬": numbered.get("6"),
    }


def _parse_gender(student: str | None) -> str | None:
    if not student:
        return None
    if re.search(r"女(?:孩|生|娃)", student):
        return "female"
    if re.search(r"男(?:孩|生|娃)", student):
        return "male"
    return None


def _parse_teacher_requirements(requirements: str | None) -> tuple[str | None, str | None, str | None]:
    if not requirements:
        return None, None, None
    unrestricted = bool(re.search(
        r"男女(?:老师|大学生|教师|教员|大)?(?:均可|不限)|"
        r"(?:老师|教员|教师)?性别不限|男(?:老师)?女(?:老师)?都可",
        requirements,
    ))
    short_suffix = r"(?=优先|$|[\s，,、;/；]|[一二三四1-4])"
    required: str | None = None
    preferred: str | None = None
    for chinese, value in (("女", "female"), ("男", "male")):
        # Only bind “优先” to gender when it immediately follows that phrase.
        # In “女老师 深大优先”, the preference belongs to the university.
        if re.search(fr"{chinese}(?:老师|大学生|教师|大{short_suffix})\s*优先", requirements):
            preferred = value
        elif re.search(fr"{chinese}(?:老师|大学生|教师|大{short_suffix})", requirements):
            required = value
    if unrestricted:
        required = None
        preferred = None
    identity = "university_student" if (
        "大学生" in requirements
        or bool(re.search(fr"[男女]大{short_suffix}", requirements))
        or bool(re.search(r"男女大(?:均可|不限)", requirements))
    ) else None
    return required, preferred, identity


def _parse_frequency(text: str) -> int | None:
    match = re.search(
        r"(?:(?:一|每)周|每(?:个)?星期)(?:上课)?\s*([一二两三四五六七1-7])\s*次",
        text,
    )
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else CN_NUMBERS[token]


def parse_duration_hours(text: str) -> float | None:
    prefix = r"(?:一次(?:课)?|每次(?:课)?|每节(?:课)?|每周末)\s*(?:上课\s*)?(?:时长|课时)?\s*[：:]?\s*"
    half_match = re.search(
        prefix + r"(?:(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:个)?)?半\s*(?:小时|h)",
        text,
        re.IGNORECASE,
    )
    if half_match:
        token = half_match.group(1)
        base = 0.0 if not token else (float(token) if token[0].isdigit() else float(CN_NUMBERS[token]))
        return base + 0.5
    match = re.search(
        prefix + r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:个)?\s*(?:小时|h)",
        text,
        re.IGNORECASE,
    )
    if not match:
        reverse_match = re.search(
            r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:个)?\s*(?:小时|h)\s*/\s*次",
            text,
            re.IGNORECASE,
        )
        if not reverse_match:
            lesson_pay_match = re.search(
                r"\d+(?:\.\d+)?(?:\s*[-–—~～至到]\s*\d+(?:\.\d+)?)?\s*(?:元)?\s*/\s*"
                r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:个)?\s*(?:小时|h)",
                text,
                re.IGNORECASE,
            )
            if not lesson_pay_match:
                return None
            token = lesson_pay_match.group(1)
            return float(token) if token[0].isdigit() else float(CN_NUMBERS[token])
        token = reverse_match.group(1)
        return float(token) if token[0].isdigit() else float(CN_NUMBERS[token])
    token = match.group(1)
    return float(token) if token[0].isdigit() else float(CN_NUMBERS[token])


def _parse_latest_time(text: str) -> str | None:
    match = re.search(r"(?:晚上|晚)?\s*(\d{1,2})\s*点\s*(半|\d{1,2}分?)?\s*前", text)
    if not match:
        return None
    hour = int(match.group(1))
    if "晚上" in match.group(0) or "晚" in match.group(0):
        if hour < 12:
            hour += 12
    minute_token = match.group(2) or ""
    minute = 30 if minute_token == "半" else int(re.sub(r"\D", "", minute_token) or 0)
    return f"{hour:02d}:{minute:02d}"


def _parse_hourly_rate(text: str) -> tuple[float | None, float | None]:
    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:元)?\s*[-~～—至到]\s*(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:每小时|元/小时|元每小时|/h|/H|/时)",
        text,
    )
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))
    single_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:每小时|元/小时|元每小时|/h|/H|/时)", text)
    if single_match:
        rate = float(single_match.group(1))
        return rate, rate
    return None, None


def _parse_lesson_pay(text: str) -> tuple[float | None, float | None]:
    # “240-260/2h” is a whole-lesson quote, unlike “150/小时”.
    timed = re.search(
        r"(\d+(?:\.\d+)?)\s*[-~～—至到]\s*(\d+(?:\.\d+)?)\s*(?:元)?\s*/\s*"
        r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:个)?\s*(?:小时|h)",
        text,
        re.IGNORECASE,
    )
    if timed:
        return float(timed.group(1)), float(timed.group(2))
    session_range = re.search(
        r"(\d+(?:\.\d+)?)\s*[-~～—至到]\s*(\d+(?:\.\d+)?)\s*(?:元)?\s*/\s*(?:次|节|课)",
        text,
    )
    if session_range:
        return float(session_range.group(1)), float(session_range.group(2))
    session_single = re.search(r"(\d+(?:\.\d+)?)\s*(?:元)?\s*/\s*(?:次|节|课)", text)
    if session_single:
        value = float(session_single.group(1))
        return value, value
    reverse = re.search(
        r"(?:一次(?:课)?|每次(?:课)?|每节(?:课)?).{0,20}?"
        r"(?:\d+(?:\.\d+)?\s*(?:小时|h)).{0,12}?"
        r"(\d+(?:\.\d+)?)\s*[-~～—至到]\s*(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    if reverse:
        return float(reverse.group(1)), float(reverse.group(2))
    return None, None


def parse_order(raw_text: str, require_complete: bool = True) -> TutorOrder:
    text = normalize_text(raw_text)
    fields = _split_numbered_fields(text) or _split_fields(text)
    order_id, tags = _parse_order_header(text, fields)

    address = _field(fields, "地址", "地点", "辅导地点", "上课地址", "联系地址", "学员地址", "具体地址")
    subject_text = _field(fields, "学科", "科目", "辅导内容", "年级科目", "辅导科目")
    student_text = _field(fields, "学员", "学生", "年级", "年玲", "学员情况", "学生情况", "年级性别", "学员年级")
    student_gender_text = " ".join(part for part in (student_text, _field(fields, "性别", "学员性别")) if part)
    requirements = _field(fields, "要求", "教员要求", "老师要求", "教员")
    schedule = _field(fields, "时间", "课次", "上课时间", "时间次数", "时间安排", "次数")
    pay = _field(fields, "薪酬", "薪资", "报价", "课酬", "课时报酬", "老师课费", "老师报酬", "老师薪水", "课费报酬", "价格", "课费", "报酬", "费用")
    requirements = _merge_text(requirements, _extract_matching_lines(text, REQUIREMENT_SIGNAL_RE))
    schedule = _merge_text(schedule, _extract_matching_lines(text, SCHEDULE_SIGNAL_RE))
    pay = pay or _extract_matching_lines(text, PAY_SIGNAL_RE)
    compact = COMPACT_HEADER_RE.search(text)
    if compact:
        compact_body = compact.group("body").strip()
        address = address or compact_body
        subject_text = subject_text or compact_body
        student_text = student_text or compact_body
        student_gender_text = student_gender_text or compact_body

    semantic_groups = sum(bool(value) for value in (address, subject_text or student_text, schedule, pay, requirements))
    if require_complete and (not (subject_text or student_text) or semantic_groups < 3):
        raise ValueError("文本块不满足订单字段完整度要求")
    combined_schedule = " ".join(part for part in (schedule, pay, text) if part)

    district = next((item for item in DISTRICTS if address and item in address), None)
    if not district and address:
        district = next((value for alias, value in DISTRICT_ALIASES.items() if alias in address), None)
    subjects = [subject for subject in SUBJECTS if subject_text and subject in subject_text]
    if subject_text and "语数英" in subject_text:
        for shorthand_subject in ("语文", "数学", "英语"):
            if shorthand_subject not in subjects:
                subjects.append(shorthand_subject)
    if subject_text and "语数外" in subject_text:
        for shorthand_subject in ("语文", "数学", "英语"):
            if shorthand_subject not in subjects:
                subjects.append(shorthand_subject)
    if subject_text and "数理化" in subject_text:
        for shorthand_subject in ("数学", "物理", "化学"):
            if shorthand_subject not in subjects:
                subjects.append(shorthand_subject)
    elif subject_text and "数理" in subject_text:
        for shorthand_subject in ("数学", "物理"):
            if shorthand_subject not in subjects:
                subjects.append(shorthand_subject)
    required_gender, preferred_gender, identity = _parse_teacher_requirements(requirements)
    rate_min, rate_max = _parse_hourly_rate(pay or "")
    lesson_min, lesson_max = _parse_lesson_pay(pay or "")
    warnings: list[str] = []
    if pay and (parse_duration_hours(pay) or _parse_latest_time(pay)):
        warnings.append("薪酬字段中包含上课时间或课时长度，已拆分解析")

    return TutorOrder(
        order_id=order_id,
        tags=tags,
        address=address,
        district=district,
        subjects=subjects,
        subject_text=subject_text,
        student_text=student_text,
        student_gender=_parse_gender(student_gender_text),
        current_level=_field(fields, "成绩", "学员成绩", "情况"),
        requirements_text=requirements,
        teacher_gender_required=required_gender,
        teacher_gender_preferred=preferred_gender,
        teacher_identity_required=identity,
        schedule_text=schedule,
        frequency_per_week=_parse_frequency(combined_schedule),
        duration_hours=parse_duration_hours(combined_schedule),
        latest_end_time=_parse_latest_time(combined_schedule),
        pay_text=pay,
        hourly_rate_min=rate_min,
        hourly_rate_max=rate_max,
        lesson_pay_min=lesson_min,
        lesson_pay_max=lesson_max,
        trial_time=_field(fields, "试课时间"),
        raw_fields=fields,
        warnings=warnings,
        raw_text=raw_text,
    )


def extract_order_blocks(raw_text: str) -> list[str]:
    normalized = normalize_text(raw_text)
    return _split_pipe_orders(normalized) or _split_header_blocks(normalized)


def looks_like_order_block(raw_text: str) -> bool:
    text = normalize_text(raw_text)
    explicit_header = bool(
        STANDARD_ORDER_RE.search(text)
        or BRACKET_ORDER_RE.search(text)
        or CODE_ORDER_RE.search(text)
        or EMOJI_ORDER_RE.search(text)
        or re.search(r"^\s*深圳家教\s*[：:]", text, re.MULTILINE)
    )
    field_lines = len(list(GENERIC_FIELD_LINE_RE.finditer(text)))
    teaching_signal = bool(
        any(subject in text for subject in SUBJECTS)
        or re.search(r"学员|学生|年级|辅导|家教", text)
    )
    return teaching_signal and (explicit_header or field_lines >= 3)


def parse_orders(raw_text: str) -> list[TutorOrder]:
    """Extract complete semantic field blocks, independent of title style."""
    candidate_blocks = extract_order_blocks(raw_text)
    orders: list[TutorOrder] = []
    for block in candidate_blocks:
        try:
            orders.append(parse_order(block))
        except ValueError:
            continue
    if not orders:
        raise ValueError("消息中没有找到字段完整的订单块")
    return orders
