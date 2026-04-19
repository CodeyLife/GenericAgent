# Context Snapshot: MCP Bridge Agent Loop Parity

## Task statement
Design how to implement missing original GenericAgent subsystems in the MCP Bridge and how to make Trae's model call them at the right time.

## Desired outcome
A consensus architecture plan, not implementation, covering agent loop, working memory injection, turn-end callback, plan mode, autonomous operation, scheduler execution, prompt/global memory, and tool-call guidance for Trae.

## Known facts / evidence
- agent_loop.py defines `agent_runner_loop(...)` with multi-turn LLM/tool loop up to max_turns=40.
- ga.py implements `_get_anchor_prompt`, plan mode, completion interception, summary extraction, retry warnings, `_keyinfo`/`_intervene`, done hooks, and `get_global_memory`.
- mcp_bridge_isolated.py currently exposes mostly stateless MCP tools; `WORKING_CHECKPOINT` is only updated by `update_working_checkpoint` and not injected automatically.
- reflect/scheduler.py has task execution eligibility logic; current MCP bridge likely exposes CRUD-like behavior only.
- Trae owns the LLM loop in MCP mode, so bridge cannot force tool choice unless it exposes protocol/tool affordances or an explicit orchestrator tool.

## Constraints
- Avoid pretending MCP server can mutate Trae's hidden system prompt after initialization.
- Separate original in-process agent semantics from MCP client-driven orchestration.
- Preserve MCP-native design where Trae can remain orchestrator, but offer stronger GenericAgent-owned loop when requested.

## Unknowns / open questions
- Exact Trae support for MCP roots/prompts/resources/sampling/elicitation may vary by version.
- Whether the project wants MCP Bridge to call external LLM APIs itself or remain model-free by default.

## Likely codebase touchpoints
- mcp_bridge_isolated.py
- mcp_server.py
- agent_loop.py
- ga.py
- reflect/scheduler.py
- reflect/autonomous.py
- memory/*_sop.md
- MCP_SETUP.md / README.md
