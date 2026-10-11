# CORSIA Design System

**Canonical reference for every UI surface in CORSIA.** Read this *before*
touching anything visual. Visual ground truth lives at `/_design`
(`frontend/src/routes/_design/+page.svelte`).

**Last standards refresh:** 2026-07-01. The current native-feel baseline is
the June 11, 2026 Liquid Glass polish pass (`9d1795d4`) plus its reviewer
follow-up (`8a7a7d4e`): Apple-HIG-aligned glass hierarchy, 44pt touch
targets, one-frame press/tap response, haptics, tokenized motion, safe-area
and keyboard contracts, reduced-motion / reduced-transparency / contrast
support, and the live token split in `app-tokens.css` + `app.css`.

---

## TL;DR

- **Use the live primitives** in `frontend/src/lib/components/ui/`: `Button`,
  `Field`, `IconButton`, `Message`, `Modal`, `Section`, `StatePill`, `Toggle`.
  Don't roll your own.
- **All design values are tokens** in `frontend/src/app-tokens.css` (imported
  by `app.css`). Never hardcode colors, spacing, type, blurs, radii, motion,
  press behavior, elevations, or scrim alphas.
- **Glass is for chrome, not content.** Sidebars, headers, modals, drawers,
  input chips, popovers — yes. Message bubbles, code blocks, project cards,
  timeline rows — never.
- **Four glass materials** (`--glass-thin / regular / regular-thick / thick`),
  picked by the surface's persistence + focus role. Pair with `--glass-blur`
  and `--glass-rim` (every glass surface needs the rim).
- **Two-repo split:** platform changes commit in the CORSIA code repository,
  not the workspace-document repository.
- If this markdown disagrees with live source, the live source wins and this
  doc must be updated in the same change.
- If a needed primitive variant doesn't exist, **propose adding it** — don't
  build a one-off.

---

## 1. Philosophy

1. **Apple-HIG-aligned, content-first.** Liquid Glass is *the navigation /
   control layer*. Content sits below it on solid surfaces and the glass
   layer recedes when the user is reading or creating.
2. **Tokens → primitives → consumers.** One-way dependency. Tokens live in
   `app-tokens.css`; global mechanics live in `app.css`; primitives live in
   `ui/`; consumers compose them. Never invert the chain (consumer-defined
   tokens, consumer-overridden primitive styles, etc.).
3. **If it's not in `/_design`, it shouldn't exist in code.** The
   kitchen-sink route is the visual contract. New variants land there first.
4. **Tweaks ripple.** Every consumer of a primitive picks up the change.
   This is the whole point. Don't duplicate the primitive's styles.
5. **Loud failure over silent fallback.** If a token is missing, fail
   visibly; don't add a hex fallback inside `var()`. Color/theme fallbacks
   inside `var()` are forbidden (numeric fallbacks like `var(--radius-xs, 4px)`
   are fine).
6. **Era prefixes are retired.** `v7-`, `v054-` were two short-lived
   conventions during the consolidation. They are gone:
   `grep -r "v7-\|v054-" frontend/src/` should stay empty.

---

## 2. Tokens

Tokens are defined in `frontend/src/app-tokens.css` and imported by
`frontend/src/app.css`. Two themes: dark (default, `:root`) and light
(`[data-theme="light"]`). `app.css` owns global element resets, markdown,
accessibility media queries, press behavior, and app-wide mechanics.

### 2.1 Color — semantic palette

The canonical accent palette is **violet / teal / sky / amber**. Each has
`--{name}`, `--{name}-2` (hover/emphasis), `--{name}-bg` (10% tint), and
`--{name}-line` (28%/32% border).

| Token | Role | Dark | Light | Notes |
|---|---|---|---|---|
| `--violet` | Brand accent | `#8b5cf6` | `#7c5cf6` | Aliased to `--purple` in dark; do not reach for `--purple` directly. |
| `--violet-2` | Hover/emphasis | `#b29bff` | `#6d4ce6` | Used for hover states, active text. |
| `--violet-bg` | Subtle fill | `rgba(139,92,246,0.10)` | `rgba(124,92,246,0.08)` | Buttons, pills, hover. |
| `--violet-line` | Border tint | `rgba(139,92,246,0.28)` | `rgba(124,92,246,0.32)` | Pill / button borders. |
| `--teal` | Local-source accent | `#2dd4bf` | `#0d9488` | Used for local model surfaces. |
| `--teal-bg` | | `rgba(45,212,191,0.10)` | `rgba(13,148,136,0.08)` | |
| `--teal-line` | | `rgba(45,212,191,0.28)` | `rgba(13,148,136,0.32)` | |
| `--sky` | Info / cloud-source accent | aliased to `--blue` `#60a5fa` | `#2563eb` | Do not reach for `--blue` directly. |
| `--sky-bg` | | `rgba(96,165,250,0.10)` | `rgba(37,99,235,0.08)` | |
| `--sky-line` | | `rgba(96,165,250,0.28)` | `rgba(37,99,235,0.32)` | |
| `--amber` | Attention / warning | `#f59e0b` | `#d97706` | Hue differs across themes (intentional — improves light-mode readability). |
| `--amber-bg` | | `rgba(245,158,11,0.10)` | `rgba(217,119,6,0.08)` | |
| `--amber-line` | | `rgba(245,158,11,0.28)` | `rgba(217,119,6,0.32)` | |
| `--green` | Positive / success | `#34d399` | `#34d399` | |
| `--green-bg` | | `rgba(52,211,153,0.08)` | `#f0fdf4` *(solid!)* | Light-mode value is solid hex, not rgba — known wart, do not "fix" without a sweep. |
| `--green-border` | | `rgba(52,211,153,0.25)` | `rgba(34,197,94,0.3)` | |
| `--green-text` | | `#34d399` | `#16a34a` | Use this for green-on-green-bg text. |
| `--red` | Destructive / error | `#f87171` | `#f87171` | |
| `--red-bg` | | `rgba(248,113,113,0.08)` | `#fef2f2` *(solid)* | |
| `--red-bg-hover` | | `rgba(248,113,113,0.14)` | `#fee2e2` *(solid)* | |

