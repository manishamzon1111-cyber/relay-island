"""Security boundary regression checks for the optional WebRTC browser lab."""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "browser-lab" / "compose.yaml"
LAUNCHER = ROOT / "browser-lab" / "browser_lab.py"
IGNORE = ROOT / ".gitignore"


class BrowserLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = COMPOSE.read_text(encoding="utf-8")
        cls.source = LAUNCHER.read_text(encoding="utf-8")

    def test_launcher_compiles(self):
        ast.parse(self.source)

    def test_no_lan_or_public_port_bindings(self):
        ports = [line.strip().strip("- ").strip('"') for line in self.compose.splitlines()
                 if "18080:" in line or "52000-" in line and "/udp" in line]
        self.assertEqual(len(ports), 2, ports)
        self.assertTrue(all(port.startswith("127.0.0.1:") for port in ports), ports)
        self.assertNotIn('0.0.0.0:', self.compose)

    def test_no_default_passwords_or_privileged_host_mounts(self):
        self.assertIn("RELAY_BROWSER_USER:?", self.compose)
        self.assertIn("RELAY_BROWSER_ADMIN:?", self.compose)
        self.assertIn("no-new-privileges:true", self.compose)
        self.assertNotIn("privileged: true", self.compose)
        self.assertNotIn("/var/run/docker.sock", self.compose)
        self.assertNotIn("volumes:", self.compose)
        self.assertIn("can_access_clipboard", self.compose)
        self.assertIn("can_share_media", self.compose)

    def test_no_automatic_restoration_after_restart(self):
        self.assertIn('restart: "no"', self.compose)

    def test_generated_password_file_is_ignored(self):
        self.assertIn("browser-lab/.env", IGNORE.read_text(encoding="utf-8"))
        self.assertIn("O_EXCL", self.source)
        self.assertIn("0o600", self.source)

    def test_no_tablet_lan_flag(self):
        self.assertNotIn("lan-host", self.source)
        self.assertNotIn("--expose", self.source)


if __name__ == "__main__":
    unittest.main()
