"""The one-line reading under the response graphs and the tactile hint for the sum line (phase 9 design §12.3, P09)."""

from app.dsp.design import design_crossover, design_filter
from app.model.config_doc import CrossoverGroup, PeqBand, default_config_doc
from app.model.device_profile import M2X4HD
from app.model.filters import CrossoverDesign, FilterDesign
from app.sync.summary import analyse_band
from app.web.graph_reading import band_words, graph_reading, output_phrase, tactile_outputs

from .conftest import SERIAL

NAMES = ["Shakers", "Sub", "Out 3", "Out 4"]
INPUTS = ["In 1", "In 2"]


def _group(kind: str, freq: float, order: int = 4) -> CrossoverGroup:
    design = CrossoverDesign(kind=kind, family="lr", order=order, freq=freq)
    return CrossoverGroup(bypass=False, design=design, compiled=design_crossover(design))


def _cut(freq: float) -> PeqBand:
    design = FilterDesign(type="peaking", freq=freq, q=4, gain_db=-4)
    return PeqBand(bypass=False, design=design, compiled=design_filter(design))


def _doc():
    doc = default_config_doc(M2X4HD)
    for i, inp in enumerate(doc.inputs):  # In 1 feeds Shakers only; nothing else is routed
        for n, cell in enumerate(inp.routing):
            cell.enabled = i == 0 and n == 0
    return doc


def test_the_worked_example_reads_like_the_cards():
    doc = _doc()
    out = doc.outputs[0]
    out.crossover = [_group("hp", 20), _group("lp", 80)]
    out.peq[0] = _cut(40)
    doc.outputs[1].peq[0] = _cut(40)
    text = graph_reading(doc, [0, 1], NAMES, inputs=(0,), input_names=INPUTS)
    assert text == "Shakers: 20–80 Hz (steep), 1 EQ cut at 40 Hz. Sub: silent here (In 1 isn't routed to it)."
    # Fed by both inputs, the Sub is still silent; once routed it reads as full range with its cut.
    assert output_phrase(doc, 1, "Sub", None, INPUTS) == "Sub: silent here (In 1 and In 2 aren't routed to it)"
    doc.inputs[0].routing[1].enabled = True
    assert output_phrase(doc, 1, "Sub", (0,), INPUTS) == "Sub: full range, 1 EQ cut at 40 Hz"
    assert graph_reading(doc, [], NAMES) == ""


def test_band_words():
    doc = _doc()
    out = doc.outputs[0]
    assert band_words(analyse_band(out)) == "full range"
    out.crossover = [_group("lp", 80), CrossoverGroup()]
    assert band_words(analyse_band(out)) == "bass below 80 Hz (steep)"
    out.crossover = [_group("hp", 120, order=2), CrossoverGroup()]
    assert band_words(analyse_band(out)) == "everything above 120 Hz (gentle)"
    out.crossover = [_group("hp", 20, order=2), _group("lp", 80, order=8)]
    assert band_words(analyse_band(out)) == "20–80 Hz (gentle below, very steep above)"
    out.crossover = [_group("hp", 100), _group("lp", 80)]
    assert band_words(analyse_band(out)).startswith("nothing passes")


def test_muted_input_and_muted_output():
    doc = _doc()
    doc.inputs[0].mute = True
    assert output_phrase(doc, 0, "Shakers", (0,), INPUTS) == "Shakers: silent here (its input is muted)"
    doc.inputs[0].mute = False
    doc.outputs[0].mute = True
    assert output_phrase(doc, 0, "Shakers", (0,), INPUTS) == "Shakers: full range, muted (drawn as if un-muted)"


def test_tactile_outputs():
    class Meta:
        def __init__(self, tactile):
            self.tactile = tactile

    meta = {("output", 0): Meta(True), ("output", 1): Meta(False), ("input", 0): Meta(True)}
    assert tactile_outputs(meta, [0, 1, 2]) == [0]
    assert tactile_outputs(meta, [1]) == []
    assert tactile_outputs(None, [0]) == []


async def test_dashboard_response_has_the_reading_and_the_tactile_outputs(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers", tactile=True)
    await ctx.session.set_channel_meta("output", 1, name="Sub")
    await ctx.session.edit(0, {"outputs": {"2": {"mute": True}, "3": {"mute": True}}})
    data = (await ctx.http.get(f"/ui/devices/{SERIAL}/response")).json()
    assert data["reading"] == "Shakers: full range. Sub: silent here (In 1 isn't routed to it)."
    assert data["tactile"] == [0]
    await ctx.session.set_channel_meta("output", 0, tactile=False)
    data = (await ctx.http.get(f"/ui/devices/{SERIAL}/response")).json()
    assert data["tactile"] == []


async def test_response_tab_has_the_reading(ctx):
    await ctx.session.set_channel_meta("output", 0, name="Shakers")
    data = (await ctx.http.get(f"/ui/devices/{SERIAL}/presets/0/outputs/0/response")).json()
    assert data["reading"] == "Shakers: full range."
