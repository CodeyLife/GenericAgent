# RALPLAN: MCP Bridge 恢复 GenericAgent 原架构能力方案

## Requirements Summary

用户指出 `mcp_bridge_isolated.py` 没有复现原 GenericAgent 的多轮 agent loop、working memory 自动注入、`turn_end_callback`、Plan 模式、自主行动、调度器执行、system prompt/global memory 等子系统，并询问：

1. 这些功能如何在 MCP 里实现？
2. 如何确保 Trae 的模型在合适时机调用这些能力？

核心结论：**不能把“确保 Trae 调用”承诺给纯 MCP 工具。** 在 Trae-driven MCP 模式下，MCP server 只能提供工具、资源、提示、状态校验和审计；是否调用仍由 Trae/模型决定。若要保证多轮循环、自动注入、turn callback、scheduler/autonomous 执行，必须让 GenericAgent 自己拥有 agent loop，或使用 Trae host-level hook/extension。

## Grounded Evidence

- `agent_loop.py:45-100`：原项目 `agent_runner_loop` 拥有 LLM 调用 → 工具执行 → `turn_end_callback` → next prompt → 下一轮 LLM 调用，默认 `max_turns=40`。
- `ga.py:466-474`：Plan mode 状态、`enter_plan_mode`、`_check_plan_completion`。
- `ga.py:476-486`：`update_working_checkpoint` 写入 `key_info` / `related_sop` 后立刻返回 `_get_anchor_prompt`。
- `ga.py:553-563`：`_get_anchor_prompt` 注入最近 20 条 history、turn、`key_info`、`related_sop`。
- `ga.py:565-592`：`turn_end_callback` 提取/补全 `<summary>`、写 history、7/10/35 轮警告、Plan hint、`_keyinfo`/`_intervene` 注入、done hooks。
- `mcp_bridge_isolated.py:92`：当前 `WORKING_CHECKPOINT` 是模块级全局 dict。
- `mcp_bridge_isolated.py:543-544` 与 `1018-1027`：当前 bridge 主要只注册 tools/list_tools 与 call_tool。
- `mcp_bridge_isolated.py:1278-1283`：`update_working_checkpoint` 仅更新全局 checkpoint 并返回；没有自动注入下一轮上下文。
- `reflect/scheduler.py:32-129`：原 scheduler 有 cooldown、weekday、max delay、L4 cron 和 due prompt 生成；执行由原 agentmain reflect loop 接管。

## RALPLAN-DR Summary

### Principles

1. **Authority clarity first**：任何入口必须声明 loop owner 是 Trae 还是 GenericAgent。
2. **Do not overpromise MCP**：纯 MCP 可建议/暴露/校验，但不能强制 Trae 模型调用时机。
3. **State must be session-scoped**：禁止继续使用跨项目/跨任务全局 checkpoint 作为语义状态。
4. **Advisory and authoritative paths must not blur**：Trae-driven lifecycle tools 和 GenericAgent-owned loop 必须分离。
5. **Autonomy requires safety/audit**：scheduler、idle、自主 loop 必须有 opt-in、锁、限额、取消、日志与权限策略。

### Decision Drivers

1. **Parity fidelity**：哪些原功能能被真实复现，而不是只暴露近似工具。
2. **Trae compatibility**：保持 MCP Bridge 可被 Trae 作为工具集使用。
3. **Safety/debuggability**：长任务和后台执行必须可追踪、可恢复、可停止。

### Viable Options

| Option | Description | Pros | Cons | Best fit |
|---|---|---|---|---|
| A. Pure MCP advisory bridge | 只加 tools/resources/prompts/session state，不让 GenericAgent 调模型 | 简单、Trae-native、低侵入 | 不能保证 lifecycle timing、memory injection、callback/autonomy | 交互式辅助、上下文读取、轻量 checkpoint |
| B. GenericAgent-owned loop via MCP tool | 暴露 `execute_task` / `agent_loop`，内部复用原 agent loop/handler | 能强制 loop、memory、callback、plan gate、scheduler | 复杂、需要 LLM backend 或 MCP sampling、Trae 变成前端 | 长任务、自主任务、原功能 parity |
| C. Trae host plugin/extension | 利用 Trae 插件 hook 自动调用 lifecycle | UX 最好，可能实现自动 context 注入 | 依赖 Trae 私有 API，移植性差 | Trae 深度集成可用时 |
| D. External orchestrator/daemon | 外部进程观察任务/日志并驱动 Trae/MCP/GenericAgent | 可做调度和企业自动化 | 运维复杂、容易 brittle、安全风险高 | CI/后台任务/定时任务 |

