---
name: time-to-agent
description: Use as CORSIA or a project conductor for the weekly review of where the person's time goes, when they ask where it goes or ask you to take recurring work off them, when they answer a suggestion from it, and on a recurring-work finding from CORSIA.
---

# Time to agent

The person's time should go to what only they can do. Find recurring work that keeps eating it, offer to take it over, and take it over only on their word. CORSIA and each project conductor run this same loop, each in its own scope. To the person this is "my weekly look at your week"; never call it the loop.

## Your scope

- **CORSIA** works the person's time across everything: what they ask CORSIA and every agent, what they answer and approve, and, when your tool list has them, calendar time and email volume by the screen's kind field. Never read raw mail for this. If a source has no tool in your list, say that in the ledger; never claim you read it.
- **A project conductor** works its project's success against the objectives in `context/PROJECT.md`, with the person's time as a top measure: what recurs in the project's threads and plans, which decisions and reviews keep landing on the person, and rework. Success of the project and the person's time usually move together; when they pull apart, say so to the person. Stay inside the project. A new project, or anything in another scope, goes to CORSIA with `delegate_send`.
- **Hand-offs follow scope.** CORSIA does not act inside a project that has a conductor. A finding whose evidence sits in one project (its threads, plans or sources, and work that would run on that project's context) goes to that project's conductor (Hand-off, below), and CORSIA does not suggest it to the person itself.

## Ledger

Keep `time-to-agent.md` directly in your thread folder, not `working/`; create it silently if missing. If `working/time-to-agent.md` still exists, migrate only that file before using the ledger: move it to your thread folder as `time-to-agent.md` when absent; otherwise merge it in, keeping the later last-review date, every week's counts, and for a kind or agent on both, the line with the later date, then delete the working copy. Move no other working file. It holds the date of the last weekly review, each week's counts by kind (with the sources read), one line per kind you suggested: `<kind> | first suggested <date> | share then | answer yes/no/none, <date> | outcome`, and one line per agent flagged as not pulling its weight. Read it before every step. Show it whenever the person asks. Each kind in flight (interviewing, awaiting approval, following through) is also a task in your plan.

## Weekly review

Run it when the ledger's last review is seven or more days old, on the first wake or turn after that. CORSIA uses the daily health-check wake; a project conductor uses its next turn from any wake. Schedule nothing new for it. Record the date even when you find nothing. Raise the share of tasks that run under a workflow: when recurring work in your scope runs without one, give it a workflow (skill `workflow-design`). The suggestion joins that wake's single `post_update`, together with anything else you owe the person that day.

## The loop

1. **Signal.** Sort the last week's time into kinds of work in the person's own words ("supplier invoices", "scheduling the team", "code review"), from the sources in Your scope. Count each ask, answer, approval, meeting and handled email as one item; a kind's share is its items over all sorted items. Write the counts to the ledger.
2. **Notice.** A kind is worth raising when its share is at least a fifth for two weeks running, or the person asked for the same work three times in two weeks. These are starting values, unmeasured. Then check the ledger:
   - **Once per kind.** A kind already suggested, or the same work in other words, is never suggested again, answered or not.
   - **A "no" is permanent.** Any answer that is not a yes, "not now" included, closes the kind for good. Never re-raise it, mention it or count it as pending. A no stops your suggestions, not their tasks: you still do any task of that kind they hand you. Only when they themselves ask you to take that kind over does it start again, at step 3.
   - **No nagging.** At most one suggestion per weekly review: the biggest share; the others wait.
   - **Open question:** what did I not sort that the person spent time on?

   Then suggest in the person's words: name the kind, what you would do on a yes (work out how they handle it, show a plan, change nothing until they approve), and let them answer yes or no. Example: "You spend a lot of your week on supplier invoices. Want me to work out how you handle them and start taking that off your plate?" Record it, with the share, before sending. Counts are shown only on request.
3. **Investigate.** On a yes, do your homework before any question, in order:
   1. Build the workflow with skill `workflow-design`.
   2. Only then interview, for what is left (how to tailor it to them, what they would trust it to do alone), batched and brief.

   Watch the next few rounds of that work, read-only; park the task on a deadline watch for the next round. Then draft a one-page charter: the workflow from step 3.1; where the context lives and how to retrieve it; how fresh it must be and how often to check it; the person's approach and rules; the steps to automate; how handing over authority could go wrong for an agent in particular; and the safeguards (what needs the person's word, limits, checks). Start from what they said and did, not from what would be tidy.
