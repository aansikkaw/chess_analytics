# Plateau Breaker design system

**Direction: the scoresheet, analyst edition.** Every screen is a chess scoresheet that a strong coach has marked up. Blue ink is you, red pen is the coach, and amber shows the move you are looking at. Beginners read the coach's words; titled players switch to the Analyst view and get engine numbers, depth and standard symbols. Both views draw on the same data.

The tokens live at the top of `app/static/app.css`. This file is the reference for them, and the two must change together.

## Principles

1. **The board is the hero.** Everything around it is quiet: no gradients, no decorative icons, no mascots.
2. **Colour means one thing, everywhere.**
   - Blue: you, your moves, links and primary actions.
   - Red: the coach, errors and losses.
   - Amber: the current move, or the last move played.
   - Nothing else is coloured.
3. **Numbers are exact and line up.** Moves and evaluations use the monospace face. Other figures use tabular numbers. Engine output always says which engine and what depth.
4. **Two reading levels.**
   - The *Coach view* explains in words: "a bishop for a pawn", winning chances in %.
   - The *Analyst view* shows pawns (+4.4), depth, three engine lines and standard symbols only.
   - Never mix the two in one panel.
5. **Our own brand.** We never use:
   - Chess.com's or Lichess's board colours: green `#739552`/`#ebecd0`, brown `#b58863`/`#f0d9b5`, blue-grey `#8ca2ad`/`#dee3e6`.
   - Their move-quality tiers (Brilliant, Great, Best, Excellent, Miss) or their colours.
   - Their wording or layouts.
6. **Handwriting is rare.** Caveat (the red pen) is only for the coach's marks: a circled grade, a margin note, the circled weakest skill. Never for buttons, labels or data, and never in the Analyst view.

## Colour

Text pairs must reach 4.5:1 (3:1 for text 24px and larger). The ratios below were measured.

### Light (default)

```css
:root {
  --paper: #eef0f3;      /* page background (the desk) */
  --sheet: #ffffff;      /* cards, panels, tables */
  --sheet-2: #f6f7f9;    /* table stripes, inset notes */
  --ink: #1a1f2b;        /* text, strong rules        16.5:1 on sheet */
  --ink-2: #545d73;      /* secondary text             6.6:1 */
  --ink-3: #646d84;      /* captions, axis labels      5.2:1 on sheet, 4.5:1 on paper */
  --rule: #d8dde6;       /* borders */
  --rule-2: #bcc3d0;     /* control borders */
  --ruling: #c8d3e6;     /* scoresheet row lines */
  --blue: #2443c4;       /* you, links, primary        7.9:1 */
  --blue-ink: #1a3299;   /* hover / pressed */
  --blue-wash: #e8ecfc;
  --on-blue: #ffffff;    /* text on blue               7.9:1 */
  --red: #c8322a;        /* coach, errors              5.3:1 */
  --red-wash: #fcebe8;
  --amber: #d9a23a;      /* fill only, never text (2.3:1) */
  --amber-wash: rgb(217 162 58 / 22%);
  --ok: #2c7a4b;         /* 5.3:1 */
  --warn: #a8670f;       /* 4.6:1 */
  --board-light: #e4e8ef;
  --board-dark: #6e7c97;
  --board-last-light: #eedfa0;
  --board-last-dark: #a99f70;
  --board-edge: #1a1f2b;
  --piece-white: #ffffff;
  --piece-black: #1c2235;
  --arrow-you: var(--blue);
  --arrow-coach: var(--red);
  --arrow-drawn: #7a8399;
  --eval-you: var(--blue);
  --eval-opponent: #d3d8e1;
}
```

### Dark

Dark mode follows the system setting (`prefers-color-scheme`). `data-theme="light"` or `"dark"` on `<html>` overrides it.

```css
:root[data-theme="dark"] {   /* same values inside @media (prefers-color-scheme: dark) */
  --paper: #10141f;
  --sheet: #181e2d;
  --sheet-2: #1e2536;
  --ink: #e7eaf3;        /* 13.8:1 */
  --ink-2: #a7afc2;      /* 7.6:1 */
  --ink-3: #7f889e;      /* 4.7:1 */
  --rule: #283047;
  --rule-2: #3a4460;
  --ruling: #2e3854;
  --blue: #8fa0ff;       /* 6.8:1 */
  --blue-ink: #b3beff;
  --blue-wash: #1e2650;
  --on-blue: #0f1430;    /* 7.4:1 */
  --red: #ff7d6c;        /* 6.7:1 */
  --red-wash: #3a2024;
  --amber: #e0b35a;
  --amber-wash: rgb(224 179 90 / 18%);
  --ok: #6cc893;
  --warn: #e9b45a;
  --board-light: #b9c1d0;
  --board-dark: #5b6884;
  --board-last-light: #d9cb8a;
  --board-last-dark: #8f8a64;
  --board-edge: #3a4460;
  --eval-opponent: #3a4460;
}
```

