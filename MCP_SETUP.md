# GenericAgent MCP 配置指南

## 概述

本文档说明如何将 GenericAgent 配置为 MCP (Model Context Protocol) 服务器，以便在 Trae 等支持 MCP 的 IDE 中使用。

## 安装步骤

### 1. 安装 MCP 依赖

```bash
cd F:\GitHubProject\GenericAgent
pip install mcp>=1.0.0
```

### 2. 配置 API Key

确保已创建 `mykey.py` 文件并配置 LLM API：

```python
# mykey.py
claude_api_key = "your-anthropic-api-key"
# 或其他支持的 LLM 配置
```

### 3. 测试 MCP 服务器

```bash
python mcp_server.py
```

如果配置正确，服务器将启动并等待 MCP 连接。

## Trae 配置

### 方法一：通过 Trae 设置界面配置

1. 打开 Trae IDE
2. 进入 **Settings** → **MCP**
3. 点击 **Add Server**
4. 填写以下信息：
   - **Name**: `genericagent`
   - **Command**: `python`
   - **Arguments**: `F:\GitHubProject\GenericAgent\mcp_server.py`
   - **Environment Variables**: `PYTHONIOENCODING=utf-8`

### 方法二：直接编辑配置文件

在 Trae 的 MCP 配置文件中添加（路径通常是 `~/.trae/mcp.json` 或类似位置）：

```json
{
  "mcpServers": {
    "genericagent": {
      "command": "python",
      "args": [
        "F:\\GitHubProject\\GenericAgent\\mcp_server.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## 可用工具

配置成功后，Trae 中的 AI 助手可以调用以下工具：

### 1. `execute_task`
执行自主任务，GenericAgent 将自动完成该任务。

**参数：**
- `task` (string, 必需): 任务描述
- `timeout` (integer, 可选): 超时时间（秒），默认 120

**示例：**
```json
{
  "task": "创建一个 Python 脚本，计算斐波那契数列前 20 项",
  "timeout": 120
}
```

### 2. `read_file`
读取文件内容。

**参数：**
- `path` (string, 必需): 文件路径

### 3. `write_file`
写入文件内容。

**参数：**
- `path` (string, 必需): 文件路径
- `content` (string, 必需): 文件内容

### 4. `run_code`
执行 Python 或 Shell 代码。

**参数：**
- `code` (string, 必需): 代码内容
- `code_type` (string, 可选): 代码类型 (`python`, `powershell`, `bash`)，默认 `python`
- `timeout` (integer, 可选): 超时时间（秒），默认 60

### 5. `web_scan`
扫描当前浏览器页面内容。

**参数：**
- `text_only` (boolean, 可选): 是否只返回文本内容，默认 `false`

### 6. `web_execute`
在浏览器中执行 JavaScript。

**参数：**
- `script` (string, 必需): JavaScript 代码

## 使用示例

在 Trae 的 AI 对话中，你可以这样使用：

> "请使用 genericagent 工具帮我分析当前目录下的所有 Python 文件"

> "使用 execute_task 工具，帮我创建一个数据可视化脚本，展示销售数据的趋势图"

> "使用 web_scan 工具获取当前浏览器页面的内容"

## 故障排除

### 1. 服务器启动失败

检查 `mykey.py` 是否正确配置：
```bash
python -c "import mykey; print('OK')"
```

### 2. 工具调用超时

增加 `timeout` 参数的值，或检查 GenericAgent 是否正确初始化。

### 3. 编码问题

确保设置了环境变量 `PYTHONIOENCODING=utf-8`。

### 4. 查看日志

在 `temp/` 目录下查看日志文件了解详细错误信息。

## 注意事项

1. **安全性**: GenericAgent 具有系统级控制能力，请谨慎使用
2. **资源占用**: 长时间运行的任务可能占用较多资源
3. **浏览器控制**: `web_scan` 和 `web_execute` 需要配合 TMWebDriver 使用

## 更多信息

- GenericAgent 文档: [README.md](README.md)
- MCP 协议文档: https://modelcontextprotocol.io
