"""Adopt wizard flows and CLI-script import/export through the session."""

from pathlib import Path

import pytest

from app.formats.cli_script import parse_script
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.sync.session import AdoptMode, ConfirmationRequired, DesiredValidationError

from ..conftest import SERIAL
from .conftest import drain, settle

PI_NOTES = (Path(__file__).parents[2] / "fixtures" / "pi_notes_commands.txt").read_text()


async def test_trust_from_the_pi_notes_writes_nothing(session, fake, repo):
    result = await session.adopt(AdoptMode.TRUST, script=PI_NOTES)
    parsed = parse_script(PI_NOTES)
    assert result.writes == 0 and result.flush is None
    assert result.known == len(parsed.device_paths) and result.unknown > 0
    assert result.revision == 1
    await settle(session)
    assert fake.configs == []

    doc = session.desired(0)
    assert doc.outputs[0].gain_db == 12.0 and doc.outputs[0].delay_ms == 9.0
    assert doc.outputs[0].crossover[0].bypass is False
    status = session.sync_status(0)
    assert status["outputs.0.gain_db"] == "synced"
    assert status["outputs.0.crossover.0.compiled.0"] == "synced"
    assert status["outputs.0.peq.0.compiled"] == "unknown"  # the script says nothing about it

    assert not session.needs_adoption
    assert session.adoption["mode"] == "trust" and session.adoption["script"] is True
    assert (await repo.get_setting(f"adopted.{SERIAL}"))["preset"] == 0
    [rev] = await repo.list_revisions(SERIAL, 0)
    assert rev.source == "adopt" and rev.summary == "adopt: trust from a CLI script"
    await drain(session)
    [event] = await repo.list_events(kind="adopt")
    assert event.data["writes"] == 0 and "0 write(s)" in event.message


async def test_trust_all_trusts_the_defaults_too(session):
    result = await session.adopt("trust", script=PI_NOTES, trust_all=True)
    assert result.unknown == 0
    assert set(session.sync_status(0).values()) == {"synced"}


async def test_trusted_paths_then_sync_normally(session, fake):
    await session.adopt("trust", script=PI_NOTES)
    await session.edit(0, {"outputs": {"0": {"gain_db": 10.0}}})
    await settle(session)
    assert fake.configs == [{"outputs": [{"index": 0, "gain": 10.0}]}]


async def test_push_needs_confirmation_and_is_mute_wrapped(session, fake):
    with pytest.raises(ConfirmationRequired):
        await session.adopt("push")
    assert fake.configs == [] and session.needs_adoption
    result = await session.adopt("push", script=PI_NOTES, confirm=True)
    assert result.flush is not None and result.flush.ok
    assert result.writes == len(fake.configs) > 2
    assert result.unknown == 0
    assert fake.configs[0] == {"outputs": [{"index": n, "mute": True} for n in range(4)]}
    # Outputs 2/3 stay muted per the script; 0/1 are released at the end.
    assert fake.configs[-1] == {"outputs": [{"index": 0, "mute": False}, {"index": 1, "mute": False}]}


async def test_unknown_mode_writes_nothing(session, fake):
    result = await session.adopt("unknown", preset=2)
    assert result.known == 0 and result.writes == 0
    assert set(session.sync_status(2).values()) == {"unknown"}
    assert session.adoption["preset"] == 2
    await settle(session, 2)
    assert fake.configs == []


async def test_adopt_an_explicit_doc(session):
    doc = default_config_doc(M2X4HD)
    doc.outputs[3].mute = True
    result = await session.adopt("trust", doc=doc, preset=1)
    assert result.preset == 1 and result.unknown == 0
    assert session.desired(1).outputs[3].mute is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "steal"}, "unknown adopt mode"),
        ({"mode": "trust", "script": "output 0 gain banana"}, "the script has errors"),
        ({"mode": "trust", "script": "x", "doc": default_config_doc(M2X4HD)}, "either a script or a document"),
        ({"mode": "push", "script": "output 0 gain 13", "confirm": True}, "outputs.0.gain_db"),
    ],
)
async def test_adopt_rejects(session, fake, kwargs, message):
    mode = kwargs.pop("mode")
    with pytest.raises(DesiredValidationError) as err:
        await session.adopt(mode, **kwargs)
    assert message in str(err.value)
    assert session.needs_adoption and fake.configs == []


async def test_adopt_without_repo(make_session):
    s = await make_session(with_repo=False)
    result = await s.adopt("trust")
    assert result.revision == 1 and not s.needs_adoption


# --- import / export -----------------------------------------------------------------------------------------------


async def test_import_script_is_a_bulk_apply(session, fake, repo):
    script = "output 2 gain -- -3\noutput 2 invert on\ngain -- -20\n"
    result = await session.import_cli_script(0, script)
    assert [w.message for w in result.parsed.warnings]  # the master line is ignored with a warning
    edit = result.edit
    assert edit.device_paths == ["outputs.2.gain_db", "outputs.2.invert"]
    assert edit.flush is not None and edit.flush.ok
    assert fake.configs[0] == {"outputs": [{"index": 2, "mute": True}]}
    assert fake.configs[1] == {"outputs": [{"index": 2, "gain": -3.0, "invert": True}]}
    assert fake.configs[-1] == {"outputs": [{"index": 2, "mute": False}]}
    [rev] = await repo.list_revisions(SERIAL, 0)
    assert rev.source == "import" and rev.summary == "import CLI script (2 setting(s))"


async def test_import_onto_defaults_and_inactive_preset(session, fake):
    await session.edit(1, {"outputs": {"3": {"gain_db": -4.0}}})
    result = await session.import_cli_script(1, "output 0 gain -- -1", base="default")
    assert result.edit.flush.staged
    assert session.desired(1).outputs[3].gain_db == 0.0
    assert fake.configs == []


async def test_import_guards(session, fake):
    with pytest.raises(DesiredValidationError, match="line 2"):
        await session.import_cli_script(0, "output 0 gain -1\noutput 9 gain 0\n")
    with pytest.raises(DesiredValidationError, match="no preset settings"):
        await session.import_cli_script(0, "# nothing\ngain -- -10\n")
    with pytest.raises(DesiredValidationError, match="unknown base"):
        await session.import_cli_script(0, "output 0 gain 0", base="other")
    with pytest.raises(ConfirmationRequired):
        await session.import_cli_script(0, PI_NOTES)  # output 0: 0 -> +12 dB
    assert session.desired(0) == default_config_doc(M2X4HD)
    result = await session.import_cli_script(0, PI_NOTES, confirm=True)
    assert result.edit.flush.ok and fake.configs


async def test_export(session):
    await session.edit(0, {"outputs": {"1": {"gain_db": -4.5}}})
    text = await session.export_cli_script(0)
    assert "output 1 gain -- -4.5" in text
    assert text.splitlines()[-1] == "output 3 mute off"
    assert "mute on" not in await session.export_cli_script(0, mute_wrap=False)
