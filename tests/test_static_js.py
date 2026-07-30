"""Guards for the no-build-step frontend: every script in static/ shares one
global namespace, so a duplicate top-level function/const in a later file
silently overwrites the earlier one. (Regression: guide.js's
ensureMatchupGames shadowed matchups.js's — different signature, silent
early-return — leaving matchup expansion stuck on "Loading…".)"""
import re
from pathlib import Path

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
        for match in DECL_RE.finditer((STATIC / js_file).read_text()):
            name = match.group(1) or match.group(2)
            if name in seen:
                duplicates.append(f"{name} ({seen[name]} vs {js_file})")
            else:
                seen[name] = js_file
    assert not duplicates, "duplicate top-level declarations: " + "; ".join(duplicates)


def test_script_list_matches_index_html():
    """If a script is added to index.html, add it to JS_FILES above so the
    duplicate-name check covers it."""
    html = (STATIC / "index.html").read_text()
    referenced = re.findall(r'<script src="([^"]+\.js)"></script>', html)
    local = [s for s in referenced if not s.startswith("vendor/")]
    assert local == JS_FILES
