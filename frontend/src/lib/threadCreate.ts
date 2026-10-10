export function buildNewThreadBody({
	projectId,
	reasoningEffort,
	modelId,
}: {
	projectId?: string | null;
	reasoningEffort?: string | null;
	modelId?: string | null;
}): Record<string, unknown> {
	const body: Record<string, unknown> = { title: 'New Chat' };
	if (projectId) body.project_id = projectId;
	if (modelId) body.model = modelId;
	if (reasoningEffort !== undefined && reasoningEffort !== null) {
		body.reasoning_effort = reasoningEffort;
	}
	return body;
}
