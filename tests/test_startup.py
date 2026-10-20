"""Port conflict and launcher reuse checks, with isolated data and no AI calls."""
import errno
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

from app.server import make_server

ROOT = Path(__file__).resolve().parents[1]


def request(url, path, data=None, token=None):
    headers = {"Origin": url}
    if token:
        headers["X-Notes-Token"] = token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=4) as response:
        return json.load(response)


class OtherProject(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404 if self.path == "/api/health" else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"app":"another-project"}')

    def log_message(self, *args):
        pass


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="diary startup ")
        self.root = Path(self.temp.name).resolve()
        self.other = HTTPServer(("127.0.0.1", 0), OtherProject)
        self.thread = threading.Thread(target=self.other.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.other.server_port
        self.other_url = "http://127.0.0.1:" + str(self.port)

    def tearDown(self):
        self.other.shutdown()
        self.other.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_occupied_port_falls_back_without_disrupting_the_other_project(self):
        server = make_server(self.root / "data", self.port, fallback_port=True)
        try:
            self.assertNotEqual(server.server_port, self.port)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertEqual(request(self.other_url, "/")["app"], "another-project")
        finally:
            server.server_close()

    def test_explicit_port_without_fallback_fails_before_opening_diary_data(self):
        data = self.root / "unopened"
        with self.assertRaises(OSError):
            make_server(data, self.port)
        self.assertFalse(data.exists())
        self.assertEqual(request(self.other_url, "/")["app"], "another-project")

    def test_unrelated_socket_errors_are_not_treated_as_port_conflicts(self):
        with patch("app.server.LocalServer", side_effect=OSError(errno.EMFILE, "synthetic descriptor limit")) as construct:
            with self.assertRaises(OSError):
                make_server(self.root / "unopened", self.port, fallback_port=True)
            self.assertEqual(construct.call_count, 1)

    def test_data_initialization_failure_closes_the_socket_without_retrying(self):
        with patch("app.server.LocalServer") as construct, patch("app.server.Application", side_effect=PermissionError("synthetic data access failure")):
            with self.assertRaises(PermissionError):
                make_server(self.root / "unopened", self.port, fallback_port=True)
            self.assertEqual(construct.call_count, 1)
            construct.return_value.server_close.assert_called_once()

    def stop_process(self, process, url=None):
        if process.poll() is not None:
            return
        if url:
            try:
                token = request(url, "/api/bootstrap")["token"]
                request(url, "/api/shutdown", {}, token)
                process.wait(timeout=6)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        process.terminate()
        process.wait(timeout=6)

    def test_cli_publishes_the_actual_port_and_preserves_loopback_guards(self):
        data = self.root / "data"
        process = subprocess.Popen([sys.executable, str(ROOT / "app" / "server.py"), "--port", str(self.port),
                                    "--fallback-port", "--data-dir", str(data)], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        url = None
        try:
            for _ in range(80):
                self.assertIsNone(process.poll(), "The isolated server exited before becoming ready")
                try:
                    runtime = json.loads((data / "runtime.json").read_text(encoding="utf-8"))
                    url = "http://127.0.0.1:" + str(runtime["port"])
                    health = request(url, "/api/health")
                    break
                except (OSError, ValueError):
                    time.sleep(.1)
            else:
                self.fail("The isolated server did not become ready")
            self.assertEqual(runtime["pid"], process.pid)
            self.assertNotEqual(runtime["port"], self.port)
            self.assertEqual(health["app"], "shiguang-diary")
            self.assertEqual(Path(request(url, "/api/bootstrap")["settings"]["data_dir"]), data)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(urllib.request.Request(url + "/api/health", headers={"Origin": self.other_url}), timeout=4)
            self.assertEqual(denied.exception.code, 403)
            self.assertEqual(request(self.other_url, "/")["app"], "another-project")
        finally:
            self.stop_process(process, url)

    @unittest.skipUnless(os.name == "nt", "Windows desktop launcher")
    def test_windows_launcher_prefers_the_previous_address_when_it_is_free(self):
        checkout = self.root / "previous address"
        checkout.mkdir()
        shutil.copyfile(ROOT / "launch.ps1", checkout / "launch.ps1")
        shutil.copytree(ROOT / "app", checkout / "app", ignore=shutil.ignore_patterns("__pycache__"))
        data = checkout / "data"
        data.mkdir()
        with socket.socket() as vacant:
            vacant.bind(("127.0.0.1", 0))
            previous_port = vacant.getsockname()[1]
        (data / "runtime.json").write_text(json.dumps({"app": "shiguang-diary", "port": previous_port, "pid": 1}), encoding="utf-8")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(checkout / "launch.ps1"),
                                 "-NoBrowser", "-PreferredPort", str(self.port)], cwd=checkout, capture_output=True,
                                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        url = None
        try:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            url = json.loads(result.stdout)["url"]
            self.assertEqual(url, "http://127.0.0.1:" + str(previous_port))
            self.assertEqual(request(self.other_url, "/")["app"], "another-project")
        finally:
            if url:
                boot = request(url, "/api/bootstrap")
                self.assertEqual(Path(boot["settings"]["data_dir"]), data)
                launched_pid = json.loads((data / "runtime.json").read_text(encoding="utf-8"))["pid"]
                request(url, "/api/shutdown", {}, boot["token"])
                subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                "Wait-Process -Id " + str(launched_pid) + " -Timeout 10 -ErrorAction SilentlyContinue"],
                               capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(50):
                    try:
                        request(url, "/api/health")
                        time.sleep(.1)
                    except OSError:
                        break

    @unittest.skipUnless(os.name == "nt", "Windows desktop launcher")
    def test_concurrent_windows_launchers_reuse_one_instance_despite_stale_runtime_and_404(self):
        checkout = self.root / "副本 with spaces"
        checkout.mkdir()
        shutil.copyfile(ROOT / "launch.ps1", checkout / "launch.ps1")
        shutil.copytree(ROOT / "app", checkout / "app", ignore=shutil.ignore_patterns("__pycache__"))
        data = checkout / "data"
        data.mkdir()
        (data / "runtime.json").write_text(json.dumps({"app": "shiguang-diary", "port": self.port, "pid": 1}), encoding="utf-8")
        import ctypes
        short_name = ctypes.create_unicode_buffer(32768)
        short_size = ctypes.windll.kernel32.GetShortPathNameW(str(checkout), short_name, len(short_name))
        launcher = Path(short_name.value) / "launch.ps1" if 0 < short_size < len(short_name) else checkout / "launch.ps1"
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
                   "-NoBrowser", "-PreferredPort", str(self.port)]
        processes = [subprocess.Popen(command, cwd=checkout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(2)]
        url = None
        try:
            outputs = []
            for process in processes:
                output, error = process.communicate(timeout=40)
                self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
                outputs.append(json.loads(output)["url"])
            self.assertEqual(outputs[0], outputs[1])
            url = outputs[0]
            self.assertNotEqual(url, self.other_url)
            runtime = json.loads((data / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(url, "http://127.0.0.1:" + str(runtime["port"]))
            repeated = subprocess.run(command, cwd=checkout, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(repeated.returncode, 0, repeated.stderr.decode(errors="replace"))
            self.assertEqual(json.loads(repeated.stdout)["url"], url)
            self.assertEqual(json.loads((data / "runtime.json").read_text(encoding="utf-8"))["pid"], runtime["pid"])
            self.assertEqual(request(self.other_url, "/")["app"], "another-project")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)
            if url is None:
                try:
                    runtime = json.loads((data / "runtime.json").read_text(encoding="utf-8"))
                    if runtime.get("pid") != 1:
                        url = "http://127.0.0.1:" + str(runtime["port"])
                except (OSError, ValueError):
                    pass
            if url:
                boot = request(url, "/api/bootstrap")
                self.assertEqual(Path(boot["settings"]["data_dir"]), data)
                launched_pid = json.loads((data / "runtime.json").read_text(encoding="utf-8"))["pid"]
                request(url, "/api/shutdown", {}, boot["token"])
                subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                "Wait-Process -Id " + str(launched_pid) + " -Timeout 10 -ErrorAction SilentlyContinue"],
                               capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(50):
                    try:
                        request(url, "/api/health")
                        time.sleep(.1)
                    except OSError:
                        break


if __name__ == "__main__":
    unittest.main()
