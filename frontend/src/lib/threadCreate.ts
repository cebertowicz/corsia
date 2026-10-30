import type { Thread } from '$lib/stores/threads';
import { modelMatchesId, type Model } from '$lib/stores/models';
import { reasonOf } from '$lib/stores/threadAccountPin';
import { CREATE_FAILED } from '$lib/messageDeliveryCopy';

export function draftAccountForModel(accountId: string | null, modelId: string | null, models: Model[]): string | null {
	const model = models.find((row) => modelMatchesId(row, modelId));
	return accountId && model?.runs_on?.some((entry) => entry.account_id === accountId) ? accountId : null;
}

/** A coded 400 to a create that named an account refuses that account: reload so its picker row shows why, and say so. */
export async function reloadOnAccountRefusal(status: number, detail: unknown, reload: () => Promise<void>): Promise<boolean> {
	if (status !== 400 || !reasonOf(detail)) return false;
	await reload();
	return true;
}

/** Any other failed create: the generic sentence, plus the server's reference when it logged one. */
export function createFailedMessage(reference: unknown): string {
	return typeof reference === 'string' && reference ? `${CREATE_FAILED} Reference: ${reference}` : CREATE_FAILED;
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