**Anti-patterns:**
- ❌ Reaching for `--purple` or `--blue` directly. Use `--violet` / `--sky`.
- ❌ Inline `rgba(139,92,246,...)` because you "need a custom violet
  opacity". Use `--violet-bg` / `--violet-line` or `color-mix(in srgb,
  var(--violet) X%, transparent)`.
- ❌ Defining a per-component fallback like `var(--bg, #fff)` — define the
  token in `app-tokens.css` instead. Color/theme fallbacks inside `var()` are
  forbidden by the "CSS Variable Convention" rule in `FRONTEND.md`.
- ❌ `--orange` (`#fb923c`) — defined but currently used only as a fallback
  in `MessageList`. Don't reach for it; use one of the canonical hues.

### 2.2 Surfaces

| Token | Role | Dark | Light |
|---|---|---|---|
| `--bg-primary` | Page bg, chat content | `#0a0a0b` | `#ffffff` |
| `--bg-secondary` | Elevated panels, sidebars, headers (solid form) | `#141416` | `#f8f8fa` |
| `--bg-tertiary` | Inputs, code blocks, secondary cards | `#1c1c1f` | `#f0f0f2` |
| `--bg-surface` | Cards (alias to secondary) | `#141416` | `#ffffff` |
| `--bg-elevated` | Cards (alias to tertiary) | `#1c1c1f` | `#ffffff` |
| `--bg-hover` | Hover states for clickable rows | `#242428` | `#e8e8ec` |
| `--bg-active` | Pressed / selected states | `#2a2a2f` | `#e0e0e5` |
| `--border` | Default borders | `#2a2a2f` | `#d8d8de` |
| `--border-subtle` | Hairlines, less-prominent dividers | `#1f1f24` | `#ececf0` |
| `--text-primary` | Body, headings | `#e8e8ed` | `#1a1a1e` |
| `--text-secondary` | Labels, secondary copy | `#8e8e93` | `#6e6e73` |
| `--text-muted` | Alias to secondary | `#8e8e93` | `#6e6e73` |
| `--text-tertiary` | Hints, captions, ghost-button color | `#636366` | `#9e9ea3` |

### 2.3 Borders & radii

| Token | Value | Use |
|---|---|---|
| `--radius` | `12px` | Modals, major containers, drawers |
| `--radius-sm` | `8px` | Buttons, inputs (when not stripped), code-block wrappers, cards |
| `--radius-xs` | `6px` | IconButton, StatePill, Message banner, Field input |
| `--radius-2xs` | `4px` | Micro chips, badges, kbd hints |
| `--radius-lg` | `16px` | Large cards (pairs with `--lg-card-radius: 14px`) |
| `--radius-xl` | `24px` | Floating input chip |
| `--radius-pill` | `999px` | Capsule pills |
| `--radius-circle` | `50%` | True circles (dots, FAB, avatar) |

Raw radius px is retired (native-feel pass): single-value radii reference
the ladder. Asymmetric corner sets (`18px 18px 4px 18px` bubbles,
directional rails) are geometric shapes, not ladder values — they stay
literal.

**Concentricity rule.** Inner radius = outer radius − inner padding. Modal
(12) with 18px padding hosts inputs at 6 (Field) — that lands the inputs
visually centered inside the panel curve. Sidebar has no outer radius (full
height); items inside it use 6.

### 2.4 Spacing

CORSIA has a formal spacing scale. New layout uses `--space-1..8`; raw
numbers are only acceptable for one-off geometric shapes that do not express
layout rhythm.

| Stop | px | Where used |
|---|---|---|
| 1 | 4 | icon gaps, micro-spacing |
| 2 | 8 | tight gaps, list gaps, card stack-spacing |
| 3 | 12 | standard internal spacing, message bottom-margin |
| 4 | 16 | section padding, drawer body padding, gap between cards |
| 5 | 20 | settings-card horizontal padding (legacy: 20 24) |
| 6 | 24 | settings-card horizontal padding (target), section header bottom margin |
| 7 | 32 | empty-state padding, page-level breathing room |
| 8 | 48 | hero padding, page top margin |

**Do** stick to this scale when adding new layout. **Don't** mix 5/10/14
into the rhythm. If a touched surface still has off-scale padding from older
work, clean that surface while you are there or document why the value is a
geometric exception.

### 2.5 Glass materials

Apple's HIG defines four material levels by opacity. CORSIA mirrors them:

| Token | Dark opacity | Light opacity | Use |
|---|---|---|---|
| `--glass-thin` | 60% | 78% | **Fleeting / floating overlays.** Title bars, app headers, input chip, ephemeral popovers. Content scrolls under cleanly; blur dominates over fill. |
| `--glass-regular` | 68% | 84% | **Drawers, slide-outs, panels.** Surfaces that overlay content with their own dense info. (TimelinePanel, WorkingMemoryPanel.) |
| `--glass-regular-thick` | 72% | 88% | **Modals, dropdowns, popovers.** Focus surfaces — readability matters. (Modal.) |
| `--glass-thick` | 80% | 90% | **Sidebars, persistent navigation.** Always-visible surfaces with high readability demands. (Sidebar.) |

All four are computed via `color-mix(in srgb, var(--bg-secondary) X%,
transparent)`. They tint with whatever the secondary background is — single
source of truth for dark vs. light.

**Pick by persistence + focus.** Persistent and readability-critical →
thick. Focus surface (user is *inside* it) → regular-thick. Drawer over
content → regular. Floating, fleeting, content-scrolls-under → thin.

**Solid (no glass)** is not optional for *content* surfaces. Message
bubbles, code blocks, project cards, timeline rows must stay solid
(`--bg-primary` or `--bg-secondary`) — never become glass.

### 2.6 Glass mechanics

```css
/* Dark mode */
--glass-blur: blur(40px) saturate(180%);
--glass-rim:  inset 0 1px 0 rgba(255, 255, 255, 0.05);

/* Light mode */
--glass-blur: blur(40px) saturate(118%);
--glass-rim:  inset 0 1px 0 rgba(255, 255, 255, 0.6);
```

Every glass surface gets all three: a `--glass-*` background, the
`--glass-blur` filter, and the `--glass-rim` highlight as a `box-shadow`.
The rim is the 1px light-catching edge that sells the material — do not skip it.

**Native-feel pass additions** (all per-theme tuned in `app.css`):

| Token | Use |
|---|---|
| `--glass-blur-soft` | `blur(8px) saturate(120%)` — close-to-content overlays (drag-drop, dialog scrims, lightbox) |
| `--glass-scrim` | The one backdrop dim behind focus surfaces (modals 0.28 dark / 0.22 light); drawers reuse it |
| `--glass-elevation-{1,2,3}` | Specular top edge + depth shadow, one `box-shadow` per level: 1 floating chips/popovers · 2 drawers/panels · 3 modals |

```css
/* canonical recipe */
.surface {
  background: var(--glass-thin);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  box-shadow: var(--glass-rim);
  border: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
}
```

**Why saturate is `180%` dark and `118%` light.** Saturating a near-white
surface over a colorful or dark wallpaper pulls the bg toward whatever's
behind, producing a "darkish hue" effect. Dropping saturate sharply in
light mode preserves the light-glass read.

**Why the rim flips opacity (0.05 → 0.6).** The dark-mode rim is barely-
visible white; in light mode the rim has to be much stronger to show on a
near-white surface. Apple's glass sells its refractive edge with this
highlight.

**Performance budget.** Apple HIG: ≤ 4 compositing layers per screen, blur
≤ 40px on phone surfaces (≤ 60px desktop). CORSIA's `40px` is at the
iPhone ceiling. Don't go higher. *Do* go lower (8–20px) for close-to-
content overlays — but introduce a token for it (current hardcoded 2px /
8px overlays are flagged in §8.1).

### 2.7 Motion & transitions

Named easing curves, a duration scale, and a small library of named
transition patterns choreographed across surfaces. `--ease-*` and
`--dur-*` are REAL TOKENS as of the native-feel pass — consumers
reference them; inline `cubic-bezier(...)` or raw durations are a
review failure. The ladder: `--ease-snap / -material / -ios-sheet /
-entrance / -spring` and `--dur-tight 120ms / -standard 220ms /
-drawer 320ms / -spring 380ms / -hero 460ms / -hero-plus 600ms`.
`--ease-spring` defaults to a back-out cubic-bezier and upgrades to a
CSS `linear()` spring (≈6% overshoot) under `@supports`; for JS-driven
Svelte transitions use `backOut` at the matching duration. The rule
covers transitions and entrance motion — **ambient loop periods**
(spinners, pulses, shimmer ≥800ms) are rhythm, not motion classes, and
stay literal.

**Transitions are part of the design system.** When you add a new
surface or a new state change, pick a *named pattern* from §2.7.3. If
no pattern fits, propose one — don't invent a curve / duration / shape
in-line.

#### 2.7.1 Easing curves

| Token | Curve | Where used |
|---|---|---|
| `--ease-snap` | `cubic-bezier(0.2, 0, 0, 1)` | Button + IconButton state changes (100ms); tight UI feedback. Source: `ui/Button.svelte:59`, `ui/IconButton.svelte:46`. |
| `--ease-material` | `cubic-bezier(0.4, 0, 0.2, 1)` | Sidebar collapse width transition (250ms). Source: `Sidebar.svelte:189`. |
| `--ease-ios-sheet` | `cubic-bezier(0.32, 0.72, 0, 1)` | Mobile drawer transform (320ms); swipe-row snap. The iOS sheet curve. Source: `Sidebar.svelte:259`. |
| `--ease-entrance` | `cubic-bezier(0.22, 1, 0.36, 1)` | Login hero-grow (600ms), fade-rise (380ms); phone `main` slide when the rail or pane opens and closes (`--dur-spring`). Pronounced ease-out with no overshoot. Source: `LoginScreen.svelte:94, 106`, `HomeShell.svelte:339, 374`. |
| `--ease-cubic-out` (Svelte built-in) | `cubicOut` ≈ `cubic-bezier(0.33, 1, 0.68, 1)` | Auth gate ↔ app cross-fade (320 / 460ms). Source: `+page.svelte:39, 347–354`. |

**Note on auth-fade curve drift.** `Sidebar.svelte:255` comment claims
the auth fade uses the iOS sheet curve. The auth fade actually uses
Svelte's `cubicOut`. The two are visually similar (both ease-out tails)
but not identical. Either align the auth fade to `--ease-ios-sheet` or
fix the comment — flagged in §8.7.

#### 2.7.2 Duration scale

| Class | Range | Use |
|---|---|---|
| Tight | 100–160ms | Button / IconButton hover; hover bg; modal backdrop fade |
| Standard | 200–250ms | Modal fly-in (200ms); sidebar collapse width (250ms) |
| Drawer | 320ms | Mobile drawer present / dismiss; swipe snap; auth-gate exit |
| Hero | 380–460ms | Auth gate enter (460ms / 200ms delay); login fade-rise (380ms / 220ms delay) |
| Hero+ | 600ms | Login hero-grow logo |

Avoid mid-band oddballs (270, 420ms etc.) — pick the class that matches
the surface's role in the user's attention.

#### 2.7.3 Named transition patterns

Reusable choreography. Currently inlined at call sites; a future round
can extract these as Svelte transition factories or
`useTransition()` helpers. Each entry is a contract — when you add a
new instance of the same surface type, **match the pattern**.

**Modal entrance / exit.** Backdrop `fade` 160ms; content `fly` y:8px /
duration 200ms. Both use Svelte's default ease. Source:
`ui/Modal.svelte:50–53`.

**Drawer slide (mobile sidebar, swipe sheets).** Transform-only, 320ms,
`--ease-ios-sheet`, with `will-change: transform`. Mobile drawers
should *only* animate transform — never width / opacity / margin.
Source: `Sidebar.svelte:259`.

**Sidebar collapse (desktop).** Width transition 250ms
`--ease-material`. Note: the only place Material curve is used. Drawer
transforms use `--ease-ios-sheet` instead. Width transitions are
intentionally separated from drawer slides because they animate
*layout* rather than *position*.

**Auth gate ↔ app cross-fade (sign-in).** Auth gate `out:fade
duration: 320, easing: cubicOut`; app `in:fade duration: 460, delay:
200, easing: cubicOut`. The 200ms delay overlaps the gate's exit so the
transition feels like a hand-off rather than a flicker. Source:
`+page.svelte:347–354`.

**Picker → login hero-grow (Tauri).** Logo `hero-grow` keyframe 600ms
`--ease-entrance`, `transform-origin: center`: scale 0.4 → 1, opacity 0
→ 1. The 0.4 floor matches the 96px picker mark to the 240px hero size
so first paint reads as continuation rather than a swap. The login
screen background also matches the picker's translucency
(`color-mix(--bg-primary 55%, transparent)` + `--glass-blur`) when in
the Tauri shell. Source: `LoginScreen.svelte:68–100`.

**Picker → login fade-rise (form).** Card content (h1 / p / input /
button / error) all share a single `fade-rise` keyframe: 380ms
`--ease-entrance` with 220ms delay; opacity 0 + translateY(6px) →
final. The 220ms delay overlaps the hero-grow's tail so the hero
settles before the form appears. Source: `LoginScreen.svelte:101–111`.

**Login → home (the new-chat reveal).** Same auth-gate cross-fade
above, terminating in the chat view's `bg-primary` solid surface. No
extra entrance choreography on the chat content — the cross-fade IS the
entrance. The view delivers the user to a clean new-chat empty state;
the InputBar's floating chip and ChatHeader's glass material are the
visual reward. Don't add additional micro-animations on top.

**Button / IconButton state.** `transition: all 100ms --ease-snap`.
Source: `ui/Button.svelte:59`, `ui/IconButton.svelte:46`.

**Disclosure / chevron expand.** Chevron `transform: rotate(180deg)`
0.15s; body uses `fadeIn 0.15s ease` keyframe from `app.css:339`.
Currently four hand-rolled instances (AssistantMessage,
LiveToolActivity, InputBar action sheet, ProjectList) — pending
Disclosure primitive (§4.2) is the intended single source of truth.

**Streaming cursor blink.** `.streaming-cursor` (`app.css:343–351`):
2px wide / 16px tall / `var(--accent)` bg / `animation: blink 1s
infinite`.

**Pulse (thinking / loading indicators).** `pulse` keyframe
(`app.css:340`): opacity 1 ↔ 0.4. LiveToolActivity uses
`pulse 1.4s infinite`; the 3-dot loader uses the same keyframe with
staggered `animation-delay` (0.0 / 0.2 / 0.4s).

**Hover tint (links / list rows / pills).** `transition: background
150ms` (typical). Pure background swap — no transform, no scale. Use
`--bg-hover` for neutral rows; `--violet-bg` + `--violet-line` for
primary pills.

**Page-level navigation.** No global page-transition framework today;
view switches inside `+page.svelte` are flat (no animation). The
auth-gate cross-fade is the lone exception. **Don't add page-transition
libraries** — the flat default is intentional.

**Fold wave (plan tree).** When finished plan rows fold into their
level's "N completed" row, every folding row plays the same three beats:
a check beat (`--dur-tight`), a slide to the bottom of its level
(`--dur-drawer`, `--ease-ios-sheet`), a fold into the count
(`--dur-standard`, scaleY + fade). Rows overlap in one wave: the start
stagger is at most 90ms and tightens so the whole wave ends within
1.8s however many rows fold; rows are never skipped or snapped to save
time. Surviving rows glide from their old place (FLIP). Only a fold the
user watches plays (never a first render); reduced motion shows the
settled state. Source: `plans/foldWave.ts`.

**Swipe gesture commit / cancel** (mobile sidebar rows): rubber-band
0.35× past zone widths, dominance check 1.4× horizontal vs. vertical
to avoid tripping vertical scroll, haptic blip on threshold cross +
commit. Snap on release uses `--ease-ios-sheet` at 320ms.

#### 2.7.4 Animation primitives

Defined in `app.css`. Reuse, don't redefine.

| Keyframe | Path | Use |
|---|---|---|
| `fadeIn` | opacity 0 → 1, translateY(4 → 0) | Disclosure body, dropdown menus, generic in |
| `pulse` | opacity 1 ↔ 0.4 | Thinking indicators, loading dots |
| `blink` | opacity 1 ↔ 0 | Streaming cursor |
| `spin` (component-local) | rotate 0 → 360 | Loading spinners (`ui/Button.svelte:125`) |

If a new keyframe is genuinely needed, add it once to `app.css` and
reach for it from any component.

#### 2.7.5 Reduced motion

`@media (prefers-reduced-motion: reduce)` should:
- Disable `hero-grow` and `fade-rise` (replace with instant
  opacity-only).
- Drop `fly` from modal entrance (keep fade).
- Replace drawer transform-slide with instant.
- Hold ambient `pulse` and `blink` at a static frame (not jarring).

**Implemented globally** in `app.css` (native-feel pass): a
`prefers-reduced-motion` block zeroes animation/transition durations
app-wide (ambient pulse/blink land on a static frame), and
`prefers-reduced-transparency` collapses every `--glass-*` material to
solid `--bg-secondary` with blur off. `prefers-contrast: more`
strengthens both border tokens.

### 2.8 Z-index ladder

| Layer | z-index | Examples |
|---|---|---|
| Content | 0 | Chat panel, drawers' parent shells |
| Sticky chrome | 10 | ChatHeader, AppShellBar, InputBar (`.input-floater`) |
| Drawers | 20–30 | TimelinePanel, WorkingMemoryPanel |
| Drag overlay (chat) | 50 | Drag-drop drop zone |
| Modal backdrop | 100 | Modal `.bg` |
| Cmd palette | 200 (legacy `.dialog-overlay`) | Searchable command surface |
| Lightbox | 1000+ (legacy) | Image viewer |
| Toast / haptic feedback | 9999 | Top-of-stack ephemera |

Several legacy values in `app.css` (`.dialog-overlay { z-index: 200 }`)
need an audit for collision with newer modal stack. Out of scope this
round; flagged in §8.

### 2.9 Typography

Font stack: `-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter',
system-ui, sans-serif`. **Platform-correct type ramp** (native-feel
pass) — desktop matches macOS density, mobile (≤768px) tracks iOS
text-style metrics. Raw `font-size` px is retired; components reference
the ramp:

| Token | Desktop | Mobile | iOS style |
|---|---|---|---|
| `--text-caption` | 10.5px | 11px | Caption |
| `--text-footnote` | 11.5px | 13px | Footnote |
| `--text-subhead` | 13px | 15px | Subheadline |
| `--text-callout` | 14px | 16px | Callout |
| `--text-body` | 15px | 17px | Body |
| `--text-title3` | 17px | 20px | Title 3 |
| `--text-title2` | 20px | 22px | Title 2 |
| `--text-title1` | 28px | 28px | Title 1 |
| `--text-input` | 16px | 16px | iOS zoom guard — a mobile media rule floor-lifts every focusable control to ≥16px |

Weight/line-height/tracking ladders (`--fw-*`, `--lh-*`, `--ls-*`)
unchanged. Markdown content uses `--text-callout` and scales down inside
`.live-reasoning-text` / `.reasoning-content` (subhead / 1.5).

**Section caps** (PROJECTS, PINNED, CONVERSATIONS, etc. — used by
`Section.svelte` and ad-hoc sidebar headers):
- 9.5–10.5px / weight 700 / 0.10–0.12em letter-spacing / uppercase
- Color: `var(--text-tertiary)` for `Section`; sidebar variants tint
  with `color-mix(in srgb, var(--violet) 55%, var(--text-tertiary))`

**Numeric / monospace:** `'SF Mono', 'Fira Code', monospace` for code,
tool-call names, and IDs.

**Markdown rendering rules** are global in `app.css:235–330`. Apply
`.md-content` to any container holding renderer output; the outer wrapper
handles its own chrome (background, border, max-height). Reasoning chips
override font-size to 13px / line-height 1.5 / `--text-tertiary` color.

### 2.10 Theme overrides — known divergences

| Token | Dark | Light | Status |
|---|---|---|---|
| `--green-bg` | rgba 0.08 | `#f0fdf4` (solid hex) | Known wart. Do not "fix" without a sweep — light-mode solid was a deliberate readability choice. |
| `--red-bg` | rgba 0.08 | `#fef2f2` (solid) | Same pattern. |
| `--amber` hue | `#f59e0b` | `#d97706` | Different hue, not just opacity. Intentional for light-mode contrast. |
| `--blue` (sky base) | `#60a5fa` | `#3b82f6` | Different hue. Intentional. |
| `--accent` | `#8b5cf6` | `#7c5cf6` | Slightly darker violet for light-mode contrast. |
| `--glass-rim` opacity | 0.05 | 0.60 | Flip rationale in §2.6. |
| `--glass-blur` saturate | 180% | 115% | Flip rationale in §2.6. |

### 2.11 Accessibility hooks

The following media queries should be honored in any glass surface:

```css
@media (prefers-reduced-transparency) {
  /* Replace the glass with a solid surface. Apple HIG: Reduce
     Transparency makes Liquid Glass frostier and obscures the
     backdrop. */
  .surface { background: var(--bg-secondary); backdrop-filter: none; }
}
@media (prefers-reduced-motion) {
  /* Disable shimmer / parallax / specular highlights. Static glass
     is still glass. */
  .specular, .shimmer { animation: none; }
}
@media (prefers-contrast: more) {
  /* Solid bg + stronger borders. */
  .surface { background: var(--bg-secondary); border-color: var(--text-primary); }
}
```

**4.5:1 minimum contrast** for body text *over* glass. Test with a busy
backdrop (open Safari behind the Tauri shell, or scroll a colorful chat),
not a blank one. The rule that matters is "after blur" — blurred but
saturated content can pull the text toward unreadable.

### 2.12 Tauri-specific tokens & contracts

Tauri shell (`desktop/src-tauri/`) coordinates with the frontend through
two mechanisms:

1. **Vibrancy.** `tauri.conf.json` sets `windowEffects: { effects:
   ["sidebar"], state: "active" }`. This applies macOS NSVisualEffectView
   *only* to the surface tagged with the `sidebar` material. The window
   itself is `transparent: true`.
2. **`html[data-shell="tauri"]` flag** set synchronously in `app.html`
   *before first paint* (Rust `on_page_load` was too late and produced a
   visible flash). When the flag is present:
   - `--header-height: 44px` (down from 56) so icon-button centers
     (y≈22) align with macOS traffic-light centers.
   - `body { background: transparent }` so vibrancy bleeds through any
     surface using a glass token.
   - `.shell-inset` rule (when sidebar collapsed) pads the chat header /
     `AppShellBar` 88px from window-left to clear the traffic lights.
3. **Chat panel solid bg.** The chat panel renders solid `--bg-primary` on
   top of the transparent body. Vibrancy therefore shows *only* through
   the sidebar — it does not bleed into the chat content area. **This
   coordination is the reason CORSIA's chat content stays readable** even
   though the body is transparent.
4. **Traffic-lights position.** `tauri.conf.json` sets
   `trafficLightsPosition: { x: 12, y: 15 }` to align with our 44px header
   icon centers. Changes here require a Tauri rebuild + new DMG; pure
   config changes don't propagate via web reload.
5. **Drag region.** `data-tauri-drag-region` attribute marks elements
   draggable; the WKWebView custom handler in `lib.rs` listens for this
   attribute and invokes `start_window_drag`. Children that should *not*
   trigger drag (icon buttons, model selector) need `app-region: no-drag`
   in CSS or to swallow `pointerdown`.
6. **Window-controls-overlay (PWA).** The same `data-tauri-drag-region`
   attribute also satisfies the macOS PWA WCO contract — single attribute
   handles both shells.

### 2.13 PWA / mobile contracts

Mobile (iOS PWA) has *no* NSVisualEffectView — nothing behind the page.
Two compensating patterns:

1. **Body pinned to `--bg-primary` on mobile** (`app.css:218–220`). The
   transparent-body trick that lets desktop vibrancy through becomes a
   non-matching color behind the iOS status bar; pinning to primary keeps
   the status-bar bg (driven by theme-color meta) seamless.
2. **3-stop linear gradient on glass headers** (ChatHeader, AppShellBar
   on mobile):
   ```css
   background-image: linear-gradient(
     180deg,
     color-mix(in srgb, var(--bg-primary) 45%, transparent) 0%,
     color-mix(in srgb, var(--bg-primary) 25%, transparent) 50%,
     transparent 100%
   );
   ```
   Replaces the inset rim on mobile (which read as a hairline seam between
   iOS status bar and our chrome).

**Safe-area insets:** `--safe-bottom: env(safe-area-inset-bottom, 0px)`
and `--safe-top: env(safe-area-inset-top, 0px)`. Use these for any
chrome that touches notch / home-indicator zones. ChatHeader grows by
`safe-top`; InputBar bottom padding includes `safe-bottom`.

**Touch targets:** 44×44 minimum on every interactive control on mobile.
14px gaps between adjacent IconButtons (was 6/8px and produced mis-taps).

**iOS sheet curve** for mobile drawers: `cubic-bezier(0.32, 0.72, 0, 1)`
at 0.32s with `will-change: transform`.

**Pressed states (touch response).** Every tappable acknowledges
touch-down within one frame. `app.css` gives all `button`s
the standalone `scale: var(--press-scale)` on `:active` (composes with
positioning transforms); row-shaped buttons opt out by carrying
`class="press-tint"` in markup — ONE central rule whose `!important`
background deterministically beats scoped `:hover`/state backgrounds.
Never hand-roll a scoped `:active` tint; the class is the mechanism
(scaling a full-width row reads as wobble). `app.html` registers an empty passive `touchstart`
listener (iOS only activates `:active` when a touch listener exists).
The iOS gray tap flash is disabled app-wide; pressed states replace it.

**Keyboard contract.** The composer declares `enterkeyhint="send"`
(return key reads **Send**), explicit `autocapitalize`/`autocorrect`,
and the layout viewport uses `interactive-widget=resizes-content` so
bottom chrome rides the keyboard.

**Overscroll.** `html, body { overscroll-behavior: none }` — the app
shell never rubber-bands as a page; inner scrollers keep their own
momentum.

**Hit-target extension.** Sub-44pt primitives (`IconButton` sm/md,
`Button` sm/md) extend their TAP AREA via an invisible `::after` inset
on touch viewports — visual size unchanged, HIG target met.

**Drawer gesture physics** (`$lib/drawerDrag`, unit-tested): canvas
tracks the finger 1:1; the open edge rubber-bands (×0.15 — small so the
canvas never exposes a seam); release is velocity-aware (a ≥0.5 px/ms
flick commits by direction, else the 40% positional snap); settle uses
`--ease-entrance` at `--dur-spring`. Never an overshooting curve such as
`--ease-spring` on a full-width transform: the overshoot carries `main`
past x=0, so content briefly sits off the left edge and the page scrolls
sideways.

**P3 accents.** Behind `@supports`, the four accent hues upgrade to
`color(display-p3 …)` (same component values, wider gamut — vivid on
OLED). Project-scoped accent reassignment (inline `setProperty` from
`+layout`) wins over the `@supports` block by design.

**Swipe gestures** (sidebar session rows):
- Rubber-band 0.35× past zone widths.
- Horizontal-vs-vertical dominance check 1.4× (so vertical scroll doesn't
  trip the swipe).
- Action zones translate3d outside the wrapper at rest, clipped by
  `overflow: hidden`, so the row stays glass-transparent.
- Haptic blip on threshold cross + commit.

---

## 3. Existing primitives

All in `frontend/src/lib/components/ui/`. Re-exported from
`ui/index.ts`. Demonstrated in every variant at `/_design`.

### 3.1 Modal

`ui/Modal.svelte`. Centered dialog with backdrop, head/body/foot slots.
**Glass material:** `--glass-regular-thick` + `--glass-blur` + 1px
soft border + drop shadow.

```ts
interface Props {
  title?: string;
  size?: 'sm' | 'md' | 'lg';            // 480 / 640 / 800
  role?: 'dialog' | 'alertdialog';       // default 'dialog'
  closeOnEscape?: boolean;               // default true
  closeOnBackdrop?: boolean;             // default true
  ariaLabel?: string;
  ariaLabelledBy?: string;
  onClose: () => void;
  head?: Snippet;                        // override default title + close X
  foot?: Snippet;                        // optional footer (action buttons)
  children: Snippet;                     // body
}
```

**Behavior:** Escape closes (when enabled), backdrop click closes (when
enabled), inner click does NOT propagate to backdrop. Body has
`overflow-y: auto`; modal max-height 86vh desktop / 92vh mobile. Fade
backdrop 160ms, fly content (y: 8px) 200ms.

**Backdrop is light dim only** — `rgba(0, 0, 0, 0.28)` solid. The modal
panel does the glass; the backdrop just de-emphasizes content. Liquid
Glass principle: the surface IS the focus, not a black scrim over it.

**Do:** Use this for any dialog. Pass `head` only if you need to render a
custom header (icons, multi-line, status). Otherwise pass `title`.

**Don't:** Nest a Modal inside a Modal for confirm-delete (ProjectSettingsView
does this today; works but odd UX). Use a Message inline + Button
variant=danger instead.

