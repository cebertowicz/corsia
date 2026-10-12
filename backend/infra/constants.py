"""Centralized constants."""

DEFAULT_MEMORY_WINDOW = 250
HISTORY_LOAD_FRACTION = 0.6
AVG_TOKENS_QA = 150
AVG_TOKENS_TOOL_HEAVY = 600
TOOL_HEAVY_THRESHOLD = 0.4
WINDOW_MIN = 50
WINDOW_MAX = 600

PRUNE_PROTECT_COUNT = 15
PRUNE_MIN_SIZE = 200
PRUNE_CAP_DEFAULT = 300

GREP_OUTPUT_CHAR_CAP = 20_000
READ_FILE_OUTPUT_CHAR_CAP = 30_000

LLM_MAX_RETRIES = 3
LLM_RETRY_DELAYS = [2, 5, 10]
# Longest one streamed provider call may send nothing before it is ended. The
# transport read timeout is per socket read, so heartbeat bytes defeat it.
LLM_STREAM_IDLE_TIMEOUT_S = 600.0

NIGHTLY_HOUR = 2
NIGHTLY_IDLE_THRESHOLD_HOURS = 4
NIGHTLY_CHECK_INTERVAL = 300
NIGHTLY_STARTUP_DELAY = 120

# Thread lifecycle & retention (tunable operating config; all in days).
# active --idle--> dormant --dormant--> archived --archived--> purged(dir-deleted)
LIFECYCLE_DORMANT_AFTER_DAYS = 14   # active -> dormant (no activity)
LIFECYCLE_ARCHIVE_AFTER_DAYS = 30   # dormant -> archived (days spent dormant)
LIFECYCLE_PURGE_AFTER_DAYS = 180    # archived -> dir-deleted (days spent archived)
LIFECYCLE_TOOL_TRIM_AFTER_DAYS = 30  # prune verbose tool-call payloads
LIFECYCLE_WORKING_TTL_DAYS = 7      # working/ tmp trim
LIFECYCLE_SETTLED_WORKER_WORKING_TTL_DAYS = 1  # settled worker working/ trim
