# Workspace organization standard

**Status:** template (adopt as `approved` once the owner accepts it) · **Owner:** conductor · **Review:** quarterly and after any category change

Applies to every thread that can write under `shared/` or `context/`: agents, conductors, and ordinary threads promoting from a working tray. Durable outputs move from a thread's `.threads/<id>/working/` tray into an approved category with the `promote_artifact` tool; this standard adds the rules that keep the layout legible over time.

## One rule per question

| Question | Rule |
|---|---|
| Is this file authoritative? | Only if it is listed in its category `INDEX.md` and its header status is `approved` or `current`. Anything unindexed is scratch that leaked. Anything that claims to be a contract must also appear in the project's `AUTHORITY.md`. |
| Can I tell from the name? | `DRAFT-<slug>.md` is unapproved; `YYYY-MM-DD-<class>-<slug>.md` is dated evidence whose relevance decays; a plain `<CLASS>-<slug>.md` or `<slug>.md` is current authority; anything under `archive/` is dead. A file is renamed exactly once, when a draft is approved. |
| Is this living or dated? | Living documents (charters, standards, contracts, PRDs, RFCs, domain references, runbooks) carry no date in the name; their last verification lives in the artifact registry, and any `Verified at:` header is informational. Dated documents (audits, retros, postmortems, measurements, briefs, incident records, snapshots) carry the date in the name and are never edited after the day they describe; a correction is a new dated file that cites the old one. |
| What goes in the header? | Line 1 is the title. Line 2 is one status line: `**Status:** draft \| proposal \| approved \| current \| evidence \| superseded by <path>`, optionally followed by informational `**Owner:** <agent or thread>` and `**Verified at:** <date or commit>`, then `**Review:** <update trigger>`. `Owner:` and `Verified at:` are never trusted; the artifact registry is the truth for ownership and verification. `Review:` holds the trigger text, never a due date. Nothing else before the first section. |
| Where does it go? | Into an existing approved `shared/<category>/`. `planning/` holds PRDs, RFCs, plans, and briefs. `standards/` holds rules. `architecture/` holds contracts and designs. `records/` holds dated evidence of every kind. `shared/<domain>/` holds one agent's canonical reference. Assets (images, logs, data) sit in `<category>/<doc-slug>.assets/` beside the document that cites them, never loose. |
| When may I create a directory? | A new category needs three or more documents that share a purpose no existing category covers, its own `INDEX.md`, and a line in `shared/INDEX.md`; the conductor creates it. A subdirectory inside a category needs a multi-document set with its own index (for example `planning/agents-experience/`). Never create an empty directory or a directory for one file. |
| How do indexes stay true? | Every write to a category updates that category's `INDEX.md` in the same turn: one line per file with class, status, owner, and purpose. `shared/INDEX.md` lists categories only. Archiving removes the entry from the live index and adds it to `archive/INDEX.md`. |
| When is a file archived? | When its status becomes `superseded`, `executed`, or `abandoned`, or when a dated document is older than ninety days and no living document cites it. Archive by moving into `shared/archive/<category>/` with the name unchanged and the header status set. Then grep the name: fix every reference in a *living* document in the same change; dated evidence and archived documents keep their historical text. |
| When is a file deleted? | Only scratch, duplicates of an identical canonical file, and assets nobody cites. Anything that ever carried authority is archived, not deleted. |
| How often is it checked? | Charters, standards, contracts: 90 days and on every change that touches them. Current PRDs and RFCs: 30 days. Domain references and runbooks: 30 days. Drafts: 30 days, then approve, archive, or delete. Dated evidence: never audited; archived once 90 days old. |

## Scope notes

- Existing documents gain the status header on their next edit; the audit lists them without blocking on them.
- Skills (`SKILL.md` frontmatter), role-context files, and thread bootloaders are governed by their own loaders and are exempt from the header rule; the caps still apply.
- Always-loaded context files (`context/*.md`) are never edited only to add a header, because every edit invalidates every thread's cache prefix.

## Why these rules

Every rule above answers an observed failure of shared agent workspaces: unindexed scratch treated as authority, drafts mistaken for contracts, dated evidence silently rewritten, and orphaned directories. Record your own observed instances as dated evidence under `shared/records/`.

## Caps

Prose accumulates; caps keep the prefix honest. Over the cap means something belongs elsewhere, not that the writing should get denser.

| Thing | Cap | Overflow goes to |
|---|---|---|
| Project charter (`PROJECT.md`) | 900 words | coordination model, standards, or the domain reference |
| Coordination model | 1,500 words | skills for procedure, the operating contract for agent duties |
| Cross-thread notices | five entries, 25 words each, each with an expiry date | nothing; expired entries are removed |
| Role operating contract | 800 words | a standard or a skill |
| `RESPONSIBILITY.md` | 700 words; lessons at most five | plan for status, domain reference for history, skills for procedure |
| Skill description (the trigger) | 60 words | the body |
| Skill body | 800 words | a one-level-deep reference file the body names |
| Standard | 1,200 words | a reference document it links |
| `INDEX.md` entry | one line, 25 words | the document's own header |
| Decision brief to the user | 200 words; 350 with options | the linked artifact |
| Plan node title / acceptance criteria / context | 120 / 500 / 800 characters (the count of active nodes matters more than any field) | a linked artifact; parked work is not active |
| Knowledge-base entry | 300 words | a shared document the entry points at |
| Dated evidence document | no cap; it is retrieved, not injected | — |

## Cross-thread notices

The last always-loaded project file carries a short **Cross-thread notices** section: provider outages, capacity limits, moratoria, and cross-cutting initiatives that change sequencing for everyone. Only the conductor writes it. An agent that needs a notice proposes the exact line (with its expiry) to the conductor; that request is a permitted narrow coordination message. It sits at the tail of the last always-loaded file so an update invalidates the least cache prefix; the charter points at it.


- Every agent keeps its own `shared/<domain>/` and its bootloader to this standard as part of its weekly self-review.
- The conductor runs the workspace audit quarterly and after any category change, files gaps as issues under owning lanes, and is the only writer at `shared/` root.
- A verification of this standard is a fresh weak-model turn that, given three files to place, names the right directory, filename, and header for each.
