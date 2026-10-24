/** Frames forwarded unchanged to their `on(type)` subscribers, which own all handling. */
export const FORWARDED_TYPES = new Set([
	'artifacts_changed', 'projects_changed', 'thread_updates_changed', 'doc_activity', 'doc_comment_changed', 'document_open_request',
	'doc_opened', 'doc_update', 'doc_ack', 'doc_received', 'doc_saved', 'doc_error', 'doc_cleared', 'doc_positions', 'doc_pending',
	'browser_opened', 'devices_updated', 'connected_apps_updated',
]);
