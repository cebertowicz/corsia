# Documentation

Every section of the docs, in reading order; each section's first page lists the rest. Start with the repository [README](../README.md) for what CORSIA is and how to install it.

## Contribute

How to go from a fresh clone to a merged change. Start with [Contributing to CORSIA](contributing/README.md), which lists every contributor page.

The rules for issues, the Contributor License Agreement and pull requests are in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## How CORSIA works

How CORSIA is built and why, for engineers deciding whether to adopt it or contribute. Start with the [overview](how-it-works/README.md), which lists every page, then [Architecture](how-it-works/architecture.md).

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

## Reference

| Page | What it covers |
|---|---|
| [`ARCHITECTURE.md`](../ARCHITECTURE.md) | Code map: where each part lives |
| [`BACKEND.md`](../BACKEND.md) · [`FRONTEND.md`](../FRONTEND.md) | Module maps and rules for each side of the code |
| [Data model](reference/data-model/README.md) | Tables and relationships in CORSIA's data stores, generated from the schema code |
| [CLI](reference/cli.md) | Commands and arguments, generated from the CLI code |
| [Configuration](reference/configuration.md) | Environment variables CORSIA reads, generated from the code |
| [Agent tools](reference/agent-tools.md) | Reachable agent tools and how to load them |
| [Glossary](reference/glossary.md) | Terms used throughout the documentation |
| [`CHANGELOG.md`](../CHANGELOG.md) | What has shipped |

## About this project

| Page | What it covers |
|---|---|
| [README evidence and limits](readme-evidence.md) | Where the README's numbers come from |
| [LLM coding agent anti-patterns](standards/llm-anti-patterns.md) | Failure modes the code reviewer checks for |