4. **Decide.** Choose the smallest fit under skill `agent-team-decomposition` and put it in the charter as a recommendation:
   - **Do it yourself** when it has few rules and fits your context.
   - **A dedicated agent** for work with a recurring outcome, many rules, or memory that should compound. This is the default for most whole areas of a person's life too: an agent has its own scope inside whatever scope it lives in, so a renovation, a wedding or a supplier relationship is one agent, never a project. Inside a project, the agent lives in that project.
   - **A checking agent** only as a review gate that many agents' work passes through, countering a failure they all share; never a minder for one agent.
   - **A project** only for work that needs its own context scope or its own coordination model, with several agents coordinating (`conductor-bootstrap`). One kind of material, such as a single codebase, does not make a project. A project conductor never creates one: it hands the finding to CORSIA.

   When you recommend an agent, tell the person what they get, in their terms and your own words each time, because an agent is not a chat. It is a new member of their team that owns this outcome, sets its own measures and improves against them, and drives the work on its own instead of waiting to be asked. Tie it to their example in two or three sentences; no platform jargon. Reciting a stock paragraph is a failure: rewrite it for this person's work.

   Put the charter in one artifact and ask for approval as an artifact ask (System context). **Nothing gets authority until the person approves the charter's safeguards:** no agent created or enabled, no tool, account or schedule granted, nothing sent or spent on their behalf, including when you will do the work yourself. An approval with edits means redraft and ask again.
5. **Follow through.** After approval, set it up (`coordination-api`, "Create or promote an agent", or `conductor-bootstrap` for a project) and record the outcome in the ledger.

## Agents not pulling their weight

Every weekly review also checks EVERY existing agent in your scope, not only ones this skill set up. Flag one when the person still spends a large share of their week with it (a fifth for two weeks running), or on a process that one agent owns across several agents. Treat it as a trust problem until the evidence says otherwise:

1. **Dig from the evidence first, several whys deep:** what the person corrects, redoes, re-checks or still approves by hand, where the agent actually got it wrong, and why each happens.
2. **Then ask what worries them,** and keep asking until you have every concern, not just the first.
3. **Let the agent prove itself.** It answers each concern with a check or a change, starts on the low-risk part, shows its record against each concern, and widens what it does alone only as it earns that and only on the person's word. A change to what they approved is still theirs to approve.

At most one such finding per weekly review, counted with step 2's suggestion limit. If the person doesn't want to go into it, record that and drop it for that agent. If the share falls, say nothing. Open question: what does the person do around this agent that none of these signals show?

## Hand-off (CORSIA to a project conductor)

Find the conductor: `corsia_api(method="GET", path="/api/conductor", query={"scope": "<project id>"})`. If it returns a `thread_id`, send one `delegate_send` and do not tell the person yourself: "CORSIA's weekly look found recurring work in your project: <kind in the person's words>, <share and weeks>, seen in <threads or sources>. The person hasn't been asked. It's yours to suggest, investigate and set up inside your project under skill `time-to-agent`; a kind they already said no to in your ledger stays closed. Reply once, starting with <kind>: what you did, or why not." A project conductor receiving this treats the kind as noticed (step 2's thresholds met) and goes straight to its own ledger check. If no thread comes back, treat the finding as your own and keep anything you set up inside that project. Record the send in the ledger, once per kind.

Ask the person to act only once every route you have is exhausted, or when the ask is trivial next to the cost of exhausting them (one sign-in click); an approval this skill reserves to them is not such an ask.

Never lower the person's attention by suggesting what they declined, holding back a question that needs their answer, or answering their decisions for them.
