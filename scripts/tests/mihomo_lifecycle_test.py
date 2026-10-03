#!/usr/bin/env python3
"""Offline lifecycle contracts: only disposable processes, no sockets or network."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

RUNTIME = Path(__file__).resolve().parents[1] / "mihomo_runtime.sh"


class LifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mihomo-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir()
        self.ready = self.root / "ready"
        self.spawned = self.root / "spawned"
        self.requests = self.root / "requests.jsonl"
        self.binary = self.bin / "mihomo"
        self.write_executable(self.binary, "#!/usr/bin/env bash\nexit 0\n")
        self.write_executable(self.bin / "nohup", '''#!/usr/bin/env python3
import os, signal, time
from pathlib import Path
Path(os.environ['TEST_SPAWNED']).write_text(str(os.getpid()))
if os.environ.get('TEST_IGNORE_TERM') == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(.35)
Path(os.environ['TEST_READY']).write_text('executed')
time.sleep(60)
''')
        self.write_executable(self.bin / "curl", '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
secret = os.environ['MIHOMO_API_SECRET']
assert not any(secret in arg for arg in args), 'secret in argv'
header = Path(args[args.index('--header') + 1][1:])
assert header.stat().st_mode & 0o777 == 0o600, 'header permissions'
assert header.read_text() == 'Authorization: Bearer ' + secret + '\\n', 'wrong auth'
with open(os.environ['TEST_REQUESTS'], 'a') as log:
    log.write(json.dumps({'file': str(header), 'timeout': float(args[args.index('--max-time') + 1])}) + '\\n')
if os.environ.get('TEST_CURL_MODE') == 'slow-fail':
    time.sleep(float(args[args.index('--max-time') + 1]))
    sys.exit(22)
print('{"version":"synthetic"}')
''')
        config = self.root / "config.yaml"
        config.write_text("proxies: []\n")
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(self.root), MIHOMO_BINARY=str(self.binary),
                        MIHOMO_CONFIG=str(config), MIHOMO_CONFIG_DIR=str(self.root),
                        MIHOMO_RUNTIME_DIR=str(self.runtime_dir), MIHOMO_LOG=str(self.root / "test.log"),
                        MIHOMO_API_SECRET="synthetic-contract-secret", MIHOMO_START_TIMEOUT="3",
                        MIHOMO_STOP_TIMEOUT="1", TEST_RUNTIME=str(RUNTIME), TEST_READY=str(self.ready),
                        TEST_SPAWNED=str(self.spawned), TEST_REQUESTS=str(self.requests))
        self.addCleanup(self.cleanup_child)

    @staticmethod
    def write_executable(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def cleanup_child(self):
        if self.spawned.exists():
            try:
                os.kill(int(self.spawned.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def launch(self):
        # Replace only the /proc adapter to make this contract portable to macOS.
        # The real startup loop, signal handling, curl auth and cleanup all run.
        command = '''source "$TEST_RUNTIME" binary >/dev/null
pid_is_mihomo() { [[ -f "$TEST_READY" ]]; }
command_start
'''
        return subprocess.Popen(["bash", "-c", command], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def assert_headers_removed(self):
        records = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertTrue(records)
        for record in records:
            self.assertFalse(Path(record["file"]).exists())
        self.assertFalse(list(self.root.glob("clash-api-auth.*")))
        return records

    def assert_child_reaped(self):
        self.assertTrue(self.spawned.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(self.spawned.read_text()), 0)
        self.assertFalse((self.runtime_dir / "mihomo.pid").exists())

    def test_exec_grace_and_header_secrecy(self):
        started = time.monotonic()
        process = self.launch()
        stdout, stderr = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertGreaterEqual(time.monotonic() - started, .3)
        self.assertIn("Mihomo started", stdout)
        self.assertTrue((self.runtime_dir / "mihomo.pid").exists())
        self.assertNotIn(self.env["MIHOMO_API_SECRET"], stdout + stderr)
        self.assert_headers_removed()

    def test_wall_clock_timeout_kills_and_reaps_term_ignoring_child(self):
        self.env.update(MIHOMO_START_TIMEOUT="2", TEST_CURL_MODE="slow-fail", TEST_IGNORE_TERM="1")
        started = time.monotonic()
        process = self.launch()
        stdout, stderr = process.communicate(timeout=6)
        elapsed = time.monotonic() - started
        self.assertNotEqual(process.returncode, 0)
        self.assertLess(elapsed, 4.8, "startup must use wall time, including slow API calls")
        self.assertIn("within 2s", stderr)
        self.assert_child_reaped()
        records = self.assert_headers_removed()
        self.assertTrue(all(record["timeout"] <= 2 for record in records))

    def test_termination_during_startup_reaps_owned_child(self):
        self.env.update(TEST_CURL_MODE="slow-fail", TEST_IGNORE_TERM="1")
        process = self.launch()
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 3
        while not self.requests.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.requests.exists())
        process.terminate()
        process.communicate(timeout=7)
        self.assertNotEqual(process.returncode, 0)
        self.assert_child_reaped()
        self.assert_headers_removed()

    def test_stored_unrelated_pid_is_never_signalled(self):
        other = subprocess.Popen(["sleep", "60"])
        def cleanup():
            if other.poll() is None:
                other.terminate()
            other.wait(timeout=3)
        self.addCleanup(cleanup)
        (self.runtime_dir / "mihomo.pid").write_text(str(other.pid) + "\n")
        result = subprocess.run(["bash", str(RUNTIME), "stop"], env=self.env,
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to signal unrelated", result.stderr)
        self.assertIsNone(other.poll())


if __name__ == "__main__":
    unittest.main(verbosity=2)
