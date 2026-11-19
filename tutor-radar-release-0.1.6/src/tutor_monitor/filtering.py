from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Any

from .parser import TutorOrder


@dataclass(slots=True)
class TutorProfile:
    gender: str = "male"
    school: str = "深圳技术大学"
    year: str = "大三"
    subjects: tuple[str, ...] = ("数学", "物理", "化学")
    preferred_subjects: tuple[str, ...] = ("数学", "物理")
    weekend_only: bool = True


@dataclass(slots=True)
class FilterDecision:
    status: str
    priority: int
    reasons: list[str] = field(default_factory=list)
    order: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_profile(path: str | Path = "config.toml") -> TutorProfile:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("profile", {})
    return TutorProfile(
        gender=str(data.get("gender", "male")),
        school=str(data.get("school", "深圳技术大学")),
        year=str(data.get("year", "大三")),
        subjects=tuple(data.get("subjects", ("数学", "物理", "化学"))),
        preferred_subjects=tuple(data.get("preferred_subjects", ("数学", "物理"))),
        weekend_only=bool(data.get("weekend_only", True)),
    )


FEMALE_TEACHER_RE = re.compile(
    r"女(?:老师|大学生|教师|生)\s*(?:优先)?|性别\s*(?:要求)?\s*女|"
    r"(?:要求|限)\s*女|女\s*优先|(?:^|[，,、;/\s])女(?:$|[，,、;/\s])|"
    r"优先.{0,4}女(?:老师|大学生|教师|生)|"
    r"女大(?=优先|$|[\s，,、;/；]|[一二三四1-4])"
)
RAW_FEMALE_TEACHER_RE = re.compile(
    r"女(?:老师|大学生|教师)\s*(?:优先)?|"
    r"(?:要求|只要|限|需|需要|找)\s*.{0,8}?女(?:老师|大学生|教师)|"
    r"(?:老师|教员|教师)(?:性别)?\s*(?:要求|限)?\s*女"
)
UNRESTRICTED_TEACHER_GENDER_RE = re.compile(
    r"男女(?:老师|教师|教员|大学生|大)?(?:均可|不限)|性别不限|男(?:老师)?女(?:老师)?都可"
)
EDUCATION_GATE_RE = re.compile(
    r"985|211|研究生|硕士|博士|师范(?:类|生)?|学历.{0,4}(?:优秀|优先|要求)|"
    r"深大|深圳大学|南科大|南方科技大学|哈工深|哈工大|哈尔滨工业大学|中山大学|"
    r"本科(?:起|以上)|一本(?:院校|大学)?|双一流|重点本科|"
    r"(?:好|优秀|重点).{0,3}学校.{0,3}优先|提供(?:高考|中考)成绩|高考成绩|"
    r"(?:中考|高考|中高考).{0,8}高分|在校证明"
)
WEEKDAY_ONLY_RE = re.compile(r"周一\s*(?:到|-|至)\s*周五|工作日|周内|周一到周四|每天|周[一二三四五](?!次)")
WEEKEND_RE = re.compile(r"周末|周六|周日")
OPTION_RE = re.compile(r"或者|或周末|时间协商|具体协商|均可")


def filter_order(order: TutorOrder, profile: TutorProfile | None = None) -> FilterDecision:
    profile = profile or TutorProfile()
    requirements = order.requirements_text or ""
    schedule = order.schedule_text or ""

    unrestricted_gender = bool(UNRESTRICTED_TEACHER_GENDER_RE.search(requirements))
    female_required = (
        order.teacher_gender_required == "female"
        or order.teacher_gender_preferred == "female"
        or bool(FEMALE_TEACHER_RE.search(requirements))
    )
    if not requirements:
        female_required = bool(RAW_FEMALE_TEACHER_RE.search(order.raw_text))
    if female_required and profile.gender == "male" and not unrestricted_gender:
        return FilterDecision("rejected", 0, ["要求女老师（包括女老师优先）"], order.to_dict())
    if order.teacher_gender_required == "male" and profile.gender == "female" and not unrestricted_gender:
        return FilterDecision("rejected", 0, ["要求男老师"], order.to_dict())

    learner_text = " ".join(part for part in (order.subject_text, order.student_text) if part)
    if re.search(r"幼儿|幼小|幼升小|学前|[1-6]\s*岁", learner_text):
        return FilterDecision("rejected", 0, ["低于小学阶段"], order.to_dict())

    supported = set(profile.subjects)
    subjects = set(order.subjects)
    matching = subjects & supported
    if re.search(r"全科|全学科", order.subject_text or "") and "可分" not in order.raw_text:
        return FilterDecision("rejected", 0, ["订单要求全科辅导"], order.to_dict())
    if not matching:
        return FilterDecision("rejected", 0, ["科目不在个人画像范围"], order.to_dict())

    unsupported = subjects - supported
    if unsupported and "可分" not in order.raw_text:
        return FilterDecision("rejected", 0, ["订单同时要求不能教授的科目"], order.to_dict())

    reasons: list[str] = []
    if EDUCATION_GATE_RE.search(requirements):
        reasons.append("院校或学历要求需确认")
    if re.search(r"经验", requirements):
        reasons.append("教学经验要求需确认")
    if profile.weekend_only:
        has_weekday = bool(WEEKDAY_ONLY_RE.search(schedule))
        has_weekend = bool(WEEKEND_RE.search(schedule))
        has_choice = bool(OPTION_RE.search(schedule))
        if has_weekday and not has_choice:
            return FilterDecision("rejected", 0, ["要求周中或每天上课"], order.to_dict())
        if not has_weekend and not has_choice:
            reasons.append("上课星期不明确，需人工或模型确认")

    priority = sum(2 if subject in profile.preferred_subjects else 1 for subject in matching)
    matched_text = "、".join(subject for subject in profile.subjects if subject in matching)
    reasons.insert(0, f"科目匹配：{matched_text}")
    status = "review" if any(
        token in reason for reason in reasons for token in ("待确认", "需确认", "不明确", "未设偏好")
    ) else "matched"
    return FilterDecision(status, priority, reasons, order.to_dict())
