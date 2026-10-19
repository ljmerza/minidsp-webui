"""The one status model (phase 9 design §11.1, PR 7): headline priority, the check overlay and the words."""

from types import SimpleNamespace

import pytest

from app.sync import status as st
from app.sync.status import (
    CheckSummary,
    Progress,
    check_summary,
    counts_of,
    headline,
    open_changes,
    path_states,
)
from app.web.glossary import term

WRITER = {
    "outputs.0.gain_db": "synced",
    "outputs.0.mute": "pending",
    "outputs.1.gain_db": "failed",
    "outputs.1.mute": "unknown",
    "outputs.2.gain_db": "synced",
}


def test_path_states_overlays_changed_on_sent_settings_only():
    states = path_states(WRITER, changed={"outputs.0.gain_db", "outputs.1.gain_db", "outputs.1.mute"})
    assert states == {
        "outputs.0.gain_db": "changed",
        "outputs.0.mute": "sending",
        "outputs.1.gain_db": "failed",  # a failed send stays failed
        "outputs.1.mute": "unknown",  # the app doesn't know it: unknown, not changed
        "outputs.2.gain_db": "sent",
    }


def test_pending_writes_wait_on_a_preset_that_isnt_playing():
    assert path_states(WRITER, waiting=True)["outputs.0.mute"] == "waiting"


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"failed": 2, "changed": 3, "sending": 4, "waiting": 5, "unknown": 6, "sent": 7}, ("failed", "2 failed")),
        ({"changed": 3, "sending": 4, "waiting": 5, "unknown": 6, "sent": 7}, ("changed", "3 changed on device")),
        ({"sending": 4, "waiting": 5, "unknown": 6, "sent": 7}, ("sending", "Sending 0 of 4")),
        ({"waiting": 5, "unknown": 6, "sent": 7}, ("waiting", "5 waiting for preset 2")),
        ({"unknown": 6, "sent": 7}, ("unknown", "6 unknown")),
        ({"sent": 7}, ("sent", "All sent")),
        ({}, ("sent", "All sent")),
    ],
)
def test_headline_priority(counts, expected):
    assert headline(counts, preset=1) == expected


def test_sending_counts_up_and_says_when_outputs_are_muted():
    assert headline({"sending": 64}, preset=0, progress=Progress(120, 184)) == ("sending", "Sending 120 of 184")
    text = headline({"sending": 64}, preset=0, progress=Progress(120, 184, outputs_muted=True))[1]
    assert text == "Sending 120 of 184 · outputs muted"
    assert headline({"sending": 1}, preset=0)[1] == "Sending 1 setting"
    # A batch that grew since it was last counted still adds up.
    assert headline({"sending": 10}, preset=0, progress=Progress(0, 4))[1] == "Sending 0 of 10"


def test_counts_of():
    counts = counts_of(path_states(WRITER, changed={"outputs.0.gain_db"}))
    assert counts == {"failed": 1, "changed": 1, "sending": 1, "waiting": 0, "unknown": 1, "sent": 1}


REPORT = {
    "preset": 0,
    "paths": ["outputs.0.gain_db", "outputs.0.peq.2.compiled", "outputs.1.gain_db", "outputs.2.delay_ms"],
    "accepted": ["outputs.1.gain_db"],
    "marked_unknown": [],
    "writes_at": 10,
}


def test_open_changes_drop_used_forgotten_and_sent_again():
    assert open_changes(REPORT, 0) == {"outputs.0.gain_db", "outputs.0.peq.2.compiled", "outputs.2.delay_ms"}
    forgotten = {**REPORT, "marked_unknown": ["outputs.0.peq.2"]}  # a whole band forgotten covers its paths
    assert open_changes(forgotten, 0) == {"outputs.0.gain_db", "outputs.2.delay_ms"}
    # Sent again after the check (a later write count) closes it; a write before the check doesn't.
    sent = {"outputs.0.gain_db": 11, "outputs.2.delay_ms": 10}
    assert open_changes(REPORT, 0, sent) == {"outputs.0.peq.2.compiled", "outputs.2.delay_ms"}


def test_open_changes_of_another_preset_or_without_a_check():
    assert open_changes(REPORT, 1) == set()
    assert open_changes(None, 0) == set()
    assert open_changes({**REPORT, "writes_at": None}, 0, {"outputs.0.gain_db": 99}) == {
        "outputs.0.gain_db",
        "outputs.0.peq.2.compiled",
        "outputs.2.delay_ms",
    }


def test_open_changes_reads_a_verify_report():
    report = SimpleNamespace(
        preset=0,
        drifted=[SimpleNamespace(path="outputs.0.gain_db"), SimpleNamespace(path="outputs.1.gain_db")],
        accepted=[],
        marked_unknown=["outputs.1.gain_db"],
        writes_at=3,
    )
    assert open_changes(report, 0, {"outputs.0.gain_db": 3}) == {"outputs.0.gain_db"}
    assert open_changes(report, 0, {"outputs.0.gain_db": 4}) == set()


def _summary(**kw):
    base = {"preset": 0, "checked_at": "2026-10-18T14:02:09+00:00", "matched": 60, "changed": 0, "differ": 0}
    return CheckSummary(**{**base, "cant_check": 100, **kw})


def test_check_result_words():
    assert _summary(changed=92).result == "92 changed on device; 100 can't be checked"
    assert _summary().result == "everything the device can report matches (60 settings); 100 can't be checked"
    assert _summary(differ=1).result == (
        "nothing changed on device; 1 unknown setting differs from the app's values; 100 can't be checked"
    )
    assert _summary(matched=0).result == "nothing to compare: no setting has been sent yet; 100 can't be checked"
    assert _summary(cant_check=0, changed=1).result == "1 changed on device"
    assert _summary(found=2).result == "2 changed on device, all sent or forgotten since; 100 can't be checked"
    assert _summary().time == "14:02"
    assert _summary().cant_check_text == (
        "100 settings can't be checked: routing, on/off switches, compressor timing and FIR."
    )


def test_check_summary_counts_open_changes_only():
    report = SimpleNamespace(
        preset=0,
        checked_at="2026-10-18T14:02:09+00:00",
        counts={"ok": 3, "drift": 2, "unknown": 1, "unverifiable": 100},
        mismatched=[object()],
        drifted=[object(), object()],
        source="ui",
    )
    summary = check_summary(report, 0, {"outputs.0.gain_db"})
    assert (summary.changed, summary.matched, summary.differ, summary.cant_check) == (1, 3, 1, 100)
    assert check_summary(report, 1, set()) is None
    assert check_summary(None, 0, set()) is None


def test_every_state_has_a_glyph_word_term_and_class():
    for state in (*st.ORDER, st.CANT_CHECK):
        assert st.GLYPHS[state] and st.WORDS[state] and st.CSS[state]
        assert term(st.TERMS[state]).label  # explained in the glossary
    assert st.word("waiting", 1) == "Waiting for preset 2"
    # The words never claim a match or "sync" (design §2).
    words = " ".join(st.WORDS.values()).lower()
    assert "match" not in words and "sync" not in words
