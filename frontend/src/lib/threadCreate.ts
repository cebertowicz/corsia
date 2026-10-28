import type { Thread } from '$lib/stores/threads';
import { modelMatchesId, type Model } from '$lib/stores/models';
import { reasonOf } from '$lib/stores/threadAccountPin';

export function draftAccountForModel(accountId: string | null, modelId: string | null, models: Model[]): string | null {
	const model = models.find((row) => modelMatchesId(row, modelId));
	return accountId && model?.runs_on?.some((entry) => entry.selected && entry.account_id === accountId) ? accountId : null;
}

export async function createAccountRefusal(status: number, detail: unknown, clear: () => void, reload: () => Promise<void>): Promise<boolean> {
	const reason = reasonOf(detail);
	if (status !== 400 || !reason || !(reason.startsWith('account_') || reason === 'usage_limited' || reason === 'project_policy_missing')) return false;
	if (reason === 'account_removed') clear();
	await reload();
	return true;
}

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

/** Construct the local thread row after creation succeeds. */
export function createdThreadRow(id: string, userId: string, projectId: string | null | undefined, model: string | null, effort: string | null): Thread {
	const now = new Date().toISOString();
	return { id, user_id: userId, profile: 'personal', title: 'New Chat', model, project_id: projectId ?? undefined, reasoning_effort: effort, created_at: now, updated_at: now };
}
