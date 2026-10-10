"""Pure normalization of one full Codex plan-meter read; no stores or effects."""
from __future__ import annotations

from connections.usage_snapshots import CODEX_DEFAULT_LIMIT, codex_windows, reset_from_snapshot


def normalize_rate_limits_read(result: object) -> dict:
    if not isinstance(result, dict):
        return {'check_error': 'Codex returned no usage verdict'}
    snapshot = result.get('rateLimits')
    if not isinstance(snapshot, dict):
        return {'check_error': 'Codex returned no usage snapshot'}
    windows = codex_windows(snapshot, source='discovery')
    for limit_id, bucket in (result.get('rateLimitsByLimitId') or {}).items():
        windows.extend(codex_windows(bucket, limit_id=limit_id, source='discovery'))
    facts = {key: snapshot.get(key) for key in ('planType', 'credits', 'individualLimit', 'spendControlReached')}
    facts['limitId'] = snapshot.get('limitId') or CODEX_DEFAULT_LIMIT
    allowed, reached = result.get('ordinaryUsageAllowed'), snapshot.get('rateLimitReachedType')
    observation = {'windows': windows, 'facts': facts}
    if allowed is True and reached:
        observation['check_error'] = 'Codex returned conflicting usage verdicts'
    elif allowed is False or reached:
        observation['verdict'] = {'state': 'limited', 'reason': str(reached or 'usage_limit_reached'),
                                  'resets_at': reset_from_snapshot(snapshot), 'source': 'discovery'}
    elif allowed is True:
        observation['verdict'] = {'state': 'allowed', 'source': 'discovery'}
    else:
        observation['check_error'] = 'Codex returned no usage verdict'
    return observation


#: Codex reasons that say the paid capacity itself is gone.
_CREDITS_DEPLETED = '_credits_depleted'
#: Verdict sources that report the plan meter; a turn's own rejection is never overridden.
_PLAN_METER_SOURCES = ('discovery', 'rate_limit_update')


def paid_capacity(usage: dict, verdict: dict) -> bool:
    """Whether credits still cover this plan-exhaustion verdict (``AccountProfile.paid_capacity``).

    Codex reports ``ordinaryUsageAllowed=false`` for included usage only: credits that remain
    and a spend control not reached leave paid usage open.
    """
    if verdict.get('source') not in _PLAN_METER_SOURCES or str(verdict.get('reason') or '').endswith(_CREDITS_DEPLETED):
        return False
    facts = usage.get('facts')
    facts = facts if isinstance(facts, dict) else {}
    credits = facts.get('credits')
    return (facts.get('spendControlReached') is not True and isinstance(credits, dict)
            and (credits.get('hasCredits') is True or credits.get('unlimited') is True))
