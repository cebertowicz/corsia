"""Core domain logic that isn't already in a dedicated package.

Turn execution, context building, agent singleton. Depends on infra; is
depended on by jobs and api. Shouldn't import from jobs, api, or
the per-domain packages (memory, llm, etc.) except at call sites where
domain-specific work is explicitly being delegated.
"""
