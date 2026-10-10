# Product Standards

**What this is:** CORSIA's durable **product contract** — what every PRD is reviewed against *before* it reaches the maintainer (the Product gate lens). Workspace config: shipped default, per-project overridable. **The cut:** Product states the *promise* — what the product does and the values it holds; **UX/IA executes that promise at the surface, Eng executes it in the build.** A feature passes all three.

**The bar: the product should feel like magic.** Not "polished," not "intuitive" — *magic*: it does the thing the user didn't believe software could do, and makes that the new floor. The ritual they were trained to *always remember* becomes the thing they *never have to.* The effort they'd resigned themselves to is simply gone. It does what they *meant*, not just what they said.

At its limit it stops being software — stops being a tool. It is the moment the distance between what a person *means* and what *comes to be* falls to nothing. The grind of execution lifts away, and what's left is the part that was always theirs alone: to imagine, to judge, to decide what is worth bringing into the world. One person carries the reach of an institution and spends it on what matters, never on toil.

It carries what they can't, never drops what matters, and gives them their time, their attention, and themselves back — to become the size of their ambition, not the size of their day.

And it never asks them to hand over what's theirs to get it: it runs on their own hardware, on their own data, and leaves both in their hands — the magic never costs them the very thing it exists to give back.

Every rule below exists to close that distance. Anything that doesn't is feature-theater.

---

## 1 · Solve the job, not the feature
- **PS1.** Start from the user's **real job-to-be-done**, not the feature they asked for. The feature is a means; serve the goal — even when that means *not* building the ask.
- **PS2.** **Every feature earns its place.** A feature's true cost is the complexity it adds forever. The best feature is often none. If it doesn't move the user's job, cut it.
- **PS26.** **Justified by evidence, not opinion.** Every new feature is backed by **product data** (a measured need or usage signal), **current research** (on the need *or* the efficacy of the approach), **or a logically-sound theory with adjacent supporting evidence** — on the record before it ships. A reasoned theory with adjacent support counts; bare opinion does not. The thorough evidence pass is the `product-thinking` skill (`skills/product-thinking/SKILL.md`), which the Product gate runs and whose recorded chain is the evidence. `[invariant]`
- **PS3.** **Opinionated where it matters, flexible where it doesn't** — strong defaults over infinite knobs; don't outsource the product's judgment to a settings page. Hold a strong product belief **only when it's irrefutably in the user's interest.**
- **PS23.** **Best-in-class, or steer to who is.** Everything the product does is best-in-class at what it does — or it's compatible with what *is* and steers the user to it, never to an inferior option. No exceptions. `[invariant]`

## 2 · The human decides; the product does the work
- **PS4.** **Interrupt only when necessary.** Surface to the human on genuine ambiguity, a high-stakes/irreversible call, or a value judgment only they can make — never for what the product can determine itself. Minimize required input — the product does the determinable work itself. *(This is in-flight toil, not the configuration a user **chooses** to own — that's PS14.)*
- **PS5.** **Do the work; don't make the user do it.** The product exists to remove toil — chasing, polling, re-explaining context, manual glue. If the human is hand-carrying state between steps, the product failed.
- **PS6.** **Every interruption carries everything needed to decide** — the question, the options, the tradeoff, the recommendation. A decision request the user can't act on cold is a defect.
- **PS25.** **An autonomous action never silently strands.** Anything the product does on its own initiative reports its own completion, stall, or failure to whatever's responsible — it never lands in a state the user must *discover* and rescue. (Unseen, it can't be caught; watching for it is itself burden — PS24.) `[invariant]`
- **PS28.** **The common path has logical defaults; consequential exceptions require informed intent.** If the product can infer the low-risk 95% path, it does so without making the user or agent remember glue such as scope, parent, or owner. If an action is high-stakes, destructive, or affects another party's ownership/attention/work, it must be explicit, reversible where possible, and fully explained before accept. `[invariant]`
- **PS29.** **Attention-worthy states reach the accountable party at the right severity.** A blocker wakes; a recoverable warning is loud but ignorable; a real choice presents options; context-only activity becomes passive digest. Nothing requiring action is buried in chat history, a hidden pane, or a surface the responsible party does not know to open. `[invariant]`

## 3 · The product is honest
- **PS7.** **Never lie about state.** No fake progress, no "done" that isn't, no spinner over a stalled task. **A long-running operation communicates real progress** — what's happening and, where possible, how long left — never an indefinite spinner. What the user sees is the truth.
- **PS8.** **Calibrated, not confident.** Surface uncertainty so the user's trust can track it. Overclaiming is a trust-debt that always comes due.
- **PS9.** **No dark patterns, ever.** Nothing engineered to extract a click, a consent, or attention against the user's interest. The product is on the user's side, full stop.

## 4 · Fail loud, fail forward
- **PS10.** **When something can't be done, say so — loudly and honestly.** Never silently degrade quality, never pretend it worked. (Fail-loud is a promise to the *user*, not just an engineering rule.)
- **PS11.** **No dead ends.** Every state — empty, error, blocked, offline — offers a way forward. The user is never stuck without a next step.
- **PS12.** **Forgiving by default** — actions are recoverable, mistakes undoable; destructive moves are deliberate, never accidental.

