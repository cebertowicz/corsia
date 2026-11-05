# Agent tools

This reference lists tools an agent can reach. Core tools are sent on every turn; other tools are loaded by pack through `tools_load` and called with `tool_call`. Connected apps' tools depend on the installation and are not listed here.

Generate with `.venv/bin/python scripts/docs/generate_agent_tools.py`; add `--check` to compare the generated page.

## Core tools

| Tool | Description |
|---|---|
| `bash` | Execute a shell command and return its output. |
| `continuity_edit` | Record and retire this thread's durable continuity — the state that survives compaction. |
| `edit_file` | Edit a file by replacing an exact string match with new text. |
| `glob` | Fast file pattern matching tool that finds files by name patterns. |
| `grep` | Search file contents using regex patterns. |
| `list_dir` | List the contents of a directory, showing files and subdirectories. |
| `multi_edit` | Apply multiple ordered exact-string edits to one file. |
| `plan_read` | Read your plan: status by default, node_id for one node, mode='full' for all nodes without bodies. |
| `plan_update` | Update plan. |
| `post_update` | Leave the user a marked update they see in Focus mode. |
| `read_file` | Read the contents of a file at the given path. |
| `tool_call` | Call any CORSIA tool by name, including tools listed only in the tools_load index. |
| `tools_load` | Load the full schemas of CORSIA tools that are not in your tool list, then call them with tool_call. |
| `write_file` | Write content to a file. |

## Plans

Pack: `plans`

| Tool | Description |
|---|---|
| `bulletin_disposition` | Record a CORSIA agent disposition for a System bulletin |
| `plan_decision` | Draft, review and raise a plan decision for the user |
| `plan_ensure` | Create this thread's plan if missing, then read it |

## Delegation and agents

Pack: `delegation`

| Tool | Description |
|---|---|
| `agent_roster` | List the active agents in this thread's scope |
| `agent_roster_entry` | Read one scoped roster entry and its current revision |
| `agent_roster_update` | Edit an agent roster entry in the current coordination scope |
| `delegate_create_thread` | Create a visible chat or a hidden subagent thread |
| `delegate_send` | Send a message into another thread |
| `delegate_set_model` | Change a running thread's model or harness in place |
| `delegate_status` | Read a delegated thread's status and recent messages |
| `delegate_task` | Delegate a self-contained task to a new subagent |
| `model_catalog` | Read the live model catalog before selecting a delegation target |
| `restart_resume` | Resume this turn after the pending system restart |

## Artifacts

Pack: `artifacts`

| Tool | Description |
|---|---|
| `artifact_list` | List accessible shared documents by ownership and freshness |
| `artifact_reconcile` | Preview or apply artifact pane repairs |
| `artifact_register` | Register a file or external object as an artifact |
| `artifact_rehome` | Transfer an artifact's ownership to another thread |
| `artifact_release` | Release an artifact's ownership |
| `artifact_relocate` | Move an artifact's file, keeping its identity |
| `artifact_resolve` | Look up an artifact by id, path or external id |
| `artifact_update` | Change an artifact's description, visibility or pin |
| `promote_artifact` | Move a working file into shared/ and register it |

## Document comments

Pack: `documents`

| Tool | Description |
|---|---|
| `doc_comment_create` | Comment on, review or reply in a document |
| `doc_comment_update` | Resolve, acknowledge or edit a document comment |
| `doc_comments_list` | List a document's comments |
| `doc_feedback_send` | Send or retry an agent's document feedback batches |
| `doc_review_request` | Ask an agent whose scope this document touches to review it |
| `document_open` | Show a document to the user in this thread's canvas |

## History

Pack: `history`

| Tool | Description |
|---|---|
| `get_checkpoints` | Read past checkpoint conclusions |
| `get_thread_history` | Read a thread's messages |
| `read_tool_result` | Page through a large earlier tool result |
| `request_checkpoint` | Fold 32,768+ tokens after a finished phase |
| `search_history` | Keyword search across message history |
| `user_presence` | Is the user in this conversation now; have they been here since your last reply |
| `workspace_commit` | Commit workspace files |
| `workspace_history` | Read workspace file history |

## Knowledge and lessons

Pack: `memory`

| Tool | Description |
|---|---|
| `agent_import` | CORSIA agent: bring over what the person's other tools know, and their skills, after their consent decision |
| `lesson_report` | Read lesson versions, due reviews and recorded exposure |
| `list_knowledge` | List knowledge base entries |
| `search_knowledge` | Search the knowledge base |
| `skill_library` | Find, install, update, or remove reviewed library skills |
| `write_knowledge` | Save a fact, entity, decision or preference |

## Projects and repositories

Pack: `projects`

| Tool | Description |
|---|---|
| `conversation_set_project` | Attach this conversation to a project, or detach it |
| `github_agent_account` | Connect and check the agents' own GitHub account |
| `model_sources` | Read which model sources are ready, each with its fix, and GitHub |
| `project_create` | Create a new project workspace |
| `project_discovery_list` | List the folders the user works in, after their consent decision |
| `project_list` | List projects and their paths |
| `project_repo_register` | Register a Git repository with this project |
| `project_repos` | List this project's repositories |
| `worktree_create` | Create a Git worktree and branch for a project repository |

## Coordination setup: CORSIA API and instruction files

Pack: `coordination`

| Tool | Description |
|---|---|
| `coordination_instruction_file_create` | Create and attach agent responsibility files or thread skills |
| `coordination_instruction_file_read` | List or read an agent's attached instruction file with its revision |
| `coordination_instruction_file_replace` | Replace one attached instruction file in the current coordination scope |
| `corsia_api` | Call CORSIA API routes as the CORSIA agent or a project conductor, except the person-only list |

