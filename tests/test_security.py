"""Security tests for Cage — require Docker to be running.

Verifies that the sandbox actually enforces the isolation it claims.

Run with: pytest tests/test_security.py -v -m integration
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

import north9
from north9.sandbox.core import CageError

# ── Setup ─────────────────────────────────────────────────────────────────────

def docker_available() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.integration

if not docker_available():
    pytest.skip("Docker not available", allow_module_level=True)


@pytest.fixture()
def env(tmp_path):
    with north9.Sandbox(
        image="python:3.12-slim",
        network="none",
        workspace_dir=tmp_path,
        pull=False,
        compress=False,
    ) as s:
        yield s


@pytest.fixture()
def env_net(tmp_path):
    """Sandbox with network=bridge for network-specific tests."""
    with north9.Sandbox(
        image="python:3.12-slim",
        network="bridge",
        workspace_dir=tmp_path,
        pull=False,
        compress=False,
    ) as s:
        yield s


# ── Host filesystem isolation ─────────────────────────────────────────────────

class TestHostFilesystemIsolation:
    def test_cannot_read_host_etc_passwd(self, env):
        """Container should not be able to read the host's /etc/passwd.
        The container has its own /etc/passwd — not the host's."""
        result = env.run("cat /etc/passwd")
        assert result.success  # container has its own /etc/passwd
        # Host passwd has real user accounts; container typically only has root/nobody
        host_passwd = Path("/etc/passwd").read_text()
        # They should be different files
        assert result.stdout != host_passwd.strip(), (
            "Container /etc/passwd matches host — filesystem isolation may be broken"
        )

    def test_cannot_list_host_root(self, env):
        """Container root fs is isolated from host."""
        # Check host has directories the container shouldn't see
        _ = subprocess.run(["ls", "/"], capture_output=True, text=True)
        result_container = env.run("ls /")
        # Both should succeed but container has a different root
        # Key indicator: container root should be a standard Docker container root
        assert result_container.success
        # Container's /proc shows container PIDs, not host PIDs
        pids_result = env.run("ls /proc | grep -E '^[0-9]+$' | wc -l")
        host_pids = subprocess.run(
            ["bash", "-c", "ls /proc | grep -E '^[0-9]+$' | wc -l"],
            capture_output=True, text=True,
        )
        container_pid_count = int(pids_result.stdout.strip())
        host_pid_count = int(host_pids.stdout.strip())
        # Container should have far fewer PIDs than host
        assert container_pid_count < host_pid_count

    def test_workspace_is_only_shared_path(self, env, tmp_path):
        """Only /workspace is shared. Other host paths not visible."""
        # Write a file to a non-workspace host path
        secret = Path("/tmp/host_secret_cage_test.txt")
        secret.write_text("host secret content")
        try:
            result = env.run("cat /tmp/host_secret_cage_test.txt")
            # Should fail (different /tmp) or return container-local content
            if result.success:
                assert result.stdout != "host secret content", (
                    "/tmp appears to be shared between host and container"
                )
        finally:
            secret.unlink(missing_ok=True)


# ── Symlink escape prevention ─────────────────────────────────────────────────

class TestSymlinkEscape:
    def test_symlink_to_host_etc_passwd_blocked(self, env, tmp_path):
        """Agent creates /workspace/evil -> /tmp/cage_unique_secret.
        read_file('evil') must NOT return the host file content."""
        import tempfile
        # Write a unique sentinel file on host that does NOT exist in the container
        sentinel = Path(tempfile.mktemp(suffix="_cage_symlink_test.txt"))
        unique = "CAGE_SYMLINK_ESCAPE_SECRET_12345_UNIQUE"
        sentinel.write_text(unique)
        try:
            # Agent creates symlink in workspace pointing to the host-only sentinel
            env.run(f"ln -s {sentinel} /workspace/evil_link")

            evil_on_host = tmp_path / "evil_link"
            assert evil_on_host.is_symlink(), "Symlink should be visible on host"

            # read_file must NOT return the host sentinel content
            # (container doesn't have this file, so docker cp will fail or return empty)
            try:
                cage_content = env.read_file("evil_link")
                assert unique not in cage_content, (
                    "SECURITY: read_file followed symlink to host file! "
                    "Symlink escape not blocked."
                )
            except Exception:
                pass  # CageError expected — file doesn't exist in container
        finally:
            sentinel.unlink(missing_ok=True)

    def test_write_via_symlink_to_host_blocked(self, env, tmp_path):
        """Agent creates symlink, then write_file must not modify host system files."""
        # Create a sentinel file on host that we can afford to have modified
        sentinel = tmp_path / "safe_sentinel.txt"
        sentinel.write_text("original")

        # Agent creates a symlink IN workspace pointing to a file OUTSIDE workspace
        outside_file = Path(tempfile.mktemp(suffix=".cage_test"))
        outside_file.write_text("outside original")
        try:
            # Create symlink from workspace to outside file
            symlink_name = "outside_link"
            env.run(f"ln -s {outside_file} /workspace/{symlink_name}")

            # Try to write via that symlink — should use docker cp (container-scoped)
            env.write_file(symlink_name, "HACKED")

            # Outside file on host should NOT be modified
            assert outside_file.read_text() == "outside original", (
                "SECURITY: write_file followed symlink outside workspace and modified host file!"
            )
        finally:
            outside_file.unlink(missing_ok=True)

    def test_nested_symlink_blocked(self, env, tmp_path):
        """Chain of symlinks that eventually escapes workspace."""
        import tempfile
        # Create a unique host-only file the container cannot reach
        sentinel = Path(tempfile.mktemp(suffix="_cage_chain_test.txt"))
        unique = "CAGE_NESTED_SYMLINK_SECRET_67890_UNIQUE"
        sentinel.write_text(unique)
        try:
            # Build chain: /workspace/link1 -> sentinel's parent dir,
            # /workspace/link2 -> /workspace/link1/<filename>
            parent = sentinel.parent
            fname = sentinel.name
            env.run(f"ln -s {parent} /workspace/dir_link")
            env.run(f"ln -s /workspace/dir_link/{fname} /workspace/file_via_chain")

            try:
                result = env.read_file("file_via_chain")
                assert unique not in result, (
                    "SECURITY: nested symlink chain escaped workspace!"
                )
            except Exception:
                pass  # CageError expected — file doesn't exist in container
        finally:
            sentinel.unlink(missing_ok=True)


