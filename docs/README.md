# Documentation

The main pages under `docs/`, by topic. Start with the repository [README](../README.md) for what CORSIA is and how to install it.

## Security

What agents can reach, what stops them, how strong each control is, and where it doesn't hold. Start with the overview, then [Known limits](security/limits.md).

| Page | What it answers |
|---|---|
| [Security model](security/README.md) | The whole model on one page: boundaries, what an agent can and can't do by default, the top limits |
| [Threat model](security/threat-model.md) | What we protect, from whom, what we assume, what's out of scope |
| [Agent sandbox](security/agent-sandbox.md) | How shell commands and file tools are confined on macOS and Linux |
| [Host access](security/host-access.md) | Leases: request, review, approval, per-command checks, expiry |
| [Secrets](security/secrets.md) | Where keys live, who can read them, masking and what it misses |
| [Data and privacy](security/data-and-privacy.md) | What leaves your machine; Local only; telemetry; data at rest; removal |
| [Network](security/network.md) | What listens; phones and other devices; outbound access |
| [Untrusted content](security/untrusted-content.md) | Prompt injection: email screening, isolated workers, what isn't screened |
| [Connected apps](security/connected-apps.md) | Tool servers, Google and Microsoft sign-in, sending email, the agents' browser |
| [Accounts and access](security/accounts-and-access.md) | Sign-in, sessions, tokens, what a project or account separates |
| [Supply chain](security/supply-chain.md) | Dependency pinning, signing, updates, the skill library |
| [Audit](security/audit.md) | What's recorded, where, for how long, how to read it |
| [Known limits](security/limits.md) | Every known limit, its consequence and what to do |
| [FAQ](security/faq.md) | The hard questions, answered |

Before reporting a vulnerability, read [`SECURITY.md`](../SECURITY.md).

## Features and internals

| Page | What it covers |
|---|---|
| [Protected commands on Linux](linux-sandbox.md) | The Linux shell sandbox in detail |
| [Browser CLI runtime](browser-cli-runtime.md) | The agents' browser runtime |
| [The skill library](skill-library.md) | Shared skills: finding, installing and updating them |
| [Coordination models](coordination-models/INDEX.md) | How a project's way of working is defined; [results](coordination-models/RESULTS.md) |
| [Conductor API and instruction setup](conductor-api-design.md) | How the CORSIA agent and project conductors change settings and set up agents |
| [Measurement](measurement.md) | The Measurement screen: finished work, your time, model cost |
| [Observability: product events](observability.md) | The product events CORSIA records locally |
| [Importing size ratings](size-rating-import.md) | Importing task size ratings |
| [Client metadata](client-metadata/README.md) | The public file a sign-in provider reads when you connect a hosted tool server |

## About this project

| Page | What it covers |
|---|---|
| [README evidence and limits](readme-evidence.md) | Where the README's numbers come from |
| [LLM coding agent anti-patterns](standards/llm-anti-patterns.md) | Failure modes the code reviewer checks for |
