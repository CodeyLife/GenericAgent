#!/usr/bin/env python3
"""
GenericAgent MCP Server
将 GenericAgent 封装为 MCP 服务，支持在 Trae 等 IDE 中调用
"""

import asyncio
import json
import sys
import os
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    ErrorData,
    INVALID_PARAMS,
    INTERNAL_ERROR,
)
from mcp.server.models import InitializationOptions

# 导入 GenericAgent 核心
from agentmain import GeneraticAgent
from ga import GenericAgentHandler, smart_format


@dataclass
class AppContext:
    """应用上下文"""
    agent: GeneraticAgent
    handler: Optional[GenericAgentHandler] = None


# 创建 MCP 服务器
server = Server("genericagent-mcp")


@server.list_tools()
async def list_tools() -> List[Tool]:
    """列出所有可用工具"""
    return [
        Tool(
            name="execute_task",
            description="执行一个任务，GenericAgent 将自主完成该任务。支持文件操作、代码执行、网页浏览、数据分析等。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "要执行的任务描述，越详细越好"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "任务超时时间（秒），默认 120",
                        "default": 120
                    }
                },
                "required": ["task"]
            }
        ),
        Tool(
            name="read_file",
            description="读取文件内容",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径"
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="write_file",
            description="写入文件内容",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容"
                    }
                },
                "required": ["path", "content"]
            }
        ),
        Tool(
            name="run_code",
            description="执行 Python 或 PowerShell/Bash 代码",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "代码内容"
                    },
                    "code_type": {
                        "type": "string",
                        "description": "代码类型: python, powershell, bash",
                        "enum": ["python", "powershell", "bash"],
                        "default": "python"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 60",
                        "default": 60
                    }
                },
                "required": ["code"]
            }
        ),
        Tool(
            name="web_scan",
            description="扫描网页内容，获取当前浏览器页面的简化 HTML",
            inputSchema={
                "type": "object",
                "properties": {
                    "text_only": {
                        "type": "boolean",
                        "description": "是否只返回文本内容",
                        "default": False
                    }
                }
            }
        ),
        Tool(
            name="web_execute",
            description="在浏览器中执行 JavaScript 代码",
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "JavaScript 代码"
                    }
                },
                "required": ["script"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """调用工具"""
    ctx = server.request_context
    app_ctx: AppContext = ctx.lifespan_context
    
    try:
        if name == "execute_task":
            task = arguments.get("task", "")
            timeout = arguments.get("timeout", 120)
            
            if not task:
                raise ValueError("task 不能为空")
            
            # 提交任务到 GenericAgent
            display_queue = app_ctx.agent.put_task(task, source="mcp")
            
            # 等待结果
            result_parts = []
            start_time = asyncio.get_event_loop().time()
            
            while True:
                try:
                    # 使用 asyncio.wait_for 实现超时
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        app_ctx.agent.abort()
                        result_parts.append("\n[超时: 任务执行超过最大时间限制]")
                        break
                    
                    # 非阻塞检查队列
                    try:
                        item = display_queue.get(timeout=0.5)
                        if 'next' in item:
                            result_parts.append(item['next'])
                        if 'done' in item:
                            result_parts.append(item['done'])
                            break
                    except:
                        await asyncio.sleep(0.1)
                        
                except Exception as e:
                    result_parts.append(f"\n[错误: {str(e)}]")
                    break
            
            result = "".join(result_parts)
            return [TextContent(type="text", text=result)]
        
        elif name == "read_file":
            from ga import file_read
            path = arguments.get("path", "")
            result = file_read(path)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        
        elif name == "write_file":
            from ga import file_write
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            result = file_write(path, content)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        
        elif name == "run_code":
            from ga import code_run
            code = arguments.get("code", "")
            code_type = arguments.get("code_type", "python")
            timeout = arguments.get("timeout", 60)
            
            # 收集生成器输出
            outputs = []
            for item in code_run(code, code_type=code_type, timeout=timeout):
                outputs.append(item)
            
            return [TextContent(type="text", text="".join(outputs))]
        
        elif name == "web_scan":
            from ga import web_scan
            text_only = arguments.get("text_only", False)
            result = web_scan(text_only=text_only)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        
        elif name == "web_execute":
            from ga import web_execute_js
            script = arguments.get("script", "")
            result = web_execute_js(script)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        
        else:
            raise ValueError(f"未知工具: {name}")
            
    except Exception as e:
        return [TextContent(type="text", text=f"错误: {str(e)}")]


@asynccontextmanager
async def app_lifespan(server_instance: Server) -> AppContext:
    """应用生命周期管理"""
    # 初始化 GenericAgent
    agent = GeneraticAgent()
    if len(agent.llmclients) == 0:
        raise RuntimeError("没有可用的 LLM 客户端，请检查 mykey.py 配置")
    
    # 启动后台任务处理线程
    import threading
    threading.Thread(target=agent.run, daemon=True).start()
    
    app_ctx = AppContext(agent=agent)
    
    try:
        yield app_ctx
    finally:
        # 清理资源
        if app_ctx.agent:
            app_ctx.agent.abort()


async def main():
    """主入口"""
    async with stdio_server(server) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="genericagent-mcp",
                server_version="1.0.0",
                capabilities=server.get_capabilities()
            ),
            app_lifespan
        )


if __name__ == "__main__":
    asyncio.run(main())
