# Measurement

Measurement shows how your agents' work is going: what they finished, how much of your time it took, and what the models cost. Open **Measurement** in the sidebar.

An **agent** is one of the assistants working for you, each with its own chat, plan and job. A **project** is a body of work, such as a product or a household, with the agents that work on it.

## Read your work

Use **Scope** to pick all work, one project, or one agent, and switch between **Days** and **Weeks**. The page shows a few key figures, a table of your agents, and charts over time.

- **Outcomes** are what the agents finished: each task an agent completed and had accepted in its plan. Work done outside a plan isn't counted.
- **Your time** is the time CORSIA was open in front of you on that work. It stops after 10 minutes without input, or when another app has focus.
- **First-try rate** is the share of outcomes accepted without being sent back, as graded by agents.
- **Cost** is shown two ways. **API-equivalent** is what the recorded tokens would cost at each provider's published API prices, which isn't what you were charged. **Actual** is what you were charged, including subscriptions, and is recorded for all work rather than per project or agent. Cost per person-hour of work delivered divides the API-equivalent figure by an estimate of how long a person would have taken to do the same outcomes.

A new install has little to show until your agents have finished some work. A blank chart doesn't mean zero; usually there isn't enough data yet. An agent with too few outcomes isn't ranked yet.

## Track something of your own

You can ask an agent to keep count of something its own work does, to wake it when something happens, or to time how long things take. A **measure** is one recorded fact; a **KPI** is a number worked out from measures, such as the share of emails you answered; a **rule** wakes the agent when a kind of event arrives, and the agent then tells you. For example: "Each time you triage my email, record whether you replied, archived or deleted it," or "When mail from my accountant arrives, look at it and tell me what it needs."

- The agent sets up what it will record, then records each time it happens.
- To check it's working, ask: "Is the email triage you're recording coming through?" The agent can tell you the counts for today and the last 7 days, any refusals and why, and a KPI's value for this week and last week.
- New email in a connected mailbox, GitHub activity, CORSIA's own events and events the agent sends can wake it. For mail, a rule matches a sender or recipient address; it can't match on a label, the subject or the message text. A rule wakes only the agent that wrote it (or an agent it started).
- A KPI can have a line it should stay on the right side of. After each week ends, if the number landed on the wrong side, the agent hears about it so it can look into it.
- Measurement doesn't show your own measures, and calendar and chat apps don't reach CORSIA. An agent can run a script on a schedule, at most every 5 minutes, that checks the app and records or sends what it finds.
