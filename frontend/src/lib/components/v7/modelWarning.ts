import { modelAvailability, type Model } from '$lib/stores/models';

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
	external: boolean,
	providerLabel?: string,
): string | null {
	if (!dbId) return null;
	const row = catalog.find((m) => m.id === dbId);
	const turn = row && modelAvailability(row);
	if (!row || turn?.status !== 'unavailable') return null;
	switch (turn.status_reason) {
		case 'missing_context_window':
			return external
				? 'Not shown in the model list: set its context window in this model’s details.'
				: 'Not shown in the model list: its context window is unknown.';
		case 'connection_unavailable':
			return `Not shown in the model list: ${providerLabel || 'its app'} isn’t connected.`;
		case 'usage_limited':
			return null;
		default:
			return 'Not shown in the model list: this model is unavailable right now.';
	}
}
