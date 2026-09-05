"""Affinity keying: stable as a conversation grows, distinct between conversations."""

from __future__ import annotations

from tokenbiryani.core.session import SESSION_HEADER, fingerprint, session_key


def convo(turns, system="You are a helpful assistant.", tools=None):
    body = {
        "model": "claude-test-1",
        "system": system,
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
            for i in range(turns)
        ],
    }
    if tools:
        body["tools"] = tools
    return body


def test_fingerprint_survives_conversation_growth():
    assert fingerprint(convo(2)) == fingerprint(convo(20))


def test_different_system_prompts_are_different_sessions():
    assert fingerprint(convo(4)) != fingerprint(convo(4, system="You are terse."))


def test_tools_participate_in_the_fingerprint():
    a = convo(4, tools=[{"name": "read_file"}])
    b = convo(4, tools=[{"name": "read_file"}, {"name": "write_file"}])
    assert fingerprint(a) != fingerprint(b)


def test_tool_order_does_not_matter():
    a = convo(4, tools=[{"name": "b"}, {"name": "a"}])
    b = convo(4, tools=[{"name": "a"}, {"name": "b"}])
    assert fingerprint(a) == fingerprint(b)


def test_explicit_header_wins():
    key = session_key(convo(4), {SESSION_HEADER: "my-session"})
    assert key == "hdr:my-session"
    assert session_key(convo(4), {}) .startswith("fp:")


def test_structured_content_blocks_are_handled():
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    assert fingerprint(body)


def test_scope_namespaces_the_session_key():
    """Two tenants sending the same session header must not share an entry."""
    headers = {SESSION_HEADER: "shared-name"}
    assert session_key(convo(4), headers, scope="tenant-a") != session_key(
        convo(4), headers, scope="tenant-b"
    )


def test_scope_namespaces_the_fingerprint_too():
    """The likelier collision: two callers running the same agent."""
    assert session_key(convo(4), {}, scope="tenant-a") != session_key(
        convo(4), {}, scope="tenant-b"
    )


def test_the_same_tenant_still_shares_one_key():
    assert session_key(convo(2), {}, scope="t") == session_key(convo(20), {}, scope="t")


def test_an_absent_scope_leaves_the_key_unprefixed():
    """Callers that pass no scope keep the original format."""
    assert session_key(convo(4), {}).startswith("fp:")
    assert session_key(convo(4), {SESSION_HEADER: "s"}).startswith("hdr:")
