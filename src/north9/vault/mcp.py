"""Vault MCP server — exposes encrypted secrets to AI agents via MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Vault

mcp = FastMCP("vault")

_vault: Vault | None = None
_ENV_KEY = "NORTH9_VAULT_KEY"
_DEFAULT_DB = Path.home() / ".vault" / "secrets.db"


def _get_vault() -> Vault:
    """Return the global Vault instance, initializing lazily."""
    global _vault
    if _vault is None:
        master_key = os.environ.get(_ENV_KEY)
        if not master_key:
            raise RuntimeError(
                "NORTH9_VAULT_KEY env var is not set. "
                "Run: export NORTH9_VAULT_KEY='your-master-key'"
            )
        db_path = os.environ.get("VAULT_DB", str(_DEFAULT_DB))
        _vault = Vault(db_path=db_path, master_key=master_key)
    return _vault


@mcp.tool()
def vault_set(name: str, value: str, tags: str = "") -> str:
    """Store a secret. Encrypted at rest with Fernet (AES-128-CBC).

    Args:
        name: Secret name (e.g. "OPENAI_API_KEY", "github-token")
        value: The secret value
        tags: Comma-separated tags (e.g. "api,openai,production")

    Requires NORTH9_VAULT_KEY env var to be set.
    """
    try:
        v = _get_vault()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        v.set(name, value, tags=tag_list)
        tag_info = f" (tags: {', '.join(tag_list)})" if tag_list else ""
        return f"Stored secret '{name}'{tag_info}."
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def vault_get(name: str) -> str:
    """Retrieve a secret by name. Returns the plaintext value."""
    try:
        v = _get_vault()
        return v.get(name)
    except KeyError:
        return f"Error: secret '{name}' not found"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def vault_list(tag: str = "") -> str:
    """List stored secrets (names and tags only — values never shown)."""
    try:
        v = _get_vault()
        secrets = v.list(tag=tag)
        if not secrets:
            msg = "No secrets stored"
            if tag:
                msg += f" with tag '{tag}'"
            return msg + "."
        lines = []
        for s in secrets:
            tag_part = f"  [{', '.join(s.tags)}]" if s.tags else ""
            lines.append(f"  {s.name}{tag_part}")
        header = f"Secrets{' (tag: ' + tag + ')' if tag else ''} ({len(secrets)} total):"
        return header + "\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def vault_delete(name: str) -> str:
    """Delete a secret permanently."""
    try:
        v = _get_vault()
        deleted = v.delete(name)
        if deleted:
            return f"Deleted secret '{name}'."
        return f"Secret '{name}' not found."
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def vault_env(names: str) -> str:
    """Get multiple secrets as shell export commands.

    Args:
        names: Comma-separated secret names

    Returns shell commands like: export OPENAI_API_KEY="sk-..."
    Use with caution — only in sandboxed environments.
    """
    try:
        v = _get_vault()
        name_list = [n.strip() for n in names.split(",") if n.strip()]
        if not name_list:
            return "Error: no secret names provided"
        missing = [n for n in name_list if not v.has(n)]
        if missing:
            return f"Error: secrets not found: {', '.join(missing)}"
        values = v.env(*name_list)
        lines = [
            "# WARNING: output contains sensitive values — handle with care",
        ]
        for k, val in values.items():
            # Escape double quotes in value
            safe_val = val.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'export {k}="{safe_val}"')
        return "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"


def _install() -> None:
    """Register Vault as an MCP server in Claude Code settings and CLAUDE.md."""
    import sys

    settings_paths = [
        Path.home() / ".claude" / "settings.json",
        Path(".claude") / "settings.json",
    ]

    vault_mcp_entry = {
        "command": sys.executable,
        "args": ["-m", "north9.vault"],
        "env": {},
    }

    for settings_path in settings_paths:
        if settings_path.exists():
            try:
                with open(settings_path) as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                settings = {}

            settings.setdefault("mcpServers", {})
            settings["mcpServers"]["north9-vault"] = vault_mcp_entry

            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            print(f"Registered vault MCP server in {settings_path}")
            break
    else:
        # Create in home .claude
        target = Path.home() / ".claude" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        settings = {"mcpServers": {"vault": vault_mcp_entry}}
        with open(target, "w") as f:
            json.dump(settings, f, indent=2)
        print(f"Created {target} with vault MCP server")

    # Add CLAUDE.md section
    claude_md = Path("CLAUDE.md")
    vault_section = '''
## Vault

Encrypted secrets for agent use. Requires NORTH9_VAULT_KEY env var.

```python
vault_set("OPENAI_API_KEY", "sk-...")    # store encrypted
vault_get("OPENAI_API_KEY")              # retrieve
vault_list()                             # names only, no values
vault_env("OPENAI_API_KEY,GITHUB_TOKEN") # export commands
```

Never hardcode credentials. Store in Vault, retrieve at runtime.
'''

    if claude_md.exists():
        content = claude_md.read_text()
        if "## Vault" not in content:
            claude_md.write_text(content + vault_section)
            print("Added Vault section to CLAUDE.md")
    else:
        claude_md.write_text(f"# Project\n{vault_section}")
        print("Created CLAUDE.md with Vault section")

    print()
    print("Setup complete. Next steps:")
    print("  1. Set NORTH9_VAULT_KEY in your environment before using vault tools:")
    print("       export NORTH9_VAULT_KEY='your-secure-master-key'")
    print("  2. Restart Claude Code to pick up the new MCP server.")
    print("  3. Use vault_set / vault_get / vault_list / vault_delete / vault_env tools.")


if __name__ == "__main__":
    mcp.run()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="vault", description="Vault MCP server — encrypted secrets for agents")
    parser.add_argument("--install", action="store_true", help="Install Vault into Claude Code")
    parser.add_argument("--db", default=None, help="SQLite DB path")
    args, _ = parser.parse_known_args()
    if args.install:
        _install()
        return
    mcp.run()