### Chosen Strategy

采用 **hybrid 3-layer**：

1. **Layer 1: Trae-driven MCP advisory parity**：默认模式，Trae 仍是 loop owner；GenericAgent 提供 session-scoped context、checkpoint、plan state、lifecycle tools、resources/prompts、state validation、audit。
2. **Layer 2: GenericAgent-owned authoritative loop**：显式 opt-in 的 `execute_task`/`agent_loop`；只有该层能承诺真实多轮自主循环、自动 memory injection、真实 turn callback、Plan gate。
3. **Layer 3: Scheduler/idle sidecars**：只负责 due/idle 检测与投递；执行要么通知 Trae，要么调用 Layer 2；不得暗中改变 authority mode。

Rejected alternatives:
- 只做 Option A：无法回答“原 agent loop 怎么复现”，只能说服 Trae 自觉调用。
- 只做 Option B：会破坏 MCP Bridge “Trae 模型驱动”的现有设计定位。
- 只做 Option C：当前不能假设 Trae 有足够 host hooks。
- 只做 Option D：太重，且安全/运维风险最大。

## Authority Modes Contract

| Mode | Loop owner | Tool timing guarantee | Memory injection guarantee | Callback guarantee | Scheduler guarantee | Failure handling |
|---|---|---|---|---|---|---|
| `trae_advisory` | Trae/model | No; only prompt/schema/state-contract guidance | No; unless Trae calls `get_context`/resource/prompt | No; `end_turn` is manual lifecycle hook | No; unless sidecar notifies or Trae polls | audit warnings, invalid-transition rejection where possible |
| `ga_authoritative` | GenericAgent | Yes, inside `agent_runner_loop` equivalent | Yes, prompt assembled before each model turn | Yes, internal `on_turn_end` runs after each turn | Yes, if daemon calls layer 2 and locks succeed | internal retry/checkpoint/cancel/max-turn handling |
| `host_integrated` | Trae host hook + MCP | Maybe, if Trae extension API supports hooks | Maybe | Maybe | Maybe | host-specific |

Product wording rule: **只有 `ga_authoritative` 或 verified host hooks 可以说“保证”；`trae_advisory` 只能说“建议、暴露、校验、审计”。**

## Enforcement Ladder: 如何让 Trae 在合适时机调用

| Level | Mechanism | What it can do | Guarantee |
|---|---|---|---|
| 0 | README / tool descriptions | 告诉模型什么时候调用 | Weak |
| 1 | MCP prompts/resources | 提供 `genericagent_begin_task`、`genericagent_turn_protocol`、`ga://session/{id}/context` | Advisory |
| 2 | Tool schema friction | 强制 `session_id`、`turn_id`、`phase`、`previous_checkpoint_id`、`expected_next_action` | Improves auditability; not timing |
| 3 | Server-side state validation | 拒绝 invalid transition；提示缺失 `begin_task`/`end_turn` | Enforces consistency only when tools are called |
| 4 | Trae host integration | 用 host hooks 自动插入 context/调用 lifecycle | Depends on Trae API |
| 5 | GenericAgent-owned loop | GenericAgent 自己调用 LLM 并执行工具 | Strong |

回答用户的关键句：**不能在 MCP server 侧“确保 Trae 模型一定调用”；只能通过 Level 0-3 提高调用概率与可审计性。真正确保要 Level 4 host hook 或 Level 5 GenericAgent-owned loop。**

## Target Architecture

### Layer 1 — Trae-driven MCP advisory parity

#### New session model

Add `SessionState` persisted under e.g. `.genericagent_mcp/sessions/{session_id}.json` or project-local state directory:

```python
@dataclass
class SessionState:
    session_id: str
    root: str
    authority_mode: Literal['trae_advisory', 'ga_authoritative']
    phase: Literal['idle','task_started','context_loaded','planning','plan_ready','approved','executing','verifying','completed','archived','blocked','cancelled','recovery_required']
    turn: int
    max_turns: int
    history_info: list[str]
    working: dict[str, Any]  # key_info, related_sop, passed_sessions
    plan_path: str | None
    checkpoint_id: str | None
    created_at: str
    updated_at: str
    warnings: list[dict]
```

Replace global `WORKING_CHECKPOINT` semantics with session-scoped state. Keep module global only as in-memory cache, never as authoritative persisted state.

#### Lifecycle tools

