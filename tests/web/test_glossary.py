"""The glossary, its "?" toggletips and /help (phase 9 design §13, PR 5)."""

import re
from collections import Counter

import pytest

from app.main import STATIC_DIR
from app.web import glossary
from app.web.glossary import GROUPS, TERMS, help_button

TEMPLATES = STATIC_DIR.parent / "templates"

# Every id of the complete copy in design §13.2, group by group.
DESIGN_IDS = {
    "Levels": "volume level route-level input-level total level-limit dbfs meter clip headroom mute",
    "Shaping the sound": (
        "eq cut-boost q peq-type peq-specialist band steepness filter-stages raw templates compressor rounding "
        "calc-response mag-phase fir rew"
    ),
    "Timing": "delay samples line-up polarity",
    "Routing and inputs": "routing mono-sum source not-in-use tactile",
    "Presets and profiles": "device-preset editing-preset waiting profile same-as store-vs-play history",
    "Sync and the device": (
        "status st-sent st-sending st-waiting st-failed st-unknown st-changed st-cant-check last-sent check-device "
        "use-device forget send set-up remember overwrite dont-know"
    ),
    "Health badges": "b-ready b-silent b-muted b-not-in-use b-check b-overload",
}

# `title` attributes that only name an icon-only control or an abbreviation (the visible text is a glyph or "r12").
# Anything that explains something must be visible text or a "?" toggletip instead (design §13.1, §16.3).
TITLE_ALLOWLIST = {
    ("components/peq_editor.html", "Reset to flat"),  # the ⟲ icon button (it has the same aria-label)
    ("profiles/_history_rows.html", "Revision {{ e.revision }}"),  # "r12"
}

HELP_CALL = re.compile(r"""\bhelp\(\s*["']([\w-]+)["']""")
TITLE_ATTR = re.compile(r"""(?<![\w-]):?title\s*=\s*(["'])(.*?)\1""", re.S)


def _templates():
    return sorted(p for p in TEMPLATES.rglob("*.html"))


def _sentences(text: str) -> int:
    return len(re.findall(r"[.!?](?:\s|$)", text))


def test_the_glossary_has_every_term_of_the_design_in_its_group():
    for group, ids in DESIGN_IDS.items():
        assert [t.id for t in TERMS.values() if t.group == group] == ids.split(), group
    assert set(TERMS) == {i for ids in DESIGN_IDS.values() for i in ids.split()}
    assert list(GROUPS) == list(DESIGN_IDS)


@pytest.mark.parametrize("term", list(TERMS.values()), ids=list(TERMS))
def test_every_term_is_complete_and_short(term):
    assert term.label and term.short and term.group in GROUPS
    assert _sentences(term.short) <= 2, "a toggletip holds at most two sentences"
    for other in term.related:
        assert other in TERMS and other != term.id
    assert "PEQ" not in term.label and "biquad" not in term.short.lower().replace("called biquads", "")


def test_glossary_copy_uses_the_design_wording():
    assert TERMS["volume"].short.startswith("One volume for all outputs, applied after everything else")
    assert TERMS["q"].short.endswith("Q 0.7 about 21–77 Hz.")
    assert TERMS["delay"].short.endswith("The device allows up to 79.8 ms.")
    assert TERMS["st-cant-check"].label == "Can't check ⊘"
    assert TERMS["eq"].more.startswith("Also called PEQ (parametric EQ).")


def test_every_help_call_in_the_templates_names_a_glossary_term():
    used: Counter[str] = Counter()
    for path in _templates():
        for term_id in HELP_CALL.findall(path.read_text()):
            assert term_id in TERMS, f"{path.relative_to(TEMPLATES)}: help({term_id!r}) is not in app/web/glossary.py"
            used[term_id] += 1
    assert used, "no template uses help()"


def test_no_template_explains_things_in_a_title_attribute():
    found = []
    for path in _templates():
        name = path.relative_to(TEMPLATES).as_posix()
        for match in TITLE_ATTR.finditer(path.read_text()):
            if (name, match.group(2)) not in TITLE_ALLOWLIST:
                found.append(f"{name}: {match.group(0)}")
    assert not found, "hover-only explanations (make them visible text or a help() toggletip):\n" + "\n".join(found)


def test_the_toggletip_markup_is_a_disclosure_button_and_a_labelled_hidden_dialog():
    html = str(help_button("q", "/mdsp"))
    button = re.search(r"<button[^>]*>", html).group(0)
    assert 'type="button"' in button and 'aria-expanded="false"' in button and "data-tt-btn" in button
    assert 'aria-label="What is Width (Q)?"' in button
    pop_id = re.search(r'aria-controls="([^"]+)"', button).group(1)
    pop = re.search(rf'<span id="{pop_id}"[^>]*>', html).group(0)
    assert 'role="dialog"' in pop and "hidden" in pop and f'aria-labelledby="{pop_id}-t"' in pop
    assert f'id="{pop_id}-t">Width (Q)</strong>' in html
    assert TERMS["q"].short in html
    assert 'href="/mdsp/help#q"' in html
    assert "<div" not in html and "<p" not in html  # phrasing content only: valid inside headings and <p>
    assert html.count("<button") == 1 and "title=" not in html


def test_each_toggletip_gets_its_own_ids():
    first, second = str(help_button("q")), str(help_button("q"))
    ids = re.findall(r'\bid="([^"]+)"', first + second)
    assert len(ids) == len(set(ids)) == 4


def test_inside_alpine_templates_the_ids_are_bound_per_copy():
    html = str(help_button("raw", key="band.index"))
    assert ' id="' not in html and 'aria-controls="' not in html.replace(":aria-controls", "")
    assert re.search(r":aria-controls=\"&#39;tt-raw-\d+-&#39; \+ \(band.index\)\"", html)
    assert re.search(r':id="&#39;tt-raw-\d+-&#39; \+ \(band.index\)"', html)


def test_an_unknown_term_fails_loudly():
    with pytest.raises(KeyError, match="nope"):
        glossary.term("nope")


async def test_help_page_lists_every_term_grouped_with_anchors_and_needs_no_script(http):
    response = await http.get("/help")
    assert response.status_code == 200
    html = response.text
    assert "<h1>Glossary</h1>" in html
    main = html.split('<main id="main"', 1)[1].split("</main>", 1)[0]
    assert "x-data" not in main and "<script" not in main and "<template" not in main
    for group in GROUPS:
        assert f">{group}</h2>" in main
    for term in TERMS.values():
        block = re.search(rf'<div class="help-term" id="{term.id}">(.*?)</div>', main, re.S)
        assert block, term.id
        assert term.short.replace("'", "&#39;").replace('"', "&#34;") in block.group(1)
        if term.more:
            assert term.more.replace("'", "&#39;").replace('"', "&#34;") in block.group(1)
        for other in term.related:
            assert f'href="#{other}"' in block.group(1)
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))


async def test_glossary_is_in_the_navigation(http):
    html = (await http.get("/help")).text
    nav = html.split('id="sidenav"', 1)[1].split("</nav>", 1)[0]
    assert '<a href="/help" aria-current="page">Glossary</a>' in nav


async def test_every_page_loads_the_toggletip_script_and_styles(http):
    html = (await http.get("/")).text
    assert "/static/js/help.js?v=" in html and "/static/css/help.css?v=" in html
    assert (STATIC_DIR / "js" / "help.js").is_file() and (STATIC_DIR / "css" / "help.css").is_file()
