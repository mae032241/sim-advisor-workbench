"""Versioned local sessions. User confirmation is scoped to one exact revision."""
from __future__ import annotations

import json
import hashlib
import copy
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .planner import answer_is_resolved, intake_plan
from .workspace_paths import WorkspacePaths


class ConflictError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class SessionStore:
    def __init__(self, data_dir: Path, kb, planner):
        self.paths = WorkspacePaths(data_dir)
        self.directory = self.paths.root / "sessions"
        self._owned_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.trash_directory = self.directory / "trash"
        self.versions_directory = self.directory / "versions"
        self.kb = kb
        self.planner = planner
        self.lock = threading.RLock()

    def _owned_path(self, path: Path) -> Path:
        if not path.is_relative_to(self.paths.root) or not path.resolve().is_relative_to(self.paths.root):
            raise PermissionError("方案路径超出工作台数据目录。")
        current = self.paths.root
        for part in path.relative_to(self.paths.root).parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise PermissionError("方案目录或记录含符号链接/联接，拒绝读取、移动、写入或删除。")
        return path

    @staticmethod
    def _validate_id(session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise FileNotFoundError("会话不存在。")

    def path(self, session_id: str) -> Path:
        self._validate_id(session_id)
        return self._owned_path(self.directory / (session_id + ".json"))

    def _trash_path(self, session_id: str) -> Path:
        # Validate the ID without resolving/stat-ing the active record a second
        # time. Callers check the active and trash locations separately.
        self._validate_id(session_id)
        return self._owned_path(self.trash_directory / (session_id + ".json"))

    def _version_path(self, session_id: str, revision: int) -> Path:
        self._validate_id(session_id)
        if type(revision) is not int or revision < 1:
            raise FileNotFoundError("方案历史版本不存在。")
        return self._owned_path(self.versions_directory / session_id / f"r{revision}.json")

    def _archive(self, value: dict):
        """Archive the last saved state, including same-revision confirmation."""
        path = self._version_path(value["id"], value["revision"])
        self._not_model_file(path, value)
        write_json(path, dict(value, model=self.paths.model_snapshot(value.get("model"), encode=True)))

    def versions(self, session_id: str) -> dict:
        """Small revision metadata; legacy operation logs are not snapshots."""
        with self.lock:
            current = self.get(session_id)  # Deleted sessions have no history API.
            revision = current["revision"]
            events = {item["revision"]: item for item in current.get("history", [])
                      if isinstance(item, dict) and type(item.get("revision")) is int and 1 <= item["revision"] <= revision}
            revisions = set(events) | {revision}
            directory = self._owned_path(self.versions_directory / session_id)
            for path in directory.glob("r*.json"):
                match = re.fullmatch(r"r([1-9][0-9]*)\.json", path.name)
                if match and int(match[1]) <= revision:
                    revisions.add(int(match[1]))
            rows = []
            for number in sorted(revisions, reverse=True):
                event = events.get(number, {})
                rows.append({"revision": number, "available": number == revision or self._version_path(session_id, number).is_file(),
                             "current": number == revision, "event": event.get("event"), "at": event.get("at"),
                             "summary": event.get("summary", "历史修订快照。")})
            return {"session_id": session_id, "current_revision": revision, "versions": rows}

    def version(self, session_id: str, revision: int) -> dict:
        with self.lock:
            path = self._version_path(session_id, revision)
            current = self.get(session_id)
            if revision == current["revision"]:
                return current
            if revision > current["revision"]:
                raise FileNotFoundError("方案历史版本不存在。")
            value = self._record(path, session_id)
            if value.get("revision") != revision:
                raise ValueError("方案历史版本与快照文件标识不一致。")
            value["model"] = self.paths.model_snapshot(value.get("model"))
            return value

    def restore_version(self, session_id: str, target_revision: int, revision: int, *, confirmed=False) -> dict:
        """Copy planning content into a new revision; keep current intake/identity."""
        if confirmed is not True:
            raise ValueError("请明确确认恢复该方案历史版本。")
        with self.lock:
            current = self.get(session_id)
            self.require_revision(current, revision)
            historical = self.version(session_id, target_revision)
            changed_intake = any(current.get(key) != historical.get(key) for key in ("goal", "model"))
            for key in ("plan", "plan_settings", "design_brief", "questions", "answers", "sources", "evidence_gaps", "research_trace",
                        "geometry_reference_catalog", "question_archive", "answer_archive", "plan_stale", "planning_incomplete"):
                if key in historical:
                    current[key] = copy.deepcopy(historical[key])
                else:
                    current.pop(key, None)
            current.setdefault("questions", [])
            current.setdefault("answers", {})
            current.setdefault("sources", [])
            checker = getattr(self.kb, "source_available", None)
            unavailable = [source.get("id") for source in current["sources"]
                           if callable(checker) and not checker(source)]
            gaps = list(current.get("evidence_gaps") or [])
            if changed_intake:
                gaps.append("历史版本的需求或模型与当前输入不同；当前需求和模型已保留，恢复的方案须重新规划。")
            if unavailable:
                gaps.append("历史引用目前不可读取（资料库可能已关闭或删除）：" + "、".join(str(item) for item in unavailable) + "；请重新查阅可用来源并审阅方案。")
                current["plan"]["evidence_status"] = "恢复的历史引用存在不可读来源；保留引用身份，当前证据未重新核实。"
            if changed_intake or unavailable:
                current["plan_stale"] = current["planning_incomplete"] = True
                current["evidence_gaps"] = gaps
            current["version_restore"] = {"from_revision": target_revision, "at": now(), "review_required": True,
                                          "input_changed": changed_intake, "unavailable_source_ids": unavailable}
            return self.save_event(current, "version_restored", f"恢复 r{target_revision} 的规划内容为新修订；保留当前需求、模型与历史，需重新审阅确认。")

    def _not_model_file(self, path, value):
        model = value.get("model")
        if isinstance(model, dict) and isinstance(model.get("path"), str):
            original = self.paths.resolve(model["path"])
            if original == path.resolve() or path.exists() and original.exists() and os.path.samefile(path, original):
                raise PermissionError("方案记录与原始模型为同一文件，拒绝移动、写入或删除。")

    @staticmethod
    def _record(path, session_id):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("id") != session_id:
            raise ValueError("方案记录与文件标识不一致。")
        value.setdefault("lifecycle_revision", 0)
        if type(value["lifecycle_revision"]) is not int or value["lifecycle_revision"] < 0:
            raise ValueError("方案生命周期版本无效。")
        return value

    def revision(self, session_id: str) -> int:
        """Current revision for status polls, without re-parsing an unchanged plan file."""
        return self.head(session_id)["revision"]

    def head(self, session_id: str) -> dict:
        """Identity fields for existence checks and polls: validates the ID, rejects
        trashed or missing plans, and parses the file only when it changed."""
        with self.lock:
            if self._trash_path(session_id).exists():
                raise FileNotFoundError("方案已移入已删除列表。")
            path = self.path(session_id)
            try:
                stat = path.stat()
            except FileNotFoundError as exc:
                raise FileNotFoundError("会话不存在。") from exc
            cache = self.__dict__.setdefault("_revision_cache", {})
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            cached = cache.get(session_id)
            if cached and cached[0] == signature:
                return dict(cached[1])
            record = self._record(path, session_id)
            value = {"id": session_id, "revision": record["revision"], "lifecycle_revision": record["lifecycle_revision"]}
            cache[session_id] = (signature, value)
            return dict(value)

    def get(self, session_id: str) -> dict:
        with self.lock:
            try:
                if self._trash_path(session_id).exists():
                    raise FileNotFoundError("方案已移入已删除列表。")
                value = self._record(self.path(session_id), session_id)
                value["model"] = self.paths.model_snapshot(value.get("model"))
                return value
            except FileNotFoundError as exc:
                raise FileNotFoundError("会话不存在。") from exc

    def list(self) -> list[dict]:
        # Keep the compact, recent-session status contract for the sidebar.
        return [{key: row[key] for key in ("id", "title", "status", "updated_at")}
                for row in self.list_page()["sessions"]]

    def list_page(self, *, offset=0, limit=100) -> dict:
        """Page lightweight summaries without exposing saved planning content."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("方案列表 offset 必须是非负整数，limit 必须是 1–200 的整数。")
        items = []
        with self.lock:
            for path in self.directory.glob("*.json"):
                try:
                    if not re.fullmatch(r"[a-f0-9]{32}", path.stem) or self._trash_path(path.stem).exists():
                        continue
                    value = self._record(self.path(path.stem), path.stem)
                    items.append({k: value[k] for k in ("id", "title", "status", "revision", "updated_at")})
                except (OSError, ValueError, KeyError):
                    continue
        items.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        end = offset + limit
        return {"sessions": items[offset:end], "total": len(items),
                "next_offset": end if end < len(items) else None}

    def deleted_list(self) -> list[dict]:
        items = []
        with self.lock:
            self._owned_path(self.trash_directory)
            for path in self.trash_directory.glob("*.json"):
                try:
                    value = self._record(self._trash_path(path.stem), path.stem)
                    items.append({**{key: value[key] for key in ("id", "title", "revision", "updated_at")},
                                  "status": "deleted", "previous_status": value["status"], "deleted_at": value.get("deleted_at")})
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(items, key=lambda item: item.get("deleted_at") or item["updated_at"], reverse=True)

    def delete(self, session_id: str, revision: int, *, confirmed=False) -> dict:
        if confirmed is not True:
            raise ValueError("请明确确认将该方案移入已删除列表。")
        with self.lock:
            value = self.get(session_id)
            self.require_revision(value, revision)
            source, target = self.path(session_id), self._trash_path(session_id)
            self._not_model_file(source, value)
            self._not_model_file(target, value)
            if target.exists():
                raise ConflictError("已删除列表存在同一方案，未覆盖。")
            self.trash_directory.mkdir(exist_ok=True)
            self._owned_path(target)
            deleted_at = now()
            source.rename(target)
            try:
                value["deleted_at"] = deleted_at
                value.setdefault("history", []).append({"revision": value["revision"], "event": "deleted", "at": deleted_at,
                                                         "summary": "方案移入已删除列表；模型、资料库与 Agent 对话保留。"})
                write_json(target, dict(value, model=self.paths.model_snapshot(value.get("model"), encode=True)))
            except Exception:
                if not source.exists():
                    target.rename(source)
                raise
            return {"id": session_id, "status": "deleted", "revision": value["revision"], "deleted_at": deleted_at}

    def restore(self, session_id: str) -> dict:
        with self.lock:
            source, target = self._trash_path(session_id), self.path(session_id)
            if target.exists():
                raise ConflictError("同一方案标识已存在，未覆盖当前方案。")
            value = self._record(source, session_id)
            self._not_model_file(source, value)
            self._not_model_file(target, value)
            original = dict(value)
            value["model"] = self.paths.model_snapshot(value.get("model"))
            value["revision"] += 1
            value["lifecycle_revision"] += 1
            value["updated_at"] = now()
            for key in ("deleted_at", "confirmed_at", "confirmed_revision"):
                value.pop(key, None)
            value["status"] = ("needs_replan" if value.get("plan_stale") or value.get("planning_incomplete") else
                               "needs_input" if self.missing_answers(value) else "draft")
            value["history"] = [*value.get("history", []), {"revision": value["revision"], "event": "restored", "at": value["updated_at"],
                                                         "summary": "从已删除列表恢复方案；保留原方案和回答，当前版本需重新审阅确认。"}]
            self._archive(original)
            write_json(source, dict(value, model=self.paths.model_snapshot(value.get("model"), encode=True)))
            try:
                source.rename(target)
            except Exception:
                write_json(source, original)
                raise
            return value

    def purge(self, session_id: str, revision: int, *, confirmed=False) -> dict:
        """Remove a deleted plan and its owned revision snapshots permanently.

        Retained save drafts belong to this plan and are removed as well.
        Models, libraries, exports and Agent conversation records have separate
        ownership and are deliberately retained. Validate the complete deletion
        set first; never recursively delete an arbitrary directory. Keep the
        tombstone until snapshots are removed so an I/O failure can be retried.
        """
        if confirmed is not True:
            raise ValueError("请明确确认彻底删除该方案及其历史版本；此操作不可恢复。")
        with self.lock:
            active, tombstone = self.path(session_id), self._trash_path(session_id)
            if active.exists():
                raise ConflictError("仅已删除列表中的方案可以彻底删除；请刷新方案列表。")
            value = self._record(tombstone, session_id)
            if type(value.get("revision")) is not int or value["revision"] < 1:
                raise ValueError("已删除方案版本无效，拒绝彻底删除。")
            self.require_revision(value, revision)
            directory = self._owned_path(self.versions_directory / session_id)
            snapshots = []
            if directory.exists():
                if not directory.is_dir():
                    raise PermissionError("方案历史版本目录无效，拒绝彻底删除。")
                for path in directory.iterdir():
                    self._owned_path(path)
                    match = re.fullmatch(r"r([1-9][0-9]*)\.json", path.name)
                    if not match or not path.is_file():
                        raise PermissionError("方案历史版本目录含非快照内容，拒绝彻底删除。")
                    snapshot = self._record(path, session_id)
                    number = int(match[1])
                    if type(snapshot.get("revision")) is not int or snapshot["revision"] != number or number > value["revision"]:
                        raise ValueError("方案历史版本与快照文件标识不一致，拒绝彻底删除。")
                    snapshots.append((path, snapshot))
            records = [(tombstone, value), *snapshots]
            draft = self._owned_path(self.paths.root / 'plan-drafts' / (session_id + '.json'))
            if draft.exists():
                if not draft.is_file() or draft.stat().st_nlink != 1:
                    raise PermissionError('方案草稿不是独占的普通文件，拒绝彻底删除。')
                if draft.stat().st_size > 2 * 1024 * 1024 + 65536:
                    raise ValueError('方案草稿超过大小限制，拒绝彻底删除。')
                record = json.loads(draft.read_text(encoding='utf-8'))
                if record.get('schema') != 'sim-advisor-plan-draft/v1' or record.get('session_id') != session_id:
                    raise ValueError('方案草稿归属不一致，拒绝彻底删除。')
            models = {record["model"]["path"]: record for _, record in records
                      if isinstance(record.get("model"), dict) and isinstance(record["model"].get("path"), str)}
            # A model can have changed between revisions. Protect every model
            # locator against every candidate, including hard-linked models.
            for path, _ in records:
                for record in models.values():
                    self._not_model_file(path, record)
            if draft.exists():
                for record in models.values():
                    self._not_model_file(draft, record)
                self._owned_path(draft).unlink()
            for path, _ in snapshots:
                self._owned_path(path).unlink()
            if directory.exists():
                self._owned_path(directory).rmdir()
            self._owned_path(tombstone).unlink()
            return {"id": session_id, "status": "purged"}

    def create(self, goal: str, model: dict | None) -> dict:
        if not isinstance(goal, str) or not 8 <= len(goal.strip()) <= 12000:
            raise ValueError("请填写 8–12000 字的仿真需求。")
        goal = goal.strip()
        model = self.paths.model_snapshot(model)
        created = now()
        value = {"id": uuid.uuid4().hex, "revision": 1, "lifecycle_revision": 0, "goal": goal, "title": goal[:46],
                 "status": "needs_replan", "created_at": created, "updated_at": created,
                 "model": model, "questions": [], "answers": {}, "plan_stale": True,
                 "plan": intake_plan(goal, model), "sources": [], "design_brief": {}, "research_trace": [],
                 "history": [{"revision": 1, "event": "created", "at": created, "summary": "记录需求与几何/模型事实，等待 Agent 进行问题分析和路线设计。"}]}
        with self.lock:
            if self.path(value["id"]).exists() or self._trash_path(value["id"]).exists():
                raise ConflictError("方案标识已存在；未覆盖现有方案或已删除记录。")
            self._write(value, create=True)
        return value

    def _write(self, value, *, create=False):
        with self.lock:
            path, current = self._write_guard(value, include_current=True, allow_create=create)
            if current is not None and value["revision"] > current["revision"]:
                self._archive(current)
            stored = dict(value, model=self.paths.model_snapshot(value.get("model"), encode=True))
            write_json(path, stored)

    def _write_guard(self, value, *, include_current=False, allow_create=False):
        path = self.path(value["id"])
        if self._trash_path(value["id"]).exists():
            raise FileNotFoundError("方案已删除，旧的保存结果不能恢复该方案。")
        self._not_model_file(path, value)
        current = None
        if path.exists():
            current = self._record(path, value["id"])
            if value.get("lifecycle_revision", 0) != current["lifecycle_revision"]:
                raise ConflictError("方案已删除或恢复，旧的保存结果已失效，请重新读取。")
            if value.get("revision", 0) < current.get("revision", 0):
                raise ConflictError("方案版本已变化，旧的保存结果已失效。")
        elif not allow_create:
            raise FileNotFoundError("方案不存在或已彻底删除，旧的保存结果不能恢复该方案。")
        return (path, current) if include_current else path

    @staticmethod
    def require_revision(session: dict, revision):
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != session["revision"]:
            raise ConflictError("会话版本已变化，请重新加载后再提交；当前操作未覆盖新版本。")

    @staticmethod
    def missing_answers(session: dict) -> list[str]:
        return [q["label"] for q in session["questions"] if q.get("required") and
                (not answer_is_resolved(session["answers"].get(q["id"])) or
                 (q["id"].startswith("decision_") and session["answers"].get(q["id"]) != "accept"))]

    def save_event(self, value: dict, event: str, summary: str) -> dict:
        with self.lock:
            self._write_guard(value)
            value["revision"] += 1
            value["updated_at"] = now()
            value.pop("confirmed_revision", None)
            value.pop("confirmed_at", None)
            value["status"] = ("needs_replan" if value.get("plan_stale") or value.get("planning_incomplete") else
                               "needs_input" if self.missing_answers(value) else "draft")
            value["history"].append({"revision": value["revision"], "event": event, "at": value["updated_at"], "summary": summary})
            self._write(value)
            return value

    def answers(self, session_id: str, revision: int, answers: dict) -> dict:
        if not isinstance(answers, dict):
            raise ValueError("answers 必须是字段和值的对象。")
        with self.lock:
            value = self.get(session_id)
            self.require_revision(value, revision)
            questions = {q["id"]: q for q in value["questions"]}
            if set(answers) - set(questions):
                raise ValueError("提交包含未知的问题字段。")
            requires_replan = False
            for key, answer in answers.items():
                if not isinstance(answer, str) or len(answer) > 8000:
                    raise ValueError("回答必须是长度不超过 8000 的文本。")
                answer = answer.strip()
                if questions[key]["kind"] == "select" and answer and answer not in {o["value"] for o in questions[key]["options"]}:
                    raise ValueError("选择值不在允许选项中。")
                if answer != value["answers"].get(key, "") and (not key.startswith("decision_") or answer != "accept"):
                    requires_replan = True
                value["answers"][key] = answer
            # Retain the actual design for comparison, but never endorse stale reasoning.
            value["plan_stale"] = bool(value.get("plan_stale")) or requires_replan
            summary = "保存用户回答；保留上一版方案供对照，需 Agent 结合新输入重新规划。" if value["plan_stale"] else "保存对当前建模建议的明确选择，方案内容未改变。"
            return self.save_event(value, "answers_saved", summary)

    @staticmethod
    def merge_questions(value: dict, bundle: dict):
        previous = {q["id"]: q for q in value["questions"]}
        merged = {key: q for key, q in previous.items() if not key.startswith("decision_")}
        retired_ids = set()
        for retired in bundle.get("retired_questions", []):
            if not isinstance(retired, dict) or retired.get("id") not in previous or str(retired.get("id", "")).startswith("decision_") or not isinstance(retired.get("reason"), str) or not retired["reason"].strip():
                raise ValueError("问题退役记录必须指向已有问题并说明建模路线变化的理由。")
            qid = retired["id"]
            retired_ids.add(qid)
            merged.pop(qid, None)
            value.setdefault("question_archive", []).append({"question": previous[qid], "reason": retired["reason"], "revision": value["revision"]})
        incoming = bundle.get("questions", [])
        if not isinstance(incoming, list) or len(incoming) > 40:
            raise ValueError("Agent 问题列表格式错误或数量过多。")
        for q in incoming:
            if not isinstance(q, dict) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(q.get("id", ""))):
                raise ValueError("Agent 问题标识无效。")
            if q["id"].startswith("decision_"):
                continue  # These are generated locally from the recommendation content.
            if q["id"] in retired_ids:
                raise ValueError("同一问题不能同时启用和退役。")
            if q.get("kind") not in {"text", "select", "number"} or not isinstance(q.get("label"), str) or not q["label"].strip() or not isinstance(q.get("required"), bool):
                raise ValueError("Agent 问题定义不完整。")
            if q["kind"] == "select" and (not isinstance(q.get("options"), list) or not q["options"] or
                    not all(isinstance(o, dict) and isinstance(o.get("value"), str) and isinstance(o.get("label"), str) for o in q["options"])):
                raise ValueError("Agent 选择问题缺少合法选项。")
            old = previous.get(q["id"])
            if old and any(old.get(key) != q.get(key) for key in ("label", "kind", "options")) and q["id"] in value["answers"]:
                value.setdefault("answer_archive", []).append({"question": old, "answer": value["answers"].pop(q["id"]), "revision": value["revision"]})
            merged[q["id"]] = q
        for decision in bundle.get("design_brief", {}).get("decisions", []):
            if not decision.get("requires_confirmation"):
                continue
            identity = {k: decision.get(k) for k in ("topic", "recommendation", "alternatives")}
            token = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            qid = "decision_" + token
            merged[qid] = {"id": qid, "label": "是否采用此建议：" + str(decision.get("topic", "建模决策")), "kind": "select", "required": True,
                           "reason": str(decision.get("recommendation", "")) + "\n依据与理由：" + str(decision.get("reason", "")),
                           "options": [{"value": "accept", "label": "接受此建议作为当前方案前提"}, {"value": "revise", "label": "需要调整，请重新分析"}, {"value": "undecided", "label": "暂时无法决定"}]}
        for candidate in bundle.get("design_brief", {}).get("simplification", {}).get("candidates", []):
            if not candidate.get("requires_confirmation"):
                continue
            # Acceptance covers the actual basis, physical protection criteria
            # and operating inputs, not merely the wording of an operation.
            identity = {k: v for k, v in candidate.items() if k not in {"id", "requires_confirmation"}}
            identity["context"] = {
                "model_fingerprint": (value.get("model") or {}).get("fingerprint"),
                "protected_features": bundle.get("design_brief", {}).get("simplification", {}).get("protected_features", []),
                "inputs": {key: answer for key, answer in value.get("answers", {}).items() if not key.startswith("decision_")},
            }
            token = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            qid = "decision_simp_" + token
            inspecting = candidate.get("status") == "needs_inspection"
            label = "是否将此简化候选纳入检查：" if inspecting else "是否采用此条件化简化建议："
            merged[qid] = {"id": qid, "label": label + candidate.get("target", ""), "kind": "select", "required": True,
                           "reason": candidate.get("action", "") + "\n成立条件：" + candidate.get("applicability", "") +
                                     "\n可能损失：" + "；".join(candidate.get("lost_effects", [])) +
                                     "\n验证要求：" + "；".join(candidate.get("verification", [])) +
                                     "\n接受只表示纳入待验证方案，不表示已验证几何存在、误差可接受或已修改模型。",
                           "options": [{"value": "accept", "label": "接受为待检查、待验证的方案建议"},
                                       {"value": "revise", "label": "需要调整，请重新分析"}, {"value": "undecided", "label": "暂时无法决定"}]}
        active_ids = set(merged)
        for key in list(value["answers"]):
            if key not in active_ids:
                value.setdefault("answer_archive", []).append({"question": previous.get(key, {"id": key}), "answer": value["answers"].pop(key), "revision": value["revision"]})
        value["questions"] = list(merged.values())

    def refine(self, session_id: str, revision: int, *, focus: str | None = None) -> dict:
        with self.lock:
            value = self.get(session_id)
            self.require_revision(value, revision)
        if focus not in {None, "simplification"}:
            raise ValueError("不支持的规划范围。")
        if focus == "simplification":
            if value.get("plan_stale") or value["plan"].get("engine") in {"intake-only", "reference-baseline"}:
                raise ValueError("请先针对当前需求完成整体方案规划，再补充模型简化指导。")
            bundle = self.planner.refine_simplification(value)
        else:
            bundle = self.planner.refine(value)
        with self.lock:
            latest = self.get(session_id)
            self.require_revision(latest, revision)
            if not isinstance(bundle, dict) or "plan" not in bundle or "sources" not in bundle:
                raise ValueError("规划器未返回路线设计和证据包。")
            self.merge_questions(latest, bundle)
            for key in ("plan", "sources", "design_brief", "research_trace"):
                latest[key] = bundle.get(key, {} if key == "design_brief" else [])
            if "geometry_reference_catalog" in bundle:
                latest["geometry_reference_catalog"] = bundle["geometry_reference_catalog"]
            latest["plan_stale"] = False
            latest["planning_incomplete"] = bool(bundle.get("planning_incomplete")) or any(
                isinstance(item, dict) and (item.get("event") == "budget_exhausted" or item.get("status") == "budget_exhausted") for item in latest["research_trace"])
            routes = latest.get("design_brief", {}).get("candidate_routes", [])
            latest["planning_incomplete"] = latest["planning_incomplete"] or bool(routes and all(route.get("status") == "blocked" for route in routes))
            return self.save_event(latest, "simplification_refined" if focus else "agent_refined",
                                   "Agent 更新模型简化指导、相关实施步骤和条件化建议；未修改 CAD。" if focus else
                                   "Agent 完成需求分析、证据查阅与路线设计；请检查实施指南、缺口和待确认决策。")

    def confirm(self, session_id: str, revision: int) -> dict:
        with self.lock:
            value = self.get(session_id)
            self.require_revision(value, revision)
            routes = value.get("design_brief", {}).get("candidate_routes", [])
            if value.get("plan_stale") or value.get("planning_incomplete") or (routes and all(route.get("status") == "blocked" for route in routes)) or value["plan"].get("engine") in {"intake-only", "reference-baseline"}:
                raise ValueError("尚未完成当前输入的 Agent 方案设计。请先规划或更新方案，再进行确认。")
            missing = self.missing_answers(value)
            if missing:
                raise ValueError("尚未提交必填意图：" + "；".join(missing))
            if value["status"] == "confirmed":
                return value
            value["status"] = "confirmed"
            value["confirmed_revision"] = value["revision"]
            value["confirmed_at"] = value["updated_at"] = now()
            value["history"].append({"revision": value["revision"], "event": "confirmed", "at": value["updated_at"], "summary": "用户确认本版本需求与方案；不表示求解已执行或物理结果已验证。"})
            self._write(value)
            return value

    def apply_agent_bundle(self, session_id: str, revision: int, bundle: dict, *, partial=False) -> dict:
        """Commit an already validated conversational edit to this exact plan."""
        with self.lock:
            value = self.get(session_id)
            self.require_revision(value, revision)
            if not partial:
                self.merge_questions(value, bundle)
            for key in ("plan", "plan_settings", "sources", "design_brief", "research_trace", "geometry_reference_catalog", "evidence_gaps"):
                if key in bundle:
                    value[key] = bundle[key]
            if not partial:
                value["plan_stale"] = False
                value["planning_incomplete"] = bool(bundle.get("planning_incomplete"))
            return self.save_event(value, "agent_steps_updated" if partial else "agent_plan_updated",
                                   "绑定的 Codex 会话更新实施节点；原有未决项继续保留。" if partial else
                                   "绑定的 Codex 会话根据需求与已读资料更新方案；等待用户审阅确认。")


def export_markdown(session: dict) -> str:
    plan = session["plan"]
    lines = ["# " + plan["title"], "", f"会话 `{session['id']}` · 版本 {session['revision']} · 状态 {session['status']}",
             "", "规划引擎：" + plan["engine"], "", "## 需求", "", session["goal"], "", "## 模型快照", ""]
    model = session.get("model")
    if model:
        lines += [f"文件：{model['file_name']}；格式：{model.get('format', '未知')}；解析器：{model['parser']}；版本：{model.get('version') or '不适用或未知'}", ""]
        for fact in model.get("facts", []):
            lines += [f"- **{fact['label']}**：{json.dumps(fact['value'], ensure_ascii=False)}（{fact.get('source', '未知来源')}）"]
        lines += ["", "未解析项：" + "；".join(str(x) for x in model.get("unknowns", [])), ""]
    else:
        lines += ["未提供模型。", ""]
    if session.get("plan_stale"):
        lines += ["**当前方案尚未针对最新输入完成规划，不能作为已确认的实施依据。**", ""]
    design = session.get("design_brief") or {}
    if design:
        lines += ["## 建模路线与依据", "", "目标：" + design.get("objective", ""), "", "计算域策略：" + design.get("domain_strategy", ""), ""]
        for observation in design.get("geometry_interpretation", []):
            lines += [f"- [{observation.get('status', 'unknown')}] {observation.get('statement', '')}；依据：{observation.get('evidence', '')}"]
        for route in design.get("candidate_routes", []):
            lines += ["", "### " + route.get("name", "候选路线"), "", "状态：" + route.get("status", "candidate"), "", "物理机制/接口：" + "；".join(route.get("physics", [])), "", route.get("rationale", ""), "", "适用前提：" + "；".join(route.get("assumptions", [])), "", "所需输入：" + "；".join(route.get("required_inputs", [])), "", "取舍：" + route.get("tradeoffs", "")]
        for decision in design.get("decisions", []):
            lines += ["", "- **" + decision.get("topic", "决策") + "**：" + decision.get("recommendation", ""), "  理由：" + decision.get("reason", ""), "  其他选项：" + "；".join(decision.get("alternatives", []))]
    simplification = design.get("simplification") or {}
    if simplification:
        lines += ["", "## 模型简化指导", "", simplification.get("objective", ""), "", "需保护的特征/物理作用：" + "；".join(simplification.get("protected_features", [])), ""]
        source_map = {source["id"]: source for source in session["sources"]}
        fact_map = {"fact:" + fact["key"]: fact for fact in (model or {}).get("facts", []) if fact.get("key")}
        body_map = {"body:" + body["id"]: body for body in (model or {}).get("geometry", {}).get("bodies", []) if body.get("id")}
        statuses = {"conditional": "满足条件后可考虑", "needs_inspection": "待检查候选", "keep": "建议保留"}
        for candidate in simplification.get("candidates", []):
            lines += ["", "### " + candidate.get("target", "简化候选"), "", "状态：" + statuses.get(candidate.get("status"), "待审阅"), "",
                      "建议：" + candidate.get("action", ""), "", "理由：" + candidate.get("rationale", ""), "",
                      "成立条件：" + candidate.get("applicability", ""), "", "预期收益：" + candidate.get("expected_benefit", ""), "",
                      "可能损失：" + "；".join(candidate.get("lost_effects", [])), "", "解释与待检查假设：" + "；".join(candidate.get("geometry_evidence", [])), "",
                      "验证方式：" + "；".join(candidate.get("verification", [])), ""]
            if "geometry_fact_refs" not in candidate:
                lines += ["旧版建议尚无结构化几何引用，需补充简化分析。", ""]
            for ref in candidate.get("geometry_fact_refs", []):
                if ref in fact_map:
                    fact = fact_map[ref]
                    lines += [f"- 几何事实 `{ref}`：{fact['label']} = {json.dumps(fact['value'], ensure_ascii=False)}；来源：{fact.get('source', '未知')}"]
                elif ref in body_map:
                    body = body_map[ref]
                    lines += [f"- 实体事实 `{ref}`：包围盒 {json.dumps(body.get('bounding_box'), ensure_ascii=False)}；体积 {json.dumps(body.get('volume'), ensure_ascii=False)}；面数 {body.get('surface_count', '未知')}；仅当前快照有效。"]
                else:
                    lines += [f"- 无法核对几何引用 `{ref}`，不能当作已观测事实。"]
            for sid in candidate.get("source_ids", []):
                if sid in source_map:
                    source = source_map[sid]
                    lines += [f"- 参考 [{source['title']}]({source['url']})"]
        lines += ["", "仍需检查：" + "；".join(simplification.get("unresolved_geometry", [])), "",
                  "以上为条件化指导；未修改 CAD，也未完成简化误差验证。", ""]
    lines += ["## 已提交的用户意图", ""]
    for q in session["questions"]:
        answer = session["answers"].get(q["id"], "未提交")
        answer = next((o["label"] for o in q.get("options", []) if o["value"] == answer), answer)
        lines += [f"- **{q['label']}**：{answer or '未提交'}"]
    lines += ["", "## 方案", "", plan["summary"], ""]
    for assumption in plan["assumptions"]:
        lines += ["- " + assumption]
    sources = {s["id"]: s for s in session["sources"]}
    for i, step in enumerate(plan["steps"], 1):
        lines += ["", f"### {i}. {step['title']}", "", step["action"], "", "位置：" + step["location"], "", "作用域：" + step["selection"], "", "输入：" + "；".join(step["inputs"]), "", "依赖：" + (", ".join(step["depends_on"]) or "无"), "", "检查：" + "；".join(step["checks"]), "", "难点：" + step["difficulty"], ""]
        for row in session.get("plan_settings", []):
            if step["id"] not in row.get("step_ids", []):
                continue
            lines += ["- 设置与依据：" + row["setting"], "  对应要求：" + row["requirement"],
                      "  拟用值：" + row["proposed_value"], "  采用状态：" + row["status"],
                      "  迁移理由：" + row["transfer_rationale"], "  状态说明：" + row["reason"],
                      "  剩余缺口：" + (row["gap"] or "作者未声明；不代表物理验证通过")]
            for ref in row["source_refs"]:
                lines += ["  参考原值：" + ref["stated_value"] + "；来源 `" + ref["source_id"] + "`；" + ref["context"] + "；定位 `" + ref["locator"] + "`"]
        for sid in step["source_ids"]:
            if sid in sources:
                source = sources[sid]
                lines += [f"- 依据 [{source['title']}]({source['url']}) · {source.get('section_path', '')}"]
    step_ids = {step["id"] for step in plan["steps"]}
    unassigned = [row for row in session.get("plan_settings", []) if not step_ids.intersection(row.get("step_ids", []))]
    if unassigned:
        lines += ["", "## 未采用与待处理设置", "", "以下保留作者的取舍和缺口，不代表已实施或通过验证。", ""]
        for row in unassigned:
            lines += ["- " + row["setting"] + "（" + row["status"] + "）", "  要求：" + row["requirement"],
                      "  拟用值：" + row["proposed_value"], "  取舍：" + row["reason"],
                      "  迁移理由：" + row["transfer_rationale"], "  缺口：" + (row["gap"] or "作者未声明")]
            for ref in row["source_refs"]:
                lines += ["  来源 `" + ref["source_id"] + "`；定位 `" + ref["locator"] + "`；原值：" + ref["stated_value"]]
    lines += ["", "## 待补充与难点", ""] + ["- " + s for s in plan["missing_information"] + plan["difficulties"]]
    lines += ["", "## 证据清单", "", plan["evidence_status"], ""]
    lines += [f"- [{s['title']}]({s['url']}) · {s['kind']} · COMSOL {s.get('version', '未知')} · `{s['id']}`" for s in session["sources"]]
    lines += ["", "## 版本记录", ""] + [f"- r{h['revision']} · {h['at']} · {h['summary']}" for h in session["history"]]
    if session.get("question_archive"):
        lines += ["", "## 因路线变化退出的旧问题", ""] + [f"- {entry['question']['label']}：{entry['reason']}" for entry in session["question_archive"]]
    lines += ["", "本文件是规划记录。用户确认只针对列明版本的需求与方案，不代表已运行求解或通过物理验证。", ""]
    return "\n".join(lines)