## 5 · The user owns it
- **PS13.** **The user owns their data** — private by default, exportable, no lock-in; nothing leaves their control without explicit consent.
- **PS14.** **The user owns their setup** — anything swappable (model, provider, process, context) is their choice. Sensible defaults, never a trap.
- **PS31.** **The full product runs locally, free, with every core capability.** Paid surfaces sell convenience, reliability, or operated scale — never access to core function, and never by introducing *intentional friction* into the free local path. The free path is the real product, not a crippled teaser; a paywall placed to make self-hosting deliberately worse is a dark pattern (PS9). `[invariant]`

## 6 · Work WITH the user's world
- **PS15.** **Integrate, don't replace.** Meet the user in the tools they already use; augment their workflow, never force a walled garden. Heavy integration *is* the product, not scope creep.
- **PS16.** **Single-player-valuable first.** A feature must deliver value to one user alone before it leans on an ecosystem, a crowd, or a network effect. The day value depends on others showing up, we've inverted the product.
- **PS22.** **Runs on the floor.** Every core capability works on the **minimum supported target — the local model and minimum hardware**; a bigger model or the cloud is optional upside, never required for core function.
- **PS30.** **Out-of-box means real use, not developer setup.** Launch-core capability must work for a non-technical user without cloning repos, running servers by hand, choosing models before they understand the system, or wrangling dependencies. Advanced control can exist, but it cannot be the first-use tax. `[invariant]`
- **PS32.** **Forward-compatible with what doesn't exist yet.** The product anticipates unknown future tools, models, and integrations: new ones should be adoptable through config, catalog, or extension contract — *not* a shipped app update — wherever the shape allows it. A capability that forces a release to recognize each new tool/model has hardcoded what should have been data. `[invariant]`

## 7 · Predictable & coherent
- **PS17.** **A concept means one thing, everywhere.** The same idea behaves the same across every surface; no two names for one thing, no one name for two. Predictability *is* trust.
- **PS18.** **The product feels instant.** The user sees the effect of an action immediately, and sees current truth without asking. Latency and staleness erode trust faster than missing features.
- **PS27.** **Predictable, always.** The same situation produces the same response; the product never drops the ball because it missed a detail. Consistency is the bedrock of trust — a single violation erodes it faster than any missing feature. `[invariant]`

## 8 · Reduce the work; compound the value
- **PS19.** **Every feature lowers the user's effort or delivers clear, material value — or it doesn't ship.** No feature earns a place on "nice to have"; it measurably reduces friction or raises the outcome.
- **PS20.** **Reduce the human engagement a job requires — never capture attention.** The north star, opposite of attention-capture: usage may rise, but value *per unit of the user's engagement* must rise with it. Every ship removes friction or cognitive burden — 1% compounds.
- **PS21.** **Value accrues with use** — context, history, and intent compound rather than reset. A feature that makes the user re-explain themselves is moving the wrong way.
- **PS24.** **Cognitive burden only ever goes down.** No solution may add a step, a decision, or a thing-to-remember — not even a small one. The only direction the user's cognitive load may move is *down*; a change that adds burden doesn't ship until that burden is removed or offset by a larger reduction elsewhere. Hard stop. `[invariant]`

---

## Reviewer Contract
- **Block:** a feature that serves no real job (PS1/PS2) · the product lying about state — fake progress / false "done" (PS7) · a dark pattern (PS9) · a silent quality degrade (PS10) · a dead-end state (PS11) · data leaving the user's control without consent (PS13) · value that depends on an ecosystem existing (PS16) · a feature with no material value and no friction reduction (PS19) · a core capability that can't run on the minimum target (PS22) · a change that adds cognitive burden (PS24) · an autonomous action that can strand needing user rescue, or finishes/stalls without reporting to its owner (PS25) · a feature shipped without product-data or current-research justification (PS26) · behavior that varies for the same situation, or an agent that drops the ball on a detail (PS27) · a consequential or multi-party action accepted without explicit informed intent (PS28) · a blocking attention state buried where the accountable party will miss it (PS29) · a launch-core capability that requires developer setup for ordinary use (PS30) · a core capability gated behind payment or intentional friction added to the free local path (PS31) · hardcoding into a release what a new tool/model/integration should be able to join through config or an extension contract (PS32).
- **Flag:** an avoidable interruption / required input the product could've determined (PS4/PS5) · overclaimed confidence (PS8) · an irreversible action without deliberate intent (PS12) · a concept that behaves differently across surfaces (PS17) · staleness the user must refresh away (PS18) · a feature that adds engagement/friction instead of removing it, or makes the user re-explain themselves (PS20/PS21) · reinventing something worse than an existing best-in-class option (PS23) · a common-path default that still requires remembered glue (PS28) · warning or digest severity that is ambiguous enough to create missed work or notification noise (PS29).
- **Nit:** copy tone, micro-flows.

> The cut: **Product = the promise · UX/IA = the surface that delivers it · Eng = the build that holds it up.** 32 obligations, 8 themes. This gate runs **first** — a PRD clears Product before it reaches the maintainer.