### 3.2 Button

`ui/Button.svelte`. Page-level and modal action button.

```ts
interface Props {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'; // default 'secondary'
  size?: 'sm' | 'md';                                      // default 'md'
  type?: 'button' | 'submit';                              // default 'button'
  disabled?: boolean;
  loading?: boolean;          // shows spinner, disables click
  ariaLabel?: string;
  title?: string;
  onclick?: (e: MouseEvent) => void;
  children: Snippet;          // text + optional icon Snippet
}
```

**Sizes:** md = 32px h / 13px h-padding / 12.5px font; sm = 28px h /
10px h-padding / 11.5px font.

**Variants:**
- `primary` — locked-on violet tint (`--violet-bg` + line + text). Most
  prominent action on a page or modal foot.
- `secondary` — neutral default (`--bg-secondary` + `--border` +
  `--text-secondary`). Hovers to violet tint.
- `danger` — red tint. Destructive or override-with-caution.
- `ghost` — transparent, low-attention. Hovers to `--bg-tertiary`.

**Loading state** shows a 12px circular spinner inside the button and
sets cursor:wait.

**Don't:** Roll your own `.btn`/`.btn.sm`/`.btn.danger-sm` (SettingsSystem
still does — flagged in §8.3). Don't add gradient fills or saturated
backgrounds; the system reserves heavy fill for state indicators.