1. `begin_task(task, authority_mode='trae_advisory', session_id?, resume=false, plan_required=false)`
   - Creates/resumes session.
   - Returns `session_id`, `turn_id=0`, `recommended_next_tool='get_context'`, `context_resource`, `authority_mode`, `protocol_summary`.

2. `get_context(session_id?, scope='session|global|all')`
   - If no active session: return global/project context with `warning='no_active_session'`; mutating tools still reject without session.
   - Mirrors `_get_anchor_prompt`: history last 20, current turn, key_info, related_sop, optional global memory.
   - Clearly labels provenance/timestamps and trusted vs untrusted content.

3. `update_working_checkpoint(session_id, key_info?, related_sop?, merge=true)`
   - Updates session `working`.
   - Returns new checkpoint, `checkpoint_id`, `context_resource`, `recommended_next_action='continue_or_end_turn'`.

4. `end_turn(session_id, assistant_response, tool_calls=[], tool_results=[], exit_reason?)`
   - In `trae_advisory`, this is a **manual lifecycle hook**, not a real callback.
   - Extracts `<summary>` or synthesizes from tool calls.
   - Increments turn, appends history, returns warnings, `next_prompt`, `requires_model_turn`, `recommended_next_action`.
   - Applies 7/10/35 turn warnings and plan hints as advisory text.

5. `enter_plan_mode(session_id, plan_path, require_approval=true)`
   - Sets phase to `planning`, stores plan path, raises max_turns to 80.

6. `get_plan_status(session_id)`
   - Counts unchecked `[ ]` in plan file.
   - Returns `remaining_unchecked`, `has_verdict`, `phase`, `can_complete`.

7. `approve_plan(session_id, verdict_ref, approved_by='user|host|policy')`
   - Transitions `plan_ready` → `approved`.

8. `end_task(session_id, final_summary, verification_ref?, force=false)`
   - In planning/verification-required state, reject or warn without verification depending on policy.
   - Archives session and writes final audit.

9. Observability tools:
   - `list_sessions`, `get_session_timeline`, `get_lifecycle_warnings`, `get_checkpoint`, `get_active_policy`.

#### MCP resources/prompts

Resources:
- `ga://session/{session_id}/context`
- `ga://session/{session_id}/working-memory`
- `ga://session/{session_id}/history`
- `ga://session/{session_id}/timeline`
- `ga://memory/global`
- `ga://plans/{session_id}/status`
- `ga://scheduler/due`

Prompts:
- `genericagent_begin_task`
- `genericagent_turn_protocol`
- `genericagent_plan_protocol`
- `genericagent_scheduler_task`
- `genericagent_checkpoint_policy`

#### Continuation envelope returned by stateful tools

```json
{
  "status": "continue",
  "authority_mode": "trae_advisory",
  "session_id": "...",
  "turn": 7,
  "phase": "executing",
  "next_prompt": "...",
  "requires_model_turn": true,
  "recommended_next_action": "call_model_with_next_prompt_then_end_turn",
  "context_resource": "ga://session/.../context",
  "warnings": [],
  "plan_status": {"in_plan_mode": true, "remaining_unchecked": 3}
}
```

All existing mutating tools (`write_file`, `patch_file`, `run_code`, `web_execute_js`, ADB/system control) should optionally accept `session_id` and emit `context_delta` + `recommended_next_lifecycle_action='end_turn'`.

### Layer 2 — GenericAgent-owned authoritative loop

Add explicit tool, separate from advisory lifecycle:

```json
{
  "name": "execute_task",
  "input": {
    "task": "string",
    "mode": "normal|plan|scheduled|autonomous",
    "session_id": "string?",
    "max_turns": "integer",
    "llm_backend": "configured|mcp_sampling|disabled",
    "tool_policy": "safe|workspace|full",
    "approval_policy": "never|destructive|always",
    "budget": {"max_seconds": 3600, "max_tool_calls": 200}
  }
}
```

Loop skeleton:

```text
load/create session
assemble system prompt = base prompt + global memory + session context + working memory
while not done and under max_turns/budget/cancellation:
  call LLM through configured backend or MCP sampling if supported
  execute tools under policy
  run internal on_turn_end exactly once
  checkpoint state and transcript
  evaluate done/continue/needs_user/error
run final verification if plan/autonomous mode requires it
archive completed/failed/cancelled state
return transcript, final_result, warnings, timeline, checkpoint_id
```