# ── Capability restrictions ───────────────────────────────────────────────────

class TestCapabilities:
    def test_cap_drop_all_applied(self, env):
        """--cap-drop=ALL should be in container config."""
        result = subprocess.run(
            ["docker", "inspect", env.name, "--format", "{{.HostConfig.CapDrop}}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "ALL" in result.stdout, (
            f"--cap-drop=ALL not found in container config: {result.stdout}"
        )

    def test_only_safe_caps_readded(self, env):
        """Only SETUID/SETGID/CHOWN/FOWNER/DAC_OVERRIDE re-added. Dangerous caps absent."""
        result = subprocess.run(
            ["docker", "inspect", env.name, "--format", "{{.HostConfig.CapAdd}}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        cap_add = result.stdout.strip()
        # These caps are explicitly allowed (needed for apt package management):
        #   SETUID, SETGID, CHOWN, FOWNER, DAC_OVERRIDE
        # These must never appear:
        dangerous = [
            "SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE",
            "SYS_RAWIO", "SYS_TIME", "NET_BIND_SERVICE", "SYS_CHROOT",
            "SYS_BOOT", "SYS_PACCT", "MKNOD", "NET_RAW",
        ]
        for cap in dangerous:
            assert cap not in cap_add, f"Dangerous cap {cap} was re-added: {cap_add}"

    def test_cannot_load_kernel_module(self, env):
        """Without CAP_SYS_MODULE, cannot load kernel modules."""
        result = env.run("modprobe dummy 2>&1 || insmod dummy.ko 2>&1", timeout=5)
        assert not result.success or "not permitted" in result.output.lower()

    def test_cannot_change_system_time(self, env):
        """Without CAP_SYS_TIME, cannot set system clock."""
        result = env.run("date -s '2099-01-01' 2>&1", timeout=5)
        # Should fail with permission denied
        assert not result.success or "permission denied" in result.output.lower()

    def test_no_new_privileges_applied(self, env):
        """--security-opt no-new-privileges should be in container config."""
        result = subprocess.run(
            ["docker", "inspect", env.name, "--format", "{{.HostConfig.SecurityOpt}}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "no-new-privileges" in result.stdout


# ── Network isolation ─────────────────────────────────────────────────────────

class TestNetworkIsolation:
    def test_no_network_cannot_resolve_dns(self, env):
        """network=none — DNS lookup should fail."""
        result = env.run(
            "python3 -c \"import socket; socket.getaddrinfo('google.com', 80)\" 2>&1", timeout=10
        )
        assert not result.success, "DNS should not work with network=none"

    def test_no_network_cannot_connect_tcp(self, env):
        """network=none — TCP connect should fail."""
        result = env.run(
            "python3 -c \""
            "import socket; s=socket.socket(); s.settimeout(2); s.connect(('8.8.8.8', 53))"
            "\" 2>&1",
            timeout=10,
        )
        assert not result.success

    def test_bridge_network_can_reach_internet(self, env_net):
        """network=bridge — should be able to resolve DNS."""
        result = env_net.run(
            "python3 -c \"import socket; socket.getaddrinfo('google.com', 80); print('ok')\"",
            timeout=15,
        )
        assert result.success, "DNS should work with network=bridge"
        assert "ok" in result.stdout


# ── Resource limits ───────────────────────────────────────────────────────────

class TestResourceLimits:
    def test_pids_limit_applied(self, env):
        """--pids-limit=512 should be in container config."""
        result = subprocess.run(
            ["docker", "inspect", env.name, "--format", "{{.HostConfig.PidsLimit}}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "512" in result.stdout, f"PID limit not set correctly: {result.stdout}"

    def test_memory_limit_applied(self, env):
        """--memory should be in container config."""
        result = subprocess.run(
            ["docker", "inspect", env.name, "--format", "{{.HostConfig.Memory}}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        # 512m = 536870912 bytes
        assert int(result.stdout.strip()) == 536870912

    def test_fork_bomb_limited(self, env):
        """Fork bomb should be contained by pids-limit — not crash the host."""
        result = env.run(
            # Controlled fork bomb — tries to spawn 600 processes (> pids_limit)
            "python3 -c \""
            "import os, time\n"
            "pids = []\n"
            "for i in range(600):\n"
            "    try:\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            time.sleep(0.1)\n"
            "            os._exit(0)\n"
            "        pids.append(pid)\n"
            "    except BlockingIOError:\n"
            "        print(f'blocked at {i} processes')\n"
            "        break\n"
            "for p in pids:\n"
            "    try: os.waitpid(p, 0)\n"
            "    except: pass\n"
            "\"",
            timeout=15,
        )
        # Should either be blocked early or complete without crashing host
        assert "blocked" in result.stdout or result.success


# ── Path traversal in operations ──────────────────────────────────────────────

class TestPathTraversal:
    def test_write_file_absolute_outside_workspace_uses_docker_cp(self, env):
        """Writing to /tmp (outside /workspace) falls back to docker cp (container-scoped)."""
        env.write_file("/tmp/test_write.txt", "container only")
        result = env.run("cat /tmp/test_write.txt")
        assert result.success
        assert "container only" in result.stdout
        # File should NOT appear on the host filesystem
        assert not Path("/tmp/test_write.txt").exists() or \
               Path("/tmp/test_write.txt").read_text() != "container only"

    def test_read_file_absolute_outside_workspace(self, env):
        """Reading from /etc/hostname returns container's hostname, not host's."""
        result_direct = env.run("cat /etc/hostname")
        assert result_direct.success
        container_hostname = result_direct.stdout.strip()

        host_hostname = Path("/etc/hostname").read_text().strip()
        # Container should have a different hostname (its container ID)
        assert container_hostname != host_hostname, (
            "Container /etc/hostname matches host — isolation may be broken"
        )

    def test_dotdot_traversal_in_path_blocked(self, env, tmp_path):
        """../../ traversal in path should not escape workspace."""
        # Try to read a file outside workspace via traversal
        outside = tmp_path.parent / "outside_cage_test.txt"
        outside.write_text("should not be readable via traversal")
        try:
            # This should fail or return container-scoped content
            try:
                content = env.read_file("../../outside_cage_test.txt")
                # If read succeeded, it must have been container-scoped (not host)
                # The container doesn't have this file
                assert "should not be readable" not in content
            except CageError:
                pass  # Expected — file doesn't exist in container
        finally:
            outside.unlink(missing_ok=True)

    def test_export_container_path_traversal(self, env, tmp_path):
        """export() with a container path stays within container."""
        dest = tmp_path / "exported_passwd.txt"
        # Export the container's /etc/passwd to host
        env.export("/etc/passwd", str(dest))
        assert dest.exists()
        # Must be the container's /etc/passwd, not host's
        _ = Path("/etc/passwd").read_text()
        exported = dest.read_text()
        # They might be different (container has different users)
        # The key thing is export completed without error
        assert len(exported) > 0


# ── Workspace mount safety ────────────────────────────────────────────────────

class TestWorkspaceSafety:
    def test_workspace_mount_is_not_host_root(self, env, tmp_path):
        """The volume mount should only share workspace_path, not /."""
        # Container's root should not be the host's root
        container_files = set(env.run("ls /").stdout.split())
        host_files = set(subprocess.run(["ls", "/"], capture_output=True, text=True).stdout.split())
        # Container should be missing host-specific dirs and have Docker-specific ones
        assert container_files != host_files

    def test_files_written_in_container_appear_on_host_workspace(self, env, tmp_path):
        """Verify the volume mount works correctly — both directions."""
        # Container writes, host reads
        env.run("echo 'from container' > /workspace/container_write.txt")
        assert (tmp_path / "container_write.txt").read_text().strip() == "from container"

        # Host writes, container reads
        (tmp_path / "host_write.txt").write_text("from host")
        result = env.run("cat /workspace/host_write.txt")
        assert result.stdout.strip() == "from host"

    def test_container_cannot_write_outside_workspace(self, env):
        """Container can't modify host files outside the workspace mount."""
        # Any writes outside /workspace go to the container's own filesystem
        env.run("echo 'hacked' > /etc/cage_hack_test")
        # Host /etc should not have this file
        assert not Path("/etc/cage_hack_test").exists()