### 3.3 IconButton

`ui/IconButton.svelte`. Icon-only button. **`ariaLabel` is required.**

```ts
interface Props {
  ariaLabel: string;          // required for a11y
  variant?: 'default' | 'ghost' | 'danger' | 'shell';
  size?: 'sm' | 'md' | 'lg'; // 24×24 / 32×32 / 36×36 (44×44 on mobile)
  disabled?: boolean;
  title?: string;
  type?: 'button' | 'submit';
  class?: string;             // forwarded to button (e.g. mobile-hidden)
  style?: string;             // forwarded to button (e.g. dynamic color)
  onclick?: (e: MouseEvent) => void;
  children: Snippet;          // SVG icon — pre-sized to size hint
}
```

**Sizes:**
- `sm` — 24×24.
- `md` — 32×32 (default). Use for close X in modals, drawers, settings.
- `lg` — 36×36 desktop / 44×44 mobile (Apple HIG minimum touch target).
  Use for all **header chrome** controls: ChatHeader toggle/new-chat/
  Working Memory/Timeline; AppShellBar toggle/new-chat.

**Variants:**
- `default` — text-tertiary, hovers to `--bg-hover` + text-primary.
- `ghost` — text-tertiary, hovers color only (no bg).
- `danger` — text-tertiary, hovers to `--red-bg` + red text.
- `shell` — **Frosted glass pill for header chrome.** `border-radius: 50%`,
  `--bg-tertiary @ 60%` fill + full `--border` + `--glass-rim` shadow.
  Sits on a glass backdrop and reads as a control, not a floating SVG.
  Always pair with `size="lg"` in header surfaces.

