"""An agentic turn must yield its ANSWER, not its opening line.

Regression for a transcript that was silently near-empty
-------------------------------------------------------
`rebuild_turns` reconstructed one user/assistant pair per user message by taking
the FIRST assistant it saw and resetting the pending user. In an agentic session
that first assistant is an empty tool-call announcement:

    user  -> assistant(finish=tool_calls, text="") -> tool -> assistant(finish=tool_calls)
          -> ... -> assistant(finish=stop, text=<the real answer>)

Measured on a 52-message session: the turn's real answer was 9858 chars at row 39,
and the transcript actually submitted was 479 chars containing neither of the two
answers. The synthesis therefore reported "extractor produced no concepts" on a
session full of durable findings — the extractor was correct, the input was
emptied.

These tests build that exact shape and assert on the substance of the transcript.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from session_synthesis_watcher import TranscriptIdleWatcher


def _db(path, rows, session_id="s1", profile_name="default"):
    """rows: list of (role, content, finish_reason)."""
    db = sqlite3.connect(path)
    db.executescript("""
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
    """)
    db.execute(
        "insert into sessions values (?, 'tui', ?, 1000.0, null)",
        (session_id, profile_name),
    )
    for i, (role, content, finish) in enumerate(rows, start=1):
        db.execute(
            "insert into messages(session_id,role,content,finish_reason,active) "
            "values (?,?,?,?,1)",
            (session_id, role, content, finish),
        )
    db.commit()
    db.close()


AGENTIC_TURN = [
    ("user", "@client-a how does the catalog work?", None),
    ("assistant", "", "tool_calls"),
    ("tool", '{"output": "a large tool dump"}', None),
    ("assistant", "Let me read the key files.", "tool_calls"),
    ("tool", '{"content": "file contents"}', None),
    ("assistant", "", "tool_calls"),
    ("assistant", "THE REAL ANSWER: the tenant_db introspector already exists.", "stop"),
]


def _watcher(tmp_path, monkeypatch, rows, profile_name="default"):
    db_path = tmp_path / "state.db"
    _db(db_path, rows, profile_name=profile_name)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "mention_prefixes": {"client-a": "vault-a"},
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")
    watcher.extra_db_paths = []
    return watcher


def test_the_answer_survives_an_agentic_turn(tmp_path, monkeypatch):
    """The final assistant text must reach the transcript.

    This is the defect: the empty tool-call announcement came first and consumed
    the turn, so the answer 6 rows later was dropped.
    """
    watcher = _watcher(tmp_path, monkeypatch, AGENTIC_TURN)
    turns = watcher.rebuild_turns("s1")

    assert len(turns) == 1, "one user message is one turn"
    assert "THE REAL ANSWER" in turns[0]["assistant"], (
        "the turn's answer must be in the transcript, not the opening line"
    )
    assert turns[0]["user"] == "@client-a how does the catalog work?"


def test_intermediate_assistant_text_is_kept_too(tmp_path, monkeypatch):
    """The narration between tool calls is the reasoning, so it is kept."""
    watcher = _watcher(tmp_path, monkeypatch, AGENTIC_TURN)
    assistant = watcher.rebuild_turns("s1")[0]["assistant"]

    assert "Let me read the key files." in assistant
    assert "THE REAL ANSWER" in assistant


def test_tool_rows_never_appear_in_the_transcript(tmp_path, monkeypatch):
    """Tool payloads are huge and not conversational; they stay out."""
    watcher = _watcher(tmp_path, monkeypatch, AGENTIC_TURN)
    assistant = watcher.rebuild_turns("s1")[0]["assistant"]

    assert "a large tool dump" not in assistant
    assert "file contents" not in assistant


def test_a_long_answer_is_not_truncated_at_1500_chars(tmp_path, monkeypatch):
    """The observed answer was 9858 chars; the old cap kept the preamble only."""
    long_answer = "SUBSTANCE " * 1000  # 10000 chars
    rows = [
        ("user", "@client-a question", None),
        ("assistant", "", "tool_calls"),
        ("assistant", long_answer, "stop"),
    ]
    watcher = _watcher(tmp_path, monkeypatch, rows)
    assistant = watcher.rebuild_turns("s1")[0]["assistant"]

    assert len(assistant) > 1500, "a 10k answer must not be cut to 1500"
    assert assistant.count("SUBSTANCE") > 500


def test_turns_are_split_at_each_user_message(tmp_path, monkeypatch):
    """Two user messages stay two turns, each with its own answer."""
    rows = [
        ("user", "@client-a first", None),
        ("assistant", "", "tool_calls"),
        ("assistant", "ANSWER ONE", "stop"),
        ("user", "@client-a second", None),
        ("assistant", "", "tool_calls"),
        ("assistant", "ANSWER TWO", "stop"),
    ]
    watcher = _watcher(tmp_path, monkeypatch, rows)
    turns = watcher.rebuild_turns("s1")

    assert len(turns) == 2
    assert "ANSWER ONE" in turns[0]["assistant"]
    assert "ANSWER TWO" not in turns[0]["assistant"], (
        "a turn must not absorb the NEXT turn's answer"
    )
    assert "ANSWER TWO" in turns[1]["assistant"]


def test_a_turn_with_no_assistant_text_is_dropped(tmp_path, monkeypatch):
    """A user message followed only by tool calls has no answer to contribute."""
    rows = [
        ("user", "@client-a only tool calls", None),
        ("assistant", "", "tool_calls"),
        ("tool", '{"output": "dump"}', None),
    ]
    watcher = _watcher(tmp_path, monkeypatch, rows)

    assert watcher.rebuild_turns("s1") == []


def test_a_truncated_response_is_not_treated_as_the_answer(tmp_path, monkeypatch):
    """finish_reason='length' means the text is incomplete, so it is skipped.

    A cut-off response is not reliable evidence of what the session concluded.
    """
    rows = [
        ("user", "@client-a question", None),
        ("assistant", "", "tool_calls"),
        ("assistant", "half a sentence that got cut off", "length"),
    ]
    watcher = _watcher(tmp_path, monkeypatch, rows)

    assert watcher.rebuild_turns("s1") == [], (
        "a truncated response must not stand in for the answer"
    )


def test_the_mentioned_actor_still_authorises_the_vault(tmp_path, monkeypatch):
    """The transcript change must not disturb the actor gate."""
    watcher = _watcher(tmp_path, monkeypatch, AGENTIC_TURN)
    turns = watcher.rebuild_turns("s1")

    assert turns[0]["vault_id"] == "vault-a"
