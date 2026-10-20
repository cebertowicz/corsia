---
name: connect-tools
description: Use when work needs a tool, service or account that isn't connected yet, or the person asks to connect one. Not for a one-off lookup.
---

# Connect your tools

## Start from the work

Settle who it is for, what it must read, change and hear about, when and where it happens, and why. That decides how to connect.

- A one-off lookup connects nothing.
- A source you notice mid-work is offered once. After a decline, offer it again only if what the work needs changes, and note the decline with that work so other agents see it.

## Reuse first

Use an existing connection before making one. Look at what is already connected and at any note of an earlier connection. A connected app the person already uses in another app (Claude, Cursor) is brought over, with their yes, rather than set up again.

## To act on a tool

Pick the method yourself, cheapest and most dependable first:

1. **A connection CORSIA ships:** maintained and tested, and it keeps keys out of your sight.
2. **The tool's own command line:** made by the tool's maker, and the cheapest per use.
3. **A connected app** (also called an MCP server): its actions' descriptions take up room in every turn that uses it, and quality varies.
4. **The browser:** slowest, breaks when pages change, and the person must sign in again whenever the session expires.

When a method can't do what the work needs, use the next one. Setting up or reconnecting a connected app is in skill `coordination-api`, for agents that have the API tool.

## To hear about changes

In this order: the tool tells CORSIA itself (a webhook, or a reader CORSIA ships); then a small script that checks for changes; then checking on a timer, as a last resort.

For both lists, find out what is available from CORSIA's own tools at the time, not from memory. New ones are added over time.

## Permissions and reach

- Reading access never permits sending. Ask the person before the first send, post, change or purchase through a new connection.
- A new connection reaches only where it is added: the whole workspace, or one project. One that would help every project goes through CORSIA, as skill `coordination-api` says; otherwise add it where it is needed.
- The person types a key only into the field CORSIA names, never into chat. You never read it.

## When it can't be done yet

Say so plainly and work around it. Quote what the tool reported, never a limit you remember. Report the gap to CORSIA the way you report any problem with it, so it can be fixed.

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
