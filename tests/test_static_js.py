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
JS_FILES = ["app.js", "matchups.js", "trends.js", "blocks.js", "learnings.js",
            "guide.js", "cooldowns.js", "research.js", "tierlist.js"]
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


def test_vendored_scripts_exist():
    """index.html loads vendor/ scripts without a CDN fallback, so a missing
    file fails silently at runtime (e.g. the block copy-as-image menu item
    needs vendor/modern-screenshot.min.js for its `modernScreenshot` global)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    vendored = re.findall(r'<script src="(vendor/[^"]+\.js)"></script>', html)
    assert "vendor/modern-screenshot.min.js" in vendored
    for path in vendored:
        assert (STATIC / path).is_file(), f"missing vendored script {path}"


def test_font_settings_agree_across_server_select_and_app_js():
    """The accessibility font/text-size options live in three places: the
    Settings selects, FONT_STACKS in app.js and the server's allow-lists. An
    option the server doesn't know makes Save 400; one app.js doesn't know
    silently falls back to the system font."""
    from server.app import FONT_FAMILIES, FONT_SCALES

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    def options(select_id):
        block = re.search(rf'<select id="{select_id}".*?</select>', html, re.S).group(0)
        return re.findall(r'<option value="([^"]+)"', block)
    assert set(options("setting-font-family")) == set(FONT_FAMILIES)
    assert {int(v) for v in options("setting-font-scale")} == set(FONT_SCALES)
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    stacks = re.search(r"const FONT_STACKS = \{(.*?)\n\};", app_js, re.S).group(1)
    assert set(re.findall(r"^\s+([a-z]+):", stacks, re.M)) == set(FONT_FAMILIES) - {"system"}


def test_vendored_fonts_exist():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    fonts = re.findall(r"url\((vendor/fonts/[^)]+\.woff2)\)", css)
    assert len(fonts) == 6
    for path in fonts:
        assert (STATIC / path).is_file(), f"missing vendored font {path}"


@pytest.mark.parametrize("name", ["style.css", "compare.html"])
def test_every_font_size_follows_the_text_size_setting(name):
    """The text-size setting works by scaling --font-scale; a hardcoded px
    font-size would stay small when the user asked for larger text."""
    text = (STATIC / name).read_text(encoding="utf-8")
    fixed = re.findall(r"font-size:\s*[0-9.]+px\s*[;}]", text)
    assert not fixed, f"unscaled font-size in {name}: {fixed[:5]}"


def _function_body(source, name):
    """Source of top-level function `name`, by brace matching (good enough for
    this codebase's functions — none put unbalanced braces in strings)."""
    start = re.search(rf"^(?:async\s+)?function\s+{name}\(", source, re.MULTILINE)
    assert start, f"{name} not found"
    i = source.index("{", source.index(")", start.end()))
    depth = 0
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[start.start():j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_background_refreshes_never_rerender_under_the_users_cursor():
    """Regression: "the textbox keeps closing before I finish my note". The
    block timeline backfill re-rendered the Blocks view when it finished, and
    crawl polling re-rendered Overview/Progress every ~10 s — innerHTML threw
    away the field being typed in and its blur-to-save closed the editor. A
    refresh the user didn't trigger must go through whenNotTyping."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    blocks = (STATIC / "blocks.js").read_text(encoding="utf-8")
    poll = _function_body(app, "pollCrawl")
    for call in ("init(", "refresh(", "loadBlocks(", "loadMatchups(", "loadProgress(",
                 "loadTrends("):
        assert call not in poll, f"pollCrawl calls {call} directly — use refreshActiveView"
    assert "whenNotTyping(" in _function_body(app, "refreshActiveView")
    # loadBlocks is the background entry point for the Blocks view (crawl and
    # timeline-backfill completion both call it)
    load_blocks = _function_body(blocks, "loadBlocks")
    assert "whenNotTyping(" in load_blocks
    assert not re.search(r"^\s*renderBlocks\(\);", load_blocks, re.MULTILINE)
    backfill = _function_body(blocks, "maybeBackfillBlockTimelines")
    assert "renderBlocks(" not in backfill


def test_when_not_typing_defers_until_focus_leaves():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app.index("function userIsTyping(")
    helpers = app[start:app.index("// Refresh whichever view is showing", start)]
    script = helpers + r"""
const listeners = [];
const root = { dataset: {}, contains: (el) => el.inRoot,
               addEventListener: (type, fn) => listeners.push(fn) };
const textarea = { inRoot: true, matches: () => true };
const button = { inRoot: true, matches: () => false };
globalThis.document = { activeElement: textarea };
const ran = [];
whenNotTyping(root, () => ran.push("first"));
whenNotTyping(root, () => ran.push("latest"));
if (ran.length) throw new Error("ran while typing: " + ran);
const focusout = () => { listeners.forEach((fn) => fn()); return new Promise((r) => setTimeout(r, 5)); };
(async () => {
  await focusout();  // tabbed to another field: still typing
  if (ran.length) throw new Error("ran while still typing: " + ran);
  document.activeElement = button;
  await focusout();
  if (ran.join() !== "latest") throw new Error("expected only the latest refresh, got " + ran);
  whenNotTyping(root, () => ran.push("now"));
  if (ran.join() !== "latest,now") throw new Error("should run immediately when not typing");
  if (listeners.length !== 1) throw new Error("focusout listener wired more than once");
})().catch((e) => { console.error(e.message); process.exit(1); });
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_notes_editor_has_the_formatting_toolbar():
    """Every Markdown notes <textarea> gets mdToolbar() right before it (the
    toolbar finds its textarea as the next sibling). Exceptions are listed
    here on purpose: a one-line table-cell editor gets the keyboard shortcuts
    only (data-md), and a read-only text dump isn't a notes editor."""
    exempt = ('class="game-notes" data-md', 'class="recording-desc-text"')
    missing = []
    for js_file in JS_FILES:
        source = (STATIC / js_file).read_text(encoding="utf-8")
        for match in re.finditer(r"<textarea\b", source):
            line = source[match.start():source.index("\n", match.start())]
            if any(e in line for e in exempt):
                continue
            preceding = source[:match.start()].rstrip()
            if not preceding.endswith("${mdToolbar()}"):
                missing.append(f"{js_file}: {line[:70]}")
    assert not missing, "notes editors without mdToolbar(): " + "; ".join(missing)


def test_markdown_toolbar_actions():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app.index("const MD_ACTIONS")
    helpers = app[start:app.index("function mdToolbarTarget(", start)]
    script = r"""
globalThis.document = { execCommand: () => false };  // force the fallback path
function ta(value, start, end = start) {
  return { value, selectionStart: start, selectionEnd: end, readOnly: false, disabled: false,
    focus() {}, dispatchEvent() {},
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
    setRangeText(text, a, b) { this.value = this.value.slice(0, a) + text + this.value.slice(b); } };
}
const sel = (t) => t.value.slice(t.selectionStart, t.selectionEnd);
const checks = [];
function check(name, got, want) { if (got !== want) checks.push(`${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
""" + helpers + r"""
let t = ta("hello world", 6, 11);
applyMdAction(t, "bold"); check("bold", t.value, "hello **world**"); check("bold sel", sel(t), "world");
applyMdAction(t, "bold"); check("unbold", t.value, "hello world"); check("unbold sel", sel(t), "world");
t = ta("hello **world**", 8, 13);
applyMdAction(t, "italic"); check("italic inside bold", t.value, "hello ***world***");
applyMdAction(t, "italic"); check("un-italic keeps bold", t.value, "hello **world**");
t = ta("say hi there", 4, 7);  // "hi " — trailing space from a double-click
applyMdAction(t, "bold"); check("trims selection", t.value, "say **hi** there");
t = ta("ab", 1);
applyMdAction(t, "bold"); check("placeholder", t.value, "a**bold text**b"); check("placeholder sel", sel(t), "bold text");
t = ta("one\ntwo\n\nthree", 0, 14);
applyMdAction(t, "bullet"); check("bullets", t.value, "- one\n- two\n\n- three");
applyMdAction(t, "numbered"); check("bullets -> numbers", t.value, "1. one\n2. two\n\n3. three");
applyMdAction(t, "numbered"); check("numbers off", t.value, "one\ntwo\n\nthree");
t = ta("first\nsecond", 8);
applyMdAction(t, "heading"); check("heading on cursor line", t.value, "first\n## second");
t = ta("a\nb\nc", 0, 4);  // triple-click style: ends just after a newline
applyMdAction(t, "quote"); check("selection ending at newline", t.value, "> a\n> b\nc");
t = ta("x\ny", 0, 3);
applyMdAction(t, "code"); check("multi-line code", t.value, "```\nx\ny\n```");
t = ta("see https://x.io", 4, 16);
applyMdAction(t, "link"); check("url link", t.value, "see [](https://x.io)"); check("url link cursor", t.selectionStart, 5);
t = ta("docs", 0, 4);
applyMdAction(t, "link"); check("text link", t.value, "[docs](https://)"); check("text link sel", sel(t), "https://");
if (checks.length) { console.error(checks.join("\n")); process.exit(1); }
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
