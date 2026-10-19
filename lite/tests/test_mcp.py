import io
import json
import os
import tempfile
import unittest

from srtforge_lite.mcp_server import McpServer
from srtforge_lite.server import App
from tests.test_server import FakeTranslator, fake_runner


class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sent = []
        cls.srv = McpServer(App(runner=fake_runner, translator=FakeTranslator()), cls.sent.append)

    def rpc(self, method, params=None, mid=1):
        return self.srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_handshake_and_tool_list(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(init["serverInfo"]["name"], "srt-forge")
        self.assertIn("tools", init["capabilities"])
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(sorted(names), ["languages", "system_check", "transcribe"])
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)

    def test_transcribe_saves_next_to_original_and_reports(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "kalba.wav")
        open(src, "wb").write(b"garsas")
        reply = self.rpc("tools/call", {"name": "transcribe", "arguments": {
            "path": src, "language": "lt", "model": "base", "outputs": ["srt", "txt"], "targets": ["en"], "style": "tv"}, "_meta": {"progressToken": "p1"}})
        self.assertNotIn("isError", reply["result"], reply)
        info = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(sorted(os.path.basename(p) for p in info["saved"]), ["kalba.en.srt", "kalba.en.txt", "kalba.srt", "kalba.txt"])
        self.assertEqual(info["translations"], ["en"])
        self.assertTrue(os.path.exists(src))
        self.assertIn("Labas", info["preview"])

    def test_bad_input_is_a_tool_error_not_a_crash(self):
        for args in ({"path": "reliatyvus.wav"}, {"path": "/nera/tokio.wav"}):
            self.assertTrue(self.rpc("tools/call", {"name": "transcribe", "arguments": args})["result"]["isError"])
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(b"x")
        f.close()
        bad = self.rpc("tools/call", {"name": "transcribe", "arguments": {"path": f.name, "style": "zz"}})["result"]
        self.assertTrue(bad["isError"])
        self.assertEqual(self.rpc("tools/call", {"name": "nera"})["error"]["code"], -32602)

    def test_system_check_and_languages(self):
        self.assertIn("recommended", self.rpc("tools/call", {"name": "system_check"})["result"]["content"][0]["text"])
        self.assertIn("lv", self.rpc("tools/call", {"name": "languages"})["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
