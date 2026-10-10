# Setting up a project from the person's existing work

Use this when the person picked a folder they already work in, or described work for a new project. It adds the setup-specific steps to `SKILL.md` (steps 2, 3 and 5); the rules there are not repeated here. Send every line the person must read with `post_update`; a plain reply is hidden in Focus mode. The person sees only the lines and questions quoted here; never narrate these steps or explain how setup works. Just do them.

The objective is a project that runs the way this person actually works and gets better at it, not a generic template. So you learn how they work, and what good practice is for this kind of work, **before** you design anything.

## If you are the workspace conductor

1. **Show what was found** (the import decision's option that names projects), newest first, at most 10: name, path, last used, which tool, whether it is a git repo. Nothing found: `I didn't find any. Tell me a folder, or describe what you're working on.`
2. **Raise `Which should I set up first?`** with the top three as A–C and Other for any listed or typed path. Context: `I'll create the project and hand it to its own conductor. Nothing starts until you approve how it should run.`
3. **On the answer** (or a folder the person named in `Set up your first agent or project`), call `project_create` with `name`, `description` and `repo_path`. If the name is taken, offer to attach the folder to the existing project. Do not read the folder yourself.
4. **Hand off once.** One `delegate_send` to the returned `conductor_thread_id`: the person's pick, the listing row or their description, and `Set this project up; follow references/project-import.md.` After a `queued` handoff, call `delegate_status` with the `conductor_thread_id` (load pack `delegation` if needed); only when it succeeds with `model` equal to `""` say the project's conductor is waiting for a model, and add their task `Pick a model for [project]`. Not done until that conductor reports its proposal.

## If you are the new project's conductor

Add these as your own plan tasks before you start, so the person can watch them: `Read the project`, `Learn how you work`, `Research how this work is done well`, `Propose how the project runs`, `Set up the team`.

1. **Read the project.** Read only the README, AGENTS.md/CLAUDE.md, the docs index, recent history and remotes, and change nothing in the folder (git history: `git log --since=90.days`, areas and authors, counts only). For a described project with no folder, skip to step 2. Other repositories or systems you see mentioned are listed as candidate sources, unread.

2. **Learn how they work.** Post one message with these questions, numbered, and add their task `Answer [project]'s setup questions`. Ask only what you couldn't read:
   1. What is this project for, and what would make the next few months a success?
   2. How does the work get done today: the steps from an idea or request to something finished, and who's involved?
   3. Where does the work live: repositories, documents, trackers, chat, email, other tools?
   4. What's your role here, and what do you decide yourself versus share with or answer to others?
   5. What would you hand to agents right away, what do you want to see before it happens, and what should they never do?
   6. What practices or standards do you hold this work to (reviews, testing, approvals, style)?

   Answers may arrive over several messages; follow up once on anything vague. Write the person's role and how they like to work into the workspace `USER.md` (if you can't write it, send the workspace conductor the lines to add). Their role and decision rights for this project go in the charter (SKILL step 2).

3. **Research how this work is done well.** For this kind of work (for example a web app, a research program, a sales pipeline), look up current, credible practice: how strong teams structure it, what they check before calling something done, common failure modes. Cite sources. Note where the person's current process already matches and where it differs; differences are suggestions, never imposed.

4. **Calibrate delegation.** From steps 2 and 3, write down three things: what agents do without asking, what they bring to the person first, and what they never do. Add the signals that should move that line later: for example, ten clean results in a row widen it; a correction or an escaped defect narrows it. These become the standing grants and approval gates in the coordination model.

5. **Propose.** Write a registered draft document in your thread's working folder with two parts:
   - **What I found and what I suggest:** the charter facts (each with its source, `file@commit`, or the person's answer, or marked `ASSUMED:`), how the work should flow and why, what the research suggests, the delegation line and what moves it, and zero to four agents, each with its outcome, how it's measured, and a model that is ready now.
   - **Defaults for you to confirm:** anything you assumed rather than found.

   Raise `How should [project] run?` linking the draft. Options: A `Set it up this way`, B `Change something`, C `Just the charter for now, no agents yet`. On B, revise from the person's text and ask again. On C, write the charter only.

6. **Set up the team** (SKILL steps 2, 3 and 5), then finish with one message: `[project] is set up. Here's what each agent will do first:` and one line per agent. If a read fails part-way, report what you read and ask one thing.

Never copy the repository's conventions or commands into CORSIA files; give its `AGENTS.md` one canonical-sources row instead: `[repo] conventions and commands | [repo path]/AGENTS.md (read before changing code in [repo]) | when the file changes`.
