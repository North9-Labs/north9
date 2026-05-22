"""Integration tests for Cage — require Docker to be running.

Run with: pytest tests/test_integration.py -v -m integration
Skip with: pytest tests/ -m "not integration"
"""

from __future__ import annotations

import subprocess
import time

import pytest

import north9
from north9.sandbox.core import CageError

# ── Fixtures ──────────────────────────────────────────────────────────────────

def docker_available() -> bool:
    result = subprocess.run(["docker", "info"], capture_output=True)
    return result.returncode == 0


pytestmark = pytest.mark.integration

if not docker_available():
    pytest.skip("Docker not available", allow_module_level=True)


@pytest.fixture(scope="module")
def env():
    """One Sandbox shared across the module — faster than per-test containers."""
    with north9.Sandbox(
        image="python:3.12-slim",
        network="bridge",   # need internet for pip installs in some tests
        pull=True,
        compress=False,     # deterministic output in tests
    ) as session:
        yield session


@pytest.fixture()
def fresh_env(tmp_path):
    """Per-test Sandbox with isolated workspace."""
    with north9.Sandbox(
        image="python:3.12-slim",
        network="bridge",
        workspace_dir=tmp_path,
        pull=False,
        compress=False,
    ) as session:
        yield session


# ── Container lifecycle ───────────────────────────────────────────────────────

class TestContainerLifecycle:
    def test_session_starts_and_runs(self):
        with north9.Sandbox(image="python:3.12-slim", pull=True, compress=False) as s:
            result = s.run("echo alive")
            assert result.success
            assert result.stdout == "alive"

    def test_session_closed_after_context_exit(self):
        with north9.Sandbox(image="python:3.12-slim", pull=False, compress=False) as s:
            name = s.name
        # Container should be gone
        result = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
        )
        assert result.returncode != 0

    def test_workspace_dir_created_on_host(self, tmp_path):
        ws = tmp_path / "my-workspace"
        with north9.Sandbox(workspace_dir=ws, pull=False, compress=False) as s:
            assert s.workspace_path.exists()
            assert s.workspace_path.is_dir()

    def test_workspace_persists_after_close(self, tmp_path):
        ws = tmp_path / "persist-ws"
        with north9.Sandbox(workspace_dir=ws, pull=False, compress=False) as s:
            s.run("echo 'hello world' > /workspace/hello.txt")
        # Container gone but workspace file remains
        assert (ws / "hello.txt").exists()


# ── Command execution ─────────────────────────────────────────────────────────

class TestRun:
    def test_echo_returns_output(self, env):
        result = env.run("echo hello")
        assert result.success
        assert result.stdout == "hello"

    def test_exit_code_propagated(self, env):
        result = env.run("exit 42", timeout=5)
        assert result.exit_code == 42
        assert not result.success

    def test_stderr_captured(self, env):
        result = env.run("echo error >&2")
        assert "error" in result.stderr

    def test_multiline_output(self, env):
        result = env.run("printf 'a\\nb\\nc'")
        assert result.success
        lines = result.stdout.splitlines()
        assert lines == ["a", "b", "c"]

    def test_python_runs(self, env):
        result = env.run("python3 -c \"print('from python')\"")
        assert result.success
        assert "from python" in result.stdout

    def test_timeout_kills_command(self, fresh_env):
        result = fresh_env.run("sleep 60", timeout=2)
        assert result.exit_code == 124
        assert "timed out" in result.stderr

    def test_custom_workdir(self, env):
        result = env.run("pwd", workdir="/tmp")
        assert result.stdout.strip() == "/tmp"

    def test_env_isolation(self, env):
        # SECRET should not be in container env unless explicitly passed
        result = env.run("echo ${MY_SECRET_VAR:-not_set}")
        assert "not_set" in result.stdout

    def test_host_filesystem_untouched(self, env, tmp_path):
        # Write a sentinel file on host
        sentinel = tmp_path / "sentinel.txt"
        sentinel.write_text("safe")
        # Agent can't reach it
        result = env.run(f"cat {sentinel}")
        assert not result.success


# ── File operations ───────────────────────────────────────────────────────────

