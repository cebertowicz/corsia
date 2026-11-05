"""Tables the installed screen reads during project and repository lifecycle tests."""
from infra.continuation_schema import AUTOMATION_SCHEDULES_SQL
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.project_policy_schema import PROJECT_REPO_POLICY_TABLE_SQL

SCREEN_INSTALL_SCHEMA_SQL = EVENT_RULE_SCHEMA_SQL + AUTOMATION_SCHEDULES_SQL + PROJECT_REPO_POLICY_TABLE_SQL