Only this layer can faithfully restore:
- `agent_loop.py` autonomous loop
- guaranteed `_get_anchor_prompt` injection
- guaranteed `turn_end_callback`
- enforced plan-mode completion guard
- autonomous operation after idle/scheduler triggers

### Layer 3 — Scheduler and idle sidecars

Sidecar contract:

- opt-in only; disabled by default.
- Each job has `job_id`, `session_id`, `idempotency_key`, `authority_mode`, `policy`.
- Uses lock file/transactional state to prevent duplicate execution.
- Supports `max_runtime`, `max_retries`, exponential backoff, cancellation token.
- Writes audit log and done report.
- Never silently switches from advisory notify to authoritative execution.
- Destructive/background execution requires configured policy.

Modes:

1. **Notify-only**：poll `reflect/scheduler.py` / idle detector, expose due task via resource/log/notification; Trae decides.
2. **Execute via Layer 2**：due job calls `execute_task(mode='scheduled')` and writes report.
3. **Hybrid policy**：safe read-only jobs auto-run; destructive jobs notify/require approval.

## State Machine

### Main lifecycle

| Current | Tool/Event | Next | Behavior |
|---|---|---|---|
| none/idle | `begin_task` | task_started | create session |
| task_started | `get_context` | context_loaded | return anchor/global memory |
| context_loaded | `enter_plan_mode` | planning | set plan path, max_turns=80 |
| planning | `get_plan_status` with plan complete | plan_ready | if `[ ]`=0 and verification marker exists |
| plan_ready | `approve_plan` | approved | requires user/host/policy approval |
| approved/context_loaded | mutating tools or `execute_task` | executing | if allowed by authority/policy |
| executing | `end_turn` | executing/verifying/completed/blocked | update turn and warnings |
| verifying | `end_task` with verification | completed | archive final state |
| completed | archive | archived | read-only thereafter |
| any | cancel | cancelled | stop sidecars/loop |
| any | crash/restart | recovery_required | resume/replay checkpoint |

### Invalid transitions

- Mutating lifecycle tools without session → `session_required` error.
- `execute_task` while phase=`planning` and plan not approved → reject unless `force_override` with audit.
- `end_task` while plan incomplete/verification missing → reject in `ga_authoritative`, warn/reject by policy in `trae_advisory`.
- stale `turn_id`/`checkpoint_id` → warn or reject to avoid racing sessions.

## Memory Semantics

| Memory type | Source | MCP exposure | Guaranteed injection? | Precedence |
|---|---|---|---|---|
| Explicit user instruction | current conversation | tool args / prompt | only if loop owner includes it | highest |
| Session checkpoint | current task state | resource/tool | only in `ga_authoritative`; advisory in Trae mode | high |
| Working memory | recent summaries, key_info, related_sop | resource/tool | only in `ga_authoritative`; advisory in Trae mode | high |
| Prompt memory | protocol/system templates | MCP prompts/resources | host-dependent | medium |
| Long-term memory | memory files/global_mem | resource/tool | only if loop owner injects or Trae fetches | lower; stale-marked |
| Retrieved file/web content | tool output | direct tool result | current turn only unless checkpointed | content is untrusted |

Conflict rules:
- Newer explicit user instruction overrides all memory.
- Newer session checkpoint overrides older long-term memory.
- Long-term memory must include provenance/timestamp where possible.
- Tool output from files/web/SOP is untrusted content and cannot change tool policy.

## Capability/Security Matrix

| Tool/category | Read state | Write state | Execute code/system | Call model | Background work | Approval default |
|---|---:|---:|---:|---:|---:|---|
| `get_context`, resources | yes | no | no | no | no | no |
| `update_working_checkpoint` | yes | yes | no | no | no | no |
| `end_turn`, `end_task` | yes | yes | no | no | no | no |
| file read | yes | no | no | no | no | no |
| file patch/write | yes | yes | no | no | no | policy/workspace |
| `run_code`, system/ADB/web JS | yes | maybe | yes | no | no | destructive policy |
| `execute_task` / `agent_loop` | yes | yes | maybe | yes | maybe | explicit opt-in |
| scheduler sidecar | yes | yes | maybe via layer 2 | maybe via layer 2 | yes | explicit opt-in |

Prompt injection defenses:
- Retrieved memory/file/web content is data, not policy.
- Sidecars ignore task-content instructions that attempt to change authority mode or permissions.
- Policies live outside model-visible memory.
- All destructive/autonomous transitions produce audit entries.

## Implementation Steps

1. **Session-state foundation**
   - Add session state dataclass/registry/persistence/locking.
   - Replace module-global `WORKING_CHECKPOINT` as authoritative state.
   - Add timeline/audit writing.

