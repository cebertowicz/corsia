import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

/** Tell the server this operation's outcome was shown. The first client to say so wins, and no device shows it again. */
export async function markOutcomeSeen(operationId: string): Promise<void> {
	const authToken = get(token);
	if (!authToken) return;
	try {
		await fetch(`/api/system/update-operations/${encodeURIComponent(operationId)}/outcome-seen`, {
			method: 'POST',
			headers: { Authorization: `Bearer ${authToken}` },
		});
	} catch { /* an unmarked outcome shows once more on the next load, never less than once */ }
}
