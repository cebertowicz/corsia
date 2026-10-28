import type { Thread } from '$lib/stores/threads';
import { modelMatchesId, type Model } from '$lib/stores/models';
import { ACCOUNT_LIMITED, ACCOUNT_SIGNED_OUT, groupKey } from '$lib/stores/modelGroups';
import type { VendorAccount } from '$lib/stores/vendorAccounts';
import { accountRefusalMessage, reasonOf } from '$lib/stores/threadAccountPin';
import { ACCOUNT_PROJECT_DENIED } from '$lib/messageDeliveryCopy';

export function draftAccountForModel(accountId: string | null, modelId: string | null, models: Model[], accounts: VendorAccount[]): string | null {
	const model = models.find((row) => modelMatchesId(row, modelId));
	const account = accounts.find((row) => row.id === accountId);
	return model && account && groupKey(model) === account.provider ? account.id : null;
}

export function createAccountRefusal(status: number, detail: unknown, clearChoice: () => void): string | null {
	const reason = reasonOf(detail);
	if (status !== 400 || !reason || !(reason.startsWith('account_') || reason === 'usage_limited' || reason === 'project_policy_missing')) return null;
	clearChoice();
	if (reason === 'account_signed_out') return ACCOUNT_SIGNED_OUT;
	if (reason === 'usage_limited') return ACCOUNT_LIMITED;
	if (reason === 'account_project_denied') return ACCOUNT_PROJECT_DENIED;
	return accountRefusalMessage(detail);
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
