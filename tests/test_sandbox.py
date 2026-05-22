"""Tests for Cage sandboxed execution environment."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from north9.sandbox.core import (
    TOOL_DEFINITIONS,
    CageError,
    ExecResult,
    Sandbox,
    Snapshot,
    _rtk_compress,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_docker_ok(stdout: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout.encode()
    m.stderr = b""
    return m


def _mock_docker_fail(stderr: str = "error") -> MagicMock:
    m = MagicMock()
    m.returncode = 1
    m.stdout = b""
    m.stderr = stderr.encode()
    return m


def _make_session(image: str = "python:3.12-slim", tmp_path: Path | None = None) -> Sandbox:
    """Create a Sandbox with Docker calls and filesystem mocked out."""
    session = Sandbox.__new__(Sandbox)
    session.image = image
    session.workdir = "/workspace"
    session.name = "cage-test1234"
    session._env = {}
    session._network = "none"
    session._memory = "512m"
    session._cpus = "1.0"
    session._ports = {}
    session._pids_limit = 512
    session._pull = False
    session._compress = False  # disable RTK in tests
    session._container_id = "abc123def456"
    session._snapshots = []
    session._closed = False
    # Use a real temp dir so workspace path operations work
    if tmp_path is not None:
        session.workspace_path = tmp_path
    else:
        session.workspace_path = Path(tempfile.mkdtemp())
    return session


# ── Unit: ExecResult ──────────────────────────────────────────────────────────

class TestExecResult:
    def test_success_true_on_zero_exit(self):
        r = ExecResult(stdout="hello", stderr="", exit_code=0)
        assert r.success is True
        assert bool(r) is True

    def test_success_false_on_nonzero(self):
        r = ExecResult(stdout="", stderr="err", exit_code=1)
        assert r.success is False
        assert bool(r) is False

    def test_output_combines_stdout_stderr(self):
        r = ExecResult(stdout="out", stderr="err", exit_code=0)
        assert "out" in r.output
        assert "err" in r.output

    def test_output_skips_empty_parts(self):
        r = ExecResult(stdout="only stdout", stderr="", exit_code=0)
        assert r.output == "only stdout"

    def test_repr_shows_status_and_preview(self):
        r = ExecResult(stdout="hello world", stderr="", exit_code=0)
        assert "ok" in repr(r)
        assert "hello world" in repr(r)

    def test_repr_shows_exit_code_on_failure(self):
        r = ExecResult(stdout="", stderr="boom", exit_code=2)
        assert "exit 2" in repr(r)


# ── Unit: Sandbox._docker ─────────────────────────────────────────────────────

class TestSessionDocker:
    def test_raises_cage_error_on_nonzero(self):
        session = _make_session()
        with patch(
            "north9.sandbox.core.subprocess.run",
            return_value=_mock_docker_fail("container not found"),
        ):
            with pytest.raises(CageError, match="container not found"):
                session._docker(["rm", "nonexistent"])

    def test_no_raise_when_check_false(self):
        session = _make_session()
        with patch("north9.sandbox.core.subprocess.run", return_value=_mock_docker_fail("err")):
            result = session._docker(["rm", "x"], check=False)
            assert result.returncode == 1


# ── Unit: Sandbox.run ─────────────────────────────────────────────────────────

class TestSessionRun:
    def test_returns_exec_result(self):
        session = _make_session()
        mock = _mock_docker_ok("hello")
        with patch("north9.sandbox.core.subprocess.run", return_value=mock):
            result = session.run("echo hello")
        assert isinstance(result, ExecResult)
        assert result.stdout == "hello"
        assert result.exit_code == 0

    def test_timeout_returns_exit_124(self):
        session = _make_session()
        with patch(
            "north9.sandbox.core.subprocess.run",
            side_effect=subprocess.TimeoutExpired("docker", 1),
        ):
            result = session.run("sleep 999", timeout=1)
        assert result.exit_code == 124
        assert "timed out" in result.stderr

    def test_raises_on_closed_session(self):
        session = _make_session()
        session._closed = True
        with pytest.raises(CageError, match="closed"):
            session.run("ls")

    def test_uses_custom_workdir(self):
        session = _make_session()
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _mock_docker_ok()

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
            session.run("ls", workdir="/tmp")

        # Last element of docker exec cmd is the sh -c payload
        sh_payload = captured[0][-1]
        assert "cd '/tmp'" in sh_payload or "cd /tmp" in sh_payload


# ── Unit: Sandbox.snapshot / rollback ─────────────────────────────────────────

class TestSnapshotRollback:
    def test_snapshot_appends_to_list(self):
        session = _make_session()

        def fake_docker(args, **kwargs):
            if args[0] == "commit":
                return _mock_docker_ok()
            return _mock_docker_ok("imgid123")

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_docker):
            snap = session.snapshot("before-install")

        assert isinstance(snap, Snapshot)
        assert snap.name == "before-install"
        assert len(session.snapshots) == 1

    def test_snapshot_auto_names_when_none(self):
        session = _make_session()

        def fake_docker(args, **kwargs):
            return _mock_docker_ok("imgid")

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_docker):
            snap = session.snapshot()

        assert snap.name.startswith("snap-")

    def test_rollback_raises_with_no_snapshots(self):
        session = _make_session()
        with pytest.raises(CageError, match="No snapshots"):
            session.rollback()

    def test_rollback_raises_on_unknown_name(self):
        session = _make_session()
        session._snapshots = [Snapshot(name="snap-abc", image_tag="cage/x:latest")]
        with pytest.raises(CageError, match="not found"):
            session.rollback("nonexistent")

    def test_rollback_uses_most_recent_by_default(self):
        session = _make_session()
        snap1 = Snapshot(name="first", image_tag="cage/first:latest")
        snap2 = Snapshot(name="second", image_tag="cage/second:latest")
        session._snapshots = [snap1, snap2]

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return _mock_docker_ok("new-container-id")

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
            session.rollback()

        # The new container should be started from snap2's image
        all_args = [arg for cmd in run_calls for arg in cmd]
        assert "cage/second:latest" in all_args

    def test_rollback_by_name(self):
        session = _make_session()
        snap1 = Snapshot(name="first", image_tag="cage/first:latest")
        snap2 = Snapshot(name="second", image_tag="cage/second:latest")
        session._snapshots = [snap1, snap2]

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return _mock_docker_ok("new-id")

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
            session.rollback("first")

        all_args = [arg for cmd in run_calls for arg in cmd]
        assert "cage/first:latest" in all_args


# ── Unit: Sandbox.handle_tool_call ────────────────────────────────────────────

class TestHandleToolCall:
    def test_bash_returns_stdout(self):
        session = _make_session()
        with patch.object(session, "run", return_value=ExecResult("output", "", 0)):
            result = session.handle_tool_call("bash", {"command": "echo hi"})
        assert "output" in result
        assert "[exit 0]" in result

    def test_bash_includes_stderr(self):
        session = _make_session()
        with patch.object(session, "run", return_value=ExecResult("", "warning", 0)):
            result = session.handle_tool_call("bash", {"command": "cmd"})
        assert "[stderr]" in result
        assert "warning" in result

    def test_write_file_returns_confirmation(self):
        session = _make_session()
        with patch.object(session, "write_file", return_value=None):
            result = session.handle_tool_call("write_file", {"path": "app.py", "content": "x = 1"})
        assert "app.py" in result

    def test_read_file_returns_content(self):
        session = _make_session()
        with patch.object(session, "read_file", return_value="file content here"):
            result = session.handle_tool_call("read_file", {"path": "app.py"})
        assert result == "file content here"

    def test_list_files_returns_joined(self):
        session = _make_session()
        with patch.object(
            session, "list_files", return_value=["/workspace/a.py", "/workspace/b.py"]
        ):
            result = session.handle_tool_call("list_files", {})
        assert "/workspace/a.py" in result
        assert "/workspace/b.py" in result

    def test_list_files_empty(self):
        session = _make_session()
        with patch.object(session, "list_files", return_value=[]):
            result = session.handle_tool_call("list_files", {})
        assert "empty" in result

    def test_snapshot_returns_name(self):
        session = _make_session()
        snap = Snapshot(name="my-snap", image_tag="cage/x:latest")
        with patch.object(session, "snapshot", return_value=snap):
            result = session.handle_tool_call("snapshot", {"name": "my-snap"})
        assert "my-snap" in result

    def test_rollback_returns_target(self):
        session = _make_session()
        with patch.object(session, "rollback", return_value=None):
            result = session.handle_tool_call("rollback", {"name": "my-snap"})
        assert "my-snap" in result

    def test_rollback_default_says_most_recent(self):
        session = _make_session()
        with patch.object(session, "rollback", return_value=None):
            result = session.handle_tool_call("rollback", {})
        assert "most recent" in result

    def test_export_returns_confirmation(self):
        session = _make_session()
        with patch.object(session, "export", return_value=None):
            result = session.handle_tool_call(
                "export", {"container_path": "dist/", "host_path": "/tmp/dist"}
            )
        assert "dist/" in result
        assert "/tmp/dist" in result

    def test_unknown_tool_returns_error_string(self):
        session = _make_session()
        result = session.handle_tool_call("fly_to_moon", {})
        assert "Unknown tool" in result

    def test_cage_error_caught_and_returned(self):
        session = _make_session()
        with patch.object(session, "run", side_effect=CageError("container died")):
            result = session.handle_tool_call("bash", {"command": "ls"})
        assert "Error:" in result
        assert "container died" in result


# ── Unit: Sandbox.tools schema ────────────────────────────────────────────────

class TestToolsSchema:
    def test_returns_list_of_dicts(self):
        session = _make_session()
        tools = session.tools()
        assert isinstance(tools, list)
        assert all(isinstance(t, dict) for t in tools)

    def test_all_tools_have_required_fields(self):
        session = _make_session()
        for tool in session.tools():
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_expected_tool_names_present(self):
        session = _make_session()
        names = {t["name"] for t in session.tools()}
        assert names == {
            "bash", "write_file", "read_file", "list_files",
            "snapshot", "rollback", "export", "upload",
        }

    def test_bash_has_command_required(self):
        session = _make_session()
        bash = next(t for t in session.tools() if t["name"] == "bash")
        assert "command" in bash["input_schema"]["required"]


# ── Unit: Sandbox lifecycle ───────────────────────────────────────────────────

class TestSessionLifecycle:
    def test_close_sets_closed_flag(self):
        session = _make_session()
        with patch("north9.sandbox.core.subprocess.run", return_value=_mock_docker_ok()):
            session.close()
        assert session._closed is True

    def test_close_idempotent(self):
        session = _make_session()
        with patch("north9.sandbox.core.subprocess.run", return_value=_mock_docker_ok()):
            session.close()
            session.close()  # should not raise

    def test_context_manager_calls_close(self):
        session = _make_session()
        with patch.object(session, "close") as mock_close:
            with session:
                pass
        mock_close.assert_called_once()

    def test_repr_shows_name_and_status(self):
        session = _make_session()
        r = repr(session)
        assert "cage-test1234" in r
        assert "running" in r

    def test_repr_shows_closed(self):
        session = _make_session()
        session._closed = True
        r = repr(session)
        assert "closed" in r


# ── Unit: Snapshot ────────────────────────────────────────────────────────────

class TestSnapshot:
    def test_age_seconds_increases(self):
        import time
        snap = Snapshot(name="x", image_tag="cage/x:latest")
        snap.created_at = time.time() - 10
        assert snap.age_seconds() >= 10

    def test_snapshots_property_returns_copy(self):
        session = _make_session()
        snap = Snapshot(name="a", image_tag="cage/a:latest")
        session._snapshots = [snap]
        copy = session.snapshots
        copy.append(Snapshot(name="b", image_tag="cage/b:latest"))
        assert len(session._snapshots) == 1  # original not mutated


# ── Unit: RTK compression ─────────────────────────────────────────────────────

class TestRtkCompression:
    def test_returns_original_when_rtk_unavailable(self):
        with patch("north9.sandbox.core.shutil.which", return_value=None):
            result = _rtk_compress("some output", "git status")
        assert result == "some output"

    def test_returns_original_on_empty_output(self):
        result = _rtk_compress("", "git status")
        assert result == ""

    def test_calls_rtk_log_and_returns_compressed(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"compressed output"
        with patch("north9.sandbox.core.shutil.which", return_value="/usr/bin/rtk"):
            with patch("north9.sandbox.core.subprocess.run", return_value=mock_result):
                result = _rtk_compress("verbose output\n" * 20, "npm install")
        assert result == "compressed output"

    def test_falls_back_to_original_on_rtk_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        with patch("north9.sandbox.core.shutil.which", return_value="/usr/bin/rtk"):
            with patch("north9.sandbox.core.subprocess.run", return_value=mock_result):
                result = _rtk_compress("original", "ls")
        assert result == "original"

    def test_compress_disabled_in_session_skips_rtk(self):
        session = _make_session()
        assert session._compress is False  # disabled in test helper

        mock_exec = _mock_docker_ok("raw output")
        with patch("north9.sandbox.core.subprocess.run", return_value=mock_exec):
            result = session.run("ls")
        assert result.stdout == "raw output"

    def test_compress_enabled_routes_through_rtk(self):
        session = _make_session()
        session._compress = True
        # RTK only fires on output with >5 lines
        long_output = "\n".join(f"line {i}" for i in range(10))

        def fake_run(cmd, **kwargs):
            if cmd[0] == "docker":
                return _mock_docker_ok(long_output)
            # RTK call
            m = MagicMock()
            m.returncode = 0
            m.stdout = b"compressed"
            return m

        with patch("north9.sandbox.core.shutil.which", return_value="/usr/bin/rtk"):
            with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
                result = session.run("git status")
        assert result.stdout == "compressed"

    def test_compress_override_per_call(self):
        session = _make_session()
        session._compress = True  # session-level enabled

        mock_exec = _mock_docker_ok("raw output")
        with patch("north9.sandbox.core.subprocess.run", return_value=mock_exec):
            # Override to disable for this call
            result = session.run("ls", compress=False)
        assert result.stdout == "raw output"


# ── Unit: TOOL_DEFINITIONS ────────────────────────────────────────────────────

class TestToolDefinitions:
    def test_is_module_level_constant(self):
        assert isinstance(TOOL_DEFINITIONS, list)
        assert len(TOOL_DEFINITIONS) > 0

    def test_session_tools_returns_same_object(self):
        session = _make_session()
        assert session.tools() is TOOL_DEFINITIONS

    def test_async_session_tools_returns_same_object(self):
        from north9.sandbox.core import AsyncSandbox
        async_session = AsyncSandbox()
        assert async_session.tools() is TOOL_DEFINITIONS


# ── Unit: install() ───────────────────────────────────────────────────────────

class TestInstall:
    def test_apt_get_default(self):
        session = _make_session()
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _mock_docker_ok()

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
            session.install("git", "curl")

        sh_payload = captured[0][-1]
        assert "apt-get install" in sh_payload
        assert "git" in sh_payload
        assert "curl" in sh_payload

    def test_pip_manager(self):
        session = _make_session()
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _mock_docker_ok()

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
            session.install("requests", manager="pip")

        sh_payload = captured[0][-1]
        assert "pip install" in sh_payload
        assert "requests" in sh_payload

    def test_package_names_quoted(self):
        session = _make_session()
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _mock_docker_ok()

        with patch("north9.sandbox.core.subprocess.run", side_effect=fake_run):
            session.install("python3-dev")

        sh_payload = captured[0][-1]
        assert "python3-dev" in sh_payload


# ── Integration: _require_docker ─────────────────────────────────────────────

class TestRequireDocker:
    def test_raises_cage_error_when_docker_missing(self):
        from north9.sandbox.core import _require_docker
        with patch(
            "north9.sandbox.core.subprocess.run",
            return_value=_mock_docker_fail("Cannot connect"),
        ):
            with pytest.raises(CageError, match="Docker is not running"):
                _require_docker()

    def test_passes_when_docker_available(self):
        from north9.sandbox.core import _require_docker
        with patch("north9.sandbox.core.subprocess.run", return_value=_mock_docker_ok()):
            _require_docker()  # should not raise


# ── Unit: workspace volume mount ──────────────────────────────────────────────

class TestWorkspace:
    def test_write_file_uses_host_path_for_workspace(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        session.write_file("app.py", "print('hello')")
        assert (tmp_path / "app.py").read_text() == "print('hello')"

    def test_write_file_creates_subdirs(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        session.write_file("src/main/app.py", "x = 1")
        assert (tmp_path / "src" / "main" / "app.py").read_text() == "x = 1"

    def test_read_file_uses_host_path_for_workspace(self, tmp_path):
        (tmp_path / "hello.txt").write_text("world")
        session = _make_session(tmp_path=tmp_path)
        content = session.read_file("hello.txt")
        assert content == "world"

    def test_read_file_raises_on_missing(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        with pytest.raises(CageError, match="not found"):
            session.read_file("nonexistent.txt")

    def test_list_files_uses_host_path(self, tmp_path):
        (tmp_path / "a.py").write_text("a")
        (tmp_path / "b.py").write_text("b")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("c")
        session = _make_session(tmp_path=tmp_path)
        files = session.list_files(".")
        assert len(files) == 3
        assert all(f.endswith(".py") for f in files)

    def test_list_files_max_depth(self, tmp_path):
        (tmp_path / "top.py").write_text("")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.py").write_text("")
        session = _make_session(tmp_path=tmp_path)
        files = session.list_files(".", max_depth=1)
        assert any("top.py" in f for f in files)
        assert not any("deep.py" in f for f in files)

    def test_write_read_roundtrip(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        session.write_file("data.json", '{"key": "value"}')
        content = session.read_file("data.json")
        assert '"key"' in content

    def test_write_binary_content(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        session.write_file("binary.bin", b"\x00\x01\x02\x03")
        assert (tmp_path / "binary.bin").read_bytes() == b"\x00\x01\x02\x03"

    def test_resolve_host_relative_path(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        result = session._resolve_host("app.py")
        assert result == tmp_path / "app.py"

    def test_resolve_host_workspace_absolute(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        result = session._resolve_host("/workspace/app.py")
        assert result == tmp_path / "app.py"

    def test_resolve_host_non_workspace_returns_none(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        result = session._resolve_host("/etc/passwd")
        assert result is None

    def test_workspace_path_in_repr(self, tmp_path):
        session = _make_session(tmp_path=tmp_path)
        assert str(tmp_path) in repr(session)


# ── Unit: stream() ────────────────────────────────────────────────────────────

class TestStream:
    def test_raises_on_closed_session(self):
        session = _make_session()
        session._closed = True
        with pytest.raises(CageError, match="closed"):
            session.stream("ls")

    def test_stream_returns_stream_result(self):
        from north9.sandbox.core import StreamResult
        session = _make_session()
        with patch("north9.sandbox.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout = iter([b"line1\n", b"line2\n"])
            mock_proc.wait.return_value = None
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            result = session.stream("echo hi")
        assert isinstance(result, StreamResult)

    def test_stream_result_iterable(self):
        session = _make_session()
        with patch("north9.sandbox.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout = iter([b"hello\n", b"world\n"])
            mock_proc.wait.return_value = None
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc
            result = session.stream("echo hello")
            lines = list(result)
        assert lines == ["hello\n", "world\n"]
        assert result.exit_code == 0
        assert result.success is True

    def test_stream_result_nonzero_exit(self):
        session = _make_session()
        with patch("north9.sandbox.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout = iter([b"error\n"])
            mock_proc.wait.return_value = None
            mock_proc.returncode = 1
            mock_popen.return_value = mock_proc
            result = session.stream("false")
            list(result)
        assert result.exit_code == 1
        assert result.success is False
