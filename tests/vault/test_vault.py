"""Tests for Vault encrypted secrets store and MCP tools."""
from __future__ import annotations

import pytest

from north9.vault.core import SecretMeta, Vault

MASTER_KEY = "test-master-key-12345"


# ---------------------------------------------------------------------------
# Core Vault tests
# ---------------------------------------------------------------------------


def test_vault_init(tmp_path):
    """Vault initializes without error."""
    v = Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY)
    assert v is not None
    v.close()


def test_set_get_roundtrip(tmp_path):
    """set() then get() returns the same value."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("OPENAI_API_KEY", "sk-abc123")
        assert v.get("OPENAI_API_KEY") == "sk-abc123"


def test_wrong_key_raises(tmp_path):
    """Decrypting with a different master key raises InvalidToken."""
    from cryptography.fernet import InvalidToken

    db = tmp_path / "secrets.db"
    with Vault(db_path=db, master_key=MASTER_KEY) as v:
        v.set("MY_SECRET", "super-secret-value")

    with Vault(db_path=db, master_key="wrong-key-totally-different") as v2:
        with pytest.raises(InvalidToken):
            v2.get("MY_SECRET")


def test_delete_returns_true(tmp_path):
    """delete() returns True when secret existed."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("TO_DELETE", "bye")
        assert v.delete("TO_DELETE") is True


def test_delete_removes_secret(tmp_path):
    """After delete(), get() raises KeyError."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("GONE", "value")
        v.delete("GONE")
        with pytest.raises(KeyError):
            v.get("GONE")


def test_delete_nonexistent_returns_false(tmp_path):
    """delete() returns False for a secret that doesn't exist."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        assert v.delete("NONEXISTENT") is False


def test_list_returns_secret_meta(tmp_path):
    """list() returns SecretMeta instances without values."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("KEY_A", "value-a", tags=["api"])
        v.set("KEY_B", "value-b", tags=["db"])
        results = v.list()
        assert len(results) == 2
        assert all(isinstance(r, SecretMeta) for r in results)
        names = {r.name for r in results}
        assert names == {"KEY_A", "KEY_B"}
        # Values never appear in SecretMeta
        for r in results:
            assert not hasattr(r, "value")


def test_list_filter_by_tag(tmp_path):
    """list(tag=...) returns only secrets with that tag."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("API_KEY", "val1", tags=["api", "production"])
        v.set("DB_PASS", "val2", tags=["db", "production"])
        v.set("LOCAL_KEY", "val3", tags=["local"])

        api_secrets = v.list(tag="api")
        assert len(api_secrets) == 1
        assert api_secrets[0].name == "API_KEY"

        prod_secrets = v.list(tag="production")
        assert len(prod_secrets) == 2


def test_has_true(tmp_path):
    """has() returns True when secret exists."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("EXISTS", "yes")
        assert v.has("EXISTS") is True


def test_has_false(tmp_path):
    """has() returns False when secret does not exist."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        assert v.has("MISSING") is False