Hard-coded colours in components are a bug. Use a token, or add one here first.

## Type

| Token | Size | Use |
|---|---|---|
| `--text-display` | clamp(2.3rem, 5.2vw, 3.9rem) | Landing headline only. Weight 820, width 116%, tracking −0.025em. |
| `--text-3xl` | 40px | Big figures (rating, accuracy). Condensed (72–80%), weight 760. |
| `--text-2xl` | 32px | h1. Weight 780, width 112%. |
| `--text-xl` | 24px | h2. Weight 740, width 110%. |
| `--text-lg` | 20px | Section titles inside a page. |
| `--text-md` | 18px | Lede, h3, card titles. |
| `--text-base` | 16px | Body. Line height 1.55. |
| `--move-size` | 15px | Moves and evaluations, in the mono face. |
| `--text-sm` | 14px | Secondary text, table cells. |
| `--text-xs` | 12px | Captions, axis labels, coordinates. The smallest size. |

**Families:**
- **Archivo** (`--font`): variable weight and width; all interface text. Wide for headings, normal for text, condensed for figures.
- **IBM Plex Mono** (`--mono`, 400 and 600): every move, evaluation, engine line and clock time. Use the `.mono` class or `<code>`/`<kbd>`.
- **Caveat** (`--hand`, 600): the coach's red-pen marks only (see Principle 6).
- **Pieces:** the board's chess glyphs.

All fonts are self-hosted in `app/static/fonts/` with their licences. No Google Fonts requests.

**Rules:**
- Sentence case everywhere. No all-caps labels and no tracked-out "eyebrow" labels.
- Line length at most 68ch for body text (`p` is capped already).
- Notation: standard algebraic, letters (Nf3), not figurines. Moves carry move numbers (12.Bxg6, 12…Nxg6).
- Evaluations are always signed with a true minus sign: +0.4, −2.0, #3 for mate.

## Space

A 4px grid. Use the tokens; don't invent in-between values.

| Token | px | Typical use |
|---|---|---|
| `--space-1` | 4 | Icon to label |
| `--space-2` | 8 | Inside controls, between buttons |
| `--space-3` | 12 | Between related lines |
| `--space-4` | 16 | Card padding on phones, gap inside a card |
| `--space-5` | 24 | Card padding, gap between cards |
| `--space-6` | 32 | Between groups |
| `--space-7` | 48 | Between page sections |
| `--space-8` | 64 | Landing section gaps |
| `--space-9` | 96 | Landing hero spacing |
| `--row` | 32 | Height of one scoresheet row (move list, tables) |

Pages are left-aligned inside a max-width container (about 1200px), with 16px side gutters on phones.

## Radii

| Token | Value | Use |
|---|---|---|
| `--radius-board` | 2px | The board and the eval bar |
| `--radius-sheet` | 4px | Cards, panels, tables, dialogs: anything that is a sheet of paper |
| `--radius-control` | 6px | Buttons, inputs, selects, segmented controls |
| `--radius-pill` | 999px | Counts and badges only |

`--r` and `--r-sm` are older names for `--radius-sheet` and `--radius-control`, kept so existing components still work. New code uses the new names.

## Shadows

| Token | Use |
|---|---|
| `--shadow-sheet` | A sheet lying on the desk: a hairline under it plus a soft, offset shadow. The default for cards. |
| `--shadow-raised` | Things above the page: dialogs, menus, toasts. |
| `--shadow-board` | The board, so it lifts slightly off its sheet. |

Never stack shadows, and never put a shadow on something inside a card.

## Motion

| Token | Duration | Use |
|---|---|---|
| `--dur-press` | 80ms | Button press feedback |
| `--dur-fast` | 140ms | Hover, focus, toggles, tab changes |
| `--dur-move` | 180ms | A piece sliding one move, an arrow appearing |
| `--dur-panel` | 240ms | A panel opening, a variation expanding |
| `--dur-reveal` | 600ms | The one orchestrated reveal on a page: Rating DNA bars growing in turn |

Easing: `--ease-out` for things arriving, `--ease-in-out` for things moving between two places.

**Rules:**
- Motion answers the user (a move played, a panel opened). At most one automatic reveal per page.
- No fade-and-slide on every section, no bouncing, no confetti.
- Pieces move in `--dur-move`. Stepping quickly through moves must never queue animations: a new move cancels the running one.
- `prefers-reduced-motion: reduce` turns all animation and transitions off (a base rule in `app.css` does this).

## Components

### Board

