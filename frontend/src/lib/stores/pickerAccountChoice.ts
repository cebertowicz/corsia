import { bindConductorFromPicker } from '$lib/conductorSourcesApi';
import { pinThreadAccount, type PinResult } from '$lib/stores/threadAccountPin';
import { type PickerAccount } from '$lib/stores/accountPicker';
import { draftAccountId } from '$lib/stores/threads';

/**
 * The model picker's one account-choice path. A draft keeps its choice in the composer;
 * existing threads use the established conductor bind or thread pin path.
 */
export async function choosePickerAccount(
	threadId: string | null,
	activeModelId: string | null,
	corsiaAgentId: string | null,
	row: PickerAccount,
): Promise<PinResult> {
	if (row.marked || !row.choosable) return { ok: true, changed: false };
	if (!threadId) {
		draftAccountId.set(row.account.id);
		return { ok: true, changed: true };
	}
	if (threadId === corsiaAgentId && !activeModelId) {
		const result = await bindConductorFromPicker(threadId, row.account.id);
		return result.ok ? { ok: true, changed: true } : result;
	}
	return pinThreadAccount(threadId, row.account.id);
}
