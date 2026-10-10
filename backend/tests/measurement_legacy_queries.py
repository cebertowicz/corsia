"""Frozen SQL oracle from pre-optimization commit 3caea88e^; never served.

Retained only to prove S1 changes preserve the original aggregate semantics.
"""
from typing import Any


def _scope_cte() -> str:
    return """
        WITH RECURSIVE scope_roots(id) AS (
          SELECT t.id FROM threads t
          WHERE t.user_id=:user_id
            AND ((:scope_kind IN ('all','project') AND t.parent_thread_id IS NULL)
                 OR (:scope_kind='agent' AND t.id=:scope_id))
            AND (:scope_kind='all'
                 OR (:scope_kind='project' AND t.project_id=:scope_id)
                 OR (:scope_kind='agent' AND t.id=:scope_id
                     AND COALESCE(t.agent_enabled,0)=1
                     AND COALESCE(t.agent_status,'active')='active'))
        ), scope_threads(id) AS (
          SELECT id FROM scope_roots
          UNION ALL
          SELECT child.id FROM threads child JOIN scope_threads parent
            ON child.parent_thread_id=parent.id WHERE child.user_id=:user_id
        ), top_plans(id) AS (
          SELECT p.id FROM plans p JOIN scope_roots r ON r.id=p.owner_id
          WHERE p.owner_kind='thread'
        )
    """


def _outcome_ctes() -> str:
    return """
        , settle AS (
          SELECT node_id, MIN(created_at) AS settled_at FROM plan_node_events
          WHERE new_state IN ('accepted','accepted_with_followup') GROUP BY node_id
          HAVING date(substr(MIN(created_at),1,10),'weekday 0','-6 days') BETWEEN :from AND :to
        ), corrections AS (
          SELECT node_id FROM plan_node_events WHERE event='correction_created' GROUP BY node_id
        ), attempts AS (
          SELECT node_id, MAX(attempt_ordinal) AS count FROM plan_node_attempts GROUP BY node_id
        ), outcome_rows AS (
          SELECT n.id, date(substr(s.settled_at,1,10),'weekday 0','-6 days') AS period,
            CASE WHEN COALESCE(a.count,0)<=1 AND NOT EXISTS (
              SELECT 1 FROM plan_node_events r WHERE r.node_id=n.id AND r.event='review_rejected'
            ) AND (n.follow_up_of IS NULL OR NOT EXISTS (
              SELECT 1 FROM plan_nodes original WHERE original.id=n.follow_up_of AND original.state='failed'
            )) THEN 1 ELSE 0 END AS first_try
          FROM plan_nodes n JOIN settle s ON s.node_id=n.id
          LEFT JOIN attempts a ON a.node_id=n.id
          WHERE n.kind='task' AND n.state IN ('accepted','accepted_with_followup')
            AND n.plan_id IN (SELECT id FROM top_plans)
            AND COALESCE(n.assignee,'')<>'user' AND n.id NOT IN (SELECT node_id FROM corrections)
        ), outcome_periods AS (
          SELECT period, COUNT(*) AS outcomes, SUM(first_try) AS first_try FROM outcome_rows GROUP BY period
        ), rejection_periods AS (
          SELECT date(substr(e.created_at,1,10),'weekday 0','-6 days') AS period, COUNT(*) AS rejections
          FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
          WHERE e.event='review_rejected' AND n.plan_id IN (SELECT id FROM top_plans)
            AND date(substr(e.created_at,1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period
        )
    """


