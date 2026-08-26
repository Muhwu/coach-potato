"""Guards for the no-build-step frontend: every script in static/ shares one
global namespace, so a duplicate top-level function/const in a later file
silently overwrites the earlier one. (Regression: guide.js's
ensureMatchupGames shadowed matchups.js's — different signature, silent
early-return — leaving matchup expansion stuck on "Loading…".)"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
# in <script> load order (index.html)
JS_FILES = ["app.js", "matchups.js", "trends.js", "blocks.js", "guide.js",
            "cooldowns.js", "research.js", "tierlist.js"]
DECL_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z0-9_]+)|^(?:const|let)\s+([A-Za-z0-9_]+)",
                     re.MULTILINE)


def test_no_duplicate_toplevel_declarations_across_scripts():
    seen = {}
    duplicates = []
    for js_file in JS_FILES:
        for match in DECL_RE.finditer((STATIC / js_file).read_text(encoding="utf-8")):
            name = match.group(1) or match.group(2)
            if name in seen:
                duplicates.append(f"{name} ({seen[name]} vs {js_file})")
            else:
                seen[name] = js_file
    assert not duplicates, "duplicate top-level declarations: " + "; ".join(duplicates)


def test_script_list_matches_index_html():
    """If a script is added to index.html, add it to JS_FILES above so the
    duplicate-name check covers it."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'<script src="([^"]+\.js)"></script>', html)
    local = [s for s in referenced if not s.startswith("vendor/")]
    assert local == JS_FILES


def test_every_script_parses():
    """A syntax error in one file silently kills every function it defines —
    the Blocks view once shipped broken because a new function was pasted
    inside another one, eating its closing brace. Nothing else here parses the
    JS, so this is the guard. Skipped when node isn't on PATH."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for js_file in JS_FILES:
        result = subprocess.run([node, "--check", str(STATIC / js_file)],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{js_file} does not parse:\n{result.stderr}"


def test_hideable_views_match_the_nav_and_the_settings_checkboxes():
    """Three lists have to agree or the app breaks in quiet ways: NAV_SECTIONS
    (what you can navigate to), the Settings checkboxes (what you can hide),
    and the server's HIDEABLE_VIEWS (what it will accept). A view offered as a
    checkbox but missing server-side makes SAVING SETTINGS fail with a 400 —
    which is exactly what happened when series/pool/tiers were added."""
    from server.app import HIDEABLE_VIEWS

    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    nav_block = re.search(r"const NAV_SECTIONS = \[(.*?)\n\];", app_js, re.S).group(1)
    nav_views = set(re.findall(r'"([a-z]+)"', nav_block)) - {"analyze", "coach", "prepare"}
    checkboxes = set(re.findall(r'class="view-toggle-cb" value="([a-z]+)"',
                                (STATIC / "index.html").read_text(encoding="utf-8")))

    assert checkboxes <= HIDEABLE_VIEWS, (
        f"hideable in the UI but rejected by the server: {checkboxes - HIDEABLE_VIEWS}")
    assert checkboxes == nav_views, (
        f"navigable but not hideable: {nav_views - checkboxes}; "
        f"hideable but not navigable: {checkboxes - nav_views}")


def test_every_settings_panel_has_a_tab_and_vice_versa():
    """Settings is tabbed; a panel with no tab button is unreachable."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    tabs = set(re.findall(r'class="settings-tab" data-tab="([^"]+)"', html))
    panels = set(re.findall(r'class="settings-panel" data-tab="([^"]+)"', html))
    assert tabs and tabs == panels
