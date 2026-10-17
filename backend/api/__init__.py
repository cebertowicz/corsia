"""Route modules — each exports an APIRouter."""

from api.auth import router as auth_router
from api.setup import router as setup_router
from api.threads import router as threads_router
from api.thread_mutations import router as thread_mutations_router
from api.thread_messages import router as thread_messages_router
from api.thread_runtime import router as thread_runtime_router
from api.models import router as models_router
from api.agents import router as agents_router
from api.system import router as system_router
from api.uploads import router as uploads_router
from api.turns import router as turns_router
from api.drain import router as drain_router
from api.costs import router as costs_router
from api.measurement import router as measurement_router
from api.measurement_where_to_look import router as measurement_where_to_look_router
from api.mcp import router as mcp_router
from api.projects import router as projects_router
from api.project_repos import router as project_repos_router
from api.fs_browse import router as fs_browse_router
from api.files import router as files_router
from api.files_revision import router as files_revision_router
from api.doc_comments import router as doc_comments_router
from api.doc_comment_thread_views import router as doc_comment_thread_views_router
from api.provider_sources import router as provider_sources_router
from api.provider_cli_link import router as provider_cli_link_router
from api.providers import router as providers_router
from api.provider_policy import router as provider_policy_router
from api.vendor_accounts import router as vendor_accounts_router
from api.discovered_models import router as discovered_models_router
from api.network_servers import router as network_servers_router
from api.version import router as version_router
from api.devices import router as devices_router
from api.webhooks import router as webhooks_router
from api.cc_attach import router as cc_attach_router
from api.conductor import router as conductor_router
from api.corsia_agent import router as corsia_agent_router
from api.context_bootloader import router as context_bootloader_router
from api.delegation import router as delegation_router
from api.delegation_docs import router as delegation_docs_router
from api.delegation_model import router as delegation_model_router
from api.skills import router as skills_router
from api.host_access import router as host_access_router
from api.audit_reads import router as audit_reads_router
from api.browser import router as browser_router
from api.plans import router as plans_router
from api.plans_folded import router as plans_folded_router

__all__ = [
    "auth_router",
    "setup_router",
    "threads_router",
    "thread_mutations_router",
    "thread_messages_router",
    "thread_runtime_router",
    "models_router",
    "system_router",
    "uploads_router",
    "turns_router",
    "drain_router",
    "costs_router",
    "measurement_router",
    "measurement_where_to_look_router",
    "mcp_router",
    "projects_router",
    "project_repos_router",
    "fs_browse_router",
    "files_router",
    "files_revision_router",
    "doc_comments_router",
    "doc_comment_thread_views_router",
    "provider_sources_router",
    "provider_cli_link_router",
    "providers_router",
    "provider_policy_router",
    "vendor_accounts_router",
    "discovered_models_router",
    "network_servers_router",
    "version_router",
    "devices_router",
    "webhooks_router",
    "cc_attach_router",
    "conductor_router",
    "corsia_agent_router",
    "delegation_router",
    "delegation_docs_router",
    "delegation_model_router",
    "skills_router",
    "host_access_router",
    "audit_reads_router",
    "browser_router",
    "plans_router",
    "plans_folded_router",
    "agents_router",
]