- **Colours:** `--board-light` / `--board-dark` are the default board. The last move is shaded with `--board-last-light` / `--board-last-dark`. The selected square and legal-move dots use `--blue` at 35–45%.
- **Frame:** a 1.5px `--board-edge` outline, `--radius-board`, and `--shadow-board`.
- **Coordinates:** outside the board in the margin: ranks on the left, files below, in `--text-xs` `--ink-3`. Never on the squares.
- **Pieces:** white pieces are white with an ink outline; black pieces are `--piece-black` with a thin `--board-light` outline, so they stay crisp on dark squares (2.8:1 without it in dark mode).
- **Arrows:** blue = your move (`--arrow-you`), red = the coach's or engine's move (`--arrow-coach`), grey = drawn by the user (`--arrow-drawn`). With several engine lines, the best line's arrow is thickest and most opaque. Circles mark a square the coach wants you to look at, in red.
- **Size:** 560px wide in a report, as large as fits (up to 720px) on the analysis board; full width on phones.
- **User themes:** may exist, but none may reuse the colours listed in Principle 5.

### Move list

- **Layout:** a scoresheet. Columns are move number, White, (eval), Black, (eval). Rows are `--row` high and separated by `--ruling` lines; there's a 1.5px `--ink` rule under the header.
- **Moves:** in `--mono` at `--move-size`. Your moves in `--blue` and weight 600, the opponent's in `--ink`. Move numbers in `--ink-3`.
- **Current move:** its row gets an `--amber-wash` band. Only one row is ever highlighted.
- **Symbols:** standard, in `--red` straight after the move: `!!` `!` `!?` `?!` `?` `??`.
  - Coach view: may circle a `??` in red pen and adds short margin notes.
  - Analyst view: symbols only, with an eval column after each side.
- **Variations:** indented under the move they branch from, in `--red` on a faint `--red-wash`, labelled "Engine line, depth N".
- **Keyboard:** ← → step, Home / End jump, ↑ ↓ choose an engine line.

### Evaluation bar

- **Shape:** vertical, 14px wide, on the board's left edge, full board height, `--radius-board`, 1px `--ink` outline.
- **Fill:** your side in `--eval-you` from the bottom (your end of the board), the opponent's in `--eval-opponent`. A tick marks equality at the centre.
- **Scale:** the fill is winning chances, not raw pawns, so +0.3 and +9 both read correctly.
- **Number:** the number (+4.4, −2.0, #3) is shown in the engine panel, in `--mono`, never inside the bar.
- **Orientation:** flips with the board.

### Cards (sheets)

- **Surface:** `--sheet` background, 1px `--rule` border, `--radius-sheet`, `--shadow-sheet`.
- **Padding:** `--space-5` (`--space-4` on phones).
- **Header:** a card with a header row (field labels, column titles) puts a 1.5px `--ink` rule under it, like the printed top of a scoresheet. Rows inside are separated by `--ruling`.
- **No:** coloured side borders, gradients, or icon tiles. Don't nest cards.

### Tables

- **Header:** in `--text-sm` `--ink-2`, sentence case, with a 1.5px `--ink` rule under it.
- **Rows:** `--row` minimum height, `--ruling` separators, optional `--sheet-2` stripes for long tables.
- **Numbers:** right-aligned, tabular. Moves and evals in `--mono`.
- **Wide tables:** scroll inside their own box on phones (`overflow-x: auto`), never the page.

### Buttons

- **Primary:** `--blue` fill, `--on-blue` text, weight 640–680. One per view.
- **Secondary:** 1.5px `--blue` border, `--blue` text, transparent fill.
- **Quiet:** text only, underlined, for low-stakes actions such as "Flip board".
- **Danger:** `--red` border and text; filled red only inside a confirmation dialog.
- **Shape:** `--radius-control`, at least 44px tall (40px allowed in dense toolbars), horizontal padding `--space-4` to `--space-5`.
- **Icon-only buttons:** a stroke SVG icon plus an `aria-label`. Never a font glyph or emoji (they render as empty boxes on some systems).
- **States:** hover darkens to `--blue-ink`; press uses `--dur-press`; focus shows the global 2.5px `--blue` outline; disabled at 55% opacity with no hover.
- **Labels:** say what happens: "Analyse my games", "Download annotated PGN". The same action keeps the same name everywhere.

## Accessibility floor

- Text contrast 4.5:1 (3:1 at 24px+), checked in both themes.
- Never colour alone: move quality always has its symbol, and the eval has its number.
- Visible focus on everything you can press; targets at least 44px.
- The board has an `aria-label` describing the position and the arrows.
- Layouts work at 390px wide with no horizontal page scroll.

## Status

**Applied:**
- The tokens and base styles in `app.css`: colours, fonts, type scale, spacing, radii, shadows, motion, focus, selection and the reduced-motion rule.
- `--r` (cards) changed from 10px to 4px as part of this.

**Not yet applied** (component rules above, to follow):
- The default `.board` still uses the old green; the "Slate" board theme already uses the new board colours.
- The report card still uses the old Brilliant/Great/Best labels and colours (`app/static/js/views/report.js`).
- Move lists aren't in the mono face yet, and the eval bar isn't built.
