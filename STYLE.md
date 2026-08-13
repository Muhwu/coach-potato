# Coach Potato UI style guide

How the frontend is styled and why. Read this before adding or changing any
UI; when a view drifts from these rules, a "design pass" means bringing it
back to them. Everything lives in `static/style.css` — there is no build
step and no CSS framework.

## Theme variables — never hardcode colors

All colors come from CSS custom properties on `:root`, defined twice (light
default + `@media (prefers-color-scheme: dark)` overrides):

| Variable | Use |
|---|---|
| `--surface-1` / `--page` | panel / page background (opacity-aware, see below) |
| `--text-primary` / `--text-secondary` / `--muted` | text hierarchy |
| `--grid` / `--border` / `--baseline` | hairlines, borders, chart baselines |
| `--series-1` | THE accent: buttons, active tabs, links, chart lines. User-overridable (accent-color setting) |
| `--series-2` | 2nd chart series when a chart needs to distinguish two lines (e.g. the game-curve chart's opponent line) |
| `--accent-wash` | translucent accent tint for active/selected backgrounds — derived from `--series-1` via `color-mix`, follows the user's accent automatically |
| `--good` / `--critical` / `--good-text` | win/positive vs loss/negative |

Rules:

- **Never write a literal accent color** (`#2a78d6`, `rgba(42,120,214,…)`).
  Use `var(--series-1)`; for tinted backgrounds use `var(--accent-wash)` or
  `color-mix(in srgb, var(--series-1) N%, transparent)`. Literals break the
  accent-color setting.
- **Never set panel/page backgrounds directly.** Use `var(--surface-1)` /
  `var(--page)` — they run through the `--ui-opacity` `color-mix` so the
  translucency setting applies for free. A hardcoded background makes one
  panel stay opaque while everything else fades.
- Both themes must work: check dark mode for anything new.

## Type scale

Body is 14px. Reuse these instead of inventing sizes:

- Section headings: `h2` (views), `h4` 15px/600 (cards, sub-blocks)
- Column/label text: `.filter-label`, `thead th` — 12px, `--text-secondary`
- Small print/status/meta: 12px `.muted`; badges 11px
- Never style raw text with inline `style=` attributes for anything reused.

## Components — reuse, don't reinvent

- **Buttons:** `.preset` (default bordered button), `.btn-primary` (the one
  main action per view, e.g. Save settings), `.preset icon-btn` (compact
  emoji/glyph button: ✎ 🗑 ✕), `.link-btn` (inline text link that acts —
  used in empty-state CTAs like "click here to create one").
- **Expand/collapse:** `.seg-toggle` button showing `▸` / `▾`, with
  `aria-expanded`. Content that needs a fetch loads lazily on first expand
  and caches per item (see sessions' clips, guide's recent games).
- **Add-forms are collapsed by default** behind a `+ Add <thing>` `.preset`
  button; the open form gets a Cancel button (see clips). Don't render a
  permanently visible form under a list.
- **Champion identity:** icon + display name together. Icons come from
  `champIcon()` (app.js) — 24×24, `border-radius: 5px` (`.champ-cell img`
  and the guide-header rule). Always pair with `displayName()` /
  `champDisplay()`; never print raw match-v5 ids (`MonkeyKing`). New places
  that show a champion icon must constrain its size in CSS — the DDragon
  source image is large.
- **Badges/chips:** small rounded-pill spans, 11-12px — `.block-badge`,
  `.guide-patch-badge`, pool chips. Background `var(--accent-wash)` or the
  chip's semantic tint, 1px `var(--border)`.
- **Status feedback:** a `.muted` `<span>` next to the action button, filled
  with "saved ✓" / the API's `detail` on error. No toasts, no alert() for
  routine results. `confirm()` guards destructive actions (delete clip/
  session/block, imports that overwrite).
- **Tables:** wrap in `.table-wrap` (horizontal scroll), first column
  left-aligned, expandable rows use a full-width `tr.games-row` under the
  toggled row.
- **Champion selectors:** a `<select>` with alphabetical display names
  (build via `championOptions()` in app.js) when the choice set is known;
  cap width (`max-width: 16em`). Only use the `#champ-list` datalist
  autocomplete where free typing is genuinely better (pool chips).

## Markdown notes & editors

- Every notes field renders as Markdown via `renderNotes()` inside an
  `md-body` element (CLAUDE.md rule). Never show raw note text.
- Every notes **editor** looks the same: a `.filter-label` label ending in
  "(Markdown)", a monospace textarea (the shared
  `.session-body textarea, .mu-notes textarea` rule: `--page` background,
  1px border, radius 6, `resize: vertical`), then a `.session-actions` row
  with Save / Cancel `.preset` buttons and a `.muted` status span.
- Editor state lives in the view's state object; sync inputs to the draft
  on `input` so unrelated re-renders don't wipe typed text (see
  `#guide-notes` wiring in guide.js).

