---
name: connect-tools
description: Use when work needs a tool, service or account you can't reach yet, or the person asks to connect one. Not for one you already reach or a one-off lookup.
---

# Connect your tools

## Start from the work

Settle who it is for, what it must read, change and hear about, when and where it happens, and why. That decides how to connect.

- A one-off lookup connects nothing.
- A source you notice mid-work is offered once. After a decline, offer it again only if what the work needs changes, and note the decline with that work so other agents see it.

## Reuse first

Use an existing connection before making one. Look at what is already connected and at any note of an earlier connection. When the person names one to use, find it in that list now and say what it reaches; that is the work, not a note for later. A connected app the person already uses in another app (Claude, Cursor) is brought over, with their yes, rather than set up again.

## To act on a tool

Pick the method yourself, cheapest and most dependable first:

1. **A connection CORSIA ships:** maintained and tested, and it keeps keys out of your sight.
2. **The tool's own command line:** made by the tool's maker, and the cheapest per use.
3. **A connected app** (also called an MCP server): its actions' descriptions take up room in every turn that uses it, and quality varies.
4. **The browser:** slowest, breaks when pages change, and the person must sign in again whenever the session expires.

When a method can't do what the work needs, use the next one. Setting up or reconnecting a connected app is in skill `coordination-api`, for agents that have the API tool. Email, calendar and files ship for Google (skill `google-setup`), Microsoft (skill `microsoft-setup`) and other hosts (skill `mail-host-setup`, also the start when you don't know the host yet); only the CORSIA thread runs those, so anywhere else ask the person to connect the account there.

## Asking to approve one

- Name the publisher only after checking it on the maker's own site or repository, through web search or opened in the browser, never from the package's description of itself. If you can't check it, say you couldn't confirm who publishes it.
- Say what will run and who can use it: every agent in the workspace, or only this project's. For a program on this computer, add `It runs as you on this computer, so it can open your files and accounts the way you can, except CORSIA's private data and your stored keys.` If CORSIA installs a runtime or CLI for it, say in one line what, from whom, and that only CORSIA's agents use that copy.

## To hear about changes

In this order: the tool tells CORSIA itself (a webhook, or a reader CORSIA ships); then a small script that checks for changes; then checking on a timer, as a last resort.

For both lists, find out what is available from CORSIA's own tools at the time, not from memory. New ones are added over time.

## Permissions and reach

- Reading access never permits sending. Ask the person before any send, post, change or purchase they have not asked for; a yes covers only the action it named.
- A CLI acts as the person's whole account there, so its first write waits for their yes even when they asked for that write. Their request doesn't lift this hold: it is one of the constraints the request is carried out within, because they haven't yet seen which account will act. Read which account it is signed in as (its status or whoami command), name it to them and ask; send nothing, not even a test, until they say yes. Learn its other commands from its help, never by trying them. Never read its stored sign-in.
- A new connection reaches only where it is added: the whole workspace, or one project. One that would help every project goes through CORSIA, as skill `coordination-api` says; otherwise add it where it is needed.
- The person types a key only into the field CORSIA names, never into chat. You never read it.

## When it can't be done yet

Say so plainly and work around it. Quote what the tool reported, never a limit you remember. Report the gap to CORSIA the way you report any problem with it, so it can be fixed.

## No connection exists yet

No tool ends at "I can't" when the only thing missing is a connection. Tell the person `There's no ready way to connect [app]. I'll build one after setup. It's on my list.` with [app] the app's own name (outside setup, drop "after setup"), and add a task of your own to build it: a small connection or script, as the methods above say. It never holds up what the person is doing. When it works, read the workspace `context/50-USER-PROFILE.md`: only when it has `- **Share improvements with the community:** Yes`, offer to share it to CORSIA's community, and only after they say yes; with No, or no such line, offer nothing. Sharing is public, so strip every account detail, key and personal name from it first. A missing permission or a CORSIA change is the same: one line naming what is needed, handled after setup. A CORSIA change is reported the way "When it can't be done yet" says.

## A macOS permission only the person can grant

Some connections need a permission macOS lets only the person give, such as Full Disk Access for the CORSIA app so a reader can open Messages. No agent can turn it on; the person does, in System Settings. Before they grant it, say what it opens: every connected app CORSIA runs, the Messages reader included, and every host command an agent runs after it passes review, could then read any of their protected files (Mail, Messages, Photos and others). Agents' own shells stay limited to the workspace either way. Then tell them how to turn it off again: System Settings › Privacy & Security › Full Disk Access, and switch CORSIA off. Build such a reader to read only what the work needs (for Messages, only its database).

## Sign-in problems

- A sign-in only the person can fix becomes one reconnect step for them, never a retry loop.
- If the setup itself is wrong (their own sign-in app is misconfigured, say), fix that first, then ask them to sign in once.
- Before telling them a tool is down, check the connection's state.

## Done means

- The agent that will use the connection has read something real through it.
- The connection is noted where other agents look (skill `memory-placement`, unless your tools already list it), so they reuse it instead of connecting again.
- If the work needs to hear about changes, the first one has arrived.

Then tell the person in one line what you can now see and still can't.

## Recurring work with no ready connection

Package what you built as a skill with its scripts (skill `skill-creator`), so other agents here reuse it unchanged. It holds no account details or keys; it reads them from the connection. Sharing it beyond this computer needs the person's approval.
