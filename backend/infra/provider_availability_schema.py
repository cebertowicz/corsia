"""Provider availability table DDL, split out of ``infra.schema``."""

PROVIDER_AVAILABILITY_TABLE_SQL = """
    -- An availability observation is a dated claim about a provider that
    -- carries its own death. A row that cannot expire is UNWRITABLE, and
    -- expiry is evaluated by the READER: there is no refresher, no
    -- revalidator, and no write on the read path. An observation whose exit
    -- depends on another component running is exitless whenever that component
    -- is absent, crashed, or unconfigured — which is exactly what happened.
    CREATE TABLE IF NOT EXISTS provider_availability (
        harness TEXT NOT NULL CHECK (length(trim(harness)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        account_scope TEXT NOT NULL CHECK (length(trim(account_scope)) > 0),
        -- 'available' is deleted. A positive latch is the same bug mirrored,
        -- and absence of a live row already means "no observed block".
        status TEXT NOT NULL
            CHECK (status IN ('transient_backoff','quota_exhausted')),
        observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
        reset_at TEXT CHECK (reset_at IS NULL OR length(trim(reset_at)) > 0),
        source TEXT NOT NULL CHECK (length(trim(source)) > 0),
        -- Rank so a transport guess cannot erase live provider evidence.
        -- provider_contract* = 2, transport:* = 1.
        evidence_rank INTEGER NOT NULL CHECK (evidence_rank IN (1, 2)),
        evidence_json TEXT NOT NULL
            CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
        -- Renamed from refresh_not_before, and the rename is load-bearing:
        -- "refresh_not_before" semantically licenses extension, which is how
        -- the read path legally pushed the block forward forever. "expires_at"
        -- means after this the row is not evidence, and nothing may extend it.
        expires_at TEXT NOT NULL CHECK (length(trim(expires_at)) > 0),
        updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
        PRIMARY KEY (harness, provider, account_scope),
        CHECK (
            (status='quota_exhausted' AND reset_at IS NOT NULL)
            OR (status!='quota_exhausted' AND reset_at IS NULL)
        ),
        CHECK (expires_at > observed_at),
        -- Bounded horizon per status: 6h quota, 15min transient.
        CHECK (
            julianday(expires_at) - julianday(observed_at)
            <= CASE status WHEN 'quota_exhausted' THEN 0.25 ELSE 0.0104166 END
        ),
        CHECK (status <> 'quota_exhausted' OR expires_at <= reset_at),
        CHECK (evidence_rank <> 2 OR source LIKE 'provider_contract%')
    )
"""