def test_env_returns_dict(tmp_path):
    """env() returns a dict mapping names to plaintext values."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("KEY1", "value1")
        v.set("KEY2", "value2")
        result = v.env("KEY1", "KEY2")
        assert result == {"KEY1": "value1", "KEY2": "value2"}


def test_vault_no_master_key_raises(tmp_path, monkeypatch):
    """Vault() without master key and without env var raises ValueError."""
    monkeypatch.delenv("NORTH9_VAULT_KEY", raising=False)
    with pytest.raises(ValueError, match="NORTH9_VAULT_KEY"):
        Vault(db_path=tmp_path / "secrets.db")


def test_vault_uses_env_var(tmp_path, monkeypatch):
    """Vault() reads master key from NORTH9_VAULT_KEY env var."""
    monkeypatch.setenv("NORTH9_VAULT_KEY", MASTER_KEY)
    with Vault(db_path=tmp_path / "secrets.db") as v:
        v.set("FROM_ENV", "test")
        assert v.get("FROM_ENV") == "test"


def test_set_overwrites(tmp_path):
    """set() on existing name updates the value."""
    with Vault(db_path=tmp_path / "secrets.db", master_key=MASTER_KEY) as v:
        v.set("KEY", "old-value")
        v.set("KEY", "new-value")
        assert v.get("KEY") == "new-value"


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_vault(tmp_path, monkeypatch):
    """Fixture: patch vault.mcp._vault with a test Vault instance."""
    import north9.vault.mcp as mcp_module

    test_vault = Vault(db_path=tmp_path / "mcp_secrets.db", master_key=MASTER_KEY)
    original = mcp_module._vault
    mcp_module._vault = test_vault
    yield test_vault
    mcp_module._vault = original
    test_vault.close()


def test_mcp_vault_set(mcp_vault):
    """vault_set stores a secret."""
    from north9.vault.mcp import vault_get, vault_set

    result = vault_set("MCP_KEY", "mcp-value")
    assert "MCP_KEY" in result
    # Confirm it's actually stored
    assert vault_get("MCP_KEY") == "mcp-value"


def test_mcp_vault_set_with_tags(mcp_vault):
    """vault_set stores a secret with tags."""
    from north9.vault.mcp import vault_set

    result = vault_set("TAGGED_KEY", "tagged-value", tags="api,prod")
    assert "TAGGED_KEY" in result
    assert "api" in result or "prod" in result or "Stored" in result


def test_mcp_vault_get(mcp_vault):
    """vault_get retrieves a stored secret."""
    from north9.vault.mcp import vault_get, vault_set

    vault_set("GET_KEY", "get-value")
    assert vault_get("GET_KEY") == "get-value"


def test_mcp_vault_get_not_found(mcp_vault):
    """vault_get returns error string for missing secret, does not raise."""
    from north9.vault.mcp import vault_get

    result = vault_get("TOTALLY_MISSING")
    assert "Error" in result
    assert "TOTALLY_MISSING" in result


def test_mcp_vault_list(mcp_vault):
    """vault_list returns names but not values."""
    from north9.vault.mcp import vault_list, vault_set

    vault_set("LIST_A", "secret-a")
    vault_set("LIST_B", "secret-b")
    result = vault_list()
    assert "LIST_A" in result
    assert "LIST_B" in result
    assert "secret-a" not in result
    assert "secret-b" not in result


def test_mcp_vault_list_by_tag(mcp_vault):
    """vault_list filters by tag."""
    from north9.vault.mcp import vault_list, vault_set

    vault_set("TAG_KEY", "tag-value", tags="mytag")
    vault_set("OTHER_KEY", "other-value", tags="othertag")
    result = vault_list(tag="mytag")
    assert "TAG_KEY" in result
    assert "OTHER_KEY" not in result


def test_mcp_vault_delete(mcp_vault):
    """vault_delete removes a secret."""
    from north9.vault.mcp import vault_delete, vault_get, vault_set

    vault_set("DEL_KEY", "to-delete")
    result = vault_delete("DEL_KEY")
    assert "DEL_KEY" in result
    # Now it should be gone
    get_result = vault_get("DEL_KEY")
    assert "Error" in get_result


def test_mcp_vault_delete_missing(mcp_vault):
    """vault_delete on nonexistent secret returns not-found message."""
    from north9.vault.mcp import vault_delete

    result = vault_delete("GHOST_KEY")
    assert "not found" in result.lower() or "GHOST_KEY" in result


def test_mcp_vault_env(mcp_vault):
    """vault_env returns shell export commands."""
    from north9.vault.mcp import vault_env, vault_set

    vault_set("ENV_KEY1", "val1")
    vault_set("ENV_KEY2", "val2")
    result = vault_env("ENV_KEY1,ENV_KEY2")
    assert "export ENV_KEY1=" in result
    assert "export ENV_KEY2=" in result
    assert "val1" in result
    assert "val2" in result
    assert "WARNING" in result or "sensitive" in result.lower()


def test_mcp_vault_env_missing_key(mcp_vault):
    """vault_env returns error when a requested secret is missing."""
    from north9.vault.mcp import vault_env

    result = vault_env("DOES_NOT_EXIST")
    assert "Error" in result
