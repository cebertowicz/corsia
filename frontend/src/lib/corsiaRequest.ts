import { writable } from 'svelte/store';

export interface CorsiaMessageRequest {
	id: string;
	text: string;
}

export const pendingCorsiaMessage = writable<CorsiaMessageRequest | null>(null);

/** Ask the home route to open the workspace CORSIA conversation and send this text once. */
export function requestCorsiaMessage(text: string): string {
	const id = crypto.randomUUID();
	pendingCorsiaMessage.set({ id, text });
	return id;
}

/** Take one request only when its active delivery still owns it. */
export function takeCorsiaMessage(id: string): CorsiaMessageRequest | null {
	let taken: CorsiaMessageRequest | null = null;
	pendingCorsiaMessage.update((current) => {
		if (!current || current.id !== id) return current;
		taken = current;
		return null;
	});
	return taken;
}
