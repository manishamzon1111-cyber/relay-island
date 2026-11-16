"""Fail-closed static checks for the optional native Lumen stream launcher."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "native-stream" / "run.py"
DOC = ROOT / "native-stream" / "README.md"


class NativeStreamChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = SCRIPT.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.code)
        cls.readme = DOC.read_text(encoding="utf-8")

    def test_compiles(self):
        compile(self.tree, str(SCRIPT), "exec")

    def test_requires_interface_host_and_selected_window(self):
        self.assertIn('p.add_argument("--host", required=True', self.code)
        self.assertIn('p.add_argument("--interface", required=True', self.code)
        self.assertIn('p.add_argument("--window", required=True', self.code)

    def test_checks_network_before_and_during_stream(self):
        self.assertGreaterEqual(self.code.count('check(a.host, a.interface)'), 2)
        self.assertIn("LAN CHECK FAILED; stopping Lumen", self.code)

    def test_does_not_advertise_remote_input_or_auto_approval(self):
        self.assertIn('["serve",', self.code.replace('lumen, "serve",', '["serve",'))
        self.assertNotIn('"--auto-accept"', self.code)
        self.assertNotIn('"--allow-lan-admin"', self.code)
        self.assertNotIn('"--bind", "0.0.0.0"', self.code)
        self.assertNotIn('"--bind", "127.0.0.1"', self.code)
        self.assertIn('"--window"', self.code)

    def test_does_not_use_docker(self):
        self.assertNotIn('"docker"', self.code)
        self.assertNotIn('import docker', self.code)
        self.assertIn("view-only", self.readme.lower())


if __name__ == "__main__":
    unittest.main()