The icon child must be pre-sized (e.g., 18×18 SVG inside a 36×36 `lg`
button); IconButton doesn't pad.

**Don't:** Build a custom `.icon-btn` class. All five legacy hand-rolled
`.icon-btn` blocks have been migrated to this primitive (ChatHeader,
AppShellBar, TimelinePanel, WorkingMemoryPanel, SettingsView). Don't omit
`ariaLabel`.

### 3.4 Field

`ui/Field.svelte`. Label + slotted control + optional hint / error /
trailing.

```ts
interface Props {
  label: string;
  hint?: string;              // muted helper line below
  error?: string;             // red text below (suppresses hint)
  required?: boolean;         // shows asterisk
  layout?: 'row' | 'stacked'; // default 'row'; auto-collapses on mobile
  trailing?: Snippet;         // right-of-control inline annotation
  children: Snippet;          // the control (input/select/textarea)
}
```

**Desktop layout (row):** 140px label col / 1fr control col / auto
trailing col. Mobile (≤720px) collapses to single column.

**Native control styling** (provided via `:global()` selectors): inputs/
selects/textareas get `--bg-tertiary` bg, `--border` border, `--radius-xs`
(6px), 32px height, 12.5px font. Focus ring is 3px `--violet-bg`
box-shadow.

Native chrome stripped from selects (custom chevron data-URI inside
`Field`) and number inputs (spinners hidden). If a real number-stepper UI
is ever needed, build a `NumberInput` primitive — don't unhide native.

