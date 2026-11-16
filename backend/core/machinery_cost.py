"""Names and catalog categories for the machinery-cost facts."""
from typing import get_args

from tools.catalog_entry import ToolCategory

DAY_EVENT = "measurement.machinery.day"
RECORD_EVENT = "measurement.machinery.thread_day"
CATEGORIES = frozenset(get_args(ToolCategory))
BUCKETS = CATEGORIES | {"connected_app", "ungrouped"}
STEMS = ("plan", "memory", "agents", "skill_read", "machinery")
SUFFIXES = ("token_pct", "tokens_per_outcome", "action_pct", "returned_pct")
METRICS = frozenset(f"{stem}_{suffix}" for stem in STEMS for suffix in SUFFIXES) | {"ungrouped_action_pct"}
