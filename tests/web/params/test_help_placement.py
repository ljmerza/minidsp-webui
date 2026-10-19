"""Where the "?" toggletips sit (phase 9 design §13.1, PR 5 acceptance): every term from the walkthroughs' "Top
confusing terms" (docs/phase9-friction.md) has one where it appears, and no page repeats an id."""

import re

import pytest

PAGES = [
    "/",
    "/meters",
    "/inputs",
    "/outputs",
    "/routing",
    "/inputs/1",
    "/outputs/1",
    "/outputs/1?tab=crossover",
    "/outputs/1?tab=peq",
    "/outputs/1?tab=delay",
    "/outputs/1?tab=polarity",
    "/outputs/1?tab=compressor",
    "/outputs/1?tab=response",
    "/inputs/1?tab=level",
    "/device",
    "/settings",
    "/help",
]

# Friction "Top confusing terms" -> (page, glossary ids that must have a "?" there). Terms the server renders inside
# Alpine templates are counted by their bound markup (help(..., key=...)) as well.
CONFUSING = {
    "PEQ": [("/outputs/1?tab=peq", ["eq"]), ("/inputs/1", ["eq"])],
    "Q": [("/outputs/1?tab=peq", ["q"])],
    "Filter types": [("/outputs/1?tab=peq", ["peq-type", "peq-specialist"])],
    "Crossover on / bypassed; LP / HP": [("/meters", ["band"]), ("/outputs/1?tab=crossover", ["band"])],
    "LR4 / Butterworth; slope": [("/outputs/1?tab=crossover", ["steepness"])],
    "Biquads; Group 1 / Group 2": [("/outputs/1?tab=crossover", ["filter-stages"])],
    "Raw coefficients": [("/outputs/1?tab=crossover", ["raw"]), ("/outputs/1?tab=peq", ["raw"])],
    "dBFS": [("/meters", ["dbfs"]), ("/outputs/1?tab=compressor", ["dbfs"])],
    "Peak hold / CLIP / Reset clip": [("/meters", ["meter", "clip"]), ("/", ["meter"])],
    "Gain ceiling": [("/settings", ["level-limit"]), ("/routing", ["level-limit"])],
    "Route gain vs output gain": [("/routing", ["route-level"]), ("/outputs/1", ["level"]), ("/meters", ["level"])],
    "Mono sum": [("/routing", ["mono-sum"])],
    "Polarity": [("/outputs/1", ["polarity"])],
    "Samples": [("/outputs/1?tab=delay", ["samples"])],
    "Electrical (response / sum)": [("/meters", ["calc-response"]), ("/outputs/1?tab=response", ["calc-response"])],
    "Device preset / editing preset / templates / profile": [
        ("/routing", ["device-preset", "editing-preset"]),
        ("/outputs/1?tab=crossover", ["templates"]),
    ],
    "Staged": [("/routing", ["editing-preset"])],
    "Sync words": [("/routing", ["status"]), ("/device", ["status", "check-device"])],
    "Device actions": [("/device", ["check-device", "set-up"])],
    "Source": [("/routing", ["source"])],
    "Volume": [("/routing", ["volume"])],
}


def _help_ids(html: str) -> set[str]:
    return set(re.findall(r'href="[^"]*/help#([\w-]+)"', html))


@pytest.mark.parametrize("path", PAGES)
async def test_pages_render_toggletips_with_unique_ids(ctx, path):
    response = await ctx.http.get(path)
    assert response.status_code == 200, path
    html = response.text
    ids = re.findall(r'\sid="([^"]+)"', html)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"{path}: duplicate ids {duplicates}"
    for button in re.findall(r"<button[^>]*data-tt-btn[^>]*>", html):
        target = re.search(r'\saria-controls="([^"]+)"', button)
        if target:  # bound ones (inside Alpine templates) get theirs from Alpine
            assert f'id="{target.group(1)}"' in html, button
    assert "title=" not in html.split('<main id="main"', 1)[1].replace('title="Reset to flat"', ""), path


@pytest.mark.parametrize(("term", "where"), list(CONFUSING.items()), ids=list(CONFUSING))
async def test_every_confusing_term_has_a_toggletip_where_it_appears(ctx, term, where):
    for path, ids in where:
        html = (await ctx.http.get(path)).text
        missing = set(ids) - _help_ids(html)
        assert not missing, f"{term}: no '?' for {sorted(missing)} on {path}"


async def test_waiting_changes_are_visible_text_not_a_tooltip(ctx):
    await ctx.session.edit(1, {"outputs": {"0": {"gain_db": -3.0}}})
    html = (await ctx.http.get("/routing")).text
    assert "Preset 2: 1 waiting" in html and "/help#waiting" in html
    bar = (await ctx.http.get("/routing?preset=2")).text
    assert (
        "You&#39;re editing preset 2. It isn&#39;t playing" in bar or "You're editing preset 2. It isn't playing" in bar
    )


async def test_vocabulary_on_the_editing_pages(ctx):
    html = (await ctx.http.get("/outputs/1?tab=crossover")).text
    assert "<h2>Band (crossover)" in html and "Settings on this output" in html
    assert '<span class="chain-text">Compressor off</span>' in html
    assert '<span class="chain-text">EQ none</span>' in html
    assert "Filter stages used:" in html and ">Templates</span>" in html and "Steepness" in html
    main = html.split('<main id="main"', 1)[1]
    assert "Group " not in main and "Uses <strong" not in main
    routing = (await ctx.http.get("/routing")).text
    assert "Route level from In 1 to Out 1 in dB" in routing and "Gain from" not in routing
    header = (await ctx.http.get("/ui/header")).text
    assert ">Device preset</span>" in header and ">Volume</label>" in header and "Vol<" not in header