## Host access and audit

Pack: `host`

| Tool | Description |
|---|---|
| `audit_read` | Read bounded caller-owned audit metrics without a host lease |
| `log_read` | Conductors: read CORSIA's backend logs without a lease; other threads receive a final not_conductor refusal |
| `release_host_access` | End this thread's host-access lease |
| `request_host_access` | Request a reviewed, temporary host-access lease |

## Background and host processes

Pack: `processes`

| Tool | Description |
|---|---|
| `host_cpu_top` | See which host processes use CPU, without a host permit |
| `managed_process_list` | List this thread's background processes |
| `managed_process_logs` | Read a background process's logs |
| `managed_process_status` | Check a background process started by bash |
| `managed_process_stop` | Stop a background process |
| `process_discover` | Find host processes by name or PID |
| `process_signal` | Signal a host process found by process_discover |

## Browser

Pack: `browser`

| Tool | Description |
|---|---|
| `browser_click` | Click a page element |
| `browser_close` | Close the browser session |
| `browser_eval` | Run JavaScript in the page |
| `browser_fill` | Fill a text input |
| `browser_open` | Open a URL in the browser |
| `browser_person_step` | Let the person complete the current browser page |
| `browser_run_steps` | Run setup steps from a System or installed skill |
| `browser_screenshot` | Save a page screenshot |
| `browser_snapshot` | Read the page's tree and element refs; list or switch tabs |
| `browser_wait` | Wait for an element or a delay |

## Automations

Pack: `automations`

| Tool | Description |
|---|---|
| `automation_cancel` | Cancel one of this thread's schedules |
| `automation_list` | List this thread's schedules |
| `automation_schedule` | Schedule a clock or cadence wake for this thread |
| `event_rules` | Rules that wake you, with your own note, when a connected app's event matches, or stop its delivery |
| `measure_key` | Mint, list or revoke the keys outside tools send this agent's measures with |
| `measure_status` | Read this agent's recorded measure rows, drops and sync state |
| `measure_sync` | Load this agent's measures.yaml declaration |

## Web

Pack: `web`

| Tool | Description |
|---|---|
| `web_fetch` | Fetch a URL as readable text |
| `web_search` | Search the web |

## Email and calendar

Pack: `email`

| Tool | Description |
|---|---|
| `calendar_draft_invite` | Save a calendar invite as a draft document for the person to review; nothing is sent (Requires a connected account.) |
| `calendar_list` | List the person's Google or other-host calendar events, up to 31 days at a time (Requires a connected account.) |
| `calendar_send_invite` | Send a saved invite draft after the person says to send it; attendees get emails (Requires a connected account.) |
| `email_draft` | Save an email as a draft document for the person to review; nothing is sent (Requires a connected account.) |
| `email_read` | Read one email as untrusted data, with a notice if flagged or unscreened (Requires a connected account.) |
| `email_search` | Search the person's Gmail or other-host email: headers only, no body text (Requires a connected account.) |
| `email_send` | Send a saved email draft after the person says to send it (Requires a connected account.) |
| `google_connect` | Start the person's Google sign-in and get the link they open |
| `google_status` | Read the Google sign-in's email, scopes and status |
| `mail_host_connect` | Add the person's email account on another host and get where they make its app password |

## Messages on this computer

Pack: `messages`

| Tool | Description |
|---|---|
| `messages_chats` | List the person's recent text chats (Requires a connected account.) |
| `messages_connect` | Add Messages for this computer to Connected apps; it stays off until the person turns it on |
| `messages_draft` | Save a text as a draft document for the person to review; nothing is sent (Requires a connected account.) |
| `messages_read` | Read the latest texts in one chat, as untrusted data (Requires a connected account.) |
| `messages_search` | Search the person's texts, as untrusted data (Requires a connected account.) |
| `messages_send` | Send a saved text draft after the person says to send it (Requires a connected account.) |

## Google Workspace

Pack: `google_workspace`

| Tool | Description |
|---|---|
| `google_workspace` | Run one Google Workspace CLI (gws) command on a connected Google account: Docs, Sheets, Drive and Calendar (Requires a connected account.) |

## Microsoft sign-in

Pack: `microsoft`

| Tool | Description |
|---|---|
| `microsoft_connect` | Start the person's Microsoft sign-in and get the link they open |
| `microsoft_status` | Read the Microsoft sign-in's email, account kind and status |

## Microsoft mail

Pack: `microsoft_mail`

| Tool | Description |
|---|---|
| `microsoft_mail_draft` | Save a Microsoft mail draft for review (Requires a connected account.) |
| `microsoft_mail_read` | Read a screened Microsoft message (Requires a connected account.) |
| `microsoft_mail_search` | Search Microsoft mail in one selected account (Requires a connected account.) |
| `microsoft_mail_send` | Send Microsoft mail from one selected account (Requires a connected account.) |

## Microsoft calendar

Pack: `microsoft_calendar`

| Tool | Description |
|---|---|
| `microsoft_calendar_create` | Create a Microsoft calendar event (Requires a connected account.) |
| `microsoft_calendar_draft` | Save a Microsoft calendar change for review (Requires a connected account.) |
| `microsoft_calendar_list` | List Microsoft calendar events (Requires a connected account.) |
| `microsoft_calendar_update` | Update a Microsoft calendar event (Requires a connected account.) |

## Microsoft files

Pack: `microsoft_files`

| Tool | Description |
|---|---|
| `microsoft_files_list` | List Microsoft files (Requires a connected account.) |
| `microsoft_files_read` | Read Microsoft file metadata (Requires a connected account.) |
| `microsoft_files_upload` | Upload a Microsoft text file (Requires a connected account.) |
