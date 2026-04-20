#!/usr/bin/env python3
"""
GenericAgent MCP Bridge - 让 Trae 的自带模型驱动，GenericAgent 只提供工具执行能力
放置: GenericAgent/mcp_bridge_isolated.py
用法: 在 Trae 的 MCP 配置中引用此文件
"""

import asyncio
import json
import sys
import os
import io
import base64
import re
import uuid
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, unquote

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    CallToolResult,
    ToolAnnotations,
)
from mcp.server.models import InitializationOptions

# 导入 GenericAgent 工具模块（无 LLM）
from ga import (
    execute_code,
    web_scan, web_execute_js, ask_user,
    file_read as ga_file_read,
    file_patch as ga_file_patch,
    expand_file_refs,
)


# 导入 GenericAgent 的扩展能力
with redirect_stdout(io.StringIO()):
    try:
        from memory.adb_ui import ui as adb_ui, tap as adb_tap
        ADB_AVAILABLE = True
    except ImportError:
        ADB_AVAILABLE = False

    try:
        from memory.ocr_utils import ocr_screen
        OCR_AVAILABLE = True
    except ImportError:
        OCR_AVAILABLE = False

    try:
        from memory.ljqCtrl import Press, SetCursorPos, MouseClick, MouseDClick
        SYSCTL_AVAILABLE = True
    except ImportError:
        SYSCTL_AVAILABLE = False

    try:
        from memory.ui_detect import detect_ui_elements
        UI_DETECT_AVAILABLE = True
    except ImportError:
        UI_DETECT_AVAILABLE = False

    try:
        from memory.procmem_scanner import scan_memory
        PROCMEM_AVAILABLE = True
    except ImportError:
        PROCMEM_AVAILABLE = False

    try:
        from memory.skill_search.skill_search.engine import search as skill_search, detect_environment
        SKILL_SEARCH_AVAILABLE = True
    except ImportError:
        SKILL_SEARCH_AVAILABLE = False

    try:
        from memory.keychain import keys
        KEYCHAIN_AVAILABLE = True
    except ImportError:
        KEYCHAIN_AVAILABLE = False


# 获取所有 SOP 文件
SOP_DIR = os.path.join(script_dir, 'memory')
SESSION_STATE_DIRNAME = ".genericagent_mcp"
GENERICAGENT_RULES_MARKER_START = "<!-- GENERICAGENT_MCP_PROTOCOL:START -->"
GENERICAGENT_RULES_MARKER_END = "<!-- GENERICAGENT_MCP_PROTOCOL:END -->"


def get_all_sops():
    """获取所有 SOP 文件列表"""
    sops = []
    try:
        for f in os.listdir(SOP_DIR):
            if f.endswith('_sop.md'):
                sops.append({
                    "name": f.replace('_sop.md', ''),
                    "file": f,
                    "path": os.path.join(SOP_DIR, f)
                })
    except:
        pass
    return sops


def resolve_workspace_path(path: str, cwd: str) -> Path:
    """Resolve a user path against request cwd, matching ga.py relative-path behavior."""
    if not path:
        raise ValueError("path 不能为空")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return candidate.resolve()


def write_file_content(path: Path, content: str, *, mode: str = "overwrite") -> Dict[str, Any]:
    """MCP version of ga.py file_write: use patch for precise edits; write for large content."""
    if mode not in ("overwrite", "append", "prepend"):
        raise ValueError("mode 必须是 overwrite/append/prepend")
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "prepend":
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        path.write_text(content + old, encoding="utf-8")
    elif mode == "append":
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return {"status": "success", "path": str(path), "mode": mode, "written_bytes": len(content)}


def load_memory_management_sop() -> str:
    path = Path(SOP_DIR) / "memory_management_sop.md"
    if not path.exists():
        return "Memory Management SOP not found. Do not update memory."
    return read_text_if_exists(path)


def screenshot_to_base64() -> str:
    """截图并返回 base64 编码"""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception as e:
        return f"截图失败: {str(e)}"


def get_window_list() -> List[Dict]:
    """获取所有窗口列表"""
    try:
        import win32gui
        windows = []

        def callback(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    rect = win32gui.GetWindowRect(hwnd)
                    windows.append({
                        "hwnd": hwnd,
                        "title": title,
                        "rect": rect,
                        "width": rect[2] - rect[0],
                        "height": rect[3] - rect[1]
                    })

        win32gui.EnumWindows(callback, None)
        return windows
    except Exception as e:
        return [{"error": str(e)}]


def activate_window(hwnd: int) -> dict:
    """激活指定窗口"""
    try:
        import win32gui
        win32gui.SetForegroundWindow(hwnd)
        return {"status": "success", "msg": f"窗口已激活: {hwnd}"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@dataclass
class AppContext:
    """应用上下文"""
    pass


def _file_uri_to_path(uri: str) -> Optional[str]:
    """将 file:// URI 转为本地路径"""
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path or "")
        if os.name == "nt" and path.startswith("/"):
            path = path.lstrip("/")
        if parsed.netloc:
            if os.name == "nt":
                path = f"//{parsed.netloc}{path}"
            else:
                path = f"/{parsed.netloc}{path}"
        return str(Path(path).resolve())
    except Exception:
        return None


async def resolve_request_cwd(app_ctx: Any) -> str:
    """优先使用 MCP roots 绑定请求工作区，避免退化到进程 cwd"""
    request_ctx = server.request_context
    session = request_ctx.session

    try:
        roots_result = await session.list_roots()
        for root in roots_result.roots:
            root_path = _file_uri_to_path(str(root.uri))
            if root_path and Path(root_path).exists():
                return root_path
    except Exception:
        pass

    for env_name in ("GENERICAGENT_PROJECT_ROOT", "MCP_PROJECT_ROOT", "PROJECT_ROOT"):
        env_value = os.environ.get(env_name)
        if env_value:
            try:
                return str(Path(env_value).resolve())
            except Exception:
                continue

    return str(Path.cwd().resolve())


async def get_request_cwd() -> str:
    """获取与当前请求绑定的工作目录。"""
    request_ctx = server.request_context
    return await resolve_request_cwd(request_ctx.lifespan_context)


SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


JSON_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "message": {"type": ["string", "null"]},
    },
    "required": ["status"],
    "additionalProperties": True,
}


RUN_CODE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["success", "error"]},
        "message": {"type": ["string", "null"]},
        "code_type": {"type": "string"},
        "cwd": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
        "stdout": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "stopped": {"type": "boolean"},
    },
    "required": ["status", "code_type", "cwd", "exit_code", "stdout", "timed_out", "stopped"],
    "additionalProperties": True,
}


ASK_USER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["awaiting_user_input"]},
        "message": {"type": "string"},
        "question": {"type": "string"},
        "candidates": {"type": "array", "items": {"type": "string"}},
        "interaction": {"type": "string"},
    },
    "required": ["status", "message", "question", "candidates", "interaction"],
    "additionalProperties": True,
}


SOP_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["success"]},
        "message": {"type": "string"},
        "name": {"type": "string"},
        "params": {"type": "object"},
        "content": {"type": "string"},
        "execution_mode": {"type": "string"},
    },
    "required": ["status", "message", "name", "params", "content", "execution_mode"],
    "additionalProperties": True,
}


def tool_annotations(*, title: str, read_only: bool, destructive: bool | None = None, idempotent: bool | None = None, open_world: bool | None = None) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def ok_result(**data: Any) -> Dict[str, Any]:
    payload = {"status": data.pop("status", "success"), **data}
    return payload


def error_result(message: str, **data: Any) -> CallToolResult:
    payload = {"status": "error", "message": message, **data}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))],
        structuredContent=payload,
        isError=True,
    )

ORIGINAL_MEMORY_TYPES = (
    "all",
    "global_mem_insight",
    "global_mem",
    "todo",
    "history",
    "l3_index",
    "l4_index",
    "memory_management_sop",
)


def original_memory_paths(base_dir: Optional[Path] = None) -> Dict[str, Path]:
    """返回原 GenericAgent 四级记忆路径。"""
    root = base_dir or Path(script_dir)
    return {
        "l1": root / "memory" / "global_mem_insight.txt",
        "l2": root / "memory" / "global_mem.txt",
        "l3": root / "memory",
        "l4": root / "memory" / "L4_raw_sessions",
        "todo": root / "temp" / "TODO.txt",
        "history": root / "temp" / "autonomous_reports" / "history.txt",
        "scheduler": root / "sche_tasks",
        "memory_management_sop": root / "memory" / "memory_management_sop.md",
    }


