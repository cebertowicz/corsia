import type { FailureObservation } from './documentFailures';

export interface HeldDocument {
	corsia_text: string;
	file_text: string;
	candidate_text: string;
	choice_pending: boolean;
}

/** The capture's selection and the browser's state, which the server tracks under the comment's id only if it is the live one. */
export interface PositionReservation {
	client_id: string;
	from_off: number;
	to_off: number;
	vector: string;
	digest: string;
	text_length_utf16: number;
}

export interface DocumentFrame extends FailureObservation {
	type: string;
	live?: boolean;
	live_unavailable?: string;
	writable?: boolean;
	artifact_id?: string;
	store_id?: string;
	request_id?: string;
	update?: string;
	author?: { thread_id: string; name: string; call_id?: string };
	vector?: string;
	digest?: string;
	message_ids?: string[];
	server_saved?: boolean;
	held?: HeldDocument | null;
	pending?: boolean;
	blocking?: boolean | FailureObservation[];
	failure_clears?: FailureObservation[];
	kind?: string;
	code?: string;
	title?: string;
	message?: string;
	seq?: number;
	reserved?: boolean;
	ranges?: { id: string; from_off: number; to_off: number }[];
}

/** A comment's own id, minted at capture so the live range and the posted comment share it. */
export function newClientId(): string {
	if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
	return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function encodeUpdate(bytes: Uint8Array): string {
	const chunks: string[] = [];
	for (let i = 0; i < bytes.length; i += 16384) chunks.push(String.fromCharCode(...bytes.subarray(i, i + 16384)));
	return btoa(chunks.join(''));
}

export function decodeUpdate(value: string): Uint8Array {
	return Uint8Array.from(atob(value), c => c.charCodeAt(0));
}

/** The server's wire digest: the first 16 lowercase hex characters of SHA-256 over UTF-8. */
export async function textDigest(text: string): Promise<string> {
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
	return Array.from(new Uint8Array(digest).subarray(0, 8), byte => byte.toString(16).padStart(2, '0')).join('');
}
