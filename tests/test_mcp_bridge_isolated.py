import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mcp.types import CallToolResult

import ga
import mcp_bridge_isolated as bridge
from memory.project_context import ProjectContext, ProjectContextManager


class TestExecuteCode(unittest.TestCase):
    def test_execute_code_default_cwd_no_crash(self):
        result = ga.execute_code("print('hello')", quiet=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["stdout"])
        self.assertIn("Running python", result["transcript"])

    def test_execute_code_quiet_suppresses_stdout(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = ga.execute_code("print('quiet-test')", quiet=True)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(result["status"], "success")

    def test_execute_code_honors_stop_signal_for_silent_process(self):
        stop_signal = []
        with tempfile.TemporaryDirectory() as tmpdir:
            script = os.path.join(tmpdir, "stop_me.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write("import time\nfor _ in range(50):\n    time.sleep(0.1)\n")

            def trigger_stop():
                import time
                time.sleep(0.2)
                stop_signal.append(True)

            import threading
            worker = threading.Thread(target=trigger_stop, daemon=True)
            worker.start()
            result = ga.execute_code(f"exec(open(r'{script}', encoding='utf-8').read())", quiet=True, stop_signal=stop_signal)
            worker.join(timeout=1)

        self.assertTrue(result["stopped"])
        self.assertEqual(result["status"], "error")


class TestMcpBridgeIsolated(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project_ctx = ProjectContext(
            project_id="test_project",
            project_name="test_project",
            project_root=os.getcwd(),
            project_type="python",
        )
        self.project_manager = ProjectContextManager(base_memory_dir=os.getcwd())

    async def _fake_request_context(self):
        return self.project_manager, os.getcwd(), self.project_ctx

    async def test_unknown_tool_returns_error_result(self):
        result = await bridge.call_tool("not_a_real_tool", {})
        self.assertIsInstance(result, CallToolResult)
        self.assertTrue(result.isError)
        self.assertIn("未注册", result.content[0].text)

    async def test_run_code_returns_structured_result(self):
        fake = {
            "status": "success",
            "exit_code": 0,
            "stdout": "done",
            "timed_out": False,
            "stopped": False,
        }
        with patch.object(bridge, "get_request_project_context", self._fake_request_context), \
             patch.object(bridge, "execute_code", return_value=fake):
            result = await bridge.call_tool("run_code", {"code": "print('x')"})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "done")
        self.assertIn("cwd", result)

    async def test_ask_user_returns_pending_interaction_contract(self):
        with patch.object(bridge, "get_request_project_context", self._fake_request_context):
            result = await bridge.call_tool("ask_user", {"question": "继续吗？", "candidates": ["是", "否"]})

        self.assertEqual(result["status"], "awaiting_user_input")
        self.assertEqual(result["interaction"], "elicitation_required")
        self.assertEqual(result["question"], "继续吗？")

    async def test_execute_sop_returns_describe_only_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sop_path = os.path.join(tmpdir, "plan_sop.md")
            with open(sop_path, "w", encoding="utf-8") as f:
                f.write("# Demo SOP\nDo something carefully.")

            with patch.object(bridge, "SOP_DIR", tmpdir), \
                 patch.object(bridge, "get_request_project_context", self._fake_request_context):
                result = await bridge.call_tool("execute_sop", {"name": "plan", "params": {"x": 1}})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["execution_mode"], "describe_only")
        self.assertNotIn("请根据以上 SOP 执行相应操作", result["content"])

    async def test_list_tools_exposes_annotations_and_output_schema(self):
        tools = await bridge.list_tools()
        run_code_tool = next(t for t in tools if t.name == "run_code")
        ask_user_tool = next(t for t in tools if t.name == "ask_user")

        self.assertIsNotNone(run_code_tool.outputSchema)
        self.assertTrue(run_code_tool.annotations.destructiveHint)
        self.assertEqual(ask_user_tool.outputSchema["properties"]["status"]["enum"], ["awaiting_user_input"])


if __name__ == "__main__":
    unittest.main()