class TestFileOps:
    def test_write_and_read_file(self, fresh_env):
        fresh_env.write_file("hello.py", "print('hello from cage')\n")
        content = fresh_env.read_file("hello.py")
        assert "hello from cage" in content

    def test_written_file_visible_in_container(self, fresh_env):
        fresh_env.write_file("test.txt", "container can see this")
        result = fresh_env.run("cat /workspace/test.txt")
        assert result.success
        assert "container can see this" in result.stdout

    def test_written_file_visible_on_host(self, fresh_env):
        fresh_env.write_file("visible.txt", "host can see this")
        host_file = fresh_env.workspace_path / "visible.txt"
        assert host_file.exists()
        assert "host can see this" in host_file.read_text()

    def test_container_write_visible_on_host(self, fresh_env):
        fresh_env.run("echo 'written by container' > /workspace/from_container.txt")
        host_file = fresh_env.workspace_path / "from_container.txt"
        assert host_file.exists()
        assert "written by container" in host_file.read_text()

    def test_write_creates_subdirs(self, fresh_env):
        fresh_env.write_file("src/lib/utils.py", "x = 1\n")
        assert (fresh_env.workspace_path / "src" / "lib" / "utils.py").exists()

    def test_read_missing_file_raises(self, fresh_env):
        with pytest.raises(CageError):
            fresh_env.read_file("does_not_exist.txt")

    def test_list_files(self, fresh_env):
        fresh_env.write_file("a.py", "")
        fresh_env.write_file("b.py", "")
        fresh_env.write_file("sub/c.py", "")
        files = fresh_env.list_files(".")
        assert len(files) == 3
        assert any("a.py" in f for f in files)
        assert any("sub/c.py" in f for f in files)

    def test_list_files_max_depth(self, fresh_env):
        fresh_env.write_file("top.py", "")
        fresh_env.write_file("sub/deep.py", "")
        files = fresh_env.list_files(".", max_depth=1)
        assert any("top.py" in f for f in files)
        assert not any("deep.py" in f for f in files)

    def test_upload_from_host(self, fresh_env, tmp_path):
        src = tmp_path / "upload_me.txt"
        src.write_text("uploaded content")
        fresh_env.upload(str(src), "received.txt")
        result = fresh_env.run("cat /workspace/received.txt")
        assert result.success
        assert "uploaded content" in result.stdout

    def test_export_to_host(self, fresh_env, tmp_path):
        fresh_env.run("echo 'export me' > /tmp/exportable.txt")
        dest = tmp_path / "exported.txt"
        fresh_env.export("/tmp/exportable.txt", str(dest))
        assert dest.exists()
        assert "export me" in dest.read_text()

    def test_write_and_execute_python(self, fresh_env):
        fresh_env.write_file("fib.py", """\
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

print(fib(10))
""")
        result = fresh_env.run("python3 /workspace/fib.py")
        assert result.success
        assert result.stdout.strip() == "55"


# ── Install ───────────────────────────────────────────────────────────────────

class TestInstall:
    def test_install_python_package(self, fresh_env):
        result = fresh_env.install("httpx", manager="pip")
        assert result.success
        verify = fresh_env.run("python3 -c 'import httpx; print(httpx.__version__)'")
        assert verify.success
        assert verify.stdout.strip()

    def test_install_apt_package(self, fresh_env):
        update = fresh_env.run("apt-get update -q", timeout=60)
        assert update.success, f"apt-get update failed: {update.stderr}"
        result = fresh_env.run("apt-get install -y -q curl", timeout=60)
        assert result.success, f"apt-get install failed: {result.stderr}"
        verify = fresh_env.run("curl --version")
        assert verify.success
        assert "curl" in verify.stdout


# ── Snapshot / rollback ───────────────────────────────────────────────────────

class TestSnapshotRollback:
    def test_snapshot_and_rollback_removes_installed_package(self, fresh_env):
        # Install httpx, snapshot, uninstall, rollback — httpx should be back
        fresh_env.install("httpx", manager="pip")
        snap = fresh_env.snapshot("after-install")
        fresh_env.run("pip uninstall -y httpx")
        # httpx gone
        check_gone = fresh_env.run("python3 -c 'import httpx'")
        assert not check_gone.success
        # Rollback
        fresh_env.rollback(snap)
        check_back = fresh_env.run("python3 -c 'import httpx; print(\"ok\")'")
        assert check_back.success
        assert "ok" in check_back.stdout

    def test_workspace_files_survive_rollback(self, fresh_env):
        fresh_env.write_file("persistent.txt", "I survive rollback")
        fresh_env.snapshot("before")
        fresh_env.run("echo new > /workspace/new_file.txt")
        fresh_env.rollback()
        # Original file still on host
        assert (fresh_env.workspace_path / "persistent.txt").exists()

    def test_rollback_to_named_snapshot(self, fresh_env):
        fresh_env.install("httpx", manager="pip")
        _ = fresh_env.snapshot("snap1")
        fresh_env.install("rich", manager="pip")
        fresh_env.snapshot("snap2")
        # Rollback to snap1 — rich should be gone, httpx present
        fresh_env.rollback("snap1")
        check_httpx = fresh_env.run("python3 -c 'import httpx; print(\"ok\")'")
        assert check_httpx.success
        check_rich = fresh_env.run("python3 -c 'import rich'")
        assert not check_rich.success

    def test_rollback_to_nonexistent_snap_raises(self, fresh_env):
        fresh_env.snapshot("real-snap")  # need at least one snap so we reach the name check
        with pytest.raises(CageError, match="not found"):
            fresh_env.rollback("ghost-snap")

    def test_rollback_no_snapshots_raises(self, fresh_env):
        with pytest.raises(CageError, match="No snapshots"):
            fresh_env.rollback()


