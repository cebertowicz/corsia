/**
 * Network holds for the agent-opens-document flows. Each holds one real
 * request or frame at the boundary and later releases it unchanged; nothing
 * here touches application state.
 */
import { expect, type Page, type Route, type WebSocketRoute } from '@playwright/test';
import { recordClientFrame, recordServerFrame, type SocketLog } from './documentOpen';

/**
 * Hold the page's document saves, so an edit is provably still unsaved on
 * the server while a request arrives. `release` lets every held save through.
 */
export async function holdDocumentSaves(page: Page, log: SocketLog) {
	const held: Route[] = [];
	const frames: Array<{ server: WebSocketRoute; payload: string | Buffer }> = [];
	let released = false;
	let updatesHeld = 0;
	await page.route((url) => url.pathname === '/api/files/content', (route) => {
		if (released || route.request().method() !== 'PUT') return route.fallback();
		held.push(route);
	});
	// Install before navigation: live edits save over the page's real socket.
	await page.routeWebSocket((url) => url.pathname === '/ws', (socket) => {
		const server = socket.connectToServer();
		socket.onMessage((payload) => {
			recordClientFrame(log, payload);
			let frame: Record<string, unknown> | null = null;
			try { frame = JSON.parse(String(payload)); } catch { /* Forward non-JSON unchanged. */ }
			if (!released && (frame?.type === 'doc_update' || frame?.type === 'doc_flush')) {
				frames.push({ server, payload });
				if (frame.type === 'doc_update') updatesHeld += 1;
				return;
			}
			server.send(payload);
		});
		server.onMessage((payload) => {
			recordServerFrame(log, payload);
			socket.send(payload);
		});
	});
	return {
		async waitForHeldSave() {
			await expect.poll(() => held.length + updatesHeld, { timeout: 10_000 }).toBeGreaterThan(0);
		},
		async release() {
			released = true;
			for (const { server, payload } of frames.splice(0)) server.send(payload);
			for (const route of held.splice(0)) await route.continue();
		},
	};
}

interface HeldFrame {
	socket: WebSocketRoute;
	payload: string | Buffer;
}

/**
 * Route the page's real socket through the test, forwarding every frame in
 * both directions as it arrives, except the first `document_open_request`
 * matching `match`, which is held until `releaseHeld`. Must be installed
 * before the page connects.
 */
export async function routeSocketHoldingOpenRequest(
	page: Page, log: SocketLog, match: (frame: Record<string, unknown>) => boolean,
) {
	let held: HeldFrame | null = null;
	let delivered = false;
	await page.routeWebSocket((url) => url.pathname === '/ws', (socket) => {
		const server = socket.connectToServer();
		socket.onMessage((payload) => {
			recordClientFrame(log, payload);
			server.send(payload);
		});
		server.onMessage((payload) => {
			const frame = recordServerFrame(log, payload);
			if (!held && frame?.type === 'document_open_request' && match(frame)) {
				held = { socket, payload };
				return;
			}
			socket.send(payload);
		});
	});
	return {
		async waitForHeld() {
			await expect.poll(() => held !== null, { timeout: 10_000 }).toBe(true);
		},
		releaseHeld() {
			if (!held || delivered) throw new Error('No held document_open_request to release');
			delivered = true;
			held.socket.send(held.payload);
		},
	};
}
