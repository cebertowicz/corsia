export let ws: WebSocket | null = null;

export function setWs(v: WebSocket | null) {
	ws = v;
}

export const subscribedThreads: Set<string> = new Set();
