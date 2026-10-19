import app.web.templating as templating
from app import REPO_URL, __version__


async def test_sidebar_footer_links_repo_and_shows_version(http):
    html = (await http.get("/")).text
    footer = html[html.index('<footer class="nav-footer">') :]
    assert f'href="{REPO_URL}"' in footer
    assert f"v{__version__}" in footer


async def test_sidebar_footer_shows_short_commit_hash(http, monkeypatch):
    commit = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(templating, "GIT_COMMIT", commit)
    html = (await http.get("/help")).text
    footer = html[html.index('<footer class="nav-footer">') :]
    assert f'href="{REPO_URL}/commit/{commit}"' in footer
    assert "<code>0123456</code>" in footer
    assert "dev build" not in footer


async def test_sidebar_footer_without_commit_says_dev_build(http, monkeypatch):
    monkeypatch.setattr(templating, "GIT_COMMIT", "")
    html = (await http.get("/")).text
    assert "dev build" in html[html.index('<footer class="nav-footer">') :]
