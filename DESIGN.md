# Design — Message Blank & Transmission Log

The visual world of the Job Agent's operator desk, recorded from the built UI in
`frontend/src/`. Written after the build, from what shipped.

## The idea

Every application is a message about to go out under the user's name. The agent drafts
it; only the user releases it. So the interface is the desk of a telegraph operator: a
tray of prepared message blanks, a register of everything transmitted, and query slips
for the lines the operator could not fill in.

This refuses the arrangement this category always ships — stat tiles, badge pills, a
sidebar, and a checkbox column with "approve selected" above it. That last one is not a
stylistic choice: a bulk approve is a button that approves things nobody read, and the
product's whole claim is that a human saw each one.

## Color

**Restrained**, two ribbons and a stock.

| Token | Light | Dark | Carries |
|---|---|---|---|
| `--stock` | `#e4e2d8` | `#16161a` | Message-blank paper; the page ground |
| `--stock-2` | `#edebe3` | `#1d1d22` | Masthead, ledger shading |
| `--blank` | `#f7f6f1` | `#222228` | The blank itself, tables, inputs |
| `--ink` | `#1a1a17` | `#e8e7e0` | The black ribbon |
| `--ink-2` / `--ink-3` | `#4a4a43` / `#7c7b71` | `#b4b3aa` / `#85847b` | Body, silkscreen labels |
| `--rule` / `--rule-strong` | `#c3c1b5` / `#a3a196` | `#35353d` / `#4a4a54` | Ruled hairlines |
| `--red` | `#b3261e` | `#e0655c` | The second ribbon: refusals, holds, stops |
| `--olive` | `#4f6349` | `#8fae86` | Released, sent, on line |
| `--amber` | `#9a6b12` | `#d3a552` | Attention that is not alarm |

The ground is deliberately a cool oat-grey, not cream: office form stock, not book paper.
Red is reserved for the agent's refusals and for anything that stopped — it never
decorates. Olive appears only when something has actually gone out or a line is healthy.

Dark mode is the same desk after hours: stock inverts, both ribbons keep their jobs.

## Type

One idea, applied consistently: **records are set in monospace because they are records**;
everything else is the system sans.

- `--font-record` (`ui-monospace`, SF Mono, Menlo) — field values, timestamps, log lines,
  email bodies, platform names, stamps, labels, buttons
- `--font-chrome` (system sans) — headings, prose, form copy

Silkscreen labels (`.label`) are 10px monospace, uppercase, `0.11em` tracking — the small
caps printed beside a field on a real form. Body is 14px/1.5. No display face anywhere;
this is an Operate surface and a display face in a UI label is noise.

## Materials

- **Ruled field rows** (`.field`) — a label column and a value column separated by a
  hairline, the way a form prints what goes where. Collapses to stacked on narrow screens.
- **Tractor-feed perforation** (`.blank--feed`) — an 18px sprocket strip down the left
  edge of every message blank, drawn with a repeating radial gradient.
- **Rubber stamps** (`.stamp`) — status as an outlined, letter-spaced mark. `.stamp--struck`
  rotates −3.5° and is used once per blank, as if pressed by hand. Never a rounded pill.
- **Ledger ruling** (`.register`) — alternating row shading, a heavy rule under the head,
  tabular numerals, right-aligned numerics.
- **Query slips** (`.slip`) — a fully ruled box in its tone, washed in that tone, with the
  instruction on its own dashed line above the space you write in.

## The one distinction the design exists to carry

Two kinds of unanswered question, which must never look alike:

- **`.slip--refused`** — red box, red wash, heading *"The agent will not answer this"*.
  Demographic, disability, veteran and compensation questions. A boundary held on purpose.
- **`.slip--unmapped`** — amber box, amber wash, heading *"The agent could not map this"*.
  A gap in what the agent knew.

They are separated into two headed sections, with the refusals explained once above them.
Collapsing these into a single "issues" list would lose the product.

## Components

Every interactive element carries default, hover, focus, active and disabled.

- `.btn` — outlined, monospace, uppercase; inverts to solid on hover
- `.btn--release` — solid ink, turns olive on hover: the lever that sends something
- `.btn--danger` — red outline, fills on discard
- `.btn--quiet` — borderless, for tertiary actions
- `.input` / `.select` / `.textarea` — 1px ruled, monospace, border darkens on focus
- `.counter` — a readout on the desk, large tabular numeral over a silkscreen label
- `.empty` — dashed box, an angled stamp, and **always a named next action**
- `.toast` — a receipt; tone reads off a 3px top edge, where paper leaves the machine

Focus is a 2px ink ring offset by the stock colour, visible in both themes.

## Motion

150–250ms on colour and border transitions. One keyframe animation (`slip-in`, 180ms) for
toasts, and a skeleton shimmer for loading. No page-load choreography — this loads into a
task. All of it collapses under `prefers-reduced-motion`.

## Layout

Masthead (sticky) over tabbed dividers, then a single content column at `max-width: 1440px`.
Tabs are the tabbed dividers of a form book: active tab joins the page by matching its
background and dropping its bottom border. Responsive behaviour is structural — field rows
stack under 640px, the masthead's motto is dropped, tables scroll inside their own
container. Type never fluid-scales.

## What must not drift

1. **No bulk approve, ever.** Releasing requires opening the blank.
2. **Refused ≠ unmapped.** Two tones, two headings, two sections.
3. **Unverified documents are announced at the moment of approval**, in red, above
   everything else, and disable the release lever.
4. **Red means the agent stopped or refused.** Never decoration.
5. **Empty states name the next action.** Never "No data".
6. **The agent's own words are shown verbatim** — its guidance, its refusal reasons, its
   detection reasoning. The API writes for a person; the UI must not paraphrase it into
   "Error".

## Contract

The direction contract is an HTML comment at the top of `<body>` in
`frontend/index.html`, and survives into the built `job_agent/dashboard/static/index.html`.
Seed key `ed083330`, candidate 5 of the grounded list.
