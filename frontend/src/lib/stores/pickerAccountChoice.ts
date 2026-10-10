import { bindConductorFromPicker } from '$lib/conductorSourcesApi';
import { pinThreadAccount, type PinResult } from '$lib/stores/threadAccountPin';
import { type PickerAccount } from '$lib/stores/accountPicker';

/**
 * The model picker's one account-choice path. The server remains the source of account selection:
 * this calls the established conductor bind or thread pin path, never writes picker state itself.
 */
export async function choosePickerAccount(
	threadId: string | null,
	activeModelId: string | null,
	workspaceConductorId: string | null,
	row: PickerAccount,
): Promise<PinResult> {
	if (!threadId || row.marked || !row.choosable) return { ok: true, changed: false };
	if (threadId === workspaceConductorId && !activeModelId) {
		const result = await bindConductorFromPicker(threadId, row.account.id);
		return result.ok ? { ok: true, changed: true } : result;
	}
	return pinThreadAccount(threadId, row.account.id);
}
