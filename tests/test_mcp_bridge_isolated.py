import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mcp.types import CallToolResult

import ga
import mcp_bridge_isolated as bridge


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
    async def _fake_request_cwd(self):
        return os.getcwd()

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
        with patch.object(bridge, "get_request_cwd", self._fake_request_cwd), \
             patch.object(bridge, "execute_code", return_value=fake):
            result = await bridge.call_tool("run_code", {"code": "print('x')"})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "done")
        self.assertIn("cwd", result)

    async def test_ask_user_returns_pending_interaction_contract(self):
        with patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
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
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                result = await bridge.call_tool("execute_sop", {"name": "plan", "params": {"x": 1}})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["execution_mode"], "describe_only")
        self.assertNotIn("请根据以上 SOP 执行相应操作", result["content"])

    async def test_list_tools_exposes_annotations_and_output_schema(self):
        tools = await bridge.list_tools()
        run_code_tool = next(t for t in tools if t.name == "run_code")
        ask_user_tool = next(t for t in tools if t.name == "ask_user")
        write_memory_tool = next(t for t in tools if t.name == "write_memory")
        read_memory_tool = next(t for t in tools if t.name == "read_memory")

        self.assertIsNotNone(run_code_tool.outputSchema)
        self.assertTrue(run_code_tool.annotations.destructiveHint)
        self.assertEqual(ask_user_tool.outputSchema["properties"]["status"]["enum"], ["awaiting_user_input"])
        self.assertNotIn("scope", write_memory_tool.inputSchema["properties"])
        self.assertEqual(write_memory_tool.inputSchema["properties"]["mode"]["enum"], ["patch", "append"])
        self.assertEqual(write_memory_tool.inputSchema["properties"]["mode"]["default"], "patch")
        self.assertEqual(read_memory_tool.inputSchema["properties"]["memory_type"]["default"], "all")
        tool_names = {t.name for t in tools}
        self.assertFalse(any(t.name in {"get_project_context", "list_project_memories"} for t in tools))
        self.assertTrue({"read_file", "patch_file", "write_file", "update_working_checkpoint", "start_long_term_update"}.issubset(tool_names))

    def _temp_original_memory_paths(self, tmpdir):
        root = bridge.Path(tmpdir)
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

    async def test_file_tools_read_and_patch_like_ga(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "demo.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("hello world\nsecond line")

            async def fake_cwd():
                return tmpdir

            with patch.object(bridge, "get_request_cwd", fake_cwd):
                read_result = await bridge.call_tool("read_file", {"path": "demo.txt", "keyword": "hello"})
                patch_result = await bridge.call_tool(
                    "patch_file",
                    {"path": "demo.txt", "old_content": "hello world", "new_content": "hello MCP"},
                )

            self.assertEqual(read_result["status"], "success")
            self.assertIn("hello world", read_result["content"])
            self.assertEqual(patch_result["status"], "success")
            with open(target, encoding="utf-8") as f:
                self.assertIn("hello MCP", f.read())

    async def test_working_and_long_term_memory_workflow_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sop_dir = os.path.join(tmpdir, "memory")
            os.makedirs(sop_dir, exist_ok=True)
            with open(os.path.join(sop_dir, "memory_management_sop.md"), "w", encoding="utf-8") as f:
                f.write("No Execution, No Memory")

            with patch.object(bridge, "SOP_DIR", sop_dir), \
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                checkpoint = await bridge.call_tool(
                    "update_working_checkpoint",
                    {"key_info": "重要约束", "related_sop": "memory_management_sop"},
                )
                long_term = await bridge.call_tool("start_long_term_update", {})

            self.assertEqual(checkpoint["status"], "success")
            self.assertEqual(checkpoint["checkpoint"]["key_info"], "重要约束")
            self.assertEqual(long_term["status"], "success")
            self.assertIn("No Execution, No Memory", long_term["content"])
            self.assertEqual(long_term["default_write_mode"], "patch")

    async def test_write_memory_uses_original_global_l2_append_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._temp_original_memory_paths(tmpdir)
            paths["l2"].parent.mkdir(parents=True, exist_ok=True)
            paths["l2"].write_text("# [Global Memory - L2]", encoding="utf-8")

            with patch.object(bridge, "original_memory_paths", lambda base_dir=None: paths), \
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                result = await bridge.call_tool("write_memory", {"content": "已验证的长期事实", "mode": "append"})

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["memory_model"], "original_four_level")
            self.assertEqual(result["path"], str(paths["l2"]))
            self.assertIn("---", paths["l2"].read_text(encoding="utf-8"))
            self.assertIn("已验证的长期事实", paths["l2"].read_text(encoding="utf-8"))

    async def test_write_memory_rejects_overwrite_for_original_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._temp_original_memory_paths(tmpdir)
            with patch.object(bridge, "original_memory_paths", lambda base_dir=None: paths), \
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                result = await bridge.call_tool(
                    "write_memory",
                    {"content": "不能覆盖", "mode": "overwrite"},
                )

            self.assertIsInstance(result, CallToolResult)
            self.assertTrue(result.isError)
            self.assertIn("不支持 overwrite", result.content[0].text)

    async def test_read_memory_returns_original_four_layers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._temp_original_memory_paths(tmpdir)
            paths["l1"].parent.mkdir(parents=True, exist_ok=True)
            paths["l1"].write_text("L1 index", encoding="utf-8")
            paths["l2"].write_text("L2 facts", encoding="utf-8")
            (paths["l3"] / "demo_sop.md").write_text("demo sop", encoding="utf-8")
            paths["l4"].mkdir(parents=True, exist_ok=True)
            (paths["l4"] / "session.txt").write_text("raw", encoding="utf-8")

            with patch.object(bridge, "original_memory_paths", lambda base_dir=None: paths), \
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                result = await bridge.call_tool("read_memory", {})

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["memory_model"], "original_four_level")
            self.assertIn("L1_global_mem_insight", result["layers"])
            self.assertIn("L2_global_mem", result["layers"])
            self.assertIn("L3_memory_index", result["layers"])
            self.assertIn("L4_raw_sessions_index", result["layers"])
            self.assertEqual(result["layers"]["L1_global_mem_insight"]["content"], "L1 index")
            self.assertEqual(result["layers"]["L2_global_mem"]["content"], "L2 facts")

    async def test_write_memory_defaults_to_patch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._temp_original_memory_paths(tmpdir)
            paths["l2"].parent.mkdir(parents=True, exist_ok=True)
            paths["l2"].write_text("alpha beta", encoding="utf-8")

            with patch.object(bridge, "original_memory_paths", lambda base_dir=None: paths), \
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                result = await bridge.call_tool(
                    "write_memory",
                    {
                        "memory_type": "global_mem",
                        "old_content": "alpha",
                        "new_content": "omega",
                    },
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["mode"], "patch")
            self.assertEqual(paths["l2"].read_text(encoding="utf-8"), "omega beta")

    async def test_write_memory_patch_updates_unique_original_memory_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._temp_original_memory_paths(tmpdir)
            paths["l1"].parent.mkdir(parents=True, exist_ok=True)
            paths["l1"].write_text("RULES:\n- old rule\n", encoding="utf-8")

            with patch.object(bridge, "original_memory_paths", lambda base_dir=None: paths), \
                 patch.object(bridge, "get_request_cwd", self._fake_request_cwd):
                result = await bridge.call_tool(
                    "write_memory",
                    {
                        "memory_type": "global_mem_insight",
                        "mode": "patch",
                        "old_content": "- old rule",
                        "new_content": "- new rule",
                    },
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["mode"], "patch")
            self.assertIn("- new rule", paths["l1"].read_text(encoding="utf-8"))
            self.assertNotIn("- old rule", paths["l1"].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
