from __future__ import annotations

from dataclasses import dataclass, fields, replace
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
from threading import Event, Lock, Thread
from typing import Any, Callable, Literal
from urllib.parse import urlparse

import requests

from .atomic_io import atomic_write_json
from .parser import SUBJECTS, TutorOrder, normalize_text
from .scoring import compensation_is_parseable


ROLE_RECOGNITION = "recognition"
ROLE_ANALYSIS = "analysis"
RECOGNITION_SYSTEM = "你是家教订单字段识别器。必须逐行阅读完整 raw_text，并检查 recognition_targets 以及其他所有缺失字段。existing_fields 中 pay_text 有文字不代表课酬已结构化；只要 lesson_pay 或 hourly_rate 仍为空，就必须从原文提取。已有可靠的非空结构化字段不要重复或改写。每个建议字段都必须附带 raw_text 中的逐字 evidence，无法从原文确认时不要猜测。按次课酬填写 lesson_pay_min/lesson_pay_max，按小时课酬填写 hourly_rate_min/hourly_rate_max。原文‘一次课，3h，450-480’必须输出 duration_hours=3、lesson_pay_min=450、lesson_pay_max=480，evidence 可填整段‘一次课，3h，450-480’。只有需要的字段都已在 existing_fields 或 fields 中时才能填 status=complete。禁止评分或推荐。"
ANALYSIS_SYSTEM = "你是家教候选解释器。程序给出的订单字段、用户画像、硬筛选、地图通勤、分项分数、总分、等级、推荐原因和不推荐原因都是最终事实，禁止修改或重新评分。风险只能改写 fixed_score.non_recommendation_reasons 和 hard_filter.reasons 中已有的内容，不得新增事实。问题只能询问订单中确实缺失或标为待确认的内容，不得询问 user_capability 已明确的信息。schedule_text 含‘周末/周六/周日’时禁止声称周末不明确；用户性别已知时禁止再询问性别。只输出纯文本，不使用 Markdown 符号。"
_RATE_CUE = re.compile(r"每\s*(?:小时|钟头)|(?:一个|1个)\s*(?:小时|钟头)|元\s*/\s*小时|/\s*h\b", re.I)
_DURATION_CUE = re.compile(r"(?:每次|一次|每节|课时|时长).{0,12}\d|\d+(?:\.\d+)?\s*(?:小时|h|分钟)", re.I)
_FREQUENCY_CUE = re.compile(r"(?:每周|一周|每星期|每个星期|一星期|周\s*[一二两三四五六七1-7]\s*次)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class ModelSettings:
    enabled: bool = False
    provider: str = "ollama"
    name: str = "qwen3.5:4b"
    endpoint: str = "http://127.0.0.1:11434"
    temperature: float = 0.1
    keep_alive: str = "10m"
    request_timeout_seconds: float = 90.0
    queue_size: int = 100
    results_file: Path = Path("data/private/model-results.json")

    def validate(self) -> None:
        if self.provider != "ollama":
            raise ValueError("当前只支持本地 Ollama")
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "localhost", "127.0.0.1", "::1",
        }:
            raise ValueError("模型地址必须是本机回环地址")
        if self.queue_size < 1:
            raise ValueError("model.queue_size 必须大于 0")


@dataclass(frozen=True, slots=True)
class ParseAudit:
    complete: bool
    issues: tuple[str, ...]


def audit_order(order: TutorOrder) -> ParseAudit:
    issues: list[str] = []
    if not order.address:
        issues.append("missing_address")
    if not order.subjects and not order.subject_text:
        issues.append("missing_subject")
    if not order.student_text:
        issues.append("missing_student")
    if not order.schedule_text:
        issues.append("missing_schedule")
    if not order.pay_text:
        issues.append("missing_compensation")
    elif not compensation_is_parseable(order):
        issues.append(
            "unparsed_hourly_rate" if _RATE_CUE.search(order.pay_text) else "unparsed_compensation"
        )
    schedule_source = " ".join(filter(None, (order.schedule_text, order.pay_text)))
    if _DURATION_CUE.search(schedule_source) and order.duration_hours is None:
        issues.append("unparsed_duration")
    elif compensation_is_parseable(order) and order.duration_hours is None:
        issues.append("missing_duration")
    return ParseAudit(not issues, tuple(issues))


RECOGNITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["complete", "information_missing"]},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [
                        "address", "subject_text", "student_text", "requirements_text",
                        "schedule_text", "pay_text", "frequency_per_week", "duration_hours",
                        "hourly_rate_min", "hourly_rate_max", "teacher_gender_required",
                        "lesson_pay_min", "lesson_pay_max",
                        "teacher_gender_preferred", "teacher_identity_required",
                    ]},
                    "value": {"type": ["string", "number", "null"]},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "value", "evidence"],
                "additionalProperties": False,
            },
        },
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "fields", "missing_fields"],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "fit_reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "fit_reasons", "risks", "questions"],
    "additionalProperties": False,
}


class OllamaClient:
    def __init__(self, settings: ModelSettings):
        settings.validate()
        self.settings = settings
        self.session = requests.Session()

    def chat(self, role: str, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            self.settings.endpoint.rstrip("/") + "/api/chat",
            json={
                "model": self.settings.name,
                "stream": False,
                "think": False,
                "keep_alive": self.settings.keep_alive,
                "format": schema,
                "options": {"temperature": 0 if role == ROLE_RECOGNITION else self.settings.temperature},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
            timeout=(3.0, self.settings.request_timeout_seconds),
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content")
        if not isinstance(content, str):
            raise ValueError("model_missing_content")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("model_invalid_json_root")
        return result

    def close(self) -> None:
        self.session.close()


def _evidence_is_valid(raw_text: str, evidence: Any) -> bool:
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    def compact(value: str) -> str:
        normalized = normalize_text(value).replace("～", "-").replace("—", "-")
        return re.sub(r"\s+", "", normalized)

    return compact(evidence) in compact(raw_text)


def _number_is_supported(value: float, evidence: str) -> bool:
    return any(abs(float(item) - value) < 0.001 for item in _NUMBER.findall(evidence))


def _numeric_evidence_is_valid(raw_text: str, evidence: Any, value: float) -> bool:
    if not isinstance(evidence, str) or not _number_is_supported(value, evidence):
        return False
    raw_numbers = {float(item) for item in _NUMBER.findall(raw_text)}
    evidence_numbers = {float(item) for item in _NUMBER.findall(evidence)}
    return value in raw_numbers and bool(evidence_numbers) and evidence_numbers.issubset(raw_numbers)


def apply_recognition(order: TutorOrder, result: dict[str, Any]) -> tuple[TutorOrder, dict[str, Any]]:
    allowed = {item.name for item in fields(TutorOrder)}
    original_audit = audit_order(order)
    fillable_fields = {
        "address", "subject_text", "student_text", "requirements_text", "schedule_text",
        "pay_text", "frequency_per_week", "duration_hours", "hourly_rate_min",
        "hourly_rate_max", "lesson_pay_min", "lesson_pay_max", "teacher_gender_required",
        "teacher_gender_preferred", "teacher_identity_required",
    }
    updates: dict[str, Any] = {}
    accepted: list[dict[str, Any]] = []
    for proposal in result.get("fields") if isinstance(result.get("fields"), list) else []:
        if not isinstance(proposal, dict):
            continue
        name, value, evidence = proposal.get("name"), proposal.get("value"), proposal.get("evidence")
        if name not in allowed or name not in fillable_fields or value is None:
            continue
        current = getattr(order, name)
        if current not in (None, "", []):
            continue
        if name in {"frequency_per_week"}:
            if (
                not isinstance(value, (int, float)) or int(value) != value
                or not 1 <= int(value) <= 7
                or not (_evidence_is_valid(order.raw_text, evidence) or _numeric_evidence_is_valid(order.raw_text, evidence, float(value)))
            ):
                continue
            value = int(value)
        elif name in {
            "duration_hours", "hourly_rate_min", "hourly_rate_max",
            "lesson_pay_min", "lesson_pay_max",
        }:
            if (
                not isinstance(value, (int, float)) or value <= 0
                or not (_evidence_is_valid(order.raw_text, evidence) or _numeric_evidence_is_valid(order.raw_text, evidence, float(value)))
            ):
                continue
            value = float(value)
        elif name in {"teacher_gender_required", "teacher_gender_preferred"}:
            if value not in {"男", "女"}:
                continue
        elif not isinstance(value, str) or len(value) > 300 or not _evidence_is_valid(order.raw_text, evidence):
            continue
        updates[name] = value
        accepted.append({"name": name, "value": value, "evidence": evidence})
    if "subject_text" in updates and not order.subjects:
        updates["subjects"] = [subject for subject in SUBJECTS if subject in updates["subject_text"]]
    enriched = replace(order, **updates)
    final_audit = audit_order(enriched)
    provenance = {
        "role": ROLE_RECOGNITION,
        "original_issues": list(original_audit.issues),
        "accepted_fields": accepted,
        "status": "complete" if final_audit.complete else "information_missing",
        "missing_fields": list(final_audit.issues),
    }
    return enriched, provenance


def validate_analysis(result: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("analysis_missing_summary")
    output["summary"] = summary.strip()[:500]
    for key in ("fit_reasons", "risks", "questions"):
        values = result.get(key)
        if not isinstance(values, list):
            raise ValueError(f"analysis_invalid_{key}")
        output[key] = [item.strip()[:300] for item in values[:8] if isinstance(item, str) and item.strip()]
    return output


def recognize_order(client: OllamaClient, order: TutorOrder) -> tuple[TutorOrder, dict[str, Any]]:
    issue_targets = {
        "missing_address": ["address"],
        "missing_subject": ["subject_text"],
        "missing_student": ["student_text"],
        "missing_schedule": ["schedule_text", "frequency_per_week", "duration_hours"],
        "missing_compensation": [
            "pay_text", "lesson_pay_min", "lesson_pay_max", "hourly_rate_min", "hourly_rate_max",
        ],
        "unparsed_compensation": [
            "lesson_pay_min", "lesson_pay_max", "hourly_rate_min", "hourly_rate_max",
        ],
        "unparsed_hourly_rate": ["hourly_rate_min", "hourly_rate_max"],
        "unparsed_duration": ["duration_hours"],
        "missing_duration": ["duration_hours"],
    }
    audit = audit_order(order)
    recognition_targets = list(dict.fromkeys(
        field_name
        for issue in audit.issues
        for field_name in issue_targets.get(issue, [])
    ))
    raw = client.chat(
        ROLE_RECOGNITION,
        RECOGNITION_SYSTEM,
        {
            "raw_text": order.raw_text,
            "existing_fields": {
                key: value for key, value in order.to_dict().items()
                if key not in {"raw_text", "raw_fields"}
            },
            "parse_issues": list(audit.issues),
            "recognition_targets": recognition_targets,
        },
        RECOGNITION_SCHEMA,
    )
    return apply_recognition(order, raw)


def analyze_order(client: OllamaClient, order: TutorOrder, context: dict[str, Any]) -> dict[str, Any]:
    raw = client.chat(ROLE_ANALYSIS, ANALYSIS_SYSTEM, context, ANALYSIS_SCHEMA)
    return {"role": ROLE_ANALYSIS, "status": "complete", **validate_analysis(raw)}


TaskKind = Literal["recognize", "analyze"]


@dataclass(slots=True)
class ModelTask:
    kind: TaskKind
    order: TutorOrder
    context: dict[str, Any]
    callback: Callable[[TutorOrder, dict[str, Any]], None]


class ModelPipeline:
    def __init__(self, settings: ModelSettings, client: OllamaClient | None = None):
        self.settings = settings
        self.client = client or OllamaClient(settings)
        self.queue: Queue[ModelTask] = Queue(maxsize=settings.queue_size)
        self.stop_event = Event()
        self.thread: Thread | None = None
        self._results_lock = Lock()

    def start(self) -> None:
        if self.thread is None:
            self.thread = Thread(target=self._run, name="local-model-pipeline", daemon=True)
            self.thread.start()

    def submit(self, task: ModelTask) -> bool:
        try:
            self.queue.put_nowait(task)
            return True
        except Full:
            return False

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                task = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                if task.kind == "recognize":
                    enriched, result = recognize_order(self.client, task.order)
                    task.callback(enriched, result)
                else:
                    task.callback(task.order, analyze_order(self.client, task.order, task.context))
            except Exception as exc:
                task.callback(task.order, {
                    "role": ROLE_RECOGNITION if task.kind == "recognize" else ROLE_ANALYSIS,
                    "status": "error",
                    "error": type(exc).__name__,
                })
            finally:
                self.queue.task_done()

    def write_result(self, order_id: str, section: str, value: dict[str, Any]) -> None:
        with self._results_lock:
            try:
                stored = json.loads(self.settings.results_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {"version": 1, "orders": {}}
            stored.setdefault("orders", {}).setdefault(order_id, {})[section] = value
            atomic_write_json(self.settings.results_file, stored)

    def stop(self, wait_seconds: float = 2.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(wait_seconds)
        self.client.close()
