# Writing docs

How to write and change a page under `docs/`. The same files are read on GitHub and will be built into a documentation website without edits, so the format rules below are what keeps them working in both places.

## Where a page goes

| Folder | Holds | Example |
|---|---|---|
| `docs/contributing/` | Getting a developer from a fresh clone to a merged change, and recipes for recurring contributor tasks | This page |
| `docs/how-it-works/` | Concepts: what a part is, why it exists, how it works, what can go wrong, where it lives in the code | [Plans](../how-it-works/plans.md) |
| `docs/security/` | The security model, one page per boundary | [Host access](../security/host-access.md) |
| `docs/` (top level) | Feature pages written before these folders existed | [The skill library](../skill-library.md) |

Write facts once. If another page already explains something, link to it instead of repeating it.

## Format

- Plain GitHub-flavored Markdown. No frontmatter, no HTML, and no syntax that belongs to one site framework (admonitions, components). A note is a bold lead-in.
- One `# H1` per page: it is the page title. The first paragraph is the page summary; a website uses it as the description, so make it say what the page is for.
- Link between pages with relative paths ending in `.md`, such as `[Plans](../how-it-works/plans.md)`. Point to code as a repository path, such as `backend/tools/shell_guard.py`, without line numbers.
- Headings are link targets. Don't rename one without updating every link to it.
- Diagrams are fenced `mermaid` blocks, which GitHub renders. No pictures of diagrams. Keep labels short and the diagram narrow enough to read on a phone.
- Use a table for anything compared across options.
- File names are lowercase with hyphens, short, and still right in a year.
- Each folder has a `README.md` as its landing page.
- Don't move or rename an existing page. Links from outside the repository depend on its path.

## Navigation

[`docs/README.md`](../README.md) is the only navigation source: it lists every section in reading order, and each section's `README.md` lists that section's pages. When you add a page, add it to its folder's `README.md`; a new top-level page or folder also goes in `docs/README.md`.

## Voice

- State what happens. Engineering documentation, not marketing: no "secure", "robust", "seamless" or any adjective doing the work of a fact.
- Short, plain sentences. Define a term the first time a page uses it, or link to where it is defined.
- Write CORSIA in prose and `corsia` in identifiers and commands.
- Say what doesn't work as plainly as what does, next to the claim it limits.
- Describe what is true at the current commit. "Not built" is fine; "coming soon" is not.

## Claims

Every factual claim traces to source, tests, or a command you ran. If you can't trace a claim, cut it or say it was not verified. Where a reader can check a claim, give the command under a **Check it yourself** heading, and run it before you publish.

## Keeping a page current

Each page ends with one line saying which commit it was last checked against, for example:

```text
Checked against the code at `7a8c55d84`.
```

When you change code a page describes, update the page in the same pull request and move that line to your commit. Pages are rechecked before each release.

## Public content

This repository is public. Leave out private data: people's names, local paths, hostnames, account IDs, tokens, and anything copied from a private workspace.

## Check it before you push

`bash scripts/check-pr` runs the docs drift check with the other gates. Then read the page rendered on GitHub, or in any Markdown preview that renders Mermaid.

Checked against the code at `7a8c55d84`.
