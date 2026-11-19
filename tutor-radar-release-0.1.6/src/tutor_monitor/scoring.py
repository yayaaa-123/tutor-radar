from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import tomllib
from typing import Any

from .parser import TutorOrder, parse_duration_hours


SCORING_VERSION = "fixed-v1"
GRADE_RANK = {"C": 0, "B": 1, "A": 2, "S": 3}
WEEKEND_RE = re.compile(r"周末|周六|周日|星期六|星期日")
LONG_TERM_RE = re.compile(r"长期|一学期|开学(?:后)?|每周|一周\s*[一二三四五六七两\d]+\s*次")
SHORT_TERM_RE = re.compile(r"短期|临时|一次性|考前|寒假|暑假|辅导\s*[一二三四五六七八九十\d]+\s*天")
EXPERIENCE_RE = re.compile(r"经验|带过|教过")
PER_LESSON_RE = re.compile(
    r"(?P<pay_min>\d+(?:\.\d+)?)"
    r"(?:\s*[-–—~～至到]\s*(?P<pay_max>\d+(?:\.\d+)?))?"
    r"\s*(?:元)?\s*/\s*(?P<hours>\d+(?:\.\d+)?)\s*(?:h|小时)",
    re.IGNORECASE,
)
PER_SESSION_RE = re.compile(
    r"(?P<pay_min>\d+(?:\.\d+)?)"
    r"(?:\s*[-–—~～至到]\s*(?P<pay_max>\d+(?:\.\d+)?))?"
    r"\s*(?:元)?\s*(?:/\s*次|每次|一节(?:课)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    education: str = "深圳技术大学大三本科生"
    strength_subjects: tuple[str, ...] = ("数学", "物理", "化学")
    preferred_subjects: tuple[str, ...] = ("数学", "物理")
    experience: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("小学六年级", ("数学",)),
        ("初三", ("数学", "物理", "化学")),
        ("高三", ("数学",)),
    )
    weekend_only: bool = True


def load_capability_profile(path: str | Path = "config.toml") -> CapabilityProfile:
    root = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    data = (root.get("scoring") or {}).get("capability") or {}
    raw_experience = data.get("experience") or []
    experience = tuple(
        (str(item["level"]), tuple(str(subject) for subject in item.get("subjects") or []))
        for item in raw_experience
        if item.get("level") and item.get("subjects")
    )
    defaults = CapabilityProfile()
    return CapabilityProfile(
        education=str(data.get("education", defaults.education)),
        strength_subjects=tuple(data.get("strength_subjects", defaults.strength_subjects)),
        preferred_subjects=tuple(data.get("preferred_subjects", defaults.preferred_subjects)),
        experience=experience if "experience" in data else defaults.experience,
        weekend_only=bool(data.get("weekend_only", defaults.weekend_only)),
    )


def _level_key(order: TutorOrder) -> str | None:
    text = " ".join(part for part in (order.subject_text, order.student_text) if part)
    patterns = (
        ("小学六年级", r"六年级|6\s*年级|小六|小学六"),
        ("初三", r"初三|九年级|9\s*年级"),
        ("高三", r"高三|十二年级|12\s*年级"),
        ("小学", r"小学|[一二三四五1-5]\s*年级|小[一二三四五]"),
        ("初中", r"初一|初二|七年级|八年级|7\s*年级|8\s*年级|初中"),
        ("高中", r"高一|高二|十年级|十一年级|10\s*年级|11\s*年级|高中"),
    )
    for key, pattern in patterns:
        if re.search(pattern, text):
            return key
    return None


def _experience_score(order: TutorOrder, profile: CapabilityProfile) -> tuple[int, str]:
    level = _level_key(order)
    subjects = set(order.subjects) & set(profile.strength_subjects)
    direct_subjects: set[str] = set()
    for experience_level, experience_subjects in profile.experience:
        if level == experience_level:
            direct_subjects.update(experience_subjects)
    if subjects and subjects <= direct_subjects:
        return 10, f"{level}科目与已有教学经历直接匹配"
    if subjects & direct_subjects:
        return 8, f"{level}部分科目与已有教学经历直接匹配"
    same_band = (
        level == "小学" and any(item[0].startswith("小学") for item in profile.experience)
        or level == "初中" and any(item[0].startswith("初") for item in profile.experience)
        or level == "高中" and any(item[0].startswith("高") for item in profile.experience)
    )
    if same_band:
        return 7, f"有同学段教学经历，但不是完全相同年级"
    if level:
        return 5, f"科目匹配，但没有{level}的直接经历记录"
    return 3, "年级信息不足，无法核对已有教学经历"


