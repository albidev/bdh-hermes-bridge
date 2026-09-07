from session_buffer import DurableSessionBuffer


def test_buffer_survives_reopen_and_preserves_scope_metadata(tmp_path):
    path = tmp_path / "buffer.json"
    first = DurableSessionBuffer(path)
    first.append("session-1", {"user": "q", "assistant": "a", "vault_id": "core", "context_only": True})

    restarted = DurableSessionBuffer(path)
    assert restarted.snapshot("session-1") == [{
        "user": "q", "assistant": "a", "vault_id": "core", "context_only": True,
    }]

    restarted.remove("session-1")
    assert restarted.snapshot("session-1") == []


def test_buffer_caps_turns_and_ignores_corrupt_state(tmp_path):
    path = tmp_path / "buffer.json"
    store = DurableSessionBuffer(path)
    for i in range(3):
        store.append("session-1", {"user": str(i)}, max_turns=2)
    assert [row["user"] for row in store.snapshot("session-1")] == ["1", "2"]

    path.write_text("not json", encoding="utf-8")
    assert DurableSessionBuffer(path).snapshot("session-1") == []
