const SESSION_SWITCH_FIELDS = ['pending_model', 'switch_status', 'switch_error', 'switch_operation_id'] as const;

export function threadSwitchFields(data: any) {
	return Object.fromEntries(SESSION_SWITCH_FIELDS
		.filter(field => data[field] !== undefined).map(field => [field, data[field]]));
}
