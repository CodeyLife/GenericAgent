# 还原 GenericAgentHandler 功能方案分析

## 📋 背景

当前 MCP Bridge 只提供工具执行能力，缺少原项目 GenericAgentHandler 的核心引擎层逻辑：
- 计划模式控制
- 轮次管理与超时检测
- 历史记忆管理
- 自动总结与验证

## 🔍 原项目 GenericAgentHandler 核心功能拆解

### 1. 对话循环控制 (do_no_tool)
- 检测 LLM 回复中的 `<summary>` 标签
- 大代码块未调用工具的二次确认
- Plan 模式完成验证拦截

### 2. 轮次管理 (turn_end_callback)
- 每 35 轮强制 ask_user 汇报
- 每 7 轮检测无效重试
- 每 10 轮注入全局记忆
- Plan 模式 70 轮上限

### 3. 历史记忆管理 (_get_anchor_prompt)
- 维护 history_info 列表（最近 20 条摘要）
- working checkpoint（key_info + related_sop）
- 自动提取 `<summary>` 标签

### 4. 计划模式 (enter_plan_mode / _check_plan_completion)
- 读取 plan.md 文件
- 检查 `[ ]` 未完成项
- 完成后自动退出并验证

## 💡 可行方案对比

### 方案 A：通过 MCP 工具返回值注入引导（推荐）

**思路**：利用工具返回结果中的提示信息引导模型行为

**实现方式**：
1. 在每个工具返回中添加 `tips` 字段
2. 通过 `update_working_checkpoint` 注入完整的工作记忆模板
3. 通过 `start_long_term_update` 注入 SOP 指令

**优点**：
- ✅ 不违反 MCP 协议
- ✅ 实现简单
- ✅ 兼容所有 MCP 客户端

**缺点**：
- ❌ 依赖模型遵循提示
- ❌ 无法强制控制轮次
- ❌ 历史记忆由 Trae 管理，格式不可控

---

### 方案 B：创建独立的 MCP 扩展协议

**思路**：在 MCP 基础上扩展自定义 capability，实现部分引擎功能

**实现方式**：
1. 添加自定义工具 `get_system_prompt` 返回完整系统提示
2. 添加 `check_turn_limit` 工具返回轮次状态
3. 添加 `validate_plan_completion` 工具验证计划

**优点**：
- ✅ 更接近原项目行为
- ✅ 可以验证某些规则

**缺点**：
- ❌ 需要 Trae 主动调用这些工具
- ❌ 模型可能忽略验证步骤
- ❌ 实现复杂度高

---

### 方案 C：Trae 侧配置系统提示词（最实际）

**思路**：在 Trae 的项目规则中写入 GenericAgentHandler 的行为要求

**实现方式**：
1. 在 `.trae/rules/project_rules.md` 中写入完整的行为规范
2. 包含轮次控制、总结要求、计划模式验证等
3. 模型每次对话都会加载这些规则

**优点**：
- ✅ 最接近原项目的系统提示效果
- ✅ 不需要修改代码
- ✅ 模型原生支持

**缺点**：
- ❌ 依赖 Trae 的规则加载机制
- ❌ 无法精确计数轮次
- ❌ 规则过长可能影响性能

---

### 方案 D：混合方案（最优）

**思路**：结合方案 A + 方案 C

**实现方式**：
1. **Trae 侧**：在 `.trae/rules/project_rules.md` 中写入核心行为规范
2. **MCP 侧**：在工具返回中注入上下文提示
3. **记忆系统**：通过 `update_working_checkpoint` 维护工作记忆

## 📐 推荐方案 D 的具体实现

### 第一步：创建项目规则文件

**文件**：`.trae/rules/project_rules.md`

**内容**：
```markdown
# GenericAgent MCP Bridge 行为规范

## 对话控制
- 每轮回复必须包含 `<summary>` 标签，总结本轮动作
- 连续执行超过 7 轮无明显进展，必须切换策略或 ask_user
- 连续执行超过 35 轮，必须总结并询问用户是否继续

## 工作记忆管理
- 任务开始时调用 update_working_checkpoint 记录关键信息
- 读取 SOP/记忆文件后，必须提取关键点更新工作记忆
- 切换子任务前必须更新 checkpoint

## 计划模式
- 如果存在 plan.md，必须先读取并引用当前步骤
- 声称完成前必须验证所有 `[ ]` 项已完成
- 完成后调用 start_long_term_update 结算记忆

## 工具使用规范
- 精细文件修改优先使用 patch_file
- Web 操作优先使用 execute_js，避免全量观察 html
- 代码执行必须小步验证，避免大段代码直接运行
```

### 第二步：增强 MCP 工具的提示注入

修改关键工具返回，添加行为引导：

```python
# read_file 返回中添加
if turn_count_exceeded:
    tips += "\n[DANGER] 已连续执行多轮，必须总结并询问用户"

# ask_user 返回中添加
return {
    "status": "awaiting_user_input",
    "message": "...",
    "enforce_summary": True,  # 要求模型先总结
}
```

### 第三步：添加轮次计数工具（可选）

新增工具 `get_session_info`：
```python
Tool(
    name="get_session_info",
    description="获取当前会话信息（轮次、工作记忆状态）",
    inputSchema={"type": "object", "properties": {}}
)
```

模型可以在不确定时调用此工具获取上下文。

## ⚖️ 方案对比总结

| 维度 | 方案 A | 方案 B | 方案 C | 方案 D（推荐）|
|------|--------|--------|--------|----------------|
| 实现复杂度 | 低 | 高 | 低 | 中 |
| 功能还原度 | 40% | 60% | 70% | 80% |
| 协议兼容性 | ✅ | ⚠️ | ✅ | ✅ |
| 模型遵循度 | 中 | 低 | 高 | 高 |
| 维护成本 | 低 | 高 | 低 | 中 |

## 🎯 最终建议

1. **立即执行**：创建 `.trae/rules/project_rules.md`（方案 C 核心）
2. **同步优化**：在 MCP 工具返回中添加更多上下文提示（方案 A 补充）
3. **按需增强**：根据实际使用情况决定是否添加轮次计数工具

**关键认知**：
- MCP Bridge 不可能 100% 还原 GenericAgentHandler
- 但通过"规则 + 提示"可以达到 80% 的效果
- 剩余 20% 由 Trae 模型自身能力弥补（通常更强）
