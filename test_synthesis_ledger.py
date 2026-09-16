"""Invariants for the synthesis ledger.

The ledger exists to answer one question per target: "has this transcript
already been submitted?". Everything here is a behaviour contract on that
answer, because getting it wrong in either direction is a real failure:

- too permissive -> the same room is re-submitted on every pass (wasted local
  model minutes, duplicate candidates);
- too strict -> a transcript is silently dropped for good, which is the
  opposite failure and harder to notice.
"""
import json

from synthesis_ledger import SynthesisLedger


def test_fresh_target_has_nothing_recorded(tmp_path):
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    assert ledger.sha_for("room-1") is None
    assert ledger.seq_for("room-1") is None
    assert ledger.unchanged("room-1", "abc123") is False


def test_recorded_digest_is_reported_unchanged(tmp_path):
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    ledger.record("room-1", sha="abc123", seq=118)
    assert ledger.unchanged("room-1", "abc123") is True


def test_a_different_digest_is_not_unchanged(tmp_path):
    """New content must be eligible again — the whole point of the gate."""
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    ledger.record("room-1", sha="abc123", seq=118)
    assert ledger.unchanged("room-1", "def456") is False


def test_digests_are_scoped_per_target(tmp_path):
    """Two rooms with identical content are still two distinct targets."""
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    ledger.record("room-1", sha="same", seq=1)
    assert ledger.unchanged("room-2", "same") is False


def test_record_survives_a_restart(tmp_path):
    """The answer must outlive the process, or a restart re-submits everything."""
    path = tmp_path / "ledger.json"
    SynthesisLedger(path).record("room-1", sha="abc123", seq=118, synthesis_id="syn-1")

    reloaded = SynthesisLedger(path)
    assert reloaded.unchanged("room-1", "abc123") is True
    assert reloaded.seq_for("room-1") == 118


def test_seq_cursor_records_which_messages_were_handled(tmp_path):
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    ledger.record("room-1", sha="abc123", seq=118)
    assert ledger.seq_for("room-1") == 118


def test_missing_ledger_file_means_nothing_recorded(tmp_path):
    """A missing file is not an error: it re-synthesizes once, then converges."""
    ledger = SynthesisLedger(tmp_path / "does-not-exist" / "ledger.json")
    assert ledger.unchanged("room-1", "abc123") is False


def test_corrupt_ledger_does_not_block_synthesis(tmp_path):
    """Corrupt bookkeeping must not make a vault permanently unlearnable.

    Failing closed here would mean refusing to synthesize forever because of a
    damaged file, which is worse than one duplicate submission.
    """
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = SynthesisLedger(path)
    assert ledger.unchanged("room-1", "abc123") is False

    ledger.record("room-1", sha="abc123")
    assert SynthesisLedger(path).unchanged("room-1", "abc123") is True


def test_entry_without_a_digest_is_ignored(tmp_path):
    """A malformed row must not read back as a valid authorisation."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({
        "version": 1,
        "targets": {"room-1": {"last_synth_seq": 5}},
    }), encoding="utf-8")
    assert SynthesisLedger(path).unchanged("room-1", "") is False


def test_incomplete_identity_is_not_recorded(tmp_path):
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    ledger.record("", sha="abc123")
    ledger.record("room-1", sha="")
    assert ledger.sha_for("room-1") is None
