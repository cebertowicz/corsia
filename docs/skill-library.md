# The skill library

Agents can install skills from the public [CORSIA library](https://github.com/cebertowicz/corsia-library) instead of writing their own. CORSIA's own items are always available; items shared by anyone else stay off until you [agree to use community improvements](#community-improvements-consent). An agent searches the library when none of its own skills fits the task, installs what it needs without asking you, and decides when to update its copy.

## What is in it

Each skill is an [Agent Skills](https://agentskills.io) folder (`SKILL.md` and the files it needs). The library's own items follow its `main` branch; items from other repositories are pinned to an exact commit. The library records a SHA-256 hash for every file in it, along with the item's version and license. It can also withdraw an item and record why.

No review checklist for library items is published. The hashes are read from `main` too, so they catch a file that doesn't match the index, not a change by someone who can push to the library. A skill is instructions and can include scripts, so an installed skill can tell an agent to do anything that agent can already do. See [Supply chain](security/supply-chain.md#shared-skills-and-setups).

## How an agent uses it

Agents do this through the `skill_library` tool:

- **Search** is for when none of the agent's own skills fits. Each result shows whether the agent already has that item, and whether a newer version exists. If nothing in the library fits either, the agent writes its own skill.
- **Install** fetches the item and checks every file against its recorded hash before writing anything. It puts a copy in the agent's own skills, and the agent can load it right away. A worker's install goes to the agent that started it. Install is refused when the item is withdrawn, or when the agent already has a skill with that name.
- **For the whole project:** a project's conductor can install, update or remove a copy for every agent in the project instead of only itself (`scope: project`). Other agents in the project use that copy but can't change it.
- **Updates** never happen on their own. The library is checked daily. When an installed item changes or is withdrawn, the agent that owns the copy gets a notice and decides whether to `update` or `remove` it. A withdrawn item stays in place until its owner removes it.

If the index can't be reached, search uses the last valid copy if one exists and says how old it is. Install and update still need to fetch and verify the item files; if those can't be reached, they stop and say to try again later.

## Community improvements consent

Items written by CORSIA work without any answer. Items anyone else shared work only after the person says yes to using community improvements. The first chat asks, and the person can change the answer any time by telling an agent. CORSIA stores that answer in the `Use community improvements` line of the profile.

An item counts as CORSIA's when the library's index records CORSIA as its author and the item comes from the library's own repository. Until the person says yes, search shows only CORSIA's items and says so, and install and update are refused for anyone else's; remove still works for a copy the agent already has. Change notices for anyone else's items stop until then, but withdrawal notices continue so agents can stop loading withdrawn copies.

## Adapting an installed skill

An installed copy is the agent's own to change, but a changed copy is no longer the library's version. Before an update or removal replaces the agent's changes, it lists the changed files and asks the agent to confirm.

So keep notes about one project out of the installed copy. Put them in that project's context files, or in a separate skill that points to the library skill. The copy then stays identical to the library's and keeps receiving updates.

## Contributing

The library does not take contributions.
