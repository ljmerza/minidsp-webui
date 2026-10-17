"""The REW Import/Export partial: rendering, and its Alpine component driven in node against a fake fetch."""

import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.model.device_profile import M2X4HD
from app.web.templating import templates

NODE = shutil.which("node")


def render(bank: str, root: str = "/mdsp") -> str:
    template = templates.env.get_template("partials/rew_buttons.html")
    return template.render(root=root, serial=911111, preset=2, session=SimpleNamespace(profile=M2X4HD), rew_bank=bank)


def component_config(html: str) -> dict:
    match = re.search(r"x-data='rewButtons\((.*?)\)'", html, re.DOTALL)
    assert match
    return json.loads(match.group(1))


def test_peq_partial():
    html = render("outputs.1.peq")
    cfg = component_config(html)
    assert cfg == {
        "bank": "outputs.1.peq",
        "crossover": False,
        "previewUrl": "/mdsp/api/v1/import/rew",
        "importUrl": "/mdsp/api/v1/devices/911111/presets/2/import/rew",
        "exportUrl": "/mdsp/api/v1/devices/911111/presets/2/export/rew?download=true&bank=outputs.1.peq",
        "bands": 10,
        "groups": 2,
        "biquads": 4,
    }
    assert "Import REW" in html and "Export REW" in html
    assert "All bands (clears the rest)" in html and "Biquad" not in html


def test_crossover_partial():
    html = render("outputs.0.crossover", root="")
    cfg = component_config(html)
    assert cfg["crossover"] is True and cfg["importUrl"] == "/api/v1/devices/911111/presets/2/import/rew"
    assert '<option value="all">Both</option>' in html and "All bands" not in html


def test_input_bank_size():
    assert component_config(render("inputs.0.peq"))["bands"] == M2X4HD.peq_per_input


DRIVER = r"""
const vm = require('node:vm');
const calls = [];
const toasts = [];
const replies = JSON.parse(process.argv[2]);
const sandbox = {
  window: {
    mdsp: { toast: (m, l) => toasts.push([m, l || 'error']) },
    confirm: () => true,
    location: { href: '' },
  },
  fetch: async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({ url, body });
    const [status, data] = replies.shift();
    return { ok: status < 400, status, json: async () => data };
  },
  JSON, Array, Number, console,
};
vm.createContext(sandbox);
vm.runInContext(process.argv[1], sandbox);
const cfg = JSON.parse(process.argv[3]);
const c = sandbox.window.rewButtons(cfg);
(async () => {
  const file = { name: 'eq.txt', size: 20, text: async () => 'biquad1,...' };
  await c.choose({ target: { files: [file], value: 'x' } });
  const afterPreview = { parsed: c.parsed, slots: c.slots(), canApply: c.canApply(), summary: c.summary() };
  c.index = '3';
  c.pick = '2';
  await c.apply();
  c.exportFile();
  const href = sandbox.window.location.href;
  console.log(JSON.stringify({ calls, toasts, afterPreview, parsedAfter: c.parsed, href }));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_component_previews_confirms_and_imports():
    html = render("outputs.1.peq")
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    cfg = component_config(html)
    preview = {"ok": True, "filters": [{"label": "a"}, {"label": "b"}], "warnings": [], "errors": []}
    replies = [
        [200, preview],
        [409, {"error": "confirmation_required", "detail": "raising x needs confirmation"}],
        [200, {"warnings": [{"message": "dropped"}]}],
    ]
    out = subprocess.run(
        [NODE, "-e", DRIVER, script, json.dumps(replies), json.dumps(cfg)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(out.stdout)
    assert result["afterPreview"] == {"parsed": preview, "slots": 10, "canApply": True, "summary": "2 filters"}
    preview_call, first, confirmed = result["calls"]
    assert preview_call == {"url": cfg["previewUrl"], "body": {"text": "biquad1,..."}}
    expected = {
        "text": "biquad1,...",
        "bank": "outputs.1.peq",
        "index": 3,
        "biquad": "all",
        "unbypass": True,
        "pick": 2,
    }
    assert first == {"url": cfg["importUrl"], "body": expected}
    assert confirmed["body"] == {**expected, "confirm": True}
    assert result["toasts"] == [["Imported eq.txt (dropped)", "info"]]
    assert result["parsedAfter"] is None
    assert result["href"] == cfg["exportUrl"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_component_reports_refusals():
    html = render("outputs.0.crossover")
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    cfg = component_config(html)
    preview = {"ok": True, "filters": [{"label": "a"}], "warnings": [], "errors": []}
    replies = [[200, preview], [422, {"detail": "too many", "problems": ["use all"]}]]
    driver = DRIVER.replace("c.index = '3';", "c.index = 'all'; c.biquad = '1';").replace("c.pick = '2';", "")
    out = subprocess.run(
        [NODE, "-e", driver, script, json.dumps(replies), json.dumps(cfg)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(out.stdout)
    assert result["afterPreview"]["slots"] == 4  # a 1-filter file defaults to group 1, not both groups
    assert result["calls"][1]["body"]["biquad"] == 1 and result["calls"][1]["body"]["index"] == "all"
    assert result["toasts"] == [["too many: use all", "error"]]
    assert result["parsedAfter"] == preview  # kept open so the user can change the target


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_hidden_pick_is_not_sent():
    html = render("outputs.1.peq")
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    cfg = component_config(html)
    preview = {"ok": True, "filters": [{"label": "a"}, {"label": "b"}], "warnings": [], "errors": []}
    replies = [[200, preview], [200, {"warnings": []}]]
    # Pick filter 2 for band 4, then switch to all bands: the Filter select is hidden and must not narrow the import.
    driver = DRIVER.replace("c.pick = '2';", "c.pick = '2'; c.index = 'all';")
    out = subprocess.run(
        [NODE, "-e", driver, script, json.dumps(replies), json.dumps(cfg)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    body = json.loads(out.stdout)["calls"][1]["body"]
    assert body["index"] == "all" and "pick" not in body


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_crossover_defaults_to_both_groups_only_when_the_file_needs_them():
    html = render("outputs.0.crossover")
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    cfg = component_config(html)
    preview = {"ok": True, "filters": [{"label": str(n)} for n in range(6)], "warnings": [], "errors": []}
    replies = [[200, preview], [200, {"warnings": []}]]
    driver = DRIVER.replace("c.index = '3';", "").replace("c.pick = '2';", "")
    out = subprocess.run(
        [NODE, "-e", driver, script, json.dumps(replies), json.dumps(cfg)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(out.stdout)
    assert result["afterPreview"]["slots"] == 8
    assert result["calls"][1]["body"]["index"] == "all"