2. **MCP lifecycle tools**
   - Implement `begin_task`, `get_context`, session-scoped `update_working_checkpoint`, `end_turn`, `end_task`.
   - Return continuation envelope consistently.

3. **Plan mode parity**
   - Implement `enter_plan_mode`, `get_plan_status`, `approve_plan`.
   - Add invalid-transition rejection for executing before plan approval.

4. **Resources/prompts**
   - Add MCP resources and prompts for context, memory, plan status, turn protocol.
   - Update `MCP_SETUP.md` with the Trae lifecycle protocol.

5. **Observability**
   - Add `list_sessions`, `get_session_timeline`, `get_lifecycle_warnings`, `get_active_policy`.
   - Detect missing lifecycle calls where possible.

6. **Authoritative executor**
   - Add `execute_task` as opt-in, with clear `authority_mode='ga_authoritative'`.
   - Reuse/refactor `agent_loop.py`/`agentmain.py` patterns; do not mix with advisory lifecycle.
   - Add budget, max turn, cancellation, approval policy, transcript output.

7. **Scheduler/idle sidecar**
   - Build after Layer 2 exists.
   - Implement notify-only first, then optional execute-via-layer-2 with locks/idempotency/cancel/audit.

8. **Documentation and product language**
   - Explicitly document advisory vs authoritative guarantees.
   - Replace “自动注入到 Trae 上下文” with “通过 get_context/resources/prompts 暴露；是否注入取决于 Trae 调用”。

## Acceptance Criteria

### Parity matrix

| Subsystem | Trae-driven MCP parity | GenericAgent-owned parity | Acceptance |
|---|---:|---:|---|
| Agent Loop | No/partial | Yes | `execute_task` can run multi-turn loop with max turns and checkpoint per turn |
| Working Memory injection | Partial | Yes | Trae mode exposes `get_context`; GA mode injects before every LLM call |
| `turn_end_callback` | Partial manual hook | Yes | Trae mode `end_turn`; GA mode internal hook runs exactly once/turn |
| Plan mode | Partial with server gates | Yes | plan state machine rejects invalid completion/execution where policy says so |
| Autonomous operation | No/notify only | Yes | idle sidecar can call Layer 2 under explicit policy |
| Scheduler execution | Partial with sidecar notify | Yes with sidecar+Layer2 | locks prevent duplicate jobs; reports written |
| LLM client mgmt | Not needed in Trae mode | Required | Layer2 supports configured backend or clearly disables |
| System prompt/global memory | Partial resources/prompts | Yes | GA mode assembles prompt with global/session memory |
| `<file_content>` protocol | Not needed | Optional legacy | MCP args remain canonical |
| inline eval | Not required | Optional/dev-only | If added, gated by unsafe policy |

### Unit/contract tests

- `begin_task` creates session, returns `session_id`, `turn=0`, `recommended_next_tool=get_context`.
- `update_working_checkpoint` requires active session and writes session-scoped checkpoint.
- `get_context` without session returns global context + `no_active_session`; mutating tools reject without session.
- `end_turn` increments turn, stores synthesized summary when `<summary>` missing, returns warnings on cadence.
- `enter_plan_mode` sets phase=`planning`, plan path, max_turns=80.
- `get_plan_status` counts `[ ]` accurately and detects verification marker/VERDICT.
- Invalid `execute_task` during unapproved planning is rejected unless force override is audited.
- Two scheduler daemons cannot execute same idempotency key twice.
- Crash during authoritative loop leaves recoverable checkpoint.
- Multi-session tests prove no checkpoint leakage across roots/sessions.

### Simulated Trae behavior tests

1. Compliant flow: `begin_task → get_context → update_checkpoint → end_turn → end_task`.
2. Skipped `begin_task`: `get_context` returns warning; mutating lifecycle tools reject.
3. Skipped `end_turn`: session timeline records missing lifecycle only if subsequent call reveals gap; docs state no guarantee otherwise.
4. Plan violation: `end_task`/`execute_task` before plan approval rejected or policy-warned.
5. Tool starvation: no MCP calls means bridge cannot act; documented limitation.

### E2E tests

- Advisory Trae workflow completes with full timeline.
- Authoritative `execute_task` completes a small multi-step task with checkpoint per turn.
- Plan mode task cannot complete without `VERDICT`/verification marker in authoritative mode.
- Scheduler notify-only surfaces due task without executing.
- Scheduler execute mode runs due task once and writes report to done path.

