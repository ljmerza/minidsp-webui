"""Phase 9 PR 6 (summary and diagnosis engine) in a real browser: the output cards of Meters & graph (the old
dashboard, at /meters since PR 9) say what each output does and flag the silent one, and the summary API reads the
same.

The state is the one the walkthroughs left the mock in (design 4.1 / 6.2): In 1 feeds Shakers (+4 dB route, +12 dB
output, 20-80 Hz Linkwitz-Riley 24 dB/oct, a 4 dB cut at 40 Hz, 8.75 ms), Out 3 and Out 4 (both muted); the Sub's
route was turned off (it keeps its 40 Hz cut); In 2 feeds nothing.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.api import Api
from tests.e2e.pages import visit

CUT = {"design": {"type": "peaking", "freq": 40, "q": 4, "gain_db": -4}, "bypass": False}
ROUTING = {
    "inputs": {
        "0": {
            "routing": {
                "0": {"enabled": True, "gain_db": 4},
                "1": {"enabled": False},
                "2": {"enabled": True},
                "3": {"enabled": True},
            }
        },
        "1": {"routing": {str(o): {"enabled": False} for o in range(4)}},
    },
    "outputs": {"0": {"gain_db": 12, "delay_ms": 8.75}, "2": {"mute": True}, "3": {"mute": True}},
}

WORKED_EXAMPLES = {
    "Shakers": "Plays 20–80 Hz (steep), fed by In 1. 1 EQ cut at 40 Hz. Waits 8.75 ms.",
    "Sub": "Plays full range, fed by nothing. 1 EQ cut at 40 Hz.",
    "Out 3": "Muted. Would play full range, fed by In 1.",
}


@pytest.fixture(scope="module")
def seed() -> Callable[[Api], None]:
    def walkthrough(api: Api) -> None:
        api.adopt_factory_defaults()
        api.name_channel("output", 0, "Shakers")
        api.name_channel("output", 1, "Sub")
        params = {"confirm": "true", "commit": "true"}
        api._ok(api.http.patch(f"{api.dev}/presets/0", params=params, json=ROUTING))
        for group, (kind, freq) in enumerate((("hp", 20), ("lp", 80))):
            design = {"kind": kind, "family": "lr", "order": 4, "freq": freq}
            body = {"design": design, "bypass": False}
            api._ok(api.http.put(f"{api.dev}/presets/0/outputs/0/crossover/{group}", params=params, json=body))
        api.put_peq_band("outputs", 0, 0, CUT)
        api.put_peq_band("outputs", 1, 0, CUT)
        api.wait_synced()

    return walkthrough


def test_the_summary_api_reads_the_worked_examples(api: Api):
    data = api._ok(api.http.get(f"{api.dev}/presets/0/summary"))
    sentences = {o["name"]: o["sentence"] for o in data["outputs"]}
    assert {name: sentences[name] for name in WORKED_EXAMPLES} == WORKED_EXAMPLES
    assert [i["sentence"] for i in data["inputs"]] == [
        "Level 0 dB, no EQ. Sent to Shakers (+4 dB), Out 3 and Out 4.",
        "Not sent to any output.",
    ]
    assert [o["badge"] for o in data["outputs"]] == ["check", "silent", "muted", "muted"]
    assert data["problems"][0]["text"] == "Sub is silent: no input is routed to it."


def test_the_cards_say_what_each_output_does(screen: Page, shoot):
    visit(screen, "/meters")
    cards = screen.locator(".output-card")
    for n, (name, sentence) in enumerate(WORKED_EXAMPLES.items()):
        card = cards.nth(n)
        expect(card.locator("h3")).to_have_text(name)
        expect(card.locator(".output-sentence")).to_have_text(sentence)
    sub = cards.nth(1)
    expect(sub.locator("[data-health-badge]")).to_have_text("⛔ Silent")
    expect(sub.locator(".output-reason")).to_contain_text("Sub is silent: no input is routed to it.")
    expect(sub.get_by_role("link", name="Open Routing")).to_be_visible()
    expect(cards.nth(0).locator("[data-health-badge]")).to_have_text("⚠ Check")
    expect(cards.nth(2).locator("[data-health-badge]")).to_have_text("🔇 Muted")
    sub.scroll_into_view_if_needed()
    shoot(screen, "p9-chunk6-dashboard-cards")


def test_marking_an_output_not_in_use_folds_its_note(screen: Page, api: Api, shoot):
    visit(screen, "/settings")
    guidance = screen.get_by_role("heading", name="Guidance")
    guidance.scroll_into_view_if_needed()
    expect(screen.get_by_label("High total level")).to_have_value("12")
    shoot(screen, "p9-chunk6-settings-guidance")
    flags = screen.locator("fieldset.channel-flags")
    expect(screen.get_by_role("group", name="How each channel is used")).to_be_visible()
    row = flags.locator("li", has_text="Out 4")
    row.get_by_label("Out 4: Not in use").check()
    flags.scroll_into_view_if_needed()
    shoot(screen, "p9-chunk6-settings-channel-flags")
    screen.get_by_role("button", name="Save channels").click()
    expect(screen.locator("#settings-channels .form-saved")).to_have_text("Saved.")
    assert api._ok(api.http.get(f"{api.dev}/presets/0/summary"))["outputs"][3]["badge"] == "not_in_use"

    visit(screen, "/meters")
    out4 = screen.locator(".output-card").nth(3)
    expect(out4.locator(".output-sentence")).to_have_text("Not in use: muted.")
    expect(out4.locator("[data-health-badge]")).to_have_text("◌ Not in use")

    # Put the flag back: pytest may run the module's other tests after this one (it groups items by their
    # parametrized fixtures), and they expect the walkthrough state where Out 4 is merely muted.
    visit(screen, "/settings")
    screen.locator("fieldset.channel-flags li", has_text="Out 4").get_by_label("Out 4: Not in use").uncheck()
    screen.get_by_role("button", name="Save channels").click()
    expect(screen.locator("#settings-channels .form-saved")).to_have_text("Saved.")
    assert api._ok(api.http.get(f"{api.dev}/presets/0/summary"))["outputs"][3]["badge"] == "muted"