**Don't:** Style inputs outside Field — the `:global()` rules in Field
own that styling. If a use-case needs a non-Field input, document why and
match the visual contract.

### 3.5 StatePill

`ui/StatePill.svelte`. Small uppercase pill that visualizes a state.

```ts
type State = 'loaded' | 'loading' | 'idle' | 'error' | 'available'
           | 'default' | 'active' | 'success' | 'warning';

interface Props {
  state: State;
  label?: string;             // override the auto-humanized label
  size?: 'sm' | 'md';         // 9px / 10px font
}
```

**State → color mapping:**
- `loaded`, `active` → violet
- `loading`, `warning` → amber
- `error` → red
- `available` → sky
- `success` → green
- `idle`, `default` → neutral (`--bg-tertiary` + `--text-tertiary`)

**Don't:** Build a per-component badge with hardcoded `rgba(...)` (Timeline
does this for active badges — flagged in §8.2). Use StatePill, or — when
you genuinely need a smaller / non-state pill — propose adding `Badge`
(see §4.5).

### 3.6 Section

`ui/Section.svelte`. Small-caps header + slotted body. The "card-with-
label" pattern.

```ts
interface Props {
  title: string;
  action?: Snippet;           // right-aligned (e.g., "Edit", a button)
  children: Snippet;
}
```

Title styling: 9.5px / 700 / 0.12em / uppercase / `--text-tertiary`.
Body uses `display: contents` (no wrapper div).

Use this for a logical grouping inside a modal body or panel. **Not** for
sidebar section caps (PROJECTS, CONVERSATIONS) — those have a slightly
different aesthetic; see §5.2.

### 3.7 Message

`ui/Message.svelte`. Inline status banner — pairs with form actions and
modal bodies.

```ts
interface Props {
  variant: 'ok' | 'err' | 'info';
  children: Snippet;
}
```

8px v / 12px h padding, `--radius-xs`, 12.5px font, 12px bottom margin.
`role="alert"` for err, `role="status"` for ok/info. Colors map to
red/green/violet `*-bg` + `*-line`/border + `*-text`.

---

### 3.8 Toggle

For Settings feature flags and other single boolean controls.
`ui/Toggle.svelte`.

```ts
interface Props {
  checked: boolean;
  disabled?: boolean;
  size?: 'sm' | 'md';
  ariaLabel: string;
  onchange: (next: boolean) => void;
}
```

---

## 4. Pending primitives

Patterns currently rolled-own across the codebase. Each entry below is a
contract a future primitive should satisfy. **Do not add new local variants**
for these jobs. Either add the shared primitive and demonstrate it in
`/_design`, or keep the touched surface on the smallest temporary native
pattern and record why the primitive was not part of that slice.

### 4.1 SegmentedControl

For three-way choices that should read as one control, not three buttons.
Used today inline in `InputBar.svelte` (effort levels: Auto / Off / Low /
Med / High / Max).

```ts
interface Props<T extends string> {
  value: T;
  options: Array<{ value: T; label: string; icon?: Snippet }>;
  size?: 'sm' | 'md';
  fullWidth?: boolean;        // mobile use
  ariaLabel: string;
  onchange: (next: T) => void;
}
```

### 4.2 Disclosure

Replaces four hand-rolled chevron-rotation impls (AssistantMessage,
LiveToolActivity, InputBar action sheet, ProjectList). Drives advanced-
section accordions (ProjectSettingsView) and tool/reasoning expansion.

```ts
interface Props {
  open: boolean;
  label?: string;             // when omitted, header is fully slotted
  size?: 'sm' | 'md';
  onToggle: (next: boolean) => void;
  header?: Snippet;
  children: Snippet;          // body
}
```

Internals: shared 0.15s chevron rotation + fadeIn animation. Single
source of truth for the expand/collapse motion.

### 4.3 EmptyState

Used by both drawer empties (Timeline, WorkingMemory) and list empties
(ProjectList "No projects yet").

```ts
interface Props {
  icon?: Snippet;
  title: string;
  description?: string;
  action?: Snippet;
  size?: 'sm' | 'md';
}
```

Locks padding (32 16 sm / 40 20 md), icon opacity 0.5, text scale.

### 4.4 PanelHeader

The shared "icon + h2 + close X" header used by Timeline, WorkingMemory,
SettingsView, and (somewhat) Modal. Currently three different impls.

```ts
interface Props {
  icon?: Snippet;
  title: string;
  trailing?: Snippet;         // action(s) / IconButton(s)
  onClose?: () => void;       // shows close X if provided
}
```

