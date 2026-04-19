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
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager, redirect_stdout
from dataclasses import dataclass
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
WORKING_CHECKPOINT: Dict[str, Any] = {}


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
    content: str = "",
    mode: str = "patch",
    old_content: str = "",
    new_content: str = "",
    target_path: str = "",
) -> Dict[str, Any]:
    """按原项目保守记忆流程写入：默认使用唯一 patch；append 需显式指定。"""
    if mode not in ("append", "patch"):
        raise ValueError("原项目记忆流程不支持 overwrite；仅允许 append 或 patch")

    paths = original_memory_paths()
    mem_path = resolve_original_memory_write_path(memory_type, target_path)
    mem_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append":
        if memory_type not in ("global_mem", "todo", "history"):
            raise ValueError("append 仅支持 global_mem/todo/history；L1/L3/L4 结构性修改请使用 patch")
        stripped = content.strip()
        if not stripped:
            raise ValueError("content 不能为空")
        existing = read_text_if_exists(mem_path)
        combined = f"{existing}\n\n---\n\n{stripped}" if existing else stripped
        with open(mem_path, "w", encoding="utf-8") as f:
            f.write(combined)
        detail = {"appended_bytes": len(stripped)}
    else:
        detail = patch_text_file(mem_path, old_content, new_content)

    return {
        "status": "success",
        "memory_model": "original_four_level",
        "path": str(mem_path),
        "memory_type": memory_type,
        "mode": mode,
        "sop": str(paths["memory_management_sop"]),
        "note": "已按原项目记忆流程写入；L1/L3 同步应继续遵循 memory_management_sop 的最小 patch 原则。",
        **detail,
    }

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
            name="update_working_checkpoint",
            description="对齐 ga.py 的短期工作记忆：保存当前任务关键约束、发现和相关 SOP；用于长任务/切换子任务前。",
            inputSchema={
                "type": "object",
                "properties": {
                    "key_info": {"type": "string"},
                    "related_sop": {"type": "string"}
                }
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Update Working Checkpoint", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        Tool(
            name="start_long_term_update",
            description="对齐 ga.py 的长期记忆结算入口：读取 memory_management_sop 并要求后续用 write_memory patch 做最小记忆更新。",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Start Long-Term Memory Update", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),

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
            description="按原 GenericAgent 记忆流程写入：默认使用唯一 patch；append 需显式指定；不支持 overwrite",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": ["global_mem_insight", "global_mem", "todo", "history", "memory_management_sop", "l3_file"], "default": "global_mem"},
                    "content": {"type": "string", "description": "append 模式追加的记忆内容"},
                    "mode": {"type": "string", "enum": ["patch", "append"], "default": "patch", "description": "patch=唯一 old_content 替换（默认，符合原项目最小修改流程）；append=追加到 L2/todo/history"},
                    "old_content": {"type": "string", "description": "patch 模式要替换的唯一旧文本"},
                    "new_content": {"type": "string", "description": "patch 模式替换后的新文本"},
                    "target_path": {"type": "string", "description": "memory_type=l3_file 时，memory/ 内的相对路径"}
                }
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
            return ok_result(**result)

        # ===== 交互工具 =====
        elif name == "ask_user":
            question = arguments.get("question", "")
            candidates = arguments.get("candidates", [])
            _ = ask_user(question, candidates)
            return {
                "status": "awaiting_user_input",
                "message": "需要用户输入。MCP 客户端应暂停自动执行并向用户展示该问题。",
                "question": question,
                "candidates": candidates,
                "interaction": "elicitation_required",
            }

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
                tips = "正在读取记忆或 SOP 文件；若按 SOP 执行，请提取关键点并调用 update_working_checkpoint。"
            return ok_result(path=str(path), content=content, tips=tips)

        elif name == "patch_file":
            path = resolve_workspace_path(arguments.get("path", ""), request_cwd)
            new_content = expand_file_refs(arguments.get("new_content", ""), base_dir=request_cwd)
            result = ga_file_patch(
                str(path),
                arguments.get("old_content", ""),
                new_content,
            )
            return ok_result(**result, path=str(path))

        elif name == "write_file":
            path = resolve_workspace_path(arguments.get("path", ""), request_cwd)
            content = expand_file_refs(arguments.get("content", ""), base_dir=request_cwd)
            result = write_file_content(path, content, mode=arguments.get("mode", "overwrite"))
            return ok_result(**result)

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

        elif name == "update_working_checkpoint":
            if "key_info" in arguments:
                WORKING_CHECKPOINT["key_info"] = arguments.get("key_info", "")
            if "related_sop" in arguments:
                WORKING_CHECKPOINT["related_sop"] = arguments.get("related_sop", "")
            return ok_result(message="工作记忆检查点已更新", checkpoint=dict(WORKING_CHECKPOINT))

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
            content = arguments.get("content", "")
            mode = arguments.get("mode", "patch")
            result = write_original_memory(
                memory_type,
                content=content,
                mode=mode,
                old_content=arguments.get("old_content", ""),
                new_content=arguments.get("new_content", ""),
                target_path=arguments.get("target_path", ""),
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
