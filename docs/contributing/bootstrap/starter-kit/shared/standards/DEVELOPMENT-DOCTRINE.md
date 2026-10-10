# Development doctrine: maintainability is the test

> CORSIA project doctrine, not one lane's preference. It applies to every change to the CORSIA repository, including contributions from other workspaces.

## The question to ask of every change

**Is this a root-cause fix, and is it the answer that leaves the codebase most maintainable?**

Not "does it work." Not "is it small." Not "did the tests pass." Those are necessary and nowhere near sufficient.

## Why this is the test

Two failure modes, and the second is the expensive one.

**1. Catching instead of curing.** A watcher, a detector, a retry control, a repair action, a health indicator — anything that notices a bad state and compensates after the fact. These are seductive because they are tractable: detection is easy to build and easy to demonstrate. But they do not remove the defect. They add a second thing that can break, they make the underlying problem feel less urgent, and they leave the original bug in the tree with a companion.

**2. Burden that compounds.** Every line is carried forward forever by every future reader, human or model. A "small" piece of unnecessary machinery is not small over its lifetime: it must be understood before any nearby change, kept consistent with everything it touches, and re-verified whenever its assumptions move. Ten small burdens do not cost ten units — they cost the cross-product of everyone who has to reason around them.

## Complexity is allowed. It has to pay for itself.

This is not a prohibition on machinery. Some problems genuinely require it.

The bar: **new complexity must buy down substantially more complexity elsewhere.** A mechanism that makes a whole class of bug unrepresentable earns its place. A mechanism that handles one more case does not.

If you cannot name what got simpler, it did not pay for itself.

## Shortcuts are worth less than they used to be

The classic argument for a shortcut is that the right fix costs more to write. When producing code is cheap, that argument mostly evaporates — but **the maintenance cost of the shortcut does not fall at all.** It is paid every time someone reads, extends, or debugs near it, forever.

So the trade has moved. Taking the shortcut now buys very little and costs the same as it always did. Do the right thing; it is no longer meaningfully more expensive to produce.

## What good looks like

In rough order of preference:

1. **Deletion.** The best fix removes code. If one fact is stored twice and they drift, delete the duplicate rather than syncing them.
2. **Making the bad state unrepresentable.** The defect cannot occur, so nothing needs to detect it.
3. **Using what already exists correctly.** A value already computed and discarded; a guard that exists but is unreachable; a path that is right on one harness and absent on the other.
4. **A new mechanism that collapses several existing ones.**
5. **A new mechanism that handles one more case.** Justify this explicitly or do not do it.

## Detection has one legitimate use

Diagnosis, while the underlying defect is being eliminated — and only with an explicit expectation that it stops firing and then gets deleted. It is never a deliverable on its own, and it is never the answer brought to a decision.

If a detector is still firing after its root causes are fixed, either the causes were not fixed or the detector was measuring something else. Both are worth knowing.

## Worked examples from this repository

Concrete cases the maintainers hit:

- A model-switch deadlock that made three worker threads permanently unusable turned out to be **one rule stored twice**. The correct version carried a comment explaining exactly why the broad version was wrong; a duplicate translated from a dead code path shadowed it. The fix was a deletion.
- A message-ownership race that silently dropped mid-turn steers was **four lines**, using a value the code was already computing and throwing away.
- A retry loop that ran 155 times over two and a half hours was traced to an internal consistency check being classified as a provider failure, which routed it into an external retry ladder. A guard was designed to bound it — and then **cancelled**, because fixing the classification removed the case the guard would have covered.
- Two user-facing recovery affordances were **withdrawn** rather than built, on the grounds that the correct outcome is that the failure cannot occur.
- One detector did ship, and is explicitly marked as temporary diagnosis with an expiry and a deletion condition.

## How to apply it

At design time, before writing: state which category above the change falls into. If it is category 5, say why 1-4 do not work.

For category 4 or 5, write the **complexity ledger** in the work object before choosing the approach. Three lines and one sentence:

- **Adds:** the mechanism, concept, state owner, or path being introduced, and who must understand it from now on.
- **Removes or collapses in this same change:** the code, concept, path, or user step it retires. A promised later removal is a follow-up node with an owner and a trigger, never a credit on this ledger.
- **Ledger:** which side each entry sits on. Internal complexity that buys user or agent simplicity is a legitimate trade (the checkpoint system is the model). Adding a user or agent step, decision, or thing to remember in order to save internal complexity is forbidden (a product standard: user and agent cognitive burden only goes down). New product capability is a buy-down when it removes a user or agent problem named and evidenced in the product-thinking chain. Never net the two ledgers against each other silently.
- **Falsification:** one sentence, stated before the work, that would show the trade did not pay: usually "the diff shrinks by about N lines", "the second path is gone", or "the user no longer does X". If no such sentence can be written, the change is category 5 without justification.

A category-4 or category-5 change without a ledger is not reviewable; the maintainers' LLM reviewer blocks it.

At review time: "does this work" and "is this covered" are table stakes. The question that decides the review is whether this is the answer a maintainer will thank you for in a year.

When declining to build something because the root cause was eliminated, **record the non-decision and its reasoning.** An undocumented "we chose not to" is indistinguishable from "nobody thought of it," and the next person will build it.