Locks h2 size, padding 0 16, height `var(--header-height)`,
border-bottom.

### 4.5 Badge

Smaller / less-formal than StatePill — a colored pill that semantically
labels (e.g., "local", "tailscale", "active") rather than visualizing a
state. Today: `entry-badge` (Timeline, hardcoded rgba), `ep-kind` (v7),
`badge` (Settings, color-mix).

```ts
interface Props {
  tone: 'violet' | 'teal' | 'sky' | 'amber' | 'green' | 'red' | 'neutral';
  size?: 'xs' | 'sm';
  label: string;
  icon?: Snippet;
}
```

May be merged with StatePill later if the prop space converges. For now,
spec keeps them distinct: StatePill = state machine; Badge = label.

### 4.6 Tabs

Used by SettingsView (vertical, left-rail nav) and v7 ProviderSettings-
Modal (horizontal, top tabs). Two orientations.

```ts
interface Props<T extends string> {
  value: T;
  tabs: Array<{ value: T; label: string; icon?: Snippet; count?: number }>;
  orientation?: 'horizontal' | 'vertical';
  onchange: (next: T) => void;
}
```

Vertical variant should support a glass background (Settings nav is solid
today — see §8.3).

---

## 5. Surface composition rules

### 5.1 App shell — Tauri vibrancy contract

The app's outer shell coordinates four moving parts:

1. **Tauri window** is `transparent: true` with `windowEffects: { effects:
   ["sidebar"] }`. macOS NSVisualEffectView paints behind the window.
2. **`body`** is `background: transparent` so vibrancy reaches surfaces.
3. **Sidebar** is `--glass-thick` + `--glass-blur` + `--glass-rim` →
   wallpaper bleed-through.
4. **Chat panel** is solid `--bg-primary` → blocks vibrancy in the content
   area (chat must stay readable over any wallpaper).

**On mobile / PWA:** body pins to `--bg-primary` (no vibrancy behind);
sidebar still uses thick glass but blurs `--bg-secondary` rather than the
desktop. Mobile glass headers add a 3-stop gradient (§2.13).

**`AppShellBar`** is a slim chrome strip used on non-chat views (Settings,
Project) — same drag region + traffic-light inset as ChatHeader, but
hosting persistent shell controls (sidebar toggle, new-chat) so users can
navigate sessions from anywhere.

### 5.2 Sidebars

Material: `--glass-thick`. **Must include `box-shadow: var(--glass-rim)`.**

Section caps inside the sidebar (PROJECTS, PINNED, CONVERSATIONS) are
unified at 10.5px / weight 700 / 0.1em letter-spacing /
`color-mix(in srgb, var(--violet) 55%, var(--text-tertiary))`. PROJECTS
hover goes full violet. Use these directly; they do *not* compose into
the `Section` primitive (different aesthetic — see §3.6 note).

### 5.3 Headers

`ChatHeader` and `AppShellBar` use `--glass-thin` + `--glass-blur` +
`--glass-rim`. Height = `var(--header-height)` (60 desktop / 64 mobile; 44 Tauri shell — set via `[data-shell="tauri"]`).

**Mobile:** rim suppressed, replaced by 3-stop gradient (§2.13).

**Tauri shell:** `:global(html[data-shell="tauri"]) .header.shell-inset`
adds `padding-left: 88px` when sidebar collapsed (clears traffic lights).

**Drag region:** the header container should carry `data-tauri-drag-region`
so users can drag the window from any non-control space. Children that
must NOT initiate drag (model selector, IconButtons) need
`-webkit-app-region: no-drag` (or `app-region: no-drag` standard).

### 5.4 Drawers (right side: Timeline, WorkingMemory)

Both drawers share a layout contract:

- 440px wide desktop, 100vw mobile.
- Header height matches `--header-height` with `padding: 0 16px` + mobile
  `safe-area-inset-top` reservation. Drawer-top edge lines up with main
  header's bottom — no double-stacked-chrome feel.
