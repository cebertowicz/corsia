"""Shipped GitHub pull request observation rules."""
from events.connectors import ShippedObserveRuleDecl
from events.identity import PR_TYPE

GITHUB_OBSERVE_RULES = (
    ShippedObserveRuleDecl(
        id="ruleset_github_pr_create", tool="bash", argv_prefix=("gh", "pr", "create"), item_type=PR_TYPE,
    ),
    ShippedObserveRuleDecl(
        id="ruleset_github_mcp_pr_create", tool="create_pull_request", argv_prefix=(), item_type=PR_TYPE,
    ),
)
