"""Identity persistence must not expose keys before chmod or destroy old keys."""
import os
import stat

import pytest

from agentforge_sdk.client import AgentIdentity


@pytest.mark.skipif(os.name != "posix", reason="POSIX creation-permission guarantee")
def test_identity_private_from_first_write_even_with_permissive_umask(tmp_path, monkeypatch):
    import agentforge_sdk.client as client_module
    original = client_module.tempfile.mkstemp
    observed = []
    def inspect_creation(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd, path
    monkeypatch.setattr(client_module.tempfile, "mkstemp", inspect_creation)
    identity = AgentIdentity.generate()
    target = tmp_path / "identity.json"
    old_umask = os.umask(0)
    try:
        identity.save(target)
    finally:
        os.umask(old_umask)
    assert observed == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert AgentIdentity.load(target).did == identity.did


def test_identity_overwrite_is_atomic_and_replaces_insecure_permissions(tmp_path):
    target = tmp_path / "identity.json"
    first, second = AgentIdentity.generate(), AgentIdentity.generate()
    first.save(target)
    target.chmod(0o644)
    second.save(target)
    assert AgentIdentity.load(target).did == second.did
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".agentforge-identity-*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixture")
def test_identity_save_replaces_symlink_instead_of_following_it(tmp_path):
    original = tmp_path / "unrelated.txt"
    original.write_text("must not be overwritten")
    target = tmp_path / "identity.json"
    target.symlink_to(original)
    identity = AgentIdentity.generate()
    identity.save(target)
    assert original.read_text() == "must not be overwritten"
    assert not target.is_symlink()
    assert AgentIdentity.load(target).did == identity.did


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_identity_save_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch, failure):
    import agentforge_sdk.client as client_module
    target = tmp_path / "identity.json"
    original = AgentIdentity.generate()
    original.save(target)
    before = target.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("injected filesystem failure")
    monkeypatch.setattr(client_module.os, failure, fail)
    with pytest.raises(OSError, match="injected"):
        AgentIdentity.generate().save(target)
    assert target.read_bytes() == before
    assert list(tmp_path.glob(".agentforge-identity-*")) == []
