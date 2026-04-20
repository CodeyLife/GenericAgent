# 记忆重复写入问题分析计划

## 问题描述
在 `mcp_bridge_isolated.py` 中，计划模式和记忆相关功能出现记忆重复写入的问题。

## 需要检查的关键区域

### 1. 记忆写入函数分析
- `write_original_memory()` (L485-525): 主要记忆写入入口
- `patch_text_file()` (L467-482): patch 模式下的文件修改
- `append()` 模式逻辑 (L502-512)

### 2. 可能导致重复写入的场景

#### 场景 1: append 模式重复调用
```python
# L502-512
if mode == "append":
    existing = read_text_if_exists(mem_path)
    combined = f"{existing}\n\n---\n\n{stripped}" if existing else stripped
    with open(mem_path, "w", encoding="utf-8") as f:
        f.write(combined)
```
**问题**: 如果 AI 多次调用 `write_memory` 使用 append 模式写入相同内容，会导致重复。

#### 场景 2: patch 模式匹配不唯一
```python
# L474-478
count = text.count(old_content)
if count == 0:
    raise ValueError("未找到匹配的 old_content")
if count > 1:
    raise ValueError(f"找到 {count} 处匹配，无法唯一 patch")
```
**问题**: 如果 AI 提供的 `old_content` 过于简单，可能匹配多次，导致 patch 失败后重试时重复写入。

#### 场景 3: 计划模式下多次刷新记忆
在 `ga_get_context()` (L1475-1492) 中，每次调用都会自动读取全局记忆：
```python
# L646-657 build_anchor_context()
if include_global_memory:
    memory = read_original_memory("all")
    l1 = memory.get("layers", {}).get("L1_global_mem_insight", {}).get("content", "")
    l2 = memory.get("layers", {}).get("L2_global_mem", {}).get("content", "")
```

**潜在问题**: AI 可能在计划模式下：
1. 调用 `ga_get_context` 获取记忆
2. 根据记忆内容做决策
3. 调用 `write_memory` 写入"新"记忆（实际是已存在的内容）
4. 循环往复导致重复

### 3. 计划模式特定问题

#### ga_enter_plan_mode() (L1549-1564)
- 设置 `phase = "planning"`
- 设置 `max_turns = 80`
- **缺少**: 没有锁定记忆写入或检查重复

#### ga_end_turn() (L1518-1547)
- 每 10 轮提醒刷新全局记忆 (L1531-1532)
- 计划模式下每 5 轮提醒检查 plan 状态 (L1533-1534)
- **问题**: 可能导致 AI 频繁读取和重写记忆

### 4. 解决方案建议

#### 方案 A: 添加内容去重检查
在 `write_original_memory()` 中添加：
```python
def check_duplicate_content(mem_path, new_content, mode):
    existing = read_text_if_exists(mem_path)
    if new_content.strip() in existing:
        raise ValueError("内容已存在，避免重复写入")
```

#### 方案 B: 计划模式限制记忆写入
- 在 `ga_enter_plan_mode()` 中设置 `memory_write_locked = True`
- 在 `ga_approve_plan()` 后才允许写入记忆
- 计划执行期间只允许读取记忆

#### 方案 C: 添加写入日志和检查
- 记录每次写入的 session_id、timestamp、content_hash
- 在写入前检查近期是否有相同写入
- 在 timeline 中添加 `memory_write` 事件

#### 方案 D: 改进 patch 模式的 old_content 验证
- 要求 `old_content` 至少 20 个字符
- 提供上下文建议，帮助 AI 选择唯一匹配
- 添加模糊匹配检测，避免相似内容重复

## 实施步骤

1. **诊断阶段**
   - 添加详细的写入日志
   - 记录重复写入的模式和频率
   - 分析是 AI 行为问题还是代码逻辑问题

2. **快速修复**
   - 实施方案 A（内容去重）
   - 添加写入前的内容检查

3. **长期优化**
   - 实施方案 B（计划模式限制）
   - 实施方案 D（改进 patch 验证）
   - 添加智能去重和合并逻辑

4. **测试验证**
   - 创建测试用例模拟重复写入场景
   - 验证去重逻辑不影响正常写入
   - 检查计划模式下的记忆管理流程

## 风险评估

- **低风险**: 方案 A（去重检查）- 不影响现有功能
- **中风险**: 方案 B（计划模式限制）- 可能影响某些工作流
- **需要谨慎**: 方案 C（写入日志）- 需要考虑性能和隐私

## 建议优先级

1. **立即实施**: 方案 A - 添加内容去重检查
2. **短期实施**: 方案 D - 改进 patch 验证
3. **长期优化**: 方案 B + C - 计划模式限制和日志
