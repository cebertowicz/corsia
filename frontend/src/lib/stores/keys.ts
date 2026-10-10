import { get } from 'svelte/store';

import { token, user } from '$lib/stores/auth';
import { connectionError } from '$lib/stores/connection';
import { disconnect } from '$lib/ws/connection';

/** Where search reads a key from: Settings › Keys, the settings file, or the environment. */
export type KeySource = 'keys' | 'settings' | 'environment';

/** One entry's metadata. The API never returns a value. */
export interface KeyEntry {
	name: string;
	label: string;
	description: string;
	builtin: boolean;
	added_by: string | null;
	used_by: string | null;
	is_set: boolean;
	source: KeySource | null;
	set_at: string | null;
	checkable: boolean;
}

export type CheckResult = { outcome: 'works' | 'key_refused' | 'failed' | 'not_set'; reason: string | null };

/** What sign-in shows after a key write ended this session. */
export const KEYS_SIGNED_OUT = 'You were signed out after three wrong passwords. Nothing was changed.';
export const KEYS_NOT_SAVED = "Couldn't save that. Nothing was changed.";

export class KeyWriteError extends Error {
	constructor(readonly code: string, message: string) {
		super(message);
	}
}

/** Writes read the session from the header only; the server never reads it from the URL. */
function headers(json = false): HeadersInit {
	return {
		Authorization: `Bearer ${get(token)}`,
		...(json ? { 'Content-Type': 'application/json' } : {}),
	};
}

/** End this browser's session the way an expired one ends, with its own sign-in message. */
function endSignedOutSession(): void {
	disconnect();
	token.set(null);
	user.set(null);
	connectionError.set(KEYS_SIGNED_OUT);
}

async function refusal(res: Response): Promise<KeyWriteError> {
	let code = 'unknown';
	let message = KEYS_NOT_SAVED;
	try {
		const detail = (await res.json())?.detail;
		if (detail && typeof detail.code === 'string') code = detail.code;
		if (detail && typeof detail.message === 'string') message = detail.message;
	} catch {
		// The fixed message stands.
	}
	if (code === 'signed_out') endSignedOutSession();
	return new KeyWriteError(code, message);
}

export async function listKeys(): Promise<KeyEntry[]> {
	const res = await fetch('/api/keys', { headers: headers() });
	if (!res.ok) throw await refusal(res);
	return (await res.json()).keys as KeyEntry[];
}

async function write(path: string, method: 'PUT' | 'POST', body: Record<string, string>): Promise<void> {
	const res = await fetch(path, { method, headers: headers(true), body: JSON.stringify(body) });
	if (!res.ok) throw await refusal(res);
}

export function saveKeyValue(name: string, value: string, password: string): Promise<void> {
	return write(`/api/keys/${encodeURIComponent(name)}/value`, 'PUT', { value, password });
}

export function clearKeyValue(name: string, password: string): Promise<void> {
	return write(`/api/keys/${encodeURIComponent(name)}/clear`, 'POST', { password });
}

export function removeKey(name: string, password: string): Promise<void> {
	return write(`/api/keys/${encodeURIComponent(name)}/remove`, 'POST', { password });
}

export async function checkKey(name: string): Promise<CheckResult> {
	const res = await fetch(`/api/keys/${encodeURIComponent(name)}/check`, { method: 'POST', headers: headers() });
	if (!res.ok) throw await refusal(res);
	return (await res.json()) as CheckResult;
}