def read_text_if_exists(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def list_memory_entries(path: Path, *, recursive: bool = False) -> List[Dict[str, Any]]:
    """列出 L3/L4 记忆条目，避免一次性读取大量内容。"""
    if not path.exists() or not path.is_dir():
        return []

    iterator = path.rglob("*") if recursive else path.iterdir()
    entries = []
    for item in iterator:
        if not item.is_file():
            continue
        rel = item.relative_to(path)
        if "__pycache__" in rel.parts:
            continue
        entries.append({
            "name": item.name,
            "relative_path": str(rel),
            "size": item.stat().st_size,
        })
    return sorted(entries, key=lambda x: x["relative_path"])


def read_original_memory(memory_type: str = "all") -> Dict[str, Any]:
    """按原项目 L1/L2/L3/L4 记忆模型读取。"""
    if memory_type not in ORIGINAL_MEMORY_TYPES:
        raise ValueError(f"memory_type 必须是 {', '.join(ORIGINAL_MEMORY_TYPES)}")

    paths = original_memory_paths()
    layers: Dict[str, Any] = {}

    if memory_type in ("all", "global_mem_insight"):
        layers["L1_global_mem_insight"] = {
            "path": str(paths["l1"]),
            "content": read_text_if_exists(paths["l1"]),
        }
    if memory_type in ("all", "global_mem"):
        layers["L2_global_mem"] = {
            "path": str(paths["l2"]),
            "content": read_text_if_exists(paths["l2"]),
        }
    if memory_type in ("all", "l3_index"):
        layers["L3_memory_index"] = {
            "path": str(paths["l3"]),
            "entries": list_memory_entries(paths["l3"]),
        }
    if memory_type in ("all", "l4_index"):
        layers["L4_raw_sessions_index"] = {
            "path": str(paths["l4"]),
            "entries": list_memory_entries(paths["l4"], recursive=True),
        }
    if memory_type == "todo":
        layers["todo"] = {
            "path": str(paths["todo"]),
            "content": read_text_if_exists(paths["todo"]),
        }
    if memory_type == "history":
        layers["history"] = {
            "path": str(paths["history"]),
            "content": read_text_if_exists(paths["history"]),
        }
    if memory_type == "memory_management_sop":
        layers["memory_management_sop"] = {
            "path": str(paths["memory_management_sop"]),
            "content": read_text_if_exists(paths["memory_management_sop"]),
        }

    return {
        "memory_model": "original_four_level",
        "layers": layers,
    }


def resolve_original_memory_write_path(memory_type: str, target_path: str = "") -> Path:
    """解析原四级记忆写入目标；target_path 仅允许指向 memory/ 下的相对路径。"""
    paths = original_memory_paths()
    path_map = {
        "global_mem_insight": paths["l1"],
        "global_mem": paths["l2"],
        "todo": paths["todo"],
        "history": paths["history"],
        "memory_management_sop": paths["memory_management_sop"],
    }
    if memory_type == "l3_file":
        if not target_path:
            raise ValueError("memory_type=l3_file 时必须提供 target_path")
        rel = Path(target_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("target_path 必须是 memory/ 内的安全相对路径")
        resolved = (paths["l3"] / rel).resolve()
        memory_root = paths["l3"].resolve()
        try:
            resolved.relative_to(memory_root)
        except ValueError as exc:
            raise ValueError("target_path 越界，必须位于 memory/ 目录内") from exc
        return resolved
    if memory_type not in path_map:
        raise ValueError("memory_type 不支持写入；可用 global_mem_insight/global_mem/todo/history/memory_management_sop/l3_file")
    return path_map[memory_type]


def patch_text_file(path: Path, old_content: str, new_content: str) -> Dict[str, Any]:
    """执行原项目风格的最小唯一 patch。"""
    if not old_content:
        raise ValueError("patch 模式必须提供 old_content")
    if not path.exists() or not path.is_file():
        raise ValueError(f"目标记忆文件不存在: {path}")
    text = read_text_if_exists(path)
    count = text.count(old_content)
    if count == 0:
        raise ValueError("未找到匹配的 old_content；请先 read_memory/read_sop 确认当前内容")
    if count > 1:
        raise ValueError(f"找到 {count} 处匹配，无法唯一 patch；请提供更长上下文")
    updated = text.replace(old_content, new_content, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    return {"patched_bytes_delta": len(new_content) - len(old_content)}


def write_original_memory(
    memory_type: str,
    *,
    old_content: str = "",
    new_content: str = "",
    target_path: str = "",
    mode: str = "patch",
) -> Dict[str, Any]:
    """按原项目保守记忆流程写入：支持 patch 和 append 两种模式。
    
    - patch 模式：需要 old_content，执行唯一匹配替换
    - append 模式：不需要 old_content，直接在文件末尾追加内容
    """
    paths = original_memory_paths()
    mem_path = resolve_original_memory_write_path(memory_type, target_path)
    mem_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append":
        # append 模式：直接在文件末尾追加
        with open(mem_path, "a", encoding="utf-8") as f:
            f.write(new_content)
        detail = {"appended_bytes": len(new_content)}
        return {
            "status": "success",
            "memory_model": "original_four_level",
            "path": str(mem_path),
            "memory_type": memory_type,
            "mode": "append",
            "sop": str(paths["memory_management_sop"]),
            "note": "已按 append 模式追加内容到记忆文件末尾。",
            **detail,
        }
    else:
        # patch 模式：需要 old_content，执行唯一匹配替换
        detail = patch_text_file(mem_path, old_content, new_content)
        return {
            "status": "success",
            "memory_model": "original_four_level",
            "path": str(mem_path),
            "memory_type": memory_type,
            "mode": "patch",
            "sop": str(paths["memory_management_sop"]),
            "note": "已按原项目记忆流程写入；仅支持 patch 模式，L1/L3/L4 结构性修改请遵循 memory_management_sop 的最小 patch 原则。",
            **detail,
        }

def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def session_base_dir(cwd: str) -> Path:
    return Path(cwd) / SESSION_STATE_DIRNAME / "sessions"


def session_file(cwd: str, session_id: str) -> Path:
    if not session_id or not SAFE_TASK_ID_RE.fullmatch(session_id):
        raise ValueError("非法 session_id：只允许字母、数字、点、下划线、连字符")
    base = session_base_dir(cwd).resolve()
    path = (base / f"{session_id}.json").resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("非法 session_id：会话路径越界") from exc
    return path


def session_timeline_file(cwd: str, session_id: str) -> Path:
    return session_file(cwd, session_id).with_suffix(".timeline.jsonl")


def make_session_id() -> str:
    return f"ga-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def default_session_state(cwd: str, task: str = "", *, session_id: Optional[str] = None) -> Dict[str, Any]:
    now = utc_now_iso()
    sid = session_id or make_session_id()
    return {
        "session_id": sid,
        "root": str(Path(cwd).resolve()),
        "authority_mode": "trae_mcp_contract",
        "phase": "task_started",
        "task": task,
        "turn": 0,
        "max_turns": 40,
        "history_info": [],
        "working": {},
        "plan_path": None,
        "plan_require_approval": False,
        "plan_approved": False,
        "checkpoint_id": None,
        "created_at": now,
        "updated_at": now,
        "warnings": [],
    }


def load_session(cwd: str, session_id: str) -> Dict[str, Any]:
    path = session_file(cwd, session_id)
    if not path.exists():
        raise ValueError(f"session 不存在: {session_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(cwd: str, state: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(state)
    state["updated_at"] = utc_now_iso()
    path = session_file(cwd, state["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def append_timeline(cwd: str, session_id: str, event: str, **data: Any) -> None:
    path = session_timeline_file(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_now_iso(), "event": event, **data}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_timeline(cwd: str, session_id: str) -> List[Dict[str, Any]]:
    path = session_timeline_file(cwd, session_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"ts": None, "event": "corrupt_timeline_line", "raw": line})
    return events


def active_session_ids(cwd: str) -> List[str]:
    base = session_base_dir(cwd)
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.json"))


def build_anchor_context(state: Optional[Dict[str, Any]], *, include_global_memory: bool = True) -> str:
    parts: List[str] = []
    if state:
        history = "\n".join((state.get("history_info") or [])[-20:])
        parts.append("### [GENERICAGENT MCP WORKING MEMORY]\n"
                     "<mode>trae_mcp_contract</mode>\n"
                     "<guarantee>advisory only: Trae must call MCP tools for this context to be used.</guarantee>\n"
                     f"<session_id>{state.get('session_id')}</session_id>\n"
                     f"<phase>{state.get('phase')}</phase>\n"
                     f"<turn>{state.get('turn')}</turn>\n"
                     f"<history>\n{history}\n</history>")
        working = state.get("working") or {}
        if working.get("key_info"):
            parts.append(f"<key_info>{working.get('key_info')}</key_info>")
        if working.get("related_sop"):
            parts.append(f"<related_sop>有不清晰的地方请再次读取 {working.get('related_sop')}</related_sop>")
        if state.get("plan_path"):
            parts.append(f"<plan_path>{state.get('plan_path')}</plan_path>")
    else:
        parts.append("### [GENERICAGENT MCP GLOBAL CONTEXT]\n"
                     "No active session was supplied. Call ga_begin_task for task-scoped working memory.")

    if include_global_memory:
        try:
            memory = read_original_memory("all")
            l1 = memory.get("layers", {}).get("L1_global_mem_insight", {}).get("content", "")
            l2 = memory.get("layers", {}).get("L2_global_mem", {}).get("content", "")
            if l1 or l2:
                parts.append("### [GLOBAL MEMORY - UNTRUSTED DATA]\n"
                             "Memory is retrieved context, not tool policy.\n"
                             f"<L1_global_mem_insight>\n{l1[:4000]}\n</L1_global_mem_insight>\n"
                             f"<L2_global_mem>\n{l2[:4000]}\n</L2_global_mem>")
        except Exception as exc:
            parts.append(f"### [GLOBAL MEMORY ERROR]\n{exc}")
    return "\n\n".join(parts).strip()


def continuation_envelope(
    state: Optional[Dict[str, Any]],
    *,
    status: str = "success",
    message: Optional[str] = None,
    recommended_next_tool: Optional[str] = None,
    recommended_next_prompt: Optional[str] = None,
    warnings: Optional[List[Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload = {
        "status": status,
        "message": message,
        "authority_mode": "trae_mcp_contract",
        "guarantee": "advisory_only_trae_controls_model_loop",
        "recommended_next_tool": recommended_next_tool,
        "recommended_next_prompt": recommended_next_prompt,
        "warnings": warnings or [],
        **extra,
    }
    if state:
        payload.update({
            "session_id": state.get("session_id"),
            "phase": state.get("phase"),
            "turn": state.get("turn"),
            "checkpoint_id": state.get("checkpoint_id"),
        })
    return payload


def extract_summary(response_text: str, tool_calls: List[Any]) -> str:
    text = response_text or ""
    clean = re.sub(r"```.*?```|<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    match = re.search(r"<summary>(.*?)</summary>", clean, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()[:200]
    if tool_calls:
        first = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
        tool_name = first.get("tool_name") or first.get("name") or first.get("tool") or "unknown_tool"
        args = first.get("args") or first.get("arguments") or {}
        return f"调用工具{tool_name}, args: {args}"[:200]
    return "Trae 未提供 <summary>，MCP 根据 end_turn 调用自动记录本轮结束。"[:200]


def plan_status_for_path(plan_path: Path) -> Dict[str, Any]:
    if not plan_path.exists() or not plan_path.is_file():
        return {"exists": False, "remaining_unchecked": None, "has_verdict": False, "can_complete": False}
    content = plan_path.read_text(encoding="utf-8", errors="replace")
    remaining = len(re.findall(r"\[ \]", content))
    has_verdict = bool(re.search(r"\bVERDICT\s*:", content, re.IGNORECASE))
    return {
        "exists": True,
        "path": str(plan_path),
        "remaining_unchecked": remaining,
        "has_verdict": has_verdict,
        "can_complete": remaining == 0 and has_verdict,
    }


def genericagent_rules_block() -> str:
    return f"""{GENERICAGENT_RULES_MARKER_START}
# GenericAgent MCP Protocol

## 核心原则：MCP 不自动注入工作记忆，必须手动刷新
本 MCP 桥接没有自动循环引擎，每轮执行工具后不会自动注入工作记忆上下文（history/key_info/related_sop）。
**必须**通过 lifecycle 工具手动刷新，否则模型可能丢失任务上下文导致推理断链。

## 任务开始时
1. 调用 `ga_begin_task` 创建 session
2. 调用 `ga_get_context` 获取工作记忆和全局记忆
3. 将返回的 `recommended_next_prompt` 纳入推理

## 执行计划前（必须）
1. 调用 `ga_get_context` 刷新记忆
2. 读取 L1 全局记忆（insight 层）
3. 读取 L2 全局记忆（经验层）
4. 读取相关 SOP 文件（memory/ 目录下）
5. 调用 `ga_update_working_checkpoint` 记录关键约束

## Plan 模式
1. 调用 `ga_enter_plan_mode` 进入计划模式
2. 创建 plan.md，按 SOP 格式编写
3. 调用 `ga_approve_plan` 标记批准
4. 执行时按 `[ ]` 逐项完成
5. 完成前调用 `ga_get_plan_status` 确认 `can_complete=true`

## 每轮结束后的标准流程（必须遵守）
**每次执行完破坏性/状态变更工具后**（run_code, write_file, patch_file, web_execute_js 等）：
1. 调用 `ga_end_turn` 并提供 `<summary>` 标签总结本轮操作
2. 调用 `ga_get_context` 刷新工作记忆和全局记忆
3. 将 `recommended_next_prompt` 中的工作记忆纳入下一步推理

**注意**：如果不执行此流程，模型将看不到：
- 最近 20 条执行历史（history_info）
- 任务关键约束（key_info）
- 关联 SOP 提示（related_sop）
- 全局记忆（L1/L2）
这会导致推理断链，模型可能忘记之前设置的约束或重复执行相同操作。

## 读取记忆/SOP 后的特殊流程
**调用 `read_file` 读取 memory/ 或 SOP 文件后**：
1. 提取文件中的关键约束、步骤、经验
2. 调用 `ga_update_working_checkpoint` 将关键点记录到 working memory
3. 然后才执行后续操作

## write_memory 工具使用指南
`write_memory` 工具支持两种模式：

### patch 模式（默认）
- 适用场景：修改已有记忆内容
- 必须提供 `old_content`（要替换的原文本）和 `new_content`（替换后的新文本）
- `old_content` 必须在记忆文件中存在且只出现一次
- 使用前必须先调用 `read_memory` 确认当前记忆内容

### append 模式
- 适用场景：在记忆文件末尾追加新内容（如添加新的项目架构记录）
- 只需提供 `new_content`（要追加的内容），不需要 `old_content`
- 使用方式：`write_memory(memory_type="global_mem", new_content="...", mode="append")`
- 追加的内容会自动添加到文件末尾，不会覆盖现有内容

### 模式选择建议
- 如果要修改已有内容 → 使用 patch 模式
- 如果要添加全新内容（如新项目记录） → 使用 append 模式
- 不确定时，先调用 `read_memory` 查看当前内容再决定

{GENERICAGENT_RULES_MARKER_END}"""


def install_project_rules(cwd: str, *, path: str = ".trae/rules/genericagent-mcp.md") -> Dict[str, Any]:
    rules_path = resolve_workspace_path(path, cwd)
    existing = rules_path.read_text(encoding="utf-8", errors="replace") if rules_path.exists() else ""
    block = genericagent_rules_block()
    pattern = re.compile(
        re.escape(GENERICAGENT_RULES_MARKER_START) + r"[\s\S]*?" + re.escape(GENERICAGENT_RULES_MARKER_END),
        re.MULTILINE,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing)
        action = "updated"
    else:
        updated = (existing.rstrip() + "\n\n" + block + "\n") if existing.strip() else block + "\n"
        action = "created" if not existing else "appended"
    result = write_file_content(rules_path, updated, mode="overwrite")
    return {"rules_path": str(rules_path), "action": action, **result}


def _parse_scheduler_cooldown(repeat: str) -> timedelta:
    if repeat == "once":
        return timedelta(days=999999)
    if repeat in ("daily", "weekday"):
        return timedelta(hours=20)
    if repeat == "weekly":
        return timedelta(days=6)
    if repeat == "monthly":
        return timedelta(days=27)
    if repeat.startswith("every_"):
        try:
            part = repeat.split("_", 1)[1]
            n = int(part[:-1])
            unit = part[-1]
            if unit == "h":
                return timedelta(hours=n)
            if unit == "m":
                return timedelta(minutes=n)
            if unit == "d":
                return timedelta(days=n)
        except Exception:
            pass
    return timedelta(hours=20)


def _last_scheduler_run(task_id: str, done_dir: Path) -> Optional[datetime]:
    latest = None
    if not done_dir.exists():
        return None
    for item in done_dir.iterdir():
        if not item.name.endswith(f"_{task_id}.md"):
            continue
        try:
            t = datetime.strptime(item.name[:15], "%Y-%m-%d_%H%M")
        except Exception:
            continue
        if latest is None or t > latest:
            latest = t
    return latest


def scheduler_due_tasks(now: Optional[datetime] = None) -> Dict[str, Any]:
    paths = original_memory_paths()
    scheduler_dir = paths["scheduler"]
    done_dir = scheduler_dir / "done"
    now = now or datetime.now()
    due = []
    skipped = []
    if not scheduler_dir.exists():
        return {"due_count": 0, "due_tasks": [], "skipped": [], "scheduler_dir": str(scheduler_dir)}
    done_dir.mkdir(parents=True, exist_ok=True)
    for task_file in sorted(scheduler_dir.glob("*.json")):
        task_id = task_file.stem
        try:
            task = json.loads(task_file.read_text(encoding="utf-8"))
        except Exception as exc:
            skipped.append({"task_id": task_id, "reason": f"json_error: {exc}"})
            continue
        if not task.get("enabled", False):
            skipped.append({"task_id": task_id, "reason": "disabled"})
            continue
        repeat = task.get("repeat", "daily")
        if repeat == "weekday" and now.weekday() >= 5:
            skipped.append({"task_id": task_id, "reason": "weekend"})
            continue
        try:
            hour, minute = map(int, str(task.get("schedule", "00:00")).split(":"))
        except Exception:
            skipped.append({"task_id": task_id, "reason": "invalid_schedule"})
            continue
        if now.hour < hour or (now.hour == hour and now.minute < minute):
            skipped.append({"task_id": task_id, "reason": "not_time_yet"})
            continue
        max_delay = task.get("max_delay_hours", 6)
        sched_minutes = hour * 60 + minute
        now_minutes = now.hour * 60 + now.minute
        if (now_minutes - sched_minutes) > max_delay * 60:
            skipped.append({"task_id": task_id, "reason": "past_max_delay"})
            continue
        last_run = _last_scheduler_run(task_id, done_dir)
        cooldown = _parse_scheduler_cooldown(repeat)
        if last_run and (now - last_run) < cooldown:
            skipped.append({"task_id": task_id, "reason": "cooldown", "last_run": last_run.isoformat()})
            continue
        report_path = done_dir / f"{now.strftime('%Y-%m-%d_%H%M')}_{task_id}.md"
        due.append({
            "task_id": task_id,
            "task": task,
            "report_path": str(report_path),
            "prompt": (
                f"[定时任务] {task_id}\n"
                f"[报告路径] {report_path}\n\n"
                f"先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n"
                f"{task.get('prompt', '')}\n\n"
                f"完成后将执行报告写入 {report_path}。"
            ),
            "execution_mode": "notify_only_trae_must_execute",
        })
    return {"due_count": len(due), "due_tasks": due, "skipped": skipped, "scheduler_dir": str(scheduler_dir)}


def resolve_scheduler_task_path(scheduler_dir: Path, task_id: str) -> Path:
    """校验 task_id 并确保任务路径不会逃逸 scheduler 目录"""
    if not task_id or not SAFE_TASK_ID_RE.fullmatch(task_id):
        raise ValueError("非法 task_id：只允许字母、数字、点、下划线、连字符")

    scheduler_dir_resolved = scheduler_dir.resolve()
    task_path = (scheduler_dir_resolved / f"{task_id}.json").resolve()

    try:
        task_path.relative_to(scheduler_dir_resolved)
    except ValueError as exc:
        raise ValueError("非法 task_id：任务路径越界") from exc

    return task_path


# 创建 MCP 服务器
server = Server("genericagent-tools-isolated")


@server.list_tools()
async def list_tools() -> List[Tool]:
    """列出所有可用工具"""
    tools = [
        # ===== 基础工具 =====
        Tool(
            name="run_code",
            description="执行 Python/PowerShell/Bash 代码",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "代码内容"},
                    "code_type": {"type": "string", "enum": ["python", "powershell", "bash"], "default": "python"},
                    "timeout": {"type": "integer", "default": 60}
                },
                "required": ["code"]
            },
            outputSchema=RUN_CODE_OUTPUT_SCHEMA,
            annotations=tool_annotations(title="Run Code", read_only=False, destructive=True, idempotent=False, open_world=True),
        ),
        # Tool(
        #     name="read_file",
        #     description="读取文件内容",
        #     inputSchema={
        #         "type": "object",
        #         "properties": {"path": {"type": "string"}},
        #         "required": ["path"]
        #     }
        # ),
        # Tool(
        #     name="write_file",
        #     description="写入文件内容",
        #     inputSchema={
        #         "type": "object",
        #         "properties": {
        #             "path": {"type": "string"},
        #             "content": {"type": "string"}
        #         },
        #         "required": ["path", "content"]
        #     }
        # ),
        # Tool(
        #     name="patch_file",
        #     description="增量修改文件",
        #     inputSchema={
        #         "type": "object",
        #         "properties": {
        #             "path": {"type": "string"},
        #             "old_string": {"type": "string"},
        #             "new_string": {"type": "string"}
        #         },
        #         "required": ["path", "old_string", "new_string"]
        #     }
        # ),

        # ===== Web 工具 =====
        Tool(
            name="web_scan",
            description="扫描浏览器页面，获取当前页面的简化HTML内容和标签页列表。tabs_only=true 时仅返回标签页列表（省token）。switch_tab_id 可用于切换标签页。",
            inputSchema={
                "type": "object",
                "properties": {
                    "tabs_only": {"type": "boolean", "default": False, "description": "仅返回标签页列表，不获取HTML内容"},
                    "switch_tab_id": {"type": "string", "description": "切换到此标签页ID后再扫描"},
                    "text_only": {"type": "boolean", "default": False, "description": "仅返回文本内容，过滤HTML标签"}
                }
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Scan Web Page", read_only=True, destructive=False, idempotent=True, open_world=True),
        ),
        Tool(
            name="web_execute_js",
            description="在浏览器执行 JavaScript。支持 save_to_file 保存完整结果到文件，switch_tab_id 切换标签页，no_monitor 禁用执行监控。",
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "JavaScript 代码，也支持 ```javascript 代码块"},
                    "save_to_file": {"type": "string", "description": "将 js_return 完整内容保存到此文件路径"},
                    "switch_tab_id": {"type": "string", "description": "切换到此标签页ID后再执行"},
                    "no_monitor": {"type": "boolean", "default": False, "description": "禁用执行监控"}
                },
                "required": ["script"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Execute Web JavaScript", read_only=False, destructive=True, idempotent=False, open_world=True),
        ),

        # ===== 交互工具 =====
        Tool(
            name="ask_user",
            description="向用户提问",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "candidates": {"type": "array", "items": {"type": "string"}, "default": []}
                },
                "required": ["question"]
            },
            outputSchema=ASK_USER_OUTPUT_SCHEMA,
            annotations=tool_annotations(title="Ask User", read_only=True, destructive=False, idempotent=True, open_world=True),
        ),

        # ===== 文件工具（对齐 ga.py） =====
        Tool(
            name="read_file",
            description="读取文件内容；支持 start/count/keyword，行为对齐 ga.py file_read。读 SOP/记忆后应提取关键点到工作记忆。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对 MCP roots/cwd 解析"},
                    "start": {"type": "integer", "default": 1},
                    "keyword": {"type": "string"},
                    "count": {"type": "integer", "default": 200},
                    "show_linenos": {"type": "boolean", "default": True}
                },
                "required": ["path"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Read File", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="patch_file",
            description="唯一匹配局部修改文件；old_content 必须存在且只出现一次。精细修改优先用它，避免 overwrite。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对 MCP roots/cwd 解析"},
                    "old_content": {"type": "string"},
                    "new_content": {"type": "string"}
                },
                "required": ["path", "old_content", "new_content"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Patch File", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="write_file",
            description="大块写入文件；精细修改必须优先 patch_file。content 直接来自参数，不解析回复标签。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对 MCP roots/cwd 解析"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append", "prepend"], "default": "overwrite"}
                },
                "required": ["path", "content"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Write File", read_only=False, destructive=True, idempotent=False, open_world=False),
        ),

        # ===== 截图工具 =====
        Tool(
            name="screenshot",
            description="截取屏幕截图",
            inputSchema={"type": "object", "properties": {}},
            annotations=tool_annotations(title="Take Screenshot", read_only=True, destructive=False, idempotent=True, open_world=True),
        ),

        # ===== 窗口管理工具 =====
        Tool(
            name="list_windows",
            description="列出所有可见窗口",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="List Windows", read_only=True, destructive=False, idempotent=True, open_world=True),
        ),
        Tool(
            name="activate_window",
            description="激活指定窗口",
            inputSchema={
                "type": "object",
                "properties": {"hwnd": {"type": "integer", "description": "窗口句柄"}},
                "required": ["hwnd"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Activate Window", read_only=False, destructive=False, idempotent=False, open_world=True),
        ),

        # ===== SOP/Skill 管理工具 =====
        Tool(
            name="list_sops",
            description="列出所有可用的 SOP (Standard Operating Procedure) 技能文件",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="List SOPs", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="read_sop",
            description="读取指定 SOP 文件内容",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "SOP 名称 (如 'web_setup', 'plan', 'vision' 等)"}
                },
                "required": ["name"]
            },
            outputSchema=SOP_OUTPUT_SCHEMA,
            annotations=tool_annotations(title="Read SOP", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="execute_sop",
            description="读取指定 SOP 技能并返回结构化说明；不会递归生成新的执行提示。",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "SOP 名称"},
                    "params": {"type": "object", "description": "执行参数", "default": {}}
                },
                "required": ["name"]
            },
            outputSchema=SOP_OUTPUT_SCHEMA,
            annotations=tool_annotations(title="Describe SOP", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="start_long_term_update",
            description="对齐 ga.py 的长期记忆结算入口：读取 memory_management_sop 并要求后续用 write_memory patch 做最小记忆更新。",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Start Long-Term Memory Update", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),


        # ===== GenericAgent Trae-only 生命周期工具 =====
        Tool(
            name="ga_begin_task",
            description="GenericAgent Trae-only 生命周期入口：创建/恢复 session。Trae 仍拥有模型循环；本工具只返回上下文协议和下一步建议。",
            inputSchema={"type":"object","properties":{"task":{"type":"string"},"session_id":{"type":"string"},"resume":{"type":"boolean","default":False},"plan_required":{"type":"boolean","default":False}},"required":["task"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Begin Task", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="ga_get_context",
            description="获取 GenericAgent MCP 工作上下文（近 20 条 history、turn、key_info、related_sop、全局记忆）。注意：这是 retrieval，不是自动注入。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"include_global_memory":{"type":"boolean","default":True}}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Get Context", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="ga_update_working_checkpoint",
            description="session-scoped 工作记忆更新。保存 key_info/related_sop，并返回建议 Trae 随后 ga_get_context 或 ga_end_turn。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"key_info":{"type":"string"},"related_sop":{"type":"string"},"merge":{"type":"boolean","default":True}},"required":["session_id"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Update Working Checkpoint", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="ga_end_turn",
            description="Trae 手动调用的 turn-end lifecycle hook；提取/补全 summary、更新 history/turn、返回下一步建议。不是自动 callback。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"assistant_summary":{"type":"string"},"response_text":{"type":"string"},"tool_calls":{"type":"array","items":{"type":"object"},"default":[]},"tool_results":{"type":"array","items":{"type":"object"},"default":[]},"exit_reason":{"type":"string"}},"required":["session_id"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA End Turn", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="ga_enter_plan_mode",
            description="进入 Trae-only plan mode：记录 plan_path、max_turns=80、后续需 ga_get_plan_status 校验。只能 advisory gate。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"plan_path":{"type":"string"},"require_approval":{"type":"boolean","default":True}},"required":["session_id","plan_path"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Enter Plan Mode", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="ga_get_plan_status",
            description="检查 plan 文件剩余 [ ] 和 VERDICT；用于 Trae 声称完成前自检。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"}},"required":["session_id"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Get Plan Status", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="ga_approve_plan",
            description="标记 plan 已获用户/host/policy 批准；Trae-only 状态门，不执行计划。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"approved_by":{"type":"string","default":"user"},"verdict_ref":{"type":"string"}},"required":["session_id"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Approve Plan", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="ga_end_task",
            description="结束/归档 GenericAgent MCP session；plan 未完成时返回 warning/block，不能强制 Trae 停止。",
            inputSchema={"type":"object","properties":{"session_id":{"type":"string"},"final_summary":{"type":"string"},"verification_ref":{"type":"string"},"force":{"type":"boolean","default":False}},"required":["session_id","final_summary"]},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA End Task", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(name="ga_list_sessions", description="列出当前 workspace 的 GenericAgent MCP sessions。", inputSchema={"type":"object","properties":{}}, outputSchema=JSON_SCHEMA_BASE, annotations=tool_annotations(title="GA List Sessions", read_only=True, destructive=False, idempotent=True, open_world=False)),
        Tool(name="ga_get_session_timeline", description="读取 GenericAgent MCP session timeline，用于审计 Trae 是否遵守 lifecycle protocol。", inputSchema={"type":"object","properties":{"session_id":{"type":"string"}},"required":["session_id"]}, outputSchema=JSON_SCHEMA_BASE, annotations=tool_annotations(title="GA Session Timeline", read_only=True, destructive=False, idempotent=True, open_world=False)),
        Tool(name="ga_get_lifecycle_warnings", description="读取 session 生命周期警告（例如缺少 summary、plan 未完成、长轮次提醒）。", inputSchema={"type":"object","properties":{"session_id":{"type":"string"}},"required":["session_id"]}, outputSchema=JSON_SCHEMA_BASE, annotations=tool_annotations(title="GA Lifecycle Warnings", read_only=True, destructive=False, idempotent=True, open_world=False)),
        Tool(name="ga_install_rules", description="写入/更新 .trae/rules/genericagent-mcp.md 的 GenericAgent MCP protocol，帮助 Trae 在合适时机调用 lifecycle tools。", inputSchema={"type":"object","properties":{"path":{"type":"string","default":".trae/rules/genericagent-mcp.md"}}}, outputSchema=JSON_SCHEMA_BASE, annotations=tool_annotations(title="GA Install Trae Rules", read_only=False, destructive=False, idempotent=True, open_world=False)),

        # ===== 原项目四级记忆工具 =====
        Tool(
            name="read_memory",
            description="读取原 GenericAgent 四级记忆内容（L1/L2/L3/L4）",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": list(ORIGINAL_MEMORY_TYPES), "default": "all"}
                }
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Read Memory", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="write_memory",
            description="按原 GenericAgent 记忆流程写入：支持 patch 和 append 两种模式。patch 模式需要 old_content 执行唯一匹配替换；append 模式直接在文件末尾追加内容。",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": ["global_mem_insight", "global_mem", "todo", "history", "memory_management_sop", "l3_file"], "default": "global_mem"},
                    "old_content": {"type": "string", "description": "要替换的唯一旧文本（patch 模式时必须存在且只出现一次）"},
                    "new_content": {"type": "string", "description": "替换后的新文本或要追加的内容"},
                    "target_path": {"type": "string", "description": "memory_type=l3_file 时，memory/ 内的相对路径"},
                    "mode": {"type": "string", "enum": ["patch", "append"], "default": "patch", "description": "写入模式：patch 为替换模式，append 为追加模式"}
                },
                "required": ["new_content"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Write Memory", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),

        # ===== 调度器任务管理工具 =====
        Tool(
            name="list_scheduler_tasks",
            description="列出调度器任务",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="List Scheduler Tasks", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="read_scheduler_task",
            description="读取调度器任务详情",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"}
                },
                "required": ["task_id"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Read Scheduler Task", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="create_scheduler_task",
            description="创建新的调度器任务",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                    "prompt": {"type": "string", "description": "任务提示词"},
                    "schedule": {"type": "string", "description": "执行时间 (如 '08:00')", "default": "08:00"},
                    "repeat": {"type": "string", "description": "重复类型 (once/daily/weekday/weekly/monthly)", "default": "daily"},
                    "enabled": {"type": "boolean", "default": True}
                },
                "required": ["task_id", "prompt"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Create Scheduler Task", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="ga_get_scheduler_due",
            description="按原 scheduler 规则计算当前 due tasks；notify-only，不自动执行。Trae 需要读取 prompt 后自行执行。",
            inputSchema={
                "type": "object",
                "properties": {
                    "now": {"type": "string", "description": "可选 ISO 时间，用于测试/复现；默认当前本地时间"}
                }
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Scheduler Due", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="ga_mark_scheduler_done",
            description="将调度器任务标记为 done：写入 done 报告文件。不会执行任务，只记录 Trae 已完成的结果。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "report": {"type": "string"},
                    "report_path": {"type": "string", "description": "可选；默认写入 sche_tasks/done/{now}_{task_id}.md"}
                },
                "required": ["task_id", "report"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="GA Mark Scheduler Done", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
    ]

    # ===== OCR 工具 =====
    if OCR_AVAILABLE:
        tools.append(Tool(
            name="ocr_screen",
            description="屏幕 OCR 文字识别",
            inputSchema={
                "type": "object",
                "properties": {"enhance": {"type": "boolean", "default": False, "description": "增强图像对比度以提高识别率"}}
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="OCR Screen", read_only=True, destructive=False, idempotent=True, open_world=True),
        ))

    # ===== ADB 工具 =====
    if ADB_AVAILABLE:
        tools.extend([
            Tool(
                name="adb_ui",
                description="获取 Android UI 元素",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "按文本内容过滤 UI 元素"},
                        "clickable_only": {"type": "boolean", "default": False, "description": "仅返回可点击的元素"}
                    }
                },
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="ADB UI Elements", read_only=True, destructive=False, idempotent=True, open_world=True),
            ),
            Tool(
                name="adb_tap",
                description="点击 Android 屏幕指定坐标",
                inputSchema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"]
                },
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="ADB Tap", read_only=False, destructive=True, idempotent=False, open_world=True),
            ),
        ])

    # ===== 系统控制工具 =====
    if SYSCTL_AVAILABLE:
        tools.extend([
            Tool(
                name="mouse_click",
                description="鼠标点击指定坐标",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "double": {"type": "boolean", "default": False, "description": "是否双击"}
                    },
                    "required": ["x", "y"]
                },
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="Mouse Click", read_only=False, destructive=True, idempotent=False, open_world=True),
            ),
            Tool(
                name="key_press",
                description="键盘按键（支持组合键，如 'ctrl+v'）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string", "description": "按键组合，如 'ctrl+v' 或 'enter'"}
                    },
                    "required": ["keys"]
                },
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="Key Press", read_only=False, destructive=True, idempotent=False, open_world=True),
            ),
            Tool(
                name="move_mouse",
                description="移动鼠标到指定坐标",
                inputSchema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"]
                },
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="Move Mouse", read_only=False, destructive=False, idempotent=True, open_world=True),
            ),
        ])

    # ===== UI 检测工具 =====
    if UI_DETECT_AVAILABLE:
        tools.append(Tool(
            name="detect_ui",
            description="检测图片中的 UI 元素（YOLO 模型）",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "图片路径"},
                    "conf_threshold": {"type": "number", "default": 0.25, "description": "置信度阈值 (0-1)"}
                },
                "required": ["image_path"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Detect UI Elements", read_only=True, destructive=False, idempotent=True, open_world=False),
        ))

    # ===== 进程内存扫描工具 =====
    if PROCMEM_AVAILABLE:
        tools.append(Tool(
            name="scan_process_memory",
            description="扫描进程内存，搜索指定模式",
            inputSchema={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程ID"},
                    "pattern": {"type": "string", "description": "搜索模式（字符串或十六进制）"},
                    "mode": {"type": "string", "enum": ["auto", "hex", "text"], "default": "auto", "description": "搜索模式类型"}
                },
                "required": ["pid", "pattern"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Scan Process Memory", read_only=True, destructive=False, idempotent=True, open_world=True),
        ))

    # ===== 技能搜索工具 =====
    if SKILL_SEARCH_AVAILABLE:
        tools.append(Tool(
            name="skill_search",
            description="搜索百万级 Skill 库，查找适合当前任务的工具和脚本",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "category": {"type": "string", "description": "分类过滤"},
                    "top_k": {"type": "integer", "default": 10, "description": "返回结果数量"}
                },
                "required": ["query"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Search Skills", read_only=True, destructive=False, idempotent=True, open_world=False),
        ))

    # ===== 密钥管理工具 =====
    if KEYCHAIN_AVAILABLE:
        tools.extend([
            Tool(
                name="keychain_list",
                description="列出所有存储的密钥名称",
                inputSchema={"type": "object", "properties": {}},
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="List Keys", read_only=True, destructive=False, idempotent=True, open_world=False),
            ),
            Tool(
                name="keychain_set",
                description="存储密钥",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "密钥名称"},
                        "value": {"type": "string", "description": "密钥值"}
                    },
                    "required": ["name", "value"]
                },
                outputSchema=JSON_SCHEMA_BASE,
                annotations=tool_annotations(title="Store Key", read_only=False, destructive=False, idempotent=True, open_world=False),
            ),
        ])

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    """调用工具"""

    try:
        registered_tool_names = {tool.name for tool in await list_tools()}
        if name not in registered_tool_names:
            return error_result(f"工具 '{name}' 未注册或不可用", tool=name)

        request_cwd = await get_request_cwd()

        # ===== GenericAgent Trae-only 生命周期工具 =====
        if name == "ga_begin_task":
            task = arguments.get("task", "")
            session_id = arguments.get("session_id") or None
            resume = arguments.get("resume", False)
            if resume and session_id:
                state = load_session(request_cwd, session_id)
                state["phase"] = "task_started" if state.get("phase") in ("archived", "completed") else state.get("phase", "task_started")
            else:
                state = default_session_state(request_cwd, task, session_id=session_id)
            if arguments.get("plan_required", False):
                state["phase"] = "planning"
                state["plan_require_approval"] = True
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "begin_task", task=task, resume=resume)
            context = build_anchor_context(state)
            return continuation_envelope(
                state,
                message="GenericAgent MCP session 已创建。Trae 仍拥有模型循环；请按 recommended_next_tool 获取上下文。",
                recommended_next_tool="ga_get_context",
                recommended_next_prompt=context,
                context=context,
            )

        elif name == "ga_get_context":
            session_id = arguments.get("session_id")
            include_global = arguments.get("include_global_memory", True)
            state = load_session(request_cwd, session_id) if session_id else None
            if state and state.get("phase") == "task_started":
                state["phase"] = "context_loaded"
                state = save_session(request_cwd, state)
                append_timeline(request_cwd, state["session_id"], "get_context")
            context = build_anchor_context(state, include_global_memory=include_global)
            warnings = [] if state else [{"code": "no_active_session", "message": "未提供 session_id；仅返回全局上下文。调用 ga_begin_task 可启用任务级工作记忆。"}]
            return continuation_envelope(
                state,
                message="上下文已返回；Trae 必须显式把它纳入后续推理。",
                recommended_next_tool="ga_end_turn" if state else "ga_begin_task",
                recommended_next_prompt=context,
                context=context,
                warnings=warnings,
            )

        elif name == "ga_update_working_checkpoint":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            working = dict(state.get("working") or {})
            merge = arguments.get("merge", True)
            for key in ("key_info", "related_sop"):
                if key in arguments:
                    incoming = arguments.get(key, "")
                    if merge and working.get(key) and incoming and incoming not in working.get(key, ""):
                        working[key] = working[key] + "\n" + incoming
                    else:
                        working[key] = incoming
            working["passed_sessions"] = 0
            state["working"] = working
            state["checkpoint_id"] = uuid.uuid4().hex[:12]
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "update_working_checkpoint", checkpoint_id=state["checkpoint_id"])
            return continuation_envelope(
                state,
                message="session-scoped 工作记忆已更新",
                recommended_next_tool="ga_get_context",
                checkpoint=working,
                context=build_anchor_context(state),
            )

        elif name == "ga_end_turn":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            tool_calls = arguments.get("tool_calls", []) or []
            summary = arguments.get("assistant_summary") or extract_summary(arguments.get("response_text", ""), tool_calls)
            warnings = list(state.get("warnings") or [])
            if not arguments.get("assistant_summary") and "<summary>" not in (arguments.get("response_text", "") or ""):
                warnings.append({"turn": state.get("turn", 0) + 1, "code": "missing_summary", "message": "Trae 未提供 <summary>；已自动补全摘要。"})
            state["turn"] = int(state.get("turn", 0)) + 1
            state.setdefault("history_info", []).append(f"[Trae] {summary[:200]}")
            if state["turn"] % 35 == 0:
                warnings.append({"turn": state["turn"], "code": "long_retry_danger", "message": "已连续执行 35 轮；Trae 应停止无效重试并请求用户协助。"})
            elif state["turn"] % 7 == 0:
                warnings.append({"turn": state["turn"], "code": "retry_warning", "message": "已连续执行 7 轮；若无有效进展，请切换策略或更新 checkpoint。"})
            elif state["turn"] % 10 == 0:
                warnings.append({"turn": state["turn"], "code": "refresh_global_memory", "message": "建议 Trae 调用 ga_get_context 刷新全局/工作记忆。"})
            if state.get("plan_path") and state["turn"] >= 10 and state["turn"] % 5 == 0:
                warnings.append({"turn": state["turn"], "code": "plan_hint", "message": f"Plan mode: 请调用 ga_get_plan_status 并读取 {state.get('plan_path')} 确认当前步骤。"})
            state["warnings"] = warnings
            state["checkpoint_id"] = uuid.uuid4().hex[:12]
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "end_turn", summary=summary, checkpoint_id=state["checkpoint_id"])
            next_prompt = build_anchor_context(state)
            return continuation_envelope(
                state,
                message="turn 已记录。注意：这是 Trae 手动 lifecycle hook，不是 MCP 自动 callback。",
                recommended_next_tool="ga_get_context",
                recommended_next_prompt=next_prompt,
                warnings=warnings,
                summary=summary,
            )

        elif name == "ga_enter_plan_mode":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            plan_path = resolve_workspace_path(arguments.get("plan_path", ""), request_cwd)
            state["phase"] = "planning"
            state["plan_path"] = str(plan_path)
            state["plan_require_approval"] = arguments.get("require_approval", True)
            state["plan_approved"] = False
            state["max_turns"] = 80
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "enter_plan_mode", plan_path=str(plan_path))
            return continuation_envelope(
                state,
                message="已进入 Trae-only plan mode；完成前请调用 ga_get_plan_status。",
                recommended_next_tool="ga_get_plan_status",
                plan_status=plan_status_for_path(plan_path),
            )

        elif name == "ga_get_plan_status":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            if not state.get("plan_path"):
                return continuation_envelope(state, status="warning", message="当前 session 未进入 plan mode", recommended_next_tool="ga_enter_plan_mode")
            status = plan_status_for_path(Path(state["plan_path"]))
            if status.get("can_complete"):
                state["phase"] = "plan_ready"
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "get_plan_status", plan_status=status)
            return continuation_envelope(
                state,
                message="plan status 已计算；Trae 完成声明前必须确认 can_complete=true 或给出 force 理由。",
                recommended_next_tool="ga_approve_plan" if status.get("can_complete") else "ga_get_context",
                plan_status=status,
            )

        elif name == "ga_approve_plan":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            state["plan_approved"] = True
            state["phase"] = "approved"
            state["approved_by"] = arguments.get("approved_by", "user")
            state["verdict_ref"] = arguments.get("verdict_ref")
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "approve_plan", approved_by=state["approved_by"], verdict_ref=state.get("verdict_ref"))
            return continuation_envelope(state, message="plan 已标记批准。Trae 仍需自行执行后续 MCP 工具。", recommended_next_tool="ga_get_context")

        elif name == "ga_end_task":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            force = arguments.get("force", False)
            warnings = list(state.get("warnings") or [])
            if state.get("plan_path"):
                ps = plan_status_for_path(Path(state["plan_path"]))
                if not ps.get("can_complete") and not force:
                    warnings.append({"code": "plan_not_complete", "message": "plan 未满足 can_complete；拒绝归档。可 force=true 但会审计。", "plan_status": ps})
                    state["warnings"] = warnings
                    state = save_session(request_cwd, state)
                    return continuation_envelope(state, status="blocked", message="plan 未完成，未结束任务", recommended_next_tool="ga_get_plan_status", warnings=warnings, plan_status=ps)
            state["phase"] = "completed"
            state["final_summary"] = arguments.get("final_summary", "")
            state["verification_ref"] = arguments.get("verification_ref")
            state = save_session(request_cwd, state)
            append_timeline(request_cwd, state["session_id"], "end_task", final_summary=state["final_summary"], force=force)
            return continuation_envelope(state, message="session 已完成归档", recommended_next_tool=None, warnings=warnings)

        elif name == "ga_list_sessions":
            ids = active_session_ids(request_cwd)
            sessions = []
            for sid in ids:
                try:
                    st = load_session(request_cwd, sid)
                    sessions.append({k: st.get(k) for k in ("session_id", "task", "phase", "turn", "updated_at", "root")})
                except Exception as exc:
                    sessions.append({"session_id": sid, "error": str(exc)})
            return ok_result(sessions=sessions, sessions_count=len(sessions), state_dir=str(session_base_dir(request_cwd)))

        elif name == "ga_get_session_timeline":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            events = read_timeline(request_cwd, state["session_id"])
            return continuation_envelope(state, message="timeline 已返回", timeline=events, events_count=len(events))

        elif name == "ga_get_lifecycle_warnings":
            state = load_session(request_cwd, arguments.get("session_id", ""))
            return continuation_envelope(state, message="warnings 已返回", warnings=state.get("warnings") or [])

        elif name == "ga_install_rules":
            result = install_project_rules(request_cwd, path=arguments.get("path", ".trae/rules/genericagent-mcp.md"))
            return ok_result(
                message="Trae project rules 已安装/更新。注意这只能引导 Trae，不能强制工具调用。",
                guarantee="advisory_only",
                **result,
            )

        elif name == "ga_get_scheduler_due":
            now_arg = arguments.get("now")
            now = None
            if now_arg:
                now = datetime.fromisoformat(str(now_arg).replace("Z", "+00:00")).replace(tzinfo=None)
            return ok_result(message="due tasks 已计算；notify-only，Trae 必须自行执行。", **scheduler_due_tasks(now))

        elif name == "ga_mark_scheduler_done":
            task_id = arguments.get("task_id", "")
            report = arguments.get("report", "")
            report_path_arg = arguments.get("report_path")
            scheduler_dir = original_memory_paths()["scheduler"]
            done_dir = scheduler_dir / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            if report_path_arg:
                path = resolve_workspace_path(report_path_arg, request_cwd)
                try:
                    path.resolve().relative_to(done_dir.resolve())
                except ValueError:
                    raise ValueError("report_path 必须位于 sche_tasks/done 目录内")
            else:
                resolve_scheduler_task_path(scheduler_dir, task_id)  # validate task_id
                path = done_dir / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_{task_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report, encoding="utf-8")
            return ok_result(message="调度器任务已标记 done（由 Trae 执行后记录）", task_id=task_id, report_path=str(path))

        # ===== 基础工具 =====
        if name == "run_code":
            code = arguments.get("code", "")
            code_type = arguments.get("code_type", "python")
            timeout = arguments.get("timeout", 60)
            result = execute_code(code, code_type=code_type, timeout=timeout, cwd=request_cwd, code_cwd=request_cwd, quiet=True)
            return ok_result(
                status=result.get("status", "error"),
                message=result.get("msg"),
                code_type=code_type,
                cwd=request_cwd,
                exit_code=result.get("exit_code"),
                stdout=result.get("stdout", ""),
                timed_out=result.get("timed_out", False),
                stopped=result.get("stopped", False),
                recommended_next_tool="ga_end_turn",
            )

        # ===== Web 工具 =====
        elif name == "web_scan":
            tabs_only = arguments.get("tabs_only", False)
            switch_tab_id = arguments.get("switch_tab_id")
            text_only = arguments.get("text_only", False)
            result = web_scan(tabs_only=tabs_only, switch_tab_id=switch_tab_id, text_only=text_only)
            return ok_result(**result)

        elif name == "web_execute_js":
            script = arguments.get("script", "")
            save_to_file = arguments.get("save_to_file", "")
            switch_tab_id = arguments.get("switch_tab_id")
            no_monitor = arguments.get("no_monitor", False)
            result = web_execute_js(script, switch_tab_id=switch_tab_id, no_monitor=no_monitor)
            if save_to_file and "js_return" in result:
                content = str(result["js_return"] or '')
                abs_path = resolve_workspace_path(save_to_file, request_cwd)
                result["js_return"] = content[:170] + f"\n\n[已保存完整内容到 {abs_path}]"
                try:
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(abs_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                except Exception as e:
                    result["js_return"] += f"\n\n[保存失败: {e}]"
            result["recommended_next_tool"] = "ga_end_turn"
            return ok_result(**result)

        # ===== 交互工具 =====
        elif name == "ask_user":
            question = arguments.get("question", "")
            candidates = arguments.get("candidates", [])
            if not question:
                return error_result("ask_user 需要提供 question 参数")

            request_ctx = server.request_context
            session = request_ctx.session
            related_request_id = request_ctx.request.id if request_ctx.request else None

            # 构建 elicitation schema
            if candidates:
                # 有候选项：使用 enum 让用户选择
                requested_schema = {
                    "type": "object",
                    "properties": {
                        "selection": {
                            "type": "string",
                            "enum": candidates,
                            "description": question,
                        }
                    },
                    "required": ["selection"],
                }
            else:
                # 无候选项：自由文本输入
                requested_schema = {
                    "type": "object",
                    "properties": {
                        "response": {
                            "type": "string",
                            "description": question,
                        }
                    },
                    "required": ["response"],
                }

            # 发送 elicitation 请求，等待用户响应
            elicitation_result = await session.elicit_form(
                message=question,
                requestedSchema=requested_schema,
                related_request_id=related_request_id,
            )

            # 处理用户响应
            action = getattr(elicitation_result, 'action', 'unknown')
            content = getattr(elicitation_result, 'content', {})

            if action == "accept" and content:
                # 提取用户选择的值
                if candidates:
                    selected = content.get("selection", "")
                else:
                    selected = content.get("response", "")
                return ok_result(
                    status="user_responded",
                    message="用户已响应",
                    question=question,
                    user_response=selected,
                    candidates=candidates if candidates else None,
                    interaction="elicitation_complete",
                )
            elif action == "decline":
                return ok_result(
                    status="user_declined",
                    message="用户拒绝响应",
                    question=question,
                    interaction="elicitation_declined",
                )
            else:
                return ok_result(
                    status="elicitation_cancelled",
                    message="elicitation 被取消",
                    question=question,
                    action=action,
                    interaction="elicitation_cancelled",
                )

        # ===== 文件工具（对齐 ga.py） =====
        elif name == "read_file":
            path = resolve_workspace_path(arguments.get("path", ""), request_cwd)
            content = ga_file_read(
                str(path),
                start=arguments.get("start", 1),
                keyword=arguments.get("keyword"),
                count=arguments.get("count", 200),
                show_linenos=arguments.get("show_linenos", True),
            )
            tips = ""
            lowered = str(path).lower()
            if "memory" in lowered or "sop" in lowered:
                tips = "正在读取记忆或 SOP 文件；若按 SOP 执行，请提取关键点并在已有 session 中调用 ga_update_working_checkpoint。"
            is_memory_sop = "memory" in lowered or "sop" in lowered
            return ok_result(
                path=str(path), content=content, tips=tips,
                recommended_next_tool="ga_update_working_checkpoint" if is_memory_sop else "ga_end_turn",
            )

        elif name == "patch_file":
            path = resolve_workspace_path(arguments.get("path", ""), request_cwd)
            new_content = expand_file_refs(arguments.get("new_content", ""), base_dir=request_cwd)
            result = ga_file_patch(
                str(path),
                arguments.get("old_content", ""),
                new_content,
            )
            return ok_result(**result, path=str(path), recommended_next_tool="ga_end_turn")

        elif name == "write_file":
            path = resolve_workspace_path(arguments.get("path", ""), request_cwd)
            content = expand_file_refs(arguments.get("content", ""), base_dir=request_cwd)
            result = write_file_content(path, content, mode=arguments.get("mode", "overwrite"))
            return ok_result(**result, recommended_next_tool="ga_end_turn")

        # ===== 截图工具 =====
        elif name == "screenshot":
            base64_img = screenshot_to_base64()
            if base64_img.startswith("截图失败"):
                return error_result(base64_img)
            structured = ok_result(message="屏幕截图已生成", mime_type="image/png")
            return (
                [
                    TextContent(type="text", text="屏幕截图已生成"),
                    ImageContent(type="image", data=base64_img, mimeType="image/png")
                ],
                structured,
            )

        # ===== 窗口管理工具 =====
        elif name == "list_windows":
            windows = get_window_list()
            return ok_result(windows=windows, count=len(windows))

        elif name == "activate_window":
            hwnd = arguments.get("hwnd", 0)
            result = activate_window(hwnd)
            return ok_result(message=result.get("msg"), hwnd=hwnd)

        # ===== OCR 工具 =====
        elif name == "ocr_screen":
            if not OCR_AVAILABLE:
                return error_result("OCR 功能不可用", capability="ocr")
            enhance = arguments.get("enhance", False)
            result = ocr_screen(enhance=enhance)
            return ok_result(**result)

        # ===== ADB 工具 =====
        elif name == "adb_ui":
            if not ADB_AVAILABLE:
                return error_result("ADB 功能不可用", capability="adb")
            keyword = arguments.get("keyword")
            clickable_only = arguments.get("clickable_only", False)
            nodes = adb_ui(keyword=keyword, clickable_only=clickable_only, raw=True)
            return ok_result(nodes_count=len(nodes), nodes=nodes)

        elif name == "adb_tap":
            if not ADB_AVAILABLE:
                return error_result("ADB 功能不可用", capability="adb")
            x = arguments.get("x", 0)
            y = arguments.get("y", 0)
            adb_tap(x, y)
            return ok_result(message=f"已点击坐标 ({x}, {y})", x=x, y=y)

        # ===== 系统控制工具 =====
        elif name == "mouse_click":
            if not SYSCTL_AVAILABLE:
                return error_result("系统控制功能不可用", capability="sysctl")
            x = arguments.get("x", 0)
            y = arguments.get("y", 0)
            double = arguments.get("double", False)
            SetCursorPos((x, y))
            if double:
                MouseDClick()
            else:
                MouseClick()
            return ok_result(message=f"已在 ({x}, {y}) {'双击' if double else '单击'}", x=x, y=y, double=double)

        elif name == "key_press":
            if not SYSCTL_AVAILABLE:
                return error_result("系统控制功能不可用", capability="sysctl")
            keys_str = arguments.get("keys", "")
            Press(keys_str)
            return ok_result(message=f"已按键: {keys_str}", keys=keys_str)

        elif name == "move_mouse":
            if not SYSCTL_AVAILABLE:
                return error_result("系统控制功能不可用", capability="sysctl")
            x = arguments.get("x", 0)
            y = arguments.get("y", 0)
            SetCursorPos((x, y))
            return ok_result(message=f"鼠标已移动到 ({x}, {y})", x=x, y=y)

        # ===== UI 检测工具 =====
        elif name == "detect_ui":
            if not UI_DETECT_AVAILABLE:
                return error_result("UI 检测功能不可用", capability="ui_detect")
            image_path = arguments.get("image_path", "")
            conf_threshold = arguments.get("conf_threshold", 0.25)
            detections = detect_ui_elements(image_path, conf_threshold=conf_threshold)
            return ok_result(detections_count=len(detections), detections=detections)

        # ===== 进程内存扫描工具 =====
        elif name == "scan_process_memory":
            if not PROCMEM_AVAILABLE:
                return error_result("进程内存扫描功能不可用", capability="procmem")
            pid = arguments.get("pid", 0)
            pattern = arguments.get("pattern", "")
            mode = arguments.get("mode", "auto")
            results = scan_memory(pid, pattern, mode=mode)
            return ok_result(matches_count=len(results), matches=results[:50])

        # ===== 技能搜索工具 =====
        elif name == "skill_search":
            if not SKILL_SEARCH_AVAILABLE:
                return error_result("技能搜索功能不可用", capability="skill_search")
            query = arguments.get("query", "")
            category = arguments.get("category")
            top_k = arguments.get("top_k", 10)
            env = detect_environment()
            results = skill_search(query, env=env, category=category, top_k=top_k)
            results_dict = []
            for r in results:
                results_dict.append({
                    "skill_name": r.skill.name,
                    "description": r.skill.description,
                    "relevance": r.relevance,
                    "quality": r.quality,
                    "final_score": r.final_score,
                    "match_reasons": r.match_reasons,
                    "warnings": r.warnings
                })
            return ok_result(results_count=len(results_dict), results=results_dict)

        # ===== SOP/Skill 管理工具 =====
        elif name == "list_sops":
            sops = get_all_sops()
            return ok_result(sops_count=len(sops), sops=sops)

        elif name == "read_sop":
            sop_name = arguments.get("name", "")
            sop_path = os.path.join(SOP_DIR, f"{sop_name}_sop.md")
            if not os.path.exists(sop_path):
                return error_result(f"SOP '{sop_name}' 不存在", name=sop_name)
            try:
                with open(sop_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                return ok_result(
                    message="SOP 内容已读取",
                    name=sop_name,
                    params={},
                    content=content,
                    execution_mode="read_only",
                )
            except Exception as e:
                return error_result(str(e), name=sop_name)

        elif name == "execute_sop":
            sop_name = arguments.get("name", "")
            params = arguments.get("params", {})
            sop_path = os.path.join(SOP_DIR, f"{sop_name}_sop.md")
            if not os.path.exists(sop_path):
                return error_result(f"SOP '{sop_name}' 不存在", name=sop_name)
            try:
                with open(sop_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                return ok_result(
                    message="SOP 已加载。该工具仅返回结构化说明，不会生成递归执行提示。",
                    name=sop_name,
                    params=params,
                    content=content,
                    execution_mode="describe_only",
                )
            except Exception as e:
                return error_result(str(e), name=sop_name)

        elif name == "start_long_term_update":
            sop = load_memory_management_sop()
            return ok_result(
                message="已加载长期记忆整理 SOP；请只写行动验证成功且长期有效的信息，优先使用 write_memory patch 最小修改。",
                content=sop,
                memory_model="original_four_level",
                default_write_mode="patch",
            )

        # ===== 原项目四级记忆工具 =====
        elif name == "read_memory":
            memory_type = arguments.get("memory_type", "all")
            result = read_original_memory(memory_type)
            return ok_result(**result)

        elif name == "write_memory":
            memory_type = arguments.get("memory_type", "global_mem")
            old_content = arguments.get("old_content", "")
            new_content = arguments.get("new_content", "")
            target_path = arguments.get("target_path", "")
            mode = arguments.get("mode", "patch")
            
            if mode == "patch" and not old_content:
                return error_result("write_memory patch 模式必须提供非空的 old_content")
            if not new_content:
                return error_result("write_memory 必须提供非空的 new_content")
            
            result = write_original_memory(
                memory_type,
                old_content=old_content,
                new_content=new_content,
                target_path=target_path,
                mode=mode,
            )
            return ok_result(message="记忆写入完成", **result)

        # ===== 调度器任务管理工具 =====
        elif name == "list_scheduler_tasks":
            scheduler_dir = original_memory_paths()["scheduler"]
            all_tasks = []
            if scheduler_dir.exists():
                for f in scheduler_dir.iterdir():
                    if f.suffix == '.json':
                        with open(f, 'r', encoding='utf-8') as fp:
                            task = json.load(fp)
                        all_tasks.append({"id": f.stem, **task})
            return ok_result(tasks_count=len(all_tasks), tasks=all_tasks)

        elif name == "read_scheduler_task":
            task_id = arguments.get("task_id", "")
            scheduler_dir = original_memory_paths()["scheduler"]
            task_path = resolve_scheduler_task_path(scheduler_dir, task_id)
            if task_path.exists():
                with open(task_path, 'r', encoding='utf-8') as f:
                    task = json.load(f)
                return ok_result(task=task)
            return error_result(f"任务 '{task_id}' 不存在", task_id=task_id)

        elif name == "create_scheduler_task":
            task_id = arguments.get("task_id", "")
            prompt = arguments.get("prompt", "")
            schedule = arguments.get("schedule", "08:00")
            repeat = arguments.get("repeat", "daily")
            enabled = arguments.get("enabled", True)
            scheduler_dir = original_memory_paths()["scheduler"]
            scheduler_dir.mkdir(parents=True, exist_ok=True)
            task = {
                "prompt": prompt,
                "schedule": schedule,
                "repeat": repeat,
                "enabled": enabled,
                "max_delay_hours": 6
            }
            task_path = resolve_scheduler_task_path(scheduler_dir, task_id)
            with open(task_path, 'w', encoding='utf-8') as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
            return ok_result(message=f"任务 '{task_id}' 已创建", task_id=task_id, task=task)

        # ===== 密钥管理工具 =====
        elif name == "keychain_list":
            if not KEYCHAIN_AVAILABLE:
                return error_result("密钥管理功能不可用", capability="keychain")
            key_list = keys.ls()
            return ok_result(keys=key_list)

        elif name == "keychain_set":
            if not KEYCHAIN_AVAILABLE:
                return error_result("密钥管理功能不可用", capability="keychain")
            key_name = arguments.get("name", "")
            value = arguments.get("value", "")
            keys.set(key_name, value)
            return ok_result(message=f"密钥 '{key_name}' 已存储", name=key_name)

        else:
            return error_result(f"未知工具: {name}", tool=name)

    except Exception as e:
        import traceback
        return error_result(f"执行错误: {str(e)}", traceback=traceback.format_exc())


@asynccontextmanager
async def app_lifespan(server_instance: Server):
    """应用生命周期管理"""
    capabilities = []
    if OCR_AVAILABLE:
        capabilities.append("OCR")
    if ADB_AVAILABLE:
        capabilities.append("ADB")
    if SYSCTL_AVAILABLE:
        capabilities.append("SYSCTL")
    if UI_DETECT_AVAILABLE:
        capabilities.append("UI_DETECT")
    if PROCMEM_AVAILABLE:
        capabilities.append("PROCMEM")
    if SKILL_SEARCH_AVAILABLE:
        capabilities.append("SKILL")
    if KEYCHAIN_AVAILABLE:
        capabilities.append("KEYS")

    cap_str = f" [{', '.join(capabilities)}]" if capabilities else ""
    print(f"[GenericAgent Tools] 启动成功{cap_str} - 使用 Trae 自带模型驱动", file=sys.stderr)

    yield AppContext()

    print("[GenericAgent Tools] 关闭", file=sys.stderr)


async def main():
    """主入口"""
    from mcp.server.stdio import stdio_server as create_stdio_server
    from mcp.server import NotificationOptions

    async with create_stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="genericagent-tools-isolated",
                server_version="2.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={}
                )
            ),
            app_lifespan
        )


if __name__ == "__main__":
    asyncio.run(main())
