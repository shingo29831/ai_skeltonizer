"""Module: @role: 分割された複数Discoveryタスクのキュー状態管理・永続化・次タスク引き継ぎ準備を担当する。"""
import contextlib
import contextvars
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ...cli_sync import ensure_gitignore_updated
from ...config import load_two_stage_config_from_meta
from ..aistudio_prompts import PromptPattern, load_prompt_for_pattern
from ..scope_builder import build_directory_index_text, build_scoped_bundle_text
from .text import format_discovery_handoff_text
from .types import DiscoveryTask

PENDING_TASKS_FILENAME = "pending_tasks.json"
BRIDGE_SESSION_DIRNAME = "bridge_sessions"
TASK_QUEUE_TTL_SEC = 12 * 60 * 60

_IN_BRIDGE_TERMINAL_APPLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aiskel_in_bridge_terminal_apply",
    default=False,
)

@contextlib.contextmanager
def bridge_terminal_apply_scope() -> Iterator[None]:
    """ブリッジ内部の終端自動適用中であることをCLI側へ通知するスコープコンテキスト。"""
    token = _IN_BRIDGE_TERMINAL_APPLY.set(True)
    try:
        yield
    finally:
        _IN_BRIDGE_TERMINAL_APPLY.reset(token)

def is_in_bridge_terminal_apply() -> bool:
    """現在ブリッジ内部の終端自動適用コンテキスト内で実行中か返す。"""
    return bool(_IN_BRIDGE_TERMINAL_APPLY.get())

def summarize_task_title(task_text: str, index: int) -> str:
    """タスク本文から表示・計画用の1行サマリーを抽出する。"""
    lines = [ln.strip() for ln in (task_text or "").splitlines() if ln.strip()]
    for prefix in ("- タスク概要:", "タスク概要:", "- 目的:", "目的:"):
        for ln in lines:
            if ln.startswith(prefix):
                val = ln[len(prefix) :].strip()
                if val:
                    return val[:97] + "..." if len(val) > 100 else val
    if lines:
        cleaned = re.sub(r"^[-*•\d.)\s]+", "", lines[0]).strip()
        if cleaned:
            return cleaned[:97] + "..." if len(cleaned) > 100 else cleaned
    return f"タスク {index}"

def _safe_float(raw: object, default: float = 0.0) -> float:
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return default
    return default

@dataclass
class DiscoveryTaskQueue:
    """1度目探索で分割された順次実行タスク群の進行状態を保持する。"""

    queue_id: str
    project_root: str
    original_request: str
    new_chat_url: str
    tasks: List[DiscoveryTask]
    current_index: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def total_tasks(self) -> int:
        return len(self.tasks)

    def has_next(self) -> bool:
        return 0 <= self.current_index < (len(self.tasks) - 1)

    def current_task(self) -> Optional[DiscoveryTask]:
        if 0 <= self.current_index < len(self.tasks):
            return self.tasks[self.current_index]
        return None

    def build_plan_lines(self, active_index: int) -> List[str]:
        lines: List[str] = []
        total = len(self.tasks)
        for idx, task in enumerate(self.tasks):
            if idx < active_index:
                status = "完了"
            elif idx == active_index:
                status = "今回担当"
            else:
                status = "待機"
            title = summarize_task_title(task.task_text, idx + 1)
            dirs_preview = ", ".join(task.dirs[:3])
            if len(task.dirs) > 3:
                dirs_preview += f" (+{len(task.dirs) - 3})"
            lines.append(f"- [{status}] {idx + 1}/{total}: {title} (対象: {dirs_preview})")
        return lines

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": 1,
            "queue_id": self.queue_id,
            "project_root": self.project_root,
            "original_request": self.original_request,
            "new_chat_url": self.new_chat_url,
            "current_index": self.current_index,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": [
                {"task_text": t.task_text, "dirs": list(t.dirs)}
                for t in self.tasks
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> Optional["DiscoveryTaskQueue"]:
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return None
        parsed_tasks: List[DiscoveryTask] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                return None
            t_text = str(item.get("task_text") or "")
            raw_dirs = item.get("dirs")
            if not isinstance(raw_dirs, list):
                return None
            dirs = tuple(str(d).strip() for d in raw_dirs if isinstance(d, str) and d.strip())
            if not dirs:
                return None
            parsed_tasks.append(DiscoveryTask(task_text=t_text, dirs=dirs))
        raw_idx = data.get("current_index", 0)
        idx = int(raw_idx) if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) else 0
        if idx < 0 or idx >= len(parsed_tasks):
            return None
        updated_at = _safe_float(data.get("updated_at"), -1.0)
        if updated_at < 0:
            return None
        if updated_at > 0 and (time.time() - updated_at) > TASK_QUEUE_TTL_SEC:
            return None
        created_at = _safe_float(data.get("created_at"), 0.0)
        return cls(
            queue_id=str(data.get("queue_id") or ""),
            project_root=str(data.get("project_root") or ""),
            original_request=str(data.get("original_request") or ""),
            new_chat_url=str(data.get("new_chat_url") or ""),
            tasks=parsed_tasks,
            current_index=idx,
            created_at=created_at,
            updated_at=updated_at,
        )

