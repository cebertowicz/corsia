"""Names and closed catalog categories for the machinery-cost facts."""

DAY_EVENT = "measurement.machinery.day"
RECORD_EVENT = "measurement.machinery.thread_day"
CATEGORIES = frozenset({
    "artifacts", "automations", "browser", "coordination", "delegation", "discovery",
    "documents", "edit", "email", "filesystem", "google_workspace", "history", "host_access",
    "memory", "messages", "microsoft", "microsoft_calendar", "microsoft_files", "microsoft_mail",
    "plans", "processes", "projects", "shell", "web",
})
BUCKETS = CATEGORIES | {"connected_app", "ungrouped"}
STEMS = ("plan", "memory", "agents", "skill_read", "machinery")
SUFFIXES = ("token_pct", "tokens_per_outcome", "action_pct", "returned_pct")
METRICS = frozenset(f"{stem}_{suffix}" for stem in STEMS for suffix in SUFFIXES) | {"ungrouped_action_pct"}