## Modal dialogs

One shared shell: `#modal-overlay` (fixed, dimmed, click-outside closes,
Esc closes) wrapping `#modal-box`. A feature renders its content into
`#modal-box` and re-wires its own handlers (see cooldowns.js). The box uses
`var(--surface-1-solid)` deliberately — a dialog over a dimmed page should
not inherit the translucency setting. Head pattern: `.modal-head` with an
`h3` and an `icon-btn` ✕.

## One global namespace

There is no build step: every `static/*.js` file's top-level declarations
share the page's global scope, and a later `<script>` silently overwrites
an earlier one's function of the same name. Prefix view-specific helpers
with their view (`ensureGuideMatchupGames`, not `ensureMatchupGames`).
`tests/test_static_js.py` fails the suite on any duplicate top-level name.

## Sortable tables

Data tables use the shared helpers in app.js: `sortableThead(columns,
sortState, leading, trailing)` for the header, `sortRows(rows, sortState,
columns)` to order rows, and `wireSortable(container, sortState, columns,
rerender)` for click-to-sort. A column spec is `{key, label, type:
"num"|"text", get?(row), sortable?, cls?}`; `sortState` is `{key, dir}` held
in the view's state. Numeric columns default to descending, text to
ascending; nulls always sort last. `wireSortable` uses a container-wide
`th[data-sort]` selector, so don't put a second sortable table inside a
sortable table's container (nested per-game lists are intentionally not
sortable for this reason). Do NOT add sorting to inherently chronological
tables whose rows compare against the previous row (coaching-progress
periods, trends buckets) — sorting breaks their delta semantics.

## Drag-to-reorder

Ordered chip lists (champion pool, skill priority) use native HTML5 DnD:
`draggable="true"`, `.dragging` (lowered opacity) on the source,
`.drag-over` (dashed `--series-1` outline) on the target, splice-reorder on
drop, `cursor: grab`. Always call `e.dataTransfer.setData` in dragstart
(Firefox refuses to drag otherwise) and re-render after drop.

## Layout

- Page is a single centered column, `max-width: 1080px`; views are `<div
  id="<view>-view">` containing `<section>` blocks.
- Filter bars: `section.filter-row` of `.filter-group`s (label above
  control).
- Settings: `h3.settings-group-head` per group, `.settings-field` per
  setting (label/control + a `.muted` explainer `<p>`), one savebar at the
  bottom. Settings that act immediately (uploads, migrations) say so in
  their explainer and give their own inline status.
- Two-column detail layouts use CSS grid with a `minmax` main column and a
  capped side column, collapsing to one column under a `@media (max-width:
  …)` breakpoint (see `.guide-row-grid`).

## Design-pass checklist

When touching a view, check it against this list:

1. No hardcoded colors/backgrounds that bypass the variables above.
2. Icons size-constrained; champion icon + name pattern used.
3. Editors match the shared Markdown-editor look; notes render via
   `renderNotes`.
4. Empty states are one `.muted` sentence, with an inline `.link-btn` CTA
   when there's an obvious next action.
5. Add-forms collapsed behind `+ Add …`; destructive actions confirm().
6. Works in dark mode, at 20% UI opacity, and with a custom accent color.
7. Interactive elements have `title`/`aria-label` when their label is a
   glyph, and `aria-expanded` on toggles.
