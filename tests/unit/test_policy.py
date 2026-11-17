import pytest

from app.core.policy.engine import Actor, PolicyDenied, PolicyEngine


def test_ui_can_edit_and_unknown_actions_are_denied():
    policy = PolicyEngine()
    assert policy.evaluate(Actor(interface="ui"), "document.operate").allowed
    assert not policy.evaluate(Actor(interface="ui"), "document.launch_missiles").allowed


def test_mcp_is_read_only_by_default():
    policy = PolicyEngine()
    mcp = Actor(interface="mcp")
    assert policy.evaluate(mcp, "document.read").allowed
    for action in ("document.operate", "document.import", "document.trash", "system.configure"):
        assert not policy.evaluate(mcp, action).allowed, action


def test_plugins_and_unknown_interfaces_get_nothing():
    policy = PolicyEngine()
    assert not policy.evaluate(Actor(interface="plugin"), "document.read").allowed
    assert not policy.evaluate(Actor(interface="telnet"), "document.read").allowed


def test_destructive_actions_need_explicit_approval():
    policy = PolicyEngine()
    decision = policy.evaluate(Actor(interface="ui"), "document.trash")
    assert not decision.allowed and decision.requires_approval
    assert policy.evaluate(Actor(interface="ui", approved=True), "document.trash").allowed


def test_safe_mode_blocks_edits_but_not_reading():
    policy = PolicyEngine()
    policy.enter_safe_mode("test")
    user = Actor(interface="ui", approved=True)
    assert policy.evaluate(user, "document.read").allowed
    assert policy.evaluate(user, "document.export").allowed
    for action in ("document.operate", "document.merge", "document.split", "document.trash", "document.revert"):
        assert not policy.evaluate(user, action).allowed, action
    with pytest.raises(PolicyDenied):
        policy.authorize(user, "document.operate")


def test_trashed_documents_cannot_be_edited():
    policy = PolicyEngine()
    user = Actor(interface="ui")
    assert not policy.evaluate(user, "document.operate", document_trashed=True).allowed
    assert policy.evaluate(user, "document.restore", document_trashed=True).allowed