def query_legacy_family(db, family: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    scope, outcomes = _scope_cte(), _outcome_ctes()
    if family == "outcomes":
        sql = scope + outcomes + """
          SELECT o.period,o.outcomes,o.first_try,COALESCE(r.rejections,0) AS rejections
          FROM outcome_periods o LEFT JOIN rejection_periods r USING(period) ORDER BY o.period
        """
    elif family == "active":
        sql = scope + outcomes + """
          , human_messages AS (
            SELECT m.id,m.created_at,LAG(m.created_at) OVER (ORDER BY m.created_at,m.id) AS prior_at
            FROM messages m WHERE m.thread_id IN (SELECT id FROM scope_threads)
              AND m.role='user' AND COALESCE(json_extract(m.metadata,'$.sender_kind'),'')=''
              AND (date(substr(m.created_at,1,10),'weekday 0','-6 days') BETWEEN :from AND :to
                   OR m.id=(SELECT prior.id FROM messages prior
                     WHERE prior.thread_id IN (SELECT id FROM scope_threads) AND prior.role='user'
                       AND COALESCE(json_extract(prior.metadata,'$.sender_kind'),'')=''
                       AND date(prior.created_at)<:from
                     ORDER BY prior.created_at DESC,prior.id DESC LIMIT 1))
          ), active_periods AS (
            SELECT date(substr(created_at,1,10),'weekday 0','-6 days') AS period,
              SUM(60 + CASE WHEN prior_at IS NULL THEN 0
                WHEN strftime('%s',created_at)-strftime('%s',prior_at)<=:max_active_gap_seconds
                THEN MAX(0,strftime('%s',created_at)-strftime('%s',prior_at)) ELSE 0 END)/3600.0 AS active_hours
            FROM human_messages WHERE date(substr(created_at,1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period
          ) SELECT a.period,a.active_hours,COALESCE(o.outcomes,0) AS outcomes
            FROM active_periods a LEFT JOIN outcome_periods o USING(period) ORDER BY a.period
        """
    elif family == "presence":
        sql = scope + """
          SELECT date(substr(v.entered_at,1,10),'weekday 0','-6 days') AS period,
            SUM(v.seconds)/3600.0 AS value,COUNT(*) AS n FROM presence_visits v
          WHERE v.user_id=:user_id AND (v.thread_id IN (SELECT id FROM scope_threads)
            OR (:scope_kind='project' AND v.project_id=:scope_id))
            AND date(substr(v.entered_at,1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period ORDER BY period
        """
    elif family == "decisions":
        sql = scope + outcomes + """
          , scoped_decisions AS (
            SELECT ai.metadata FROM artifact_identities ai JOIN threads t
              ON t.id=json_extract(ai.metadata,'$.plan_decision.thread_id')
            WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
              AND t.id IN (SELECT id FROM scope_threads)
          ), ask_periods AS (
            SELECT date(substr(json_extract(metadata,'$.plan_decision.raised_at'),1,10),'weekday 0','-6 days') AS period,
              COUNT(*) AS raised FROM scoped_decisions WHERE json_extract(metadata,'$.plan_decision.raised_at') IS NOT NULL
                AND date(substr(json_extract(metadata,'$.plan_decision.raised_at'),1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period
          ), hold_periods AS (
            SELECT date(substr(h.created_at,1,10),'weekday 0','-6 days') AS period,COUNT(*) AS user_holds
            FROM plan_node_holds h JOIN plan_nodes n ON n.id=h.node_id JOIN plans p ON p.id=n.plan_id
            WHERE h.owner_kind='user' AND p.owner_id IN (SELECT id FROM scope_threads)
              AND date(substr(h.created_at,1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period
          ), answered_periods AS (
            SELECT date(substr(json_extract(metadata,'$.plan_decision.answered_at'),1,10),'weekday 0','-6 days') AS period,
              COUNT(*) AS answered,
              SUM(json_extract(metadata,'$.plan_decision.answer.option_id')=json_extract(metadata,'$.plan_decision.recommendation')) AS recommendation,
              SUM(json_extract(metadata,'$.plan_decision.answer.option_id')='other') AS other
            FROM scoped_decisions WHERE json_extract(metadata,'$.plan_decision.state')='answered'
              AND json_extract(metadata,'$.plan_decision.answered_at') IS NOT NULL
              AND date(substr(json_extract(metadata,'$.plan_decision.answered_at'),1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period
          ) SELECT o.period,o.outcomes,COALESCE(d.raised,0) AS raised,COALESCE(h.user_holds,0) AS user_holds,
              COALESCE(a.answered,0) AS answered,COALESCE(a.recommendation,0) AS recommendation,COALESCE(a.other,0) AS other
            FROM outcome_periods o LEFT JOIN ask_periods d USING(period) LEFT JOIN hold_periods h USING(period)
              LEFT JOIN answered_periods a USING(period) ORDER BY o.period
        """
    elif family == "tokens":
        sql = scope + outcomes + """
          , token_periods AS (
            SELECT date(substr(u.created_at,1,10),'weekday 0','-6 days') AS period,
              COALESCE(cm.harness,'unclassified') AS harness,SUM(u.total_tokens) AS tokens
            FROM llm_usage u LEFT JOIN context_manifests cm ON cm.id=u.context_manifest_id
            WHERE u.thread_id IN (SELECT id FROM scope_threads)
              AND date(substr(u.created_at,1,10),'weekday 0','-6 days') BETWEEN :from AND :to GROUP BY period,harness
          ) SELECT t.period,t.harness,t.tokens,COALESCE(o.outcomes,0) AS outcomes
            FROM token_periods t LEFT JOIN outcome_periods o USING(period) ORDER BY t.period,t.harness
        """
    else:
        return []
    return [dict(row) for row in db.execute(sql, params).fetchall()]