def _ability(order: TutorOrder, profile: CapabilityProfile) -> tuple[int, dict[str, int], list[str], list[str]]:
    subjects = set(order.subjects)
    strengths = subjects & set(profile.strength_subjects)
    preferred = strengths & set(profile.preferred_subjects)
    if {"数学", "物理"} <= strengths:
        subject_score = 20
    elif preferred:
        subject_score = 19
    elif "化学" in strengths:
        subject_score = 16
    else:
        subject_score = 0

    experience_score, experience_reason = _experience_score(order, profile)
    requirements = order.requirements_text or ""
    if EXPERIENCE_RE.search(requirements):
        requirement_score = 5 if experience_score >= 8 else 3
    else:
        requirement_score = 5
    positives = [
        f"优势科目匹配：{'、'.join(subject for subject in profile.strength_subjects if subject in strengths) or '无'}",
        experience_reason,
    ]
    negatives: list[str] = []
    if subject_score < 19:
        negatives.append("科目不是优先级最高的数学或物理")
    if experience_score < 8:
        negatives.append(experience_reason)
    if requirement_score < 5:
        negatives.append("订单要求相关经验，但现有经历不是直接匹配")
    details = {
        "subject_fit": subject_score,
        "experience_fit": experience_score,
        "requirement_fit": requirement_score,
    }
    return sum(details.values()), details, positives, negatives


def compensation_is_parseable(order: TutorOrder) -> bool:
    return bool(
        order.hourly_rate_min is not None
        or order.lesson_pay_min is not None
        or PER_LESSON_RE.search(order.pay_text or "")
        or PER_SESSION_RE.search(order.pay_text or "")
    )


def compensation_facts(order: TutorOrder) -> tuple[float | None, float | None, float | None]:
    duration = order.duration_hours or parse_duration_hours(order.schedule_text or "")
    if duration and order.hourly_rate_min is not None:
        low = order.hourly_rate_min * duration
        high_rate = order.hourly_rate_max if order.hourly_rate_max is not None else order.hourly_rate_min
        return low, high_rate * duration, duration
    if duration and order.lesson_pay_min is not None:
        high_pay = order.lesson_pay_max if order.lesson_pay_max is not None else order.lesson_pay_min
        return min(order.lesson_pay_min, high_pay), max(order.lesson_pay_min, high_pay), duration
    match = PER_LESSON_RE.search(order.pay_text or "")
    if match:
        low = float(match.group("pay_min"))
        high = float(match.group("pay_max") or match.group("pay_min"))
        return min(low, high), max(low, high), float(match.group("hours"))
    session_match = PER_SESSION_RE.search(order.pay_text or "")
    if duration and session_match:
        low = float(session_match.group("pay_min"))
        high = float(session_match.group("pay_max") or session_match.group("pay_min"))
        return min(low, high), max(low, high), duration
    return None, None, duration


def _effective_hourly(
    order: TutorOrder, commute: dict[str, Any] | None
) -> tuple[float | None, float | None, dict[str, float | None]]:
    lesson_low, lesson_high, teaching_hours = compensation_facts(order)
    duration_minutes = None
    if commute and commute.get("status") == "ok":
        duration_minutes = (commute.get("route") or {}).get("duration_minutes")
    if not lesson_low or not lesson_high or not teaching_hours or duration_minutes is None:
        return None, None, {
            "lesson_pay_min": lesson_low,
            "lesson_pay_max": lesson_high,
            "teaching_hours": teaching_hours,
            "one_way_commute_minutes": duration_minutes,
            "total_time_hours": None,
        }
    total_hours = teaching_hours + 2 * float(duration_minutes) / 60
    low = lesson_low / total_hours
    high = lesson_high / total_hours
    return low, high, {
        "lesson_pay_min": round(lesson_low, 1),
        "lesson_pay_max": round(lesson_high, 1),
        "teaching_hours": teaching_hours,
        "one_way_commute_minutes": float(duration_minutes),
        "total_time_hours": round(total_hours, 2),
    }


def _earnings_score(value: float | None) -> int:
    if value is None:
        return 0
    if value >= 80:
        return 30
    if value >= 65:
        return 26
    if value >= 50:
        return 22
    if value >= 40:
        return 17
    if value >= 30:
        return 12
    return 6