def _get_pending_tasks_candidates(project_root: Path, output_dir: Optional[Path] = None) -> List[Path]:
    resolved = project_root.resolve()
    candidates: List[Path] = []
    if output_dir is not None:
        candidates.append(output_dir.resolve() / PENDING_TASKS_FILENAME)
    for meta_name in ("ai_meta", ".ai_meta"):
        cand = resolved / meta_name / PENDING_TASKS_FILENAME
        if cand not in candidates:
            candidates.append(cand)
    return candidates

def save_pending_task_queue(
    project_root: Path,
    queue: DiscoveryTaskQueue,
    output_dir: Optional[Path] = None,
) -> Optional[Path]:
    """分割タスクキューをプロジェクトのメタディレクトリへ永続化する。"""
    resolved = project_root.resolve()
    target = (output_dir.resolve() / PENDING_TASKS_FILENAME) if output_dir is not None else (resolved / "ai_meta" / PENDING_TASKS_FILENAME)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_gitignore_updated(resolved, target.parent)
        target.write_text(json.dumps(queue.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target
    except OSError:
        return None

def load_pending_task_queue(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> Optional[DiscoveryTaskQueue]:
    """永続化された分割タスクキューを読み込む（期限切れ・不整合時は削除）。"""
    resolved = project_root.resolve()
    for cand in _get_pending_tasks_candidates(resolved, output_dir=output_dir):
        if not cand.is_file():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        queue = DiscoveryTaskQueue.from_dict(data)
        if queue is None:
            try:
                cand.unlink()
            except OSError:
                pass
            continue
        if queue.project_root:
            try:
                if Path(queue.project_root).resolve() != resolved:
                    continue
            except (OSError, ValueError):
                continue
        return queue
    return None

def clear_pending_task_queue(project_root: Path, output_dir: Optional[Path] = None) -> None:
    """永続化された分割タスクキューを削除する。"""
    for cand in _get_pending_tasks_candidates(project_root, output_dir=output_dir):
        if cand.is_file():
            try:
                cand.unlink()
            except OSError:
                pass

def _get_bridge_session_file(project_root: Path, session_dir: Optional[Path] = None) -> Path:
    base = session_dir.resolve() if session_dir is not None else (Path.home() / ".aiskel" / BRIDGE_SESSION_DIRNAME)
    key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return base / f"{key}.json"

def register_bridge_session(
    project_root: Path,
    url: str,
    token: Optional[str],
    *,
    output_dir: Optional[Path] = None,
    session_dir: Optional[Path] = None,
) -> Optional[Path]:
    """稼働中ローカルブリッジの接続先情報をユーザー領域へ記録する。"""
    target = _get_bridge_session_file(project_root, session_dir=session_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_root": str(project_root.resolve()),
            "url": url.rstrip("/"),
            "token": token or "",
            "output_dir": str(output_dir.resolve()) if output_dir is not None else "",
        }
        target.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target
    except OSError:
        return None

def unregister_bridge_session(
    project_root: Path,
    *,
    expected_url: Optional[str] = None,
    session_dir: Optional[Path] = None,
) -> None:
    """終了時にローカルブリッジ接続先情報を削除する。"""
    target = _get_bridge_session_file(project_root, session_dir=session_dir)
    if not target.is_file():
        return
    if expected_url is not None:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("url") or "").rstrip("/") != expected_url.rstrip("/"):
                return
        except Exception:
            pass
    try:
        target.unlink()
    except OSError:
        pass

def load_bridge_session(project_root: Path, *, session_dir: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """対象プロジェクトに対応する稼働中ブリッジ接続先情報を読み込む。"""
    target = _get_bridge_session_file(project_root, session_dir=session_dir)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        raw_root = str(data.get("project_root") or "")
        url = str(data.get("url") or "").strip()
        if not url or not raw_root or Path(raw_root).resolve() != project_root.resolve():
            return None
        return {
            "project_root": raw_root,
            "url": url,
            "token": str(data.get("token") or ""),
            "output_dir": str(data.get("output_dir") or ""),
        }
    except Exception:
        return None

def notify_bridge_advance_task(
    project_root: Path,
    *,
    session_dir: Optional[Path] = None,
    timeout_sec: float = 5.0,
) -> Optional[Dict[str, object]]:
    """稼働中ローカルブリッジへ次タスク進行をHTTP通知する。"""
    session = load_bridge_session(project_root, session_dir=session_dir)
    if not session:
        return None
    endpoint = f"{session['url'].rstrip('/')}/api/tasks/advance"
    headers = {"Content-Type": "application/json"}
    if session.get("token"):
        headers["X-Aiskel-Token"] = session["token"]
    body = json.dumps({"project_root": str(project_root.resolve())}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if 200 <= resp.status < 300:
                parsed = json.loads(resp.read().decode("utf-8"))
                return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return None

def prepare_task_combined_text(
    project_root: Path,
    output_dir: Optional[Path],
    queue: DiscoveryTaskQueue,
    task_index_0: int,
) -> Tuple[str, str, int, int]:
    """指定タスク番号の最新スコープ限定バンドルとプロンプトを統合して返す。"""
    task = queue.tasks[task_index_0]
    two_stage_cfg = load_two_stage_config_from_meta(project_root, output_dir)
    scoped_bundle_text, resolution = build_scoped_bundle_text(
        project_root,
        list(task.dirs),
        default_recursive=False,
        include_deps=two_stage_cfg.scope_include_deps,
    )
    if not scoped_bundle_text.strip():
        # 先行タスク後のパス不整合時は軽量インデックスを添付しaiskel cで追従可能にする
        scoped_bundle_text = build_directory_index_text(
            project_root,
            include_files=two_stage_cfg.index_include_files,
        )
    second_stage_prompt = load_prompt_for_pattern(project_root, PromptPattern.DEFAULT)
    combined_text = format_discovery_handoff_text(
        second_stage_prompt,
        scoped_bundle_text,
        original_request=queue.original_request,
        organized_task=task.task_text,
        missing_specs=resolution.missing_specs,
        task_index=task_index_0 + 1,
        total_tasks=queue.total_tasks,
        task_plan_lines=queue.build_plan_lines(task_index_0),
    )
    resolved_list: Sequence[str] = resolution.resolved_specs or task.dirs
    target_summary = ", ".join(resolved_list[:4])
    if len(resolved_list) > 4:
        target_summary += f" (+{len(resolved_list) - 4})"
    return (
        combined_text,
        target_summary,
        len(resolution.primary_files),
        len(resolution.dependency_files),
    )

def advance_task_queue_after_cli_apply(
    project_root: Path,
    *,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    session_dir: Optional[Path] = None,
) -> bool:
    """CLIのaiskel a成功時に分割タスクキューを次タスクへ進め、ブリッジ通知またはクリップボード保存を行う。"""
    if is_in_bridge_terminal_apply():
        return False
    session = load_bridge_session(project_root, session_dir=session_dir)
    session_output_dir = Path(session["output_dir"]) if session and session.get("output_dir") else None
    queue = load_pending_task_queue(project_root, output_dir=session_output_dir)
    if queue is None and session is None:
        return False

    bridge_res = notify_bridge_advance_task(project_root, session_dir=session_dir)
    if bridge_res and bridge_res.get("ok"):
        if bridge_res.get("skipped_bridge_apply"):
            return False
        if bridge_res.get("advanced"):
            idx = bridge_res.get("task_index")
            tot = bridge_res.get("total_tasks")
            print(f"\n🚀 実行中のブリッジへ次のタスク ({idx}/{tot}) の新規タブ引き継ぎを通知しました。", flush=True)
            return True
        if bridge_res.get("all_tasks_completed"):
            tot = bridge_res.get("total_tasks")
            print(f"\n🎉 分割タスク全 {tot} 件が完了しました！", flush=True)
            return True
        if not bridge_res.get("no_bound_tab"):
            return False

    if queue is None:
        return False

    if not queue.has_next():
        total = queue.total_tasks
        clear_pending_task_queue(project_root, output_dir=session_output_dir)
        print(f"\n🎉 分割タスク全 {total} 件が完了しました！", flush=True)
        return True

    next_index_0 = queue.current_index + 1
    try:
        combined_text, target_summary, _, _ = prepare_task_combined_text(
            project_root,
            session_output_dir,
            queue,
            next_index_0,
        )
    except Exception:
        return False

    if clipboard_writer is not None:
        try:
            clipboard_writer(combined_text)
        except Exception:
            return False

    queue.current_index = next_index_0
    queue.updated_at = time.time()
    save_pending_task_queue(project_root, queue, output_dir=session_output_dir)

    if clipboard_writer is not None:
        task_num = next_index_0 + 1
        print(
            f"\n📋 次のタスク ({task_num}/{queue.total_tasks}: {target_summary}) の"
            "スコープ限定バンドルとプロンプトをクリップボードにコピーしました。",
            flush=True,
        )
    return True
