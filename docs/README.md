# Documentation

The main pages under `docs/`, by topic. Start with the repository [README](../README.md) for what CORSIA is and how to install it.

## Security

What agents can reach, what stops them, how strong each control is, and where it doesn't hold. Start with the [security model](security/README.md), which lists every security page, then [Known limits](security/limits.md).

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