- Material: `--glass-regular` + `--glass-blur` + `--glass-rim`.
- Backdrop: 25% scrim (compare to Modal's 28%; see §8.1 — pick one).
- Body padding: **16px** (`var(--space-4)`). WorkingMemory currently
  delegates to children (audit §8.2); fix in Phase B.
- Empty state: padding `32px 16px`, icon opacity `0.5`. WorkingMemory
  currently `48px 24px` / `0.4` (audit §8.2); fix in Phase B.

### 5.5 Modals

Material: `--glass-regular-thick` + `--glass-blur` + `--glass-rim`.
Backdrop: 28% black scrim — light dim only, not heavy. Sizes 480/640/800.

**Focus:** Escape closes (default), backdrop click closes (default),
inner click does NOT propagate. `aria-modal="true"`. Modals trap focus
implicitly via the role + structure.

**Foot pattern** for action modals:
```svelte
{#snippet foot()}
  <Button variant="ghost" onclick={...}>Cancel</Button>
  <span style="flex: 1"></span>
  <Button variant="primary" onclick={...}>Save</Button>
{/snippet}
```
Optional `<Button variant="secondary">Test</Button>` between Cancel and
Save when the action benefits from a try-before-commit.

### 5.6 Floating chips (input chip)

`InputBar.svelte`'s outer `.input-floater` is `position: absolute` with
`pointer-events: none` — taps pass through the gap to chat content. The
inner `.input-chip` has `pointer-events: auto` and uses `--glass-thin` +
`--glass-blur` + `--glass-rim` + 24px border-radius + subtle drop shadow.

Two-row layout inside the chip: textarea full-width on top, action row
below (plus / inline effort label / spacer / send). Auto-expanding
textarea height drives a `--input-bar-height` CSS variable via
`ResizeObserver`; FAB and MessageList padding use this var as their
single source of truth.

**Do not** nest a glass surface inside the chip when you can avoid it.
The action sheet that currently expands above the chip uses `--glass-thin`
on a `--glass-thin` floater — works but is borderline glass-on-glass.
Consider solid-elevated for nested layers (§7).

### 5.7 Overlays

| Surface | Current | Recommended | Notes |
|---|---|---|---|
| Drag-drop overlay (chat) | `--glass-scrim` + `--glass-blur-soft` | close-to-content frost |
| Cmd palette | `--glass-scrim` + `--glass-blur-soft` | close-to-content frost |
| Lightbox | `--glass-blur-soft` | close-to-content frost |

Use `--glass-blur-soft` for close-to-content overlays where the full 40px
blur is too aggressive.

---

## 6. Mobile

### 6.1 Touch targets
- Minimum 44×44 on every interactive control.
- 14px gap between adjacent IconButtons (was 6/8 → mis-tap mitigation).
- InputBar send button: 40×40 desktop, 44×44 mobile. Plus button: 32/44.
- ChatHeader/AppShellBar shell buttons: **36×36 desktop, 44×44 mobile**
  via `<IconButton variant="shell" size="lg">`. ✅ Now compliant.

### 6.2 Safe-area insets
Use `--safe-bottom` and `--safe-top`. Examples:
- ChatHeader `height: calc(var(--header-height) + env(safe-area-inset-top))`.
- InputBar bottom padding includes `var(--safe-bottom)`.

### 6.3 Mobile glass gradient
Replaces inset rim on mobile glass headers. See §2.13 for the recipe.

### 6.4 Swipe gestures
Sidebar session rows: rubber-band 0.35×, dominance 1.4×, haptic blip on
threshold cross + commit. Action zones translate3d outside the wrapper
at rest (clipped by `overflow: hidden`) so the row stays glass-trans.

### 6.5 iOS sheet curve
Mobile drawers use `cubic-bezier(0.32, 0.72, 0, 1)` at 0.32s (was Material
`(0.4, 0, 0.2, 1)` — felt wrong on iOS).

---

## 7. Anti-patterns

The following are *don'ts*. Violations should be caught in review.

1. **Glass over content.** Message bubbles, code blocks, project cards,
   timeline rows must stay solid.
2. **Glass-in-glass nesting.** A glass dropdown inside a glass modal, a
   glass action sheet inside a glass input chip — at minimum re-evaluate;
   prefer solid-elevated for nested layers.
3. **Hardcoded blur radii.** No `backdrop-filter: blur(2px)` literals.
   Use `--glass-blur` or `--glass-blur-soft`.
4. **Custom buttons / fields / pills.** Use the primitives. If a needed
   variant doesn't exist, propose adding it.
5. **Hand-rolled chevron / disclosure animations.** Disclosure is a pending
   primitive (§4.2). Do not add another local disclosure variant; add the
   shared primitive or use the smallest temporary native `<details>` pattern
   with visible focus treatment.
6. **Mixed gap/margin rhythm.** Don't pair `padding: 12px` with
   `margin-bottom: 16px` on the same axis — pick a rhythm and stick.
7. **Reaching for `--purple` or `--blue`.** Use `--violet` / `--sky`.
8. **Era prefixes (`v7-`, `v054-`).** Retired. Grep should stay empty.
9. **`var(--bg, #fff)` color fallbacks.** Forbidden by FRONTEND.md's CSS
   Variable Convention rule. Numeric fallbacks (`var(--radius-xs, 4px)`)
   are fine.
10. **Defining tokens inside components.** Tokens belong in `app-tokens.css`.
11. **Nesting Modal inside Modal** for confirm-deletes. Use inline
    `<Message>` + danger button instead.
12. **Skipping the rim** on a glass surface.
13. **Inlining new easing curves and durations.** New transitions pick a
    curve from §2.7.1 and a duration from §2.7.2, or propose adding to
    the table. No one-off `cubic-bezier(...)` literals.
14. **Mid-band durations.** Don't pick 270 or 420ms because they "feel
    right". Pick the duration class that matches the surface's role.
15. **Inventing a new transition pattern when one fits.** Re-read §2.7.3
    before adding a slide / fade / scale / rotate. Match an existing
    pattern; keep muscle memory consistent.
16. **Page-level animation frameworks.** Flat view switches are
    intentional. Don't bring in a global page-transition library or wrap
    `+page.svelte` in animated route transitions.
17. **Animations that ignore `prefers-reduced-motion`.** Every keyframe /
    `transition:` should reduce gracefully. See §2.7.5.

---

## 8. Current Cleanup Backlog

This backlog is current as of 2026-07-01. It is not the source of truth for
the CTX/ownership cutover; it is the design-system hygiene list that prevents
future UI work from rediscovering the same standards gaps.

### 8.1 Already Landed In The Native-Feel Baseline

- `--space-1..8`, `--ease-*`, `--dur-*`, `--press-*`, `--glass-scrim`,
  `--glass-blur-soft`, and `--glass-elevation-{1,2,3}` are live tokens.
- Global tap behavior is live: transparent tap highlight, touch-action
  manipulation, no callout/user-select on controls, and the iOS `touchstart`
  enabler for `:active`.
- Reduced motion, reduced transparency, and increased contrast are handled in
  `app.css`.
- Sidebar, headers, modals, command palette, lightbox, and chat drop overlays
  use the current glass token family.
- `Toggle` is a live primitive.

### 8.2 Still Open

- **Disclosure primitive.** Repeated expand/collapse rows still exist. New work
  must not add another local chevron implementation; add the primitive or keep
  a minimal temporary native `<details>` pattern with focus-visible treatment.
- **SegmentedControl, EmptyState, PanelHeader, Badge, Tabs.** These remain
  pending primitives. Add them only when a real surface needs them, then show
  every variant in `/_design`.
- **Legacy globals.** Global status badges, dialog classes, and settings-card
  classes still predate the primitive system. Migrate only when touching those
  surfaces for product work.
- **Settings density and primitive adoption.** Some Settings surfaces still use
  older local buttons, cards, and dense padding. Bring touched areas onto
  primitives and `--space-*` tokens.
- **Model/provider selector UX.** The selector still needs a product pass for
  truthful provider/model state, token window, readiness, cost/speed where
  available, and mobile/desktop parity.
- **Message/tool/reasoning disclosure.** Tool and reasoning expansion needs one
  shared disclosure treatment and a calmer hierarchy.

### 8.3 CTX Sheet-Specific Gate

The CTX composer sheet is a high-risk transparency surface. It must follow the
same standard:

- categories collapsed by default;
- one clean line per file or dynamic context row;
- no preview bodies, repeated prefixes, raw ids, raw continuity modes, or
  internal provider/preflight rows in the primary sheet;
- rows grouped by user-recognizable source category;
- row actions are either the whole row or an existing/shared primitive, not a
  custom inline button;
- all spacing uses `--space-*`;
- keyboard focus is visible on summaries and row actions;
- delivery/provenance proof belongs in the message/event inspector, not the
  next-turn preview sheet.

---

## 9. References

- **Visual ground truth:** `frontend/src/routes/_design/+page.svelte`
  (live at `/_design`).
- **Token source of truth:** `frontend/src/app-tokens.css` imported by
  `frontend/src/app.css`.
- **Primitive source:** `frontend/src/lib/components/ui/`.
- **Standards gates:** root `PRODUCT-STANDARDS.md`,
  `PRD-UX-IA-STANDARDS.md`, `PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md`,
  and `UX-REVIEW-GATE-TEMPLATE.md`.
- **Native-feel implementation baseline:** commits `9d1795d4` and
  `8a7a7d4e`.
- **Execution history and design rationale:** git history, and the workspace
  under `shared/`. Current UI requirements come from the approved PRD/AC and the
  standards gates above.

### Apple Liquid Glass external sources

- HIG Materials — https://developer.apple.com/design/human-interface-guidelines/materials
- Liquid Glass overview — https://developer.apple.com/documentation/TechnologyOverviews/liquid-glass
- WWDC25: Meet Liquid Glass — https://developer.apple.com/videos/play/wwdc2025/219/
- WWDC25: Get to know the new design system — https://developer.apple.com/videos/play/wwdc2025/356/
- Practical guidance + accessibility — https://designedforhumans.tech/blog/liquid-glass-smart-or-bad-for-accessibility
- SwiftUI Liquid Glass overview — https://medium.com/@madebyluddy/overview-37b3685227aa

---

## 10. Design Review Rule

Before any user-visible UI change is called ready, review the composed surface
against:

1. the live `/_design` page and UI primitive exports;
2. `PRODUCT-STANDARDS.md`, `PRD-UX-IA-STANDARDS.md`, and
   `PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md`;
3. this design system's token/material/motion/touch rules;
4. a browser proof of the actual shell, not only an isolated component;
5. the user-perspective verdict: less cognitive burden, less noise, no raw
   internals, no duplicate concepts, and no hidden loss of capability.

---

*End of spec. Live at `/_design`. Tweaks to any primitive ripple to every
consumer — that's the point.*
