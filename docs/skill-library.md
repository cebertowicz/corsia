# The skill library

Agents can install reviewed skills from the public [CORSIA library](https://github.com/cebertowicz/corsia-library) instead of writing their own. You don't need to do anything: an agent searches the library when none of its own skills fits the task, installs what it needs, and keeps the copy current.

## What is in it

Each item is an [Agent Skills](https://agentskills.io) folder (`SKILL.md` and the files it needs), pinned to an exact commit and reviewed before it is listed. The library records a SHA-256 hash for every file in it, along with the item's version and license. It can also withdraw an item and record why.

## How an agent uses it

Agents do this through the `skill_library` tool:

- **Search** is for when none of the agent's own skills fits. Each result shows whether the agent already has that item, and whether a newer version exists. If nothing in the library fits either, the agent writes its own skill.
- **Install** fetches the item at its pinned commit and checks every file against its recorded hash before writing anything. It puts a copy in the agent's own skills, and the agent can load it right away. A temporary agent's install goes to the agent that started it. Install is refused when the item is withdrawn, or when the agent already has a skill with that name.
- **Updates** never happen on their own. The library is checked daily. When an installed item changes or is withdrawn, the agent that owns the copy gets a notice and decides whether to `update` or `remove` it. A withdrawn item stays in place until its owner removes it.

If the library can't be reached, search uses the last copy it fetched and says how old that is. Install and update stop and say to try again later.

## Community improvements consent

The library works only after the person says yes to using community improvements. The first chat asks, and the person can change the answer any time by telling an agent. CORSIA stores that answer in the `Use community improvements` line of the profile. Until then, search, install, and update are refused; remove still works for a copy the agent already has. Change notices stop while the library is off, but withdrawal notices continue so agents can stop loading withdrawn copies.

## Adapting an installed skill

An installed copy is the agent's own to change, but a changed copy is no longer the library's version. Before an update or removal replaces the agent's changes, it lists the changed files and asks the agent to confirm.

So keep notes about one project out of the installed copy. Put them in that project's context files, or in a separate skill that points to the library skill. The copy then stays identical to the library's and keeps receiving updates.

## Contributing

The library does not take contributions.