# ── Streaming ─────────────────────────────────────────────────────────────────

class TestStream:
    def test_stream_collects_lines(self, env):
        result = env.stream("printf 'line1\\nline2\\nline3\\n'")
        lines = [line.rstrip("\n") for line in result]
        assert lines == ["line1", "line2", "line3"]
        assert result.exit_code == 0
        assert result.success

    def test_stream_exit_code_nonzero(self, env):
        result = env.stream("exit 5")
        list(result)
        assert result.exit_code == 5
        assert not result.success

    def test_stream_live_output(self, env):
        lines = []
        start = time.time()
        result = env.stream("for i in 1 2 3; do echo $i; done")
        for line in result:
            lines.append(line.strip())
            assert time.time() - start < 10  # should be fast
        assert lines == ["1", "2", "3"]

    def test_stream_long_running_command(self, env):
        """Run a command that produces output over time — verify streaming works."""
        lines = []
        result = env.stream("for i in $(seq 1 5); do echo $i; sleep 0.1; done", timeout=10)
        for line in result:
            lines.append(line.strip())
        assert len(lines) == 5
        assert lines == ["1", "2", "3", "4", "5"]


# ── Port forwarding ───────────────────────────────────────────────────────────

class TestPortForwarding:
    def test_port_exposed_and_reachable(self, tmp_path):
        """Start a simple HTTP server in the cage and hit it from the host."""
        import socket
        import urllib.request

        # Find a free port
        with socket.socket() as s:
            s.bind(("", 0))
            host_port = s.getsockname()[1]

        server_py = """\
import http.server, threading
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"cage-ok")
    def log_message(self, *a): pass
httpd = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
httpd.serve_forever()
"""
        with north9.Sandbox(
            network="bridge",
            ports={host_port: 8080},
            pull=False,
            compress=False,
            workspace_dir=tmp_path,
        ) as s:
            s.write_file("server.py", server_py)
            # Start server in background
            proc = subprocess.Popen(
                ["docker", "exec", "-d", s.name, "python3", "/workspace/server.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)  # give server time to start
            try:
                resp = urllib.request.urlopen(f"http://localhost:{host_port}", timeout=5)
                assert resp.read() == b"cage-ok"
            finally:
                proc.wait()


# ── handle_tool_call ──────────────────────────────────────────────────────────

class TestHandleToolCallIntegration:
    def test_bash_tool(self, fresh_env):
        result = fresh_env.handle_tool_call("bash", {"command": "echo tool-works"})
        assert "tool-works" in result
        assert "[exit 0]" in result

    def test_write_read_tool_roundtrip(self, fresh_env):
        fresh_env.handle_tool_call("write_file", {"path": "data.txt", "content": "tool content"})
        content = fresh_env.handle_tool_call("read_file", {"path": "data.txt"})
        assert content == "tool content"

    def test_list_files_tool(self, fresh_env):
        fresh_env.write_file("x.py", "")
        result = fresh_env.handle_tool_call("list_files", {})
        assert "x.py" in result

    def test_snapshot_rollback_tool(self, fresh_env):
        snap_result = fresh_env.handle_tool_call("snapshot", {"name": "test-snap"})
        assert "test-snap" in snap_result

        rollback_result = fresh_env.handle_tool_call("rollback", {"name": "test-snap"})
        assert "test-snap" in rollback_result

    def test_upload_tool(self, fresh_env, tmp_path):
        src = tmp_path / "upload_src.txt"
        src.write_text("from upload tool")
        result = fresh_env.handle_tool_call("upload", {
            "host_path": str(src),
            "container_path": "uploaded.txt",
        })
        assert "uploaded.txt" in result
        content = fresh_env.run("cat /workspace/uploaded.txt")
        assert "from upload tool" in content.stdout

    def test_unknown_tool_returns_error_string(self, fresh_env):
        result = fresh_env.handle_tool_call("fly_to_mars", {})
        assert "Unknown tool" in result