def _commute_score(minutes: float | None) -> int:
    if minutes is None:
        return 0
    if minutes <= 30:
        return 25
    if minutes <= 45:
        return 22
    if minutes <= 60:
        return 18
    if minutes <= 75:
        return 13
    if minutes <= 90:
        return 8
    if minutes <= 120:
        return 3
    return 0


def _base_grade(total: int) -> str:
    if total >= 85:
        return "S"
    if total >= 70:
        return "A"
    if total >= 55:
        return "B"
    return "C"


def _cap_grade(grade: str, cap: str) -> str:
    return grade if GRADE_RANK[grade] <= GRADE_RANK[cap] else cap


def score_order(
    order: TutorOrder,
    commute: dict[str, Any] | None,
    profile: CapabilityProfile | None = None,
) -> dict[str, Any]:
    profile = profile or CapabilityProfile()
    ability_score, ability_details, positives, negatives = _ability(order, profile)
    effective_low, effective_high, economics = _effective_hourly(order, commute)
    earnings_score = _earnings_score(effective_low)
    commute_minutes = economics["one_way_commute_minutes"]
    commute_score = _commute_score(commute_minutes)

    schedule = order.schedule_text or ""
    explicitly_long = bool(LONG_TERM_RE.search(schedule))
    explicitly_short = bool(SHORT_TERM_RE.search(schedule)) and not explicitly_long
    if explicitly_long:
        long_term_score = 10
        positives.append("订单具有开学后、每周或长期安排")
    elif explicitly_short:
        long_term_score = 5 if effective_low is not None and effective_low >= 70 else 0
        if long_term_score:
            positives.append("订单虽为短期，但保守真实时薪达到高价例外线 70 元")
        else:
            negatives.append("短期订单且保守真实时薪未达到 70 元，不符合长期价值要求")
    else:
        long_term_score = 4
        negatives.append("订单持续周期不明确")

    if effective_low is None:
        negatives.append("缺少课酬、授课时长或通勤数据，无法计算真实时薪")
    else:
        positives.append(f"保守真实时薪 {effective_low:.1f} 元/小时")
        if earnings_score < 22:
            negatives.append(f"计入往返通勤后，保守真实时薪仅 {effective_low:.1f} 元/小时")
    if commute_minutes is None:
        negatives.append("公共交通通勤时间缺失")
    elif commute_minutes <= 60:
        positives.append(f"单程公共交通约 {commute_minutes:.0f} 分钟，通勤在 60 分钟内")
    elif commute_minutes <= 90:
        negatives.append(f"单程公共交通约 {commute_minutes:.0f} 分钟，通勤成本较高")
    else:
        negatives.append(f"单程公共交通约 {commute_minutes:.0f} 分钟，超过 90 分钟")

    components = {
        "ability": {"score": ability_score, "max_score": 35, "details": ability_details},
        "earnings": {"score": earnings_score, "max_score": 30},
        "commute": {"score": commute_score, "max_score": 25},
        "long_term": {"score": long_term_score, "max_score": 10},
    }
    total = sum(component["score"] for component in components.values())
    grade = _base_grade(total)
    caps: list[dict[str, str]] = []
    if explicitly_short and long_term_score == 0:
        grade = _cap_grade(grade, "C")
        caps.append({"grade": "C", "reason": "短期且真实时薪未达到高价例外线"})
    if profile.weekend_only and not WEEKEND_RE.search(schedule):
        grade = _cap_grade(grade, "B")
        caps.append({"grade": "B", "reason": "周末时间尚未明确"})
        negatives.append("只接周末，但订单没有明确周六、周日或周末")
    if effective_low is None or commute_minutes is None:
        grade = _cap_grade(grade, "B")
        caps.append({"grade": "B", "reason": "真实时薪或通勤数据不完整"})

    economics.update(
        {
            "effective_hourly_min": round(effective_low, 1) if effective_low is not None else None,
            "effective_hourly_max": round(effective_high, 1) if effective_high is not None else None,
            "formula": "lesson_pay / (teaching_hours + 2 * one_way_commute_hours)",
        }
    )
    return {
        "version": SCORING_VERSION,
        "total_score": total,
        "recommendation": grade,
        "component_scores": components,
        "economics": economics,
        "recommendation_reasons": positives,
        "non_recommendation_reasons": negatives,
        "grade_caps": caps,
        "capability_profile": asdict(profile),
    }
