"""Cage: sandboxed execution environment for AI agents.

Every command runs inside an ephemeral Docker container. The workspace is
volume-mounted from the host so you can open it in your IDE and watch the
agent work in real time. The rest of the container (installed packages,
system files, network) is fully isolated.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── RTK integration ───────────────────────────────────────────────────────────

def _rtk_available() -> bool:
    return shutil.which("rtk") is not None


# Maps command prefixes to the best RTK stdin filter for their output.
_RTK_ROUTE: dict[str, list[str]] = {
    "git":    ["log"],
    "npm":    ["log"],
    "pnpm":   ["log"],
    "yarn":   ["log"],
    "bun":    ["log"],
    "pytest": ["log"],
    "python": ["log"],
    "node":   ["log"],
    "tsc":    ["log"],
    "cargo":  ["log"],
    "go":     ["log"],
    "make":   ["log"],
    "apt":    ["log"],
    "apt-get":["log"],
    "pip":    ["log"],
    "docker": ["log"],
}

_RTK_SMART_THRESHOLD = 40  # lines above which we try rtk smart


def _rtk_compress(output: str, cmd: str = "") -> str:
    """Compress command output through RTK to save tokens.

    For short output: pass through unchanged.
    For medium output: rtk log (deduplicate + filter noise).
    For long output: rtk smart (heuristic 2-line summary).
    Falls back to raw output if RTK is unavailable or errors.
    """
    if not output.strip() or not _rtk_available():
        return output

    lines = output.splitlines()
    first_word = cmd.strip().split()[0] if cmd.strip() else ""

    if len(lines) < 5:
        return output

    # Pick RTK subcommand
    if len(lines) >= _RTK_SMART_THRESHOLD:
        # Try smart summary first for very long output
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
                tmp.write(output)
                tmp_path = tmp.name
            result = subprocess.run(
                ["rtk", "smart", tmp_path],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode(errors="replace")
        except Exception:
            pass
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    # Fall back to log filter (dedup + noise removal)
    rtk_args = _RTK_ROUTE.get(first_word, ["log"])
    try:
        result = subprocess.run(
            ["rtk"] + rtk_args,
            input=output.encode(),
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.decode(errors="replace")
    except Exception:
        pass

    return output


# ── Tool definitions (static — no session needed) ─────────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command in the sandboxed container. "
            "Working directory is /workspace — files written here are immediately "
            "visible on the host at the workspace path shown in cage_status. "
            "Host filesystem outside /workspace is never affected. "
            "Container is Debian-based (apt-get for packages). "
            "Always use snapshot before destructive or experimental operations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default 120)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file inside the sandbox. "
            "Files written to /workspace are immediately visible on the host. "
            "Parent directories are created automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to /workspace, or absolute)",
                },
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in the sandbox workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (default: /workspace)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "snapshot",
        "description": (
            "Save a checkpoint of the container state (installed packages, env). "
            "Workspace files are always visible on the host regardless of snapshots. "
            "Call before installing packages or making risky system changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional checkpoint name"},
            },
            "required": [],
        },
    },
    {
        "name": "rollback",
        "description": (
            "Restore container state to a previous snapshot. "
            "Rolls back installed packages and system changes. "
            "Workspace files are NOT rolled back — they remain on the host."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Snapshot name to restore (default: most recent)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "export",
        "description": (
            "Copy a file or directory from the sandbox to a specific host path. "
            "Note: files in /workspace are already on the host at the workspace path. "
            "Use this to copy from other container locations (e.g. /tmp, /root)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "container_path": {
                    "type": "string",
                    "description": "Path inside the sandbox",
                },
                "host_path": {
                    "type": "string",
                    "description": "Destination path on the host",
                },
            },
            "required": ["container_path", "host_path"],
        },
    },
    {
        "name": "upload",
        "description": "Copy a file or directory from the host into the sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host_path": {
                    "type": "string",
                    "description": "Source path on the host filesystem",
                },
                "container_path": {
                    "type": "string",
                    "description": (
                        "Destination inside the sandbox (relative to /workspace or absolute)"
                    ),
                },
            },
            "required": ["host_path", "container_path"],
        },
    },
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ExecResult:
    """Result of a command executed inside the cage."""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        status = "ok" if self.success else f"exit {self.exit_code}"
        preview = (self.stdout or self.stderr)[:60].replace("\n", "\\n")
        return f"ExecResult({status}, {preview!r})"


@dataclass
class Snapshot:
    """A point-in-time image of the container state."""

    name: str
    image_tag: str
    created_at: float = field(default_factory=time.time)

    def age_seconds(self) -> float:
        return time.time() - self.created_at


class StreamResult:
    """Iterable that streams command output line-by-line and exposes exit_code after exhaustion.

    Usage::

        result = env.stream("npm install")
        for line in result:
            print(line, end="")
        print(f"Exit: {result.exit_code}")
    """

    def __init__(self, proc: subprocess.Popen, timeout: int) -> None:
        self._proc = proc
        self._timeout = timeout
        self.exit_code: int | None = None

    def __iter__(self) -> Generator[str, None, None]:
        assert self._proc.stdout is not None
        try:
            for raw_line in self._proc.stdout:
                yield raw_line.decode(errors="replace")
            self._proc.wait(timeout=self._timeout)
            self.exit_code = self._proc.returncode
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            yield f"\n[timed out after {self._timeout}s]\n"
            self.exit_code = 124

    @property
    def success(self) -> bool:
        return self.exit_code == 0


class CageError(Exception):
    """Raised when a cage operation fails."""


# ── Docker availability check ─────────────────────────────────────────────────

def _require_docker() -> None:
    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        raise CageError(
            "Docker is not running or not installed. "
            "Install Docker Desktop or Docker Engine: https://docs.docker.com/get-docker/"
        )


def _default_workspace_root() -> Path:
    return Path.home() / ".cage" / "workspaces"


# ── Session ───────────────────────────────────────────────────────────────────

class Sandbox:
    """A sandboxed execution environment backed by a Docker container.

    The /workspace directory is volume-mounted from the host — files the agent
    writes are immediately visible in your file manager or IDE. Everything else
    (installed packages, network activity, system files) is isolated.

    Basic usage::

        with north9.Sandbox() as env:
            print(f"Open in your editor: {env.workspace_path}")
            env.install("git", "curl")
            env.snapshot("after-setup")
            result = env.run("python app.py")
            if not result.success:
                env.rollback("after-setup")

    With streaming output::

        with north9.Sandbox() as env:
            for line in env.stream("npm install"):
                print(line, end="")

    With AI tool_use (Anthropic)::

        with north9.Sandbox() as env:
            while True:
                response = client.messages.create(
                    model="claude-opus-4-7",
                    tools=env.tools(),
                    messages=messages,
                )
                for block in response.content:
                    if block.type == "tool_use":
                        result = env.handle_tool_call(block.name, block.input)
    """

    def __init__(
        self,
        image: str = "python:3.12-slim",
        workspace_dir: str | Path | None = None,
        name: str | None = None,
        env: dict[str, str] | None = None,
        network: str = "none",
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 512,
        ports: dict[int, int] | None = None,
        pull: bool = True,
        compress: bool | None = None,
    ) -> None:
        """Create and start a sandboxed session.

        Args:
            image:         Docker image. Defaults to python:3.12-slim (Debian/apt).
                           Use "ubuntu:22.04" for git, curl, make, gcc, and more tools.
            workspace_dir: Host path to mount as /workspace. Files written here
                           are immediately visible on the host. Auto-created at
                           ~/.cage/workspaces/{name}/ if not provided.
            name:          Container name (auto-generated if not provided).
            env:           Environment variables to set inside the container.
            network:       "none" (default, fully isolated) or "bridge" (internet
                           access for pip/apt/npm). Never use "host".
            memory:        Memory limit ("512m", "1g", "2g").
            cpus:          CPU limit.
            ports:         Port mapping {host_port: container_port} for web apps.
                           Requires network != "none".
            pull:          Pull the image before starting (default True).
            compress:      Route output through RTK to save tokens. Auto-detects
                           RTK installation if not specified.
        """
        _require_docker()

        if network == "host":
            raise CageError(
                "network='host' is not allowed — it exposes the host network stack. "
                "Use 'bridge' for internet access or 'none' for full isolation."
            )
        if network not in ("none", "bridge"):
            raise CageError(f"network must be 'none' or 'bridge', got: {network!r}")

        # Sanitize container name: alphanumeric + dash + underscore only.
        import re as _re
        raw_name = name or f"cage-{uuid.uuid4().hex[:8]}"
        if not _re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", raw_name):
            raise CageError(
                f"Invalid container name {raw_name!r}. Use alphanumeric, dash, dot, underscore."
            )
        self.name = raw_name

        self.image = image
        self._env = env or {}
        self._network = network
        self._memory = memory
        self._cpus = str(cpus)
        self._pids_limit = pids_limit
        self._ports = ports or {}
        self._pull = pull
        self._compress = compress if compress is not None else _rtk_available()
        self._container_id: str | None = None
        self._snapshots: list[Snapshot] = []
        self._closed = False

        # Set up workspace directory (volume-mounted into container).
        if workspace_dir is None:
            ws = _default_workspace_root() / self.name
        else:
            ws = Path(workspace_dir)
        ws.mkdir(parents=True, exist_ok=True)
        self.workspace_path = ws.resolve()

        # Reject workspace pointing at sensitive host directories.  If an AI agent
        # writes to /workspace and it's actually /etc or /usr, it can corrupt the host.
        _DANGEROUS_ROOTS = {
            Path("/"), Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"),
            Path("/lib"), Path("/lib64"), Path("/boot"), Path("/sys"),
            Path("/proc"), Path("/dev"), Path("/var/run"), Path("/run"),
        }
        if self.workspace_path in _DANGEROUS_ROOTS:
            raise CageError(
                f"workspace_dir resolves to {self.workspace_path!r} which is a "
                "protected system path. Choose a directory under your home folder "
                "or /tmp."
            )

        self.workdir = "/workspace"
        self._start()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _docker(
        self, args: list[str], input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(["docker"] + args, capture_output=True, input=input)
        if check and result.returncode != 0:
            raise CageError(f"docker {args[0]} failed:\n{result.stderr.decode().strip()}")
        return result

    def _start(self) -> None:
        if self._pull:
            self._docker(["pull", "--quiet", self.image])

        run_args = [
            "run", "-d",
            "--name", self.name,
            "--workdir", self.workdir,
            f"--network={self._network}",
            f"--memory={self._memory}",
            f"--cpus={self._cpus}",
            f"--pids-limit={self._pids_limit}",
            "--cap-drop=ALL",                       # drop all Linux capabilities
            "--cap-add=SETUID",                     # needed: apt drops to _apt user
            "--cap-add=SETGID",                     # needed: apt group switching
            "--cap-add=CHOWN",                      # needed: apt chown of list dirs
            "--cap-add=FOWNER",                     # needed: apt chmod on partial dirs
            "--cap-add=DAC_OVERRIDE",               # needed: apt unlink _apt-owned files
            "--security-opt", "no-new-privileges",  # prevent setuid escalation
            # Mount workspace as volume — changes visible on host immediately
            "-v", f"{self.workspace_path}:{self.workdir}",
        ]

        for host_port, container_port in self._ports.items():
            run_args += ["-p", f"{host_port}:{container_port}"]

        for k, v in self._env.items():
            run_args += ["-e", f"{k}={v}"]

        run_args += [self.image, "tail", "-f", "/dev/null"]
        result = self._docker(run_args)
        self._container_id = result.stdout.decode().strip()

    def _resolve_host(self, path: str) -> Path | None:
        """Return the host path for a container path if it's safely inside the workspace.

        Returns None if the path is outside /workspace or if it's a symlink that
        escapes the workspace (e.g. ln -s /etc/passwd /workspace/evil). In that
        case callers fall back to docker cp, which is container-scoped and safe.
        """
        from pathlib import PurePosixPath
        abs_path = path if path.startswith("/") else f"{self.workdir}/{path}"
        normalized = str(PurePosixPath(abs_path))
        if normalized != self.workdir and not normalized.startswith(self.workdir + "/"):
            return None
        rel = normalized[len(self.workdir):].lstrip("/")
        candidate = (self.workspace_path / rel) if rel else self.workspace_path
        # Guard against symlink escape: an agent could create
        # ln -s /etc/passwd /workspace/evil to read host system files.
        # If the candidate exists as a symlink, resolve it and verify it stays
        # inside workspace_path. If it escapes, return None so docker cp handles
        # it (which reads from container filesystem, not host).
        if candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(self.workspace_path.resolve())
            except ValueError:
                return None  # symlink escapes workspace — use docker cp
        return candidate

    def _resolve(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.workdir}/{path}"

    def _ensure_open(self) -> None:
        if self._closed or not self._container_id:
            raise CageError("Sandbox is closed")

    # ── Core operations ───────────────────────────────────────────────────────

    def run(
        self, cmd: str, timeout: int = 120, workdir: str | None = None, compress: bool | None = None
    ) -> ExecResult:
        """Execute a shell command inside the cage.

        Args:
            cmd:       Shell command. Runs via sh -c.
            timeout:   Seconds before the command is killed.
            workdir:   Override working directory for this call.
            compress:  Route output through RTK. Defaults to session setting.

        Returns:
            ExecResult with .stdout, .stderr, .exit_code, .success.
        """
        self._ensure_open()
        wd = shlex.quote(workdir or self.workdir)
        try:
            result = subprocess.run(
                ["docker", "exec", self.name, "sh", "-c", f"cd {wd} && {cmd}"],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                stdout="", stderr=f"Command timed out after {timeout}s", exit_code=124
            )

        stdout = result.stdout.decode(errors="replace").strip()
        stderr = result.stderr.decode(errors="replace").strip()

        should_compress = compress if compress is not None else self._compress
        if should_compress and stdout:
            stdout = _rtk_compress(stdout, cmd)

        return ExecResult(stdout=stdout, stderr=stderr, exit_code=result.returncode)

    def stream(self, cmd: str, timeout: int = 300, workdir: str | None = None) -> StreamResult:
        """Stream output from a command line-by-line as it runs.

        Returns a StreamResult iterable. Iterate it to get output lines.
        After exhaustion, check .exit_code for the return code.

        Example::

            result = env.stream("npm install")
            for line in result:
                print(line, end="")
            print(f"Exit: {result.exit_code}")
        """
        self._ensure_open()
        wd = shlex.quote(workdir or self.workdir)
        proc = subprocess.Popen(
            ["docker", "exec", self.name, "sh", "-c", f"cd {wd} && {cmd}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return StreamResult(proc, timeout)

    def install(self, *packages: str, manager: str = "apt-get") -> ExecResult:
        """Install packages inside the cage.

        Args:
            *packages: Package names.
            manager:   "apt-get" (default), "pip", "apk", or "npm".

        Example::

            env.install("git", "curl", "make")
            env.install("requests", "httpx", manager="pip")
        """
        pkg_str = " ".join(shlex.quote(p) for p in packages)
        if manager == "apt-get":
            cmd = f"apt-get install -y --no-install-recommends {pkg_str}"
        elif manager == "pip":
            cmd = f"pip install {pkg_str}"
        elif manager == "apk":
            cmd = f"apk add --no-cache {pkg_str}"
        elif manager == "npm":
            cmd = f"npm install -g {pkg_str}"
        else:
            cmd = f"{manager} install {pkg_str}"
        return self.run(cmd)

    def write_file(self, path: str, content: str | bytes) -> None:
        """Write a file into the cage workspace.

        Files in /workspace are written directly to the host filesystem via
        the volume mount — no docker cp needed, and immediately visible.

        Args:
            path:     File path. Relative paths resolve against /workspace.
            content:  String or bytes.
        """
        self._ensure_open()
        if isinstance(content, str):
            content_bytes = content.encode()
        else:
            content_bytes = content

        host_path = self._resolve_host(path)
        if host_path is not None:
            # Fast path: write directly to mounted volume
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.write_bytes(content_bytes)
        else:
            # Slow path for paths outside /workspace: docker cp
            abs_path = self._resolve(path)
            parent = str(Path(abs_path).parent)
            self.run(f"mkdir -p {shlex.quote(parent)}")
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(content_bytes)
                tmp_path = tmp.name
            try:
                self._docker(["cp", tmp_path, f"{self.name}:{abs_path}"])
            finally:
                os.unlink(tmp_path)

    def read_file(self, path: str) -> str:
        """Read a file from the cage.

        Files in /workspace are read directly from the host filesystem.

        Args:
            path: File path. Relative paths resolve against /workspace.

        Returns:
            File contents as a string.
        """
        self._ensure_open()
        host_path = self._resolve_host(path)
        if host_path is not None:
            # Fast path: read directly from mounted volume
            if not host_path.exists():
                raise CageError(f"File not found: {path}")
            return host_path.read_text(errors="replace")
        else:
            abs_path = self._resolve(path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".cage") as tmp:
                tmp_path = tmp.name
            try:
                self._docker(["cp", f"{self.name}:{abs_path}", tmp_path])
                return Path(tmp_path).read_text(errors="replace")
            except CageError as e:
                raise CageError(f"Cannot read {path}: {e}") from e
            finally:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    def upload(self, host_path: str, container_path: str) -> None:
        """Copy a file or directory from the host into the cage.

        Files are automatically available in /workspace via the volume mount.
        Use this to copy files from other host locations.

        Args:
            host_path:       Source path on the host.
            container_path:  Destination inside the container.
        """
        self._ensure_open()
        abs_dst = self._resolve(container_path)
        parent = str(Path(abs_dst).parent)
        self.run(f"mkdir -p {shlex.quote(parent)}")
        self._docker(["cp", host_path, f"{self.name}:{abs_dst}"])

    def list_files(self, path: str = ".", max_depth: int | None = None) -> list[str]:
        """List files in a directory.

        For /workspace paths, lists from the host filesystem directly.

        Args:
            path:      Directory. Relative paths resolve against /workspace.
            max_depth: Limit traversal depth.

        Returns:
            Sorted list of file paths relative to the listed directory.
        """
        if max_depth is not None and max_depth < 1:
            raise CageError(f"max_depth must be >= 1, got {max_depth}")
        self._ensure_open()
        host_path = self._resolve_host(path)

        if host_path is not None and host_path.exists():
            # Fast path: use host filesystem. Return workspace-relative paths
            # for consistency with the container find path.
            files = []
            for p in sorted(host_path.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    rel = p.relative_to(self.workspace_path)
                    if max_depth is not None and len(rel.parts) > max_depth:
                        continue
                    files.append(f"{self.workdir}/{rel}")
            return files

        abs_path = self._resolve(path)
        depth_arg = ["-maxdepth", str(max_depth)] if max_depth is not None else []
        find_cmd = ["find", abs_path] + depth_arg + ["-type", "f"]
        result = self.run(" ".join(shlex.quote(a) for a in find_cmd) + " | sort")
        if not result.success:
            raise CageError(f"Cannot list {path}: {result.stderr}")
        return [line for line in result.stdout.splitlines() if line.strip()]

    # ── Snapshot / rollback ───────────────────────────────────────────────────

    def snapshot(self, name: str | None = None) -> Snapshot:
        """Commit the current container state as a restorable snapshot.

        Snapshots capture the container filesystem (installed packages, config).
        Workspace files are NOT snapshotted — they live on the host.
        Call before installing packages or making risky system-level changes.

        Args:
            name: Optional snapshot name.

        Returns:
            Snapshot object.
        """
        self._ensure_open()
        snap_name = name or f"snap-{uuid.uuid4().hex[:8]}"
        image_tag = f"cage/{self.name}-{snap_name}:latest"
        self._docker(["commit", self.name, image_tag])
        snap = Snapshot(name=snap_name, image_tag=image_tag)
        self._snapshots.append(snap)
        return snap

    def rollback(self, snapshot: Snapshot | str | None = None) -> None:
        """Restore the container to a previous snapshot.

        Restores installed packages and system state. Workspace files on the
        host are NOT affected.

        Args:
            snapshot: Snapshot, name, or None for most recent.

        Raises:
            CageError: If no snapshots exist or named snapshot not found.
        """
        if not self._snapshots:
            raise CageError("No snapshots to roll back to. Call snapshot() first.")

        if snapshot is None:
            target = self._snapshots[-1]
        elif isinstance(snapshot, str):
            matches = [s for s in self._snapshots if s.name == snapshot]
            if not matches:
                available = ", ".join(s.name for s in self._snapshots)
                raise CageError(f"Snapshot '{snapshot}' not found. Available: {available}")
            target = matches[-1]
        else:
            target = snapshot

        self._docker(["rm", "-f", self.name], check=False)

        run_args = [
            "run", "-d",
            "--name", self.name,
            "--workdir", self.workdir,
            f"--network={self._network}",
            f"--memory={self._memory}",
            f"--cpus={self._cpus}",
            f"--pids-limit={self._pids_limit}",
            "--cap-drop=ALL",
            "--cap-add=SETUID",
            "--cap-add=SETGID",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--cap-add=DAC_OVERRIDE",
            "--security-opt", "no-new-privileges",
            "-v", f"{self.workspace_path}:{self.workdir}",
        ]
        for host_port, container_port in self._ports.items():
            run_args += ["-p", f"{host_port}:{container_port}"]
        for k, v in self._env.items():
            run_args += ["-e", f"{k}={v}"]
        run_args += [target.image_tag, "tail", "-f", "/dev/null"]

        result = self._docker(run_args)
        self._container_id = result.stdout.decode().strip()

    @property
    def snapshots(self) -> list[Snapshot]:
        return list(self._snapshots)

    # ── Export / upload ───────────────────────────────────────────────────────

    def export(self, container_path: str, host_path: str) -> None:
        """Copy a file or directory from the cage to the host.

        Note: files already in /workspace are on the host at workspace_path.
        Use this for files outside /workspace (e.g. /tmp/output).

        Args:
            container_path: Path inside the container.
            host_path:      Destination on the host.
        """
        self._ensure_open()
        abs_src = self._resolve(container_path)
        self._docker(["cp", f"{self.name}:{abs_src}", host_path])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Destroy the container and snapshot images.

        Workspace files on the host are NOT deleted — they persist at
        workspace_path after the session ends.
        """
        if self._closed:
            return
        self._closed = True
        self._docker(["rm", "-f", self.name], check=False)
        for snap in self._snapshots:
            self._docker(["rmi", "-f", snap.image_tag], check=False)
        self._snapshots.clear()
        self._container_id = None

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        status = "closed" if self._closed else "running"
        return (
            f"Sandbox(name={self.name!r}, image={self.image!r}, "
            f"workspace={self.workspace_path}, status={status}, snapshots={len(self._snapshots)})"
        )

    # ── AI tool integration ───────────────────────────────────────────────────

    def tools(self) -> list[dict]:
        """Return Anthropic-compatible tool definitions."""
        return TOOL_DEFINITIONS

    def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Execute an AI tool call and return the result as a string.

        For use in Anthropic tool_use agent loops::

            for block in response.content:
                if block.type == "tool_use":
                    result_text = env.handle_tool_call(block.name, block.input)
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                        ],
                    })
        """
        try:
            if tool_name == "bash":
                result = self.run(tool_input["command"], timeout=tool_input.get("timeout", 120))
                parts = []
                if result.stdout:
                    parts.append(result.stdout)
                if result.stderr:
                    parts.append(f"[stderr]\n{result.stderr}")
                parts.append(f"[exit {result.exit_code}]")
                return "\n".join(parts)

            elif tool_name == "write_file":
                self.write_file(tool_input["path"], tool_input["content"])
                return f"Wrote {tool_input['path']}"

            elif tool_name == "read_file":
                return self.read_file(tool_input["path"])

            elif tool_name == "list_files":
                files = self.list_files(tool_input.get("path", "."))
                return "\n".join(files) if files else "(empty directory)"

            elif tool_name == "snapshot":
                snap = self.snapshot(tool_input.get("name") or None)
                return f"Snapshot saved: {snap.name}"

            elif tool_name == "rollback":
                name = tool_input.get("name") or None
                self.rollback(name)
                return f"Rolled back to: {name or 'most recent snapshot'}"

            elif tool_name == "export":
                self.export(tool_input["container_path"], tool_input["host_path"])
                return f"Exported {tool_input['container_path']} → {tool_input['host_path']}"

            elif tool_name == "upload":
                self.upload(tool_input["host_path"], tool_input["container_path"])
                return f"Uploaded {tool_input['host_path']} → {tool_input['container_path']}"

            else:
                return f"Unknown tool: {tool_name}"

        except CageError as e:
            return f"Error: {e}"


# ── AsyncSandbox ──────────────────────────────────────────────────────────────

class AsyncSandbox:
    """Async wrapper around Sandbox for async agent loops.

    All methods are coroutines. Docker operations run in a thread pool.

    Usage::

        async with cage.AsyncSandbox(network="bridge") as env:
            await env.run("pip install requests")
            await env.snapshot()
            result = await env.run("python app.py")
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._session: Sandbox | None = None

    async def _get_session(self) -> Sandbox:
        if self._session is None:
            loop = asyncio.get_running_loop()
            self._session = await loop.run_in_executor(None, lambda: Sandbox(**self._kwargs))
        return self._session

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        session = await self._get_session()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: getattr(session, method)(*args, **kwargs))

    async def run(
        self, cmd: str, timeout: int = 120, workdir: str | None = None, compress: bool | None = None
    ) -> ExecResult:
        return await self._call("run", cmd, timeout, workdir, compress)

    async def install(self, *packages: str, manager: str = "apt-get") -> ExecResult:
        return await self._call("install", *packages, manager=manager)

    async def write_file(self, path: str, content: str | bytes) -> None:
        await self._call("write_file", path, content)

    async def read_file(self, path: str) -> str:
        return await self._call("read_file", path)

    async def upload(self, host_path: str, container_path: str) -> None:
        await self._call("upload", host_path, container_path)

    async def list_files(self, path: str = ".", max_depth: int | None = None) -> list[str]:
        return await self._call("list_files", path, max_depth)

    async def snapshot(self, name: str | None = None) -> Snapshot:
        return await self._call("snapshot", name)

    async def rollback(self, snapshot: Snapshot | str | None = None) -> None:
        await self._call("rollback", snapshot)

    async def export(self, container_path: str, host_path: str) -> None:
        await self._call("export", container_path, host_path)

    @property
    def workspace_path(self) -> Path | None:
        return self._session.workspace_path if self._session else None

    def tools(self) -> list[dict]:
        return TOOL_DEFINITIONS

    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        return await self._call("handle_tool_call", tool_name, tool_input)

    async def close(self) -> None:
        if self._session:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._session.close)
            self._session = None

    async def __aenter__(self) -> AsyncSandbox:
        await self._get_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
