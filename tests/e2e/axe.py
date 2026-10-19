"""axe-core accessibility checks for the browser tests (phase 9 design §16).

``axe_violations(page)`` runs axe-core (the copy axe-playwright-python bundles) on the page as it is now and returns the
violations, each as a short dict. ``assert_accessible(page)`` fails with a readable list. Both take ``allow``: rule ids
that are known and tracked elsewhere (keep it short, and say why next to the call), and ``include``/``exclude``:
CSS selectors that narrow the check to the part of the page a test is about.
"""

from collections.abc import Iterable
from typing import Any

from axe_playwright_python.base import AXE_SCRIPT
from playwright.sync_api import Page

# WCAG 2.x A and AA, plus axe's best practices (e.g. one main landmark, headings in order).
DEFAULT_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice")
# axe rules tagged "experimental" that are off by default but check a WCAG A criterion: 2.5.3 Label in Name (a
# control's accessible name contains its visible text, so speech input can name it; Lighthouse runs it).
EXTRA_RULES = ("label-content-name-mismatch",)


def axe_violations(
    page: Page,
    *,
    allow: Iterable[str] = (),
    include: str | list[str] | None = None,
    exclude: str | list[str] | None = None,
    tags: Iterable[str] = DEFAULT_TAGS,
) -> list[dict[str, Any]]:
    context: dict[str, Any] | None = None
    if include or exclude:
        context = {}
        if include:
            context["include"] = [include] if isinstance(include, str) else list(include)
        if exclude:
            context["exclude"] = [exclude] if isinstance(exclude, str) else list(exclude)
    options = {
        "resultTypes": ["violations"],
        "runOnly": {"type": "tag", "values": list(tags)},
        "rules": {rule: {"enabled": True} for rule in EXTRA_RULES},
    }
    if not page.evaluate("() => typeof window.axe === 'object'"):
        page.evaluate(AXE_SCRIPT)
    results = page.evaluate("([context, options]) => axe.run(context || document, options)", [context, options])
    allowed = set(allow)
    return [
        {
            "id": v["id"],
            "impact": v["impact"],
            "help": v["help"],
            "nodes": [" ".join(map(str, n["target"])) for n in v["nodes"]],
        }
        for v in results["violations"]
        if v["id"] not in allowed
    ]


def format_violations(violations: list[dict[str, Any]], url: str = "") -> str:
    lines = [f"{len(violations)} accessibility violation(s){' on ' + url if url else ''}:"]
    for v in violations:
        lines.append(f"- {v['id']} ({v['impact']}): {v['help']}")
        lines.extend(f"    {target}" for target in v["nodes"][:5])
        if len(v["nodes"]) > 5:
            lines.append(f"    ... and {len(v['nodes']) - 5} more")
    return "\n".join(lines)


def assert_accessible(page: Page, **kwargs: Any) -> None:
    violations = axe_violations(page, **kwargs)
    assert not violations, format_violations(violations, page.url)