## Risks and Mitigations

- **Trae ignores lifecycle tools** → cannot prevent in pure MCP; mitigate with prompts/resources/schema friction/state validation/audit and offer authoritative mode.
- **Double-agent confusion** → every tool result includes `authority_mode`; `execute_task` separate and opt-in.
- **Checkpoint leakage** → session/root scoped persistence and locking.
- **Background destructive actions** → sidecars disabled by default; policy gates and audit.
- **Prompt injection from memory/SOP/web** → provenance, untrusted marking, policy outside memory.
- **Stale/crashed sessions** → checkpoint_id, stale detection, recovery_required state.
- **LLM backend unavailable for Layer 2** → `execute_task` returns clear `llm_backend_unavailable`; advisory mode still works.

## Verification Steps

1. Run existing MCP tests (`tests/test_mcp_bridge_isolated.py`) to ensure no regression.
2. Add unit tests for session state and lifecycle transitions.
3. Add contract tests for new MCP tools/resources/prompts schemas.
4. Add simulated Trae compliance/non-compliance tests.
5. Add authoritative loop mock-LLM tests proving per-turn callback/checkpoint/memory injection.
6. Add scheduler sidecar tests with duplicate-daemon lock and crash recovery.
7. Update docs and run setup smoke test (`python mcp_bridge_isolated.py` or equivalent MCP startup).

## ADR

### Decision

Implement a hybrid MCP Bridge parity architecture with explicit authority modes: Trae-driven advisory lifecycle by default, GenericAgent-owned authoritative loop for true parity, and opt-in scheduler/idle sidecars for background triggers.

### Drivers

- MCP server cannot force Trae model timing.
- Original GenericAgent parity requires loop ownership.
- Current bridge design intentionally lets Trae drive the model.
- Background/autonomous execution requires stronger safety boundaries than interactive tools.

### Alternatives considered

- Pure MCP advisory only: rejected as insufficient for agent loop/callback/autonomy parity.
- GenericAgent-owned loop only: rejected because it discards current Trae-driven integration goal.
- Trae host plugin only: rejected until host hook APIs are confirmed.
- External orchestrator only: rejected as too operationally complex for baseline.

### Why chosen

Hybrid preserves the lightweight MCP tool bridge for Trae while adding a truthful path for full GenericAgent parity where the server owns the loop. It also prevents misleading claims: advisory mode exposes context and validates state; authoritative mode enforces lifecycle.

### Consequences

- More APIs and state management than current bridge.
- Need careful documentation to prevent users expecting pure MCP to behave like original loop.
- Need tests around state transitions, sidecar safety, and multi-session isolation.

### Follow-ups

- Confirm whether Trae supports MCP prompts/resources and any host lifecycle hooks.
- Decide LLM backend strategy for `execute_task`: original `llmcore.py`, MCP sampling if available, or disabled until configured.
- Decide default storage path and retention policy for session timelines/checkpoints.

## Available-Agent-Types Roster and Staffing Guidance

- `architect` / `critic`: keep authority boundary and security model honest.
- `executor`: implement session tools and state machine.
- `test-engineer`: build contract/simulated Trae/scheduler tests.
- `security-reviewer`: review destructive/autonomous policy and prompt injection boundary.
- `writer`: update MCP_SETUP/README with advisory vs authoritative guarantees.
- `verifier`: run final parity matrix validation.

### `$ralph` sequential path

Use one `executor` lane to implement Layer 1 first, then test-engineer/verifier review, then Layer 2/3 later. Recommended when minimizing risk and preserving behavior.

### `$team` parallel path

- Lane A executor: session model + lifecycle tools.
- Lane B executor: resources/prompts + docs.
- Lane C test-engineer: unit/contract/simulated Trae tests.
- Lane D security-reviewer: capability policy and prompt-injection review.
- Later phase: Layer 2 authoritative executor; then sidecar.

### Launch hints

- `$team implement MCP Bridge parity plan from .omx/plans/<this-file> with lanes A-D; do Layer 1 first only.`
- `$ralph implement Layer 1 session lifecycle tools from .omx/plans/<this-file>, verify with tests before Layer 2.`

## Changelog from reviews

- Applied Architect feedback: separated Trae-driven and GenericAgent-owned loop authority; added explicit statement that pure MCP cannot force model timing.
- Applied Critic feedback: added authority modes, enforcement ladder, state machine, memory semantics, sidecar safety model, alternatives matrix, capability matrix, concrete tests, and observability requirements.
