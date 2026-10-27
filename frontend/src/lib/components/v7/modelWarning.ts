import { modelAvailability, type Model } from '$lib/stores/models';

export function canEditCheckpointWindow(external: boolean, labelEditable: boolean, providerName: string): boolean {
	return external || labelEditable || providerName === 'omlx';
}

/**
 * Why an enabled provider model is missing from the chat model picker.
 *
 * The picker lists only catalog rows whose availability is `ready`. Settings
 * deliberately shows an account's `usage_limited` state in its account row,
 * while the picker omits that unavailable model.
 * Returns null when the model is selectable or not in the catalog at all, and
 * when the only block is the selected account's usage limit: account usage
 * belongs to the account, and a model row says only whether the model is available.
 */
export function chatPickerWarning(
	catalog: Model[],
	dbId: string | undefined,
	canEditWindow: boolean,
	providerLabel?: string,
): string | null {
	if (!dbId) return null;
	const row = catalog.find((m) => m.id === dbId);
	const turn = row && modelAvailability(row);
	if (!row || turn?.status !== 'unavailable') return null;
	switch (turn.status_reason) {
		case 'missing_context_window':
			return canEditWindow
				? 'Can’t be used in chats: set its context window in this model’s details.'
				: 'Can’t be used in chats: its context window is unknown.';
		case 'connection_unavailable':
			return `Can’t be used in chats: ${providerLabel || 'its provider'} isn’t connected.`;
		case 'usage_limited':
			return null;
		default:
			return 'Can’t be used in chats right now.';
	}
}
