import { modelMatchesId, type Model } from '$lib/stores/models';
import { pinThreadAccount } from '$lib/stores/threadAccountPin';
import type { Thread } from '$lib/stores/threads';

export function buildNewThreadBody({
	projectId,
	reasoningEffort,
	modelId,
	accountId,
}: {
	projectId?: string | null;
	reasoningEffort?: string | null;
	modelId?: string | null;
	accountId?: string | null;
}): Record<string, unknown> {
	const body: Record<string, unknown> = { title: 'New Chat' };
	if (projectId) body.project_id = projectId;
	if (modelId) body.model = modelId;
	if (accountId) body.account_id = accountId;
	if (reasoningEffort !== undefined && reasoningEffort !== null) {
		body.reasoning_effort = reasoningEffort;
	}
	return body;
}

/** Snapshot the draft's account choice against the model being sent. */
export function prepareDraftThread(projectId: string | null | undefined, reasoningEffort: string | null, modelId: string | null, models: Model[], choices: Record<string, string>) {
	const model = models.find((entry) => modelMatchesId(entry, modelId));
	const vendor = model?.connection ?? model?.provider ?? model?.backend;
	return { vendor, body: buildNewThreadBody({ projectId, reasoningEffort, modelId, accountId: vendor ? choices[vendor] : undefined }) };
}

/** Preserve choices for other vendors through the existing account pin path. */
export async function pinOtherDraftAccounts(threadId: string, vendor: string | undefined, choices: Record<string, string>, showError: (message: string) => void) {
	for (const [choiceVendor, accountId] of Object.entries(choices)) {
		if (choiceVendor === vendor) continue;
		const result = await pinThreadAccount(threadId, accountId);
		if (!result.ok) showError(result.message);
	}
}

/** Construct the local thread row after creation succeeds. */
export function createdThreadRow(id: string, userId: string, projectId: string | null | undefined, model: string | null, effort: string | null): Thread {
	const now = new Date().toISOString();
	return { id, user_id: userId, profile: 'personal', title: 'New Chat', model, project_id: projectId ?? undefined, reasoning_effort: effort, created_at: now, updated_at: now };
}
