# UX / UI / IA Standards

**Status:** current · **Owner:** CORSIA maintainers

**What this is:** CORSIA's durable **design contract** — what every UI change is reviewed against (the UX/IA design-gate lens). Workspace config: shipped default, per-project overridable. The concrete tokens, primitives, glass materials, and the `/_design` visual contract live in `frontend/DESIGN-SYSTEM.md` — that's the *how*; **this doc is the law it implements.**

**The bar: top-1% product craft.** A change ships only if it would survive a senior product designer's review. The test for every rule below: does it make the product feel **cohesive, intentional, and effortless** — or is it clutter?

---

## 1 · One system, no forks
- **UX1.** There is **one design system**. Reuse a primitive before building anything; **never fork a component or re-invent an existing pattern.** A second thing doing an existing job is a defect, not a variant.
- **UX2.** **One pattern per job** — the same action looks and behaves identically everywhere (a destructive action, a confirm, a row, a sheet). Inconsistency is the most expensive UX bug.
- **UX3.** All design values are **tokens** (color · space · radius · blur · motion · type). Never hardcode a value; a value that isn't a token is a gap in the system, fixed *in the system*.
- **UX4.** A new primitive/variant is **proposed into the system** and lands in `/_design` (the visual contract) — never built one-off inside a feature.

## 2 · Information architecture
- **UX5.** Organize by the **user's mental model**, not the system's structure — name and group by what the user controls and recognizes.
- **UX6.** **One primary action per surface.** Explicit hierarchy: one clear primary, secondaries subordinate, the rest out of the way.
- **UX7.** **Progressive disclosure** — show what's needed now, reveal depth on demand. Density is earned, never the default.
- **UX8.** Every surface answers at a glance: *where am I · what matters here · what can I do next.* If it can't, the IA is wrong.
- **UX29.** **Maximize intent signal with minimal effort.** A user should not copy, scroll, transfer, re-explain, or hunt to make their intent legible. Put actions where the user already is, layer access by frequency, keep scope-owned things manageable inside that scope, and show source scope when a surface composes multiple scopes.

## 3 · Responsive & platform-native
- **UX9.** **Always responsive, and specifically optimized for both desktop and mobile** — not one scaled to fit the other. IA **may diverge** to optimize each (desktop sidebar ↔ mobile drawer); **capability coverage must not** — every capability is reachable on both.
- **UX10.** Comply with **platform conventions** (Apple HIG as the baseline) — native type, touch, gesture, and safe-area behavior, not our own reinvention. (The numeric specifics live in the design system.)

## 4 · Hierarchy & restraint (anti-clutter)
- **UX11.** **Every element must earn its place** — communicate real value or be cut. If removing it loses nothing, remove it.
- **UX12.** **Spend boldness in one place** — one signature moment per surface carries the personality; everything around it stays quiet and disciplined.
- **UX13.** **Consistent spacing rhythm** from the space scale — no ad-hoc padding. Whitespace is a tool; clutter is unedited UI.
- **UX14.** **Surface by priority** — the most important thing is the most prominent; never give equal weight to unequal things, never make the user hunt.

## 5 · Material & motion (the native feel)
- **UX15.** **Glass is for chrome, not content** — controls/navigation recede when the user reads or creates; content sits on solid surfaces.
- **UX16.** Motion is **purposeful, tokenized, and shared** — transitions are reusable classes/tokens (enter/exit · sheet · drawer), never per-component one-offs. Motion communicates change (where something came from/went), never decorates.
- **UX17.** Motion respects **reduced-motion** and never blocks interaction.

## 6 · State & real-time
- **UX18.** **Every visible state renders in real-time and stays consistent across devices/tabs** — anything on screen reflects current truth with no manual refresh.
- **UX28.** **What a surface shows is what's actually true.** Every displayed value — a label, badge, status, count, or the driver/model a thread is *actually* running on — reflects the **resolved runtime state**, never a stale, default, or guessed value that contradicts it. A surface that can't resolve the true value says so; it never renders a confident wrong answer. (The surface-level twin of Product **PS7**.) `[invariant]`
- **UX19.** **Every state is designed:** loading · empty · error · stale · offline · unauthorized. No state is a blank or an afterthought.
- **UX20.** See [COPY-STANDARD.md](COPY-STANDARD.md) C9.
- **UX30.** **Attention severity is visible in the right place.** Blocking, loud-but-ignorable, option-presenting, and passive-digest states have distinct treatment. A state requiring attention appears in the accountable party's normal work surface, not only inside a long chat stream, buried viewer, or hidden drawer.
- **UX31.** **A design reaches the owner complete and reviewed.** Organize it by stage (who acts, when, and what each stage hands to the next), and give each stage only its own job. Make every state of every element a row: today's behavior cited `file:line` on a named ref, the proposed change marked as proposed, with its final copy. Walk the main journeys across the stages, and list each unverified premise as *Not verified*, with what changes if it's wrong. Before it's sent, a fresh-context reviewer walks every journey as the person and returns no Block or Flag finding, and each round's findings map to their fixes. Bring one recommended design, never an option menu. Not a fix the owner asked for, a defect that restores approved behavior, or copy/spacing inside approved behavior.

## 7 · The UI explains itself
- **UX21.** **No inline instructions to the user.** If a surface needs a sentence telling the user how to use it, the UX has failed — fix the design, don't caption it. (Pet-peeve-level: this is a root-cause signal, not a copy task.)
- **UX22.** See [COPY-STANDARD.md](COPY-STANDARD.md) C4 and C12.
- **UX23.** See [COPY-STANDARD.md](COPY-STANDARD.md) C2, C5 and C15.

## 8 · Theming & accessibility floor
- **UX24.** **Accent color is project-scoped** — a project's identity colors its surfaces consistently; light/dark parity required.
- **UX25.** Accessibility is a floor: visible keyboard focus · sufficient contrast · screen-reader labels on every control.

## 9 · When to use X vs Y (the recurring forks)
- **UX26.** **Chat is for exploratory/ambiguous intent only** — never the default for content feedback, change approval, or status. Content → canvas + anchored comments · a concrete change → diff + approve · status → ambient/digest.
- **UX27.** **Modal/blocking only for a true forcing function** (high-stakes, irreversible). Everything else is inline / async / pull — don't interrupt for what can wait.

---

## Reviewer Contract
- **Block:** a forked component / re-invented pattern · a hardcoded design value · glass-on-content · a missing designed state (error/empty/loading) on a touched surface · an inline-instruction band-aid over a UX gap · a capability reachable on one platform but not the other · a surface displaying a value that contradicts runtime truth — a wrong driver/model/status label (UX28) · an attention-required state that is buried or only reachable from the wrong surface (UX30) · a design or PRD sent to the owner without stage organization, cited state coverage, a *Not verified* list, or a clean fresh review, or as an option menu rather than one recommendation (UX31).
- **Flag:** weak hierarchy / clutter (elements that don't earn their place) · ad-hoc spacing · decorative or non-reduced-motion-safe motion · copy findings follow [COPY-STANDARD.md](COPY-STANDARD.md) §Severity · unverified-in-browser · a journey that forces copy/scroll/re-explain/manual transfer loops (UX29) · common configuration cluttering the primary path or frequent power-user configuration hidden too deeply (UX7/UX29).
- **Nit:** micro-spacing; copy findings follow [COPY-STANDARD.md](COPY-STANDARD.md) §Severity.

> The *how* lives in `frontend/DESIGN-SYSTEM.md` (tokens · primitives · glass · `/_design`). This standard is the *what/why* those implement. 31 obligations, 9 themes — internalize the themes; the rules are their teeth.
