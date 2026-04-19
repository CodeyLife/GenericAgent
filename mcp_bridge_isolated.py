#!/usr/bin/env python3
"""
GenericAgent MCP Bridge - 多项目记忆隔离版本
让 Trae 的自带模型驱动，GenericAgent 只提供工具执行能力
支持项目级记忆隔离

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
    web_scan, web_execute_js, ask_user
)

# 导入项目上下文管理（实现记忆隔离）
from memory.project_context import (
    ProjectContextManager, list_projects
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
    project_manager: ProjectContextManager


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


def extract_project_manager(lifespan_context: Any) -> ProjectContextManager:
    """兼容不同 lifespan_context 形态，提取项目管理器"""
    if isinstance(lifespan_context, AppContext):
        return lifespan_context.project_manager

    if isinstance(lifespan_context, ProjectContextManager):
        return lifespan_context

    if isinstance(lifespan_context, dict):
        manager = lifespan_context.get("project_manager")
        if isinstance(manager, ProjectContextManager):
            return manager

    manager = getattr(lifespan_context, "project_manager", None)
    if isinstance(manager, ProjectContextManager):
        return manager

    return ProjectContextManager()


async def get_request_project_context() -> tuple[ProjectContextManager, str, Any]:
    """获取与当前请求绑定的项目上下文"""
    request_ctx = server.request_context
    project_manager = extract_project_manager(request_ctx.lifespan_context)
    request_cwd = await resolve_request_cwd(request_ctx.lifespan_context)
    project_ctx = project_manager.detect_project(cwd=request_cwd)
    return project_manager, request_cwd, project_ctx


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
        # ===== 项目上下文工具 =====
        Tool(
            name="get_project_context",
            description="获取当前项目上下文信息（自动检测当前所属项目）",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Get Project Context", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="list_project_memories",
            description="列出所有已存在的项目记忆空间",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="List Project Memories", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        
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
            description="扫描浏览器页面",
            inputSchema={
                "type": "object",
                "properties": {"text_only": {"type": "boolean", "default": False}}
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Scan Web Page", read_only=True, destructive=False, idempotent=True, open_world=True),
        ),
        Tool(
            name="web_execute_js",
            description="在浏览器执行 JavaScript",
            inputSchema={
                "type": "object",
                "properties": {"script": {"type": "string"}},
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
        
        # ===== 项目隔离的记忆工具 =====
        Tool(
            name="read_memory",
            description="读取记忆内容（支持项目级隔离）",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": ["global_mem", "todo", "history"], "default": "global_mem"},
                    "scope": {"type": "string", "enum": ["auto", "project", "global"], "default": "auto", "description": "读取范围：auto=自动(优先项目), project=仅项目级, global=仅全局"}
                }
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Read Memory", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="write_memory",
            description="写入记忆内容（支持项目级隔离）",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": ["global_mem", "todo", "history"], "default": "global_mem"},
                    "content": {"type": "string", "description": "记忆内容"},
                    "scope": {"type": "string", "enum": ["project", "global"], "default": "project", "description": "写入范围：project=项目级, global=全局"},
                    "mode": {"type": "string", "enum": ["append", "overwrite"], "default": "overwrite", "description": "写入模式：append=追加, overwrite=全量覆盖"}
                },
                "required": ["content"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Write Memory", read_only=False, destructive=False, idempotent=False, open_world=False),
        ),
        
        # ===== 调度器任务管理工具（项目隔离） =====
        Tool(
            name="list_scheduler_tasks",
            description="列出当前项目的调度器任务",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_global": {"type": "boolean", "default": False, "description": "是否包含全局任务"}
                }
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="List Scheduler Tasks", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="read_scheduler_task",
            description="读取调度器任务详情",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                    "scope": {"type": "string", "enum": ["auto", "project", "global"], "default": "auto"}
                },
                "required": ["task_id"]
            },
            outputSchema=JSON_SCHEMA_BASE,
            annotations=tool_annotations(title="Read Scheduler Task", read_only=True, destructive=False, idempotent=True, open_world=False),
        ),
        Tool(
            name="create_scheduler_task",
            description="创建新的调度器任务（默认创建到项目空间）",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                    "prompt": {"type": "string", "description": "任务提示词"},
                    "schedule": {"type": "string", "description": "执行时间 (如 '08:00')", "default": "08:00"},
                    "repeat": {"type": "string", "description": "重复类型 (once/daily/weekday/weekly/monthly)", "default": "daily"},
                    "enabled": {"type": "boolean", "default": True},
                    "scope": {"type": "string", "enum": ["project", "global"], "default": "project"}
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
                "properties": {"enhance": {"type": "boolean", "default": False}}
            }
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
                        "keyword": {"type": "string"},
                        "clickable_only": {"type": "boolean", "default": False}
                    }
                }
            ),
            Tool(
                name="adb_tap",
                description="点击 Android 屏幕",
                inputSchema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"]
                }
            ),
        ])
    
    # ===== 系统控制工具 =====
    if SYSCTL_AVAILABLE:
        tools.extend([
            Tool(
                name="mouse_click",
                description="鼠标点击",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "double": {"type": "boolean", "default": False}
                    },
                    "required": ["x", "y"]
                }
            ),
            Tool(
                name="key_press",
                description="键盘按键",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string", "description": "按键组合，如 'ctrl+v' 或 'enter'"}
                    },
                    "required": ["keys"]
                }
            ),
            Tool(
                name="move_mouse",
                description="移动鼠标",
                inputSchema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"]
                }
            ),
        ])
    
    # ===== UI 检测工具 =====
    if UI_DETECT_AVAILABLE:
        tools.append(Tool(
            name="detect_ui",
            description="检测图片中的 UI 元素（YOLO）",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "图片路径"},
                    "conf_threshold": {"type": "number", "default": 0.25}
                },
                "required": ["image_path"]
            }
        ))
    
    # ===== 进程内存扫描工具 =====
    if PROCMEM_AVAILABLE:
        tools.append(Tool(
            name="scan_process_memory",
            description="扫描进程内存",
            inputSchema={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程ID"},
                    "pattern": {"type": "string", "description": "搜索模式（字符串或十六进制）"},
                    "mode": {"type": "string", "enum": ["auto", "hex", "text"], "default": "auto"}
                },
                "required": ["pid", "pattern"]
            }
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
                    "top_k": {"type": "integer", "default": 10}
                },
                "required": ["query"]
            }
        ))
    
    # ===== 密钥管理工具 =====
    if KEYCHAIN_AVAILABLE:
        tools.extend([
            Tool(
                name="keychain_list",
                description="列出所有存储的密钥名称",
                inputSchema={"type": "object", "properties": {}}
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
                }
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

        project_manager, request_cwd, project_ctx = await get_request_project_context()

        # ===== 项目上下文工具 =====
        if name == "get_project_context":
            return ok_result(
                **project_ctx.to_dict(),
                request_cwd=request_cwd,
            )
        
        elif name == "list_project_memories":
            projects = list_projects()
            return ok_result(projects=projects, count=len(projects))
        
        # ===== 基础工具 =====
        elif name == "run_code":
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
            text_only = arguments.get("text_only", False)
            result = web_scan(text_only=text_only)
            return ok_result(**result)
        
        elif name == "web_execute_js":
            script = arguments.get("script", "")
            result = web_execute_js(script)
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
        
        # ===== 项目隔离的记忆工具 =====
        elif name == "read_memory":
            memory_type = arguments.get("memory_type", "global_mem")
            scope = arguments.get("scope", "auto")
            result = project_manager.read_memory(memory_type, scope, cwd=request_cwd)
            return ok_result(**result)
        
        elif name == "write_memory":
            memory_type = arguments.get("memory_type", "global_mem")
            content = arguments.get("content", "")
            scope = arguments.get("scope", "project")
            mode = arguments.get("mode", "append")
            result = project_manager.write_memory(memory_type, content, scope, cwd=request_cwd, mode=mode)
            return ok_result(message="记忆写入完成", **result)
        
        # ===== 调度器任务管理工具（项目隔离） =====
        elif name == "list_scheduler_tasks":
            include_global = arguments.get("include_global", False)
            
            all_tasks = []
            
            # 读取项目级任务
            if project_ctx.project_id != "global":
                project_scheduler_dir = project_manager.get_memory_path("scheduler", scope="project", cwd=request_cwd)
                if project_scheduler_dir.exists():
                    for f in project_scheduler_dir.iterdir():
                        if f.suffix == '.json':
                            with open(f, 'r', encoding='utf-8') as fp:
                                task = json.load(fp)
                            all_tasks.append({"id": f.stem, "scope": "project", **task})
            
            # 读取全局任务
            if include_global:
                global_scheduler_dir = project_manager.get_memory_path("scheduler", scope="global", cwd=request_cwd)
                if global_scheduler_dir.exists():
                    for f in global_scheduler_dir.iterdir():
                        if f.suffix == '.json':
                            with open(f, 'r', encoding='utf-8') as fp:
                                task = json.load(fp)
                            all_tasks.append({"id": f.stem, "scope": "global", **task})
            
            return ok_result(tasks_count=len(all_tasks), tasks=all_tasks)
        
        elif name == "read_scheduler_task":
            task_id = arguments.get("task_id", "")
            scope = arguments.get("scope", "auto")
            
            # 确定查找范围
            if scope == "auto":
                scopes = ["project", "global"]
            else:
                scopes = [scope]
            
            for s in scopes:
                scheduler_dir = project_manager.get_memory_path("scheduler", scope=s, cwd=request_cwd)
                task_path = resolve_scheduler_task_path(scheduler_dir, task_id)
                if task_path.exists():
                    with open(task_path, 'r', encoding='utf-8') as f:
                        task = json.load(f)
                    return ok_result(scope=s, task=task)
            
            return error_result(f"任务 '{task_id}' 不存在", task_id=task_id)
        
        elif name == "create_scheduler_task":
            task_id = arguments.get("task_id", "")
            prompt = arguments.get("prompt", "")
            schedule = arguments.get("schedule", "08:00")
            repeat = arguments.get("repeat", "daily")
            enabled = arguments.get("enabled", True)
            scope = arguments.get("scope", "project")
            
            scheduler_dir = project_manager.get_memory_path("scheduler", scope=scope, cwd=request_cwd)
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
            
            return ok_result(scope=scope, message=f"任务 '{task_id}' 已创建", task_id=task_id, task=task)
        
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
    
    # 初始化项目上下文管理器
    project_manager = ProjectContextManager()
    ctx = project_manager.detect_project()
    
    cap_str = f" [{', '.join(capabilities)}]" if capabilities else ""
    print(f"[GenericAgent Tools] 启动成功{cap_str} - 使用 Trae 自带模型驱动", file=sys.stderr)
    print(f"[Project Context] 当前项目: {ctx.project_name} ({ctx.project_id}) [{ctx.project_type}]", file=sys.stderr)
    
    yield AppContext(project_manager=project_manager)
    
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
