const SESSION_SWITCH_FIELDS = ['pending_model', 'switch_status', 'switch_error', 'switch_operation_id'] as const;

export function threadSwitchFields(data: any) {
	return Object.fromEntries(SESSION_SWITCH_FIELDS
		.filter(field => data[field] !== undefined).map(field => [field, data[field]]));
}

const SESSION_AGENT_FIELDS = [
	'agent_enabled', 'agent_responsibility', 'agent_paths', 'agent_domains', 'agent_status', 'agent_notes',
] as const;

/** Roster fields a roster change carries, so a promote or demote moves the cached row without a reload. */
export function threadAgentFields(data: any) {
	return Object.fromEntries(SESSION_AGENT_FIELDS
		.filter(field => data[field] !== undefined).map(field => [field, data[field]]));
}
