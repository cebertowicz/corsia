import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { updateSettings } from '$lib/stores/settings';

export const USAGE_SHARING_LINE = 'Share usage counts to help improve CORSIA. Counts only, with a random install ID: no name, email or content.';
export const USAGE_SHARING_ERROR = 'Couldn’t save your usage sharing choice. Try again.';

/** Writes the choice through the settings route the Settings page uses, with the session already in the token store. */
export async function saveUsageSharing(next: boolean): Promise<boolean> {
	return (await updateSettings({ usage_sharing: next })).ok;
}

export type AccountChanges = { name?: string; email?: string; password?: string };

/** Saves what changed on the account setup made. An email has nowhere to go until the profile exists, and the name on screen is what builds it. */
export async function putSetupAccount(changes: AccountChanges, name: string): Promise<{ response: Response; result: { code?: string; detail?: unknown; ref?: string; token?: string; user?: { id: string; name: string } } | null }> {
	const send = () => fetch('/api/setup/account', { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${get(token)}` }, body: JSON.stringify(changes) });
	let response = await send();
	let result = await response.json().catch(() => null);
	if (response.status === 422 && result?.code === 'profile_missing' && changes.name === undefined) {
		changes.name = name;
		response = await send();
		result = await response.json().catch(() => null);
	}
	return { response, result };
}
