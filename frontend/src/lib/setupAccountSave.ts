import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

export type AccountChanges = { name?: string; email?: string; password?: string; usage_sharing?: boolean };

/** A failed account save's line, with the reference its log entry carries when the server gave one. */
export function withReference(line: string, result: { ref?: unknown } | null): string {
	return typeof result?.ref === 'string' && result.ref ? `${line} Reference: ${result.ref}` : line;
}

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
