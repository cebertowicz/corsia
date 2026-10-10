"""The one token measure every Measurement read shares: fresh tokens, their unknowns and cache re-reads."""

# One home for the measurement side's token sum. The tokens family, its root-grain
# twin, the Models share and the per-agent column all read usage through this
# expression, so a correction to what a token counts lands in one edit that every
# measurement surface picks up at once instead of drifting between them.
#
# The one served token measure is FRESH tokens: what this scope actually made, with
# cache re-reads excluded. ``total_tokens`` is 96-98% cache re-reads in production,
# which tracks how many times an agent re-read its own context rather than how much
# work it delivered, so serving the total measured the number of calls.
#
# Two shapes, both measured against production rather than assumed:
#
# * ``total_tokens`` present -- fresh is the total minus its cache re-reads.
# * ``total_tokens`` NULL (22,009 of 176,714 unclassified rows since 2026-08-10,
#   18,726 of them in the 09-14 week) -- the total is rebuilt from its parts and the
#   cache re-reads subtracted again. The rebuild is the measured shape of a total,
#   not a guess: for every harness present, ``total_tokens`` equals prompt +
#   completion + cache_creation + cache_read (codex 160,244 of 160,244 non-NULL
#   rows, claude-code 89,285 of 89,285, native 34,126 of 34,168, unclassified
#   149,714 of 154,705), so a harness's prompt count does NOT already carry cache
#   reads and the four parts, not three, are the whole total. Subtracting the same
#   cache_read back out leaves prompt + completion + cache_creation -- never
#   negative, per row and per period.
#
# Nothing here clamps a value to hide one. Production has zero rows whose
# ``total_tokens - cache_read_tokens`` is negative, and 17,476 NULL-total rows whose
# cache re-reads exceed their other parts; both counts are reported, not corrected.
FRESH_TOKEN_SQL = (
    "CASE WHEN u.total_tokens IS NOT NULL "
    "THEN u.total_tokens - COALESCE(u.cache_read_tokens, 0) ELSE "
    "COALESCE(u.prompt_tokens, 0) + COALESCE(u.completion_tokens, 0) + "
    "COALESCE(u.cache_creation_tokens, 0) END"
)
# 1 when ``FRESH_TOKEN_SQL`` read a missing part as 0, so the row's fresh tokens are a
# lower bound. Cost's ``tokens_unreported`` is a pricing rule (it also needs cache
# re-reads) and answers a different question.
FRESH_TOKEN_UNKNOWN_SQL = (
    "CASE WHEN u.total_tokens IS NULL AND (u.prompt_tokens IS NULL OR "
    "u.completion_tokens IS NULL OR u.cache_creation_tokens IS NULL) THEN 1 ELSE 0 END"
)
# ``api.usage_rollup`` groups rows by hour with ``FRESH_TOKEN_SQL``; every reader sums those.
ROLLUP_TOKEN_SUM_SQL = "SUM(u.fresh_tokens)"
# The cache re-reads the fresh measure leaves out, read from the same rows so the two
# figures always describe one set of calls. A row that recorded none counts 0, so the
# figure is a lower bound exactly where ``FRESH_TOKEN_UNKNOWN_SQL`` says the row's fresh
# tokens are.
CACHED_TOKEN_SQL = "COALESCE(u.cache_read_tokens, 0)"
# Key-figure token lines as (name, value, unknown-row) SQL. The total is fresh plus cache re-reads; input is what it
# leaves after the three recorded parts, so the four lines sum to the total on every row and input is unknown where any is.
_KEY_PARTS = {"cache_write": "cache_creation_tokens", "cache_read": "cache_read_tokens", "output": "completion_tokens"}
_KEY_TOTAL = f"({FRESH_TOKEN_SQL} + {CACHED_TOKEN_SQL})"
_KEY_INPUT = "".join([f"({_KEY_TOTAL}", *(f" - COALESCE(u.{column}, 0)" for column in _KEY_PARTS.values()), ")"])
_KEY_INPUT_UNKNOWN = "".join([f"CASE WHEN ({FRESH_TOKEN_UNKNOWN_SQL}) = 1", *(f" OR u.{column} IS NULL" for column in _KEY_PARTS.values()), " THEN 1 ELSE 0 END"])
TOKEN_FIGURE_SQL = (("total", _KEY_TOTAL, FRESH_TOKEN_UNKNOWN_SQL), ("input", _KEY_INPUT, _KEY_INPUT_UNKNOWN), *(
    (name, f"COALESCE(u.{column}, 0)", f"CASE WHEN u.{column} IS NULL THEN 1 ELSE 0 END") for name, column in _KEY_PARTS.items()))
