import { expect, type BrowserContext, type Page, type WebSocketRoute } from '@playwright/test';

/** Delay only document-save replies; all writes and acknowledgements still use the real mock-mode backend. */
export async function routeDocumentSaves(context: BrowserContext, delayMs = 0) {
	let delay = delayMs;
	let blocked = false;
	let gate: Promise<void> | undefined;
	let openGate: (() => void) | undefined;
	let starts = 0;
	let completed = 0;
	const sockets: Array<{ page: WebSocketRoute; server: WebSocketRoute }> = [];
	const timers = new Set<ReturnType<typeof setTimeout>>();
	await context.routeWebSocket(url => url.pathname === '/ws', async page => {
		if (blocked) { await page.close(); return; }
		const server = page.connectToServer();
		sockets.push({ page, server });
		page.onMessage(message => {
			if (JSON.parse(String(message)).type === 'doc_flush') starts += 1;
			server.send(message);
		});
		server.onMessage(message => {
			const type = JSON.parse(String(message)).type;
			if (gate && (type === 'doc_saved' || (type === 'doc_ack' && starts > 0))) {
				void gate.then(() => {
					if (blocked) return;
					if (type === 'doc_saved') completed += 1;
					page.send(message);
				});
				return;
			}
			if (type !== 'doc_saved' && !(type === 'doc_ack' && starts > 0 && delay > 0)) { page.send(message); return; }
			const timer = setTimeout(() => {
				timers.delete(timer);
				if (!blocked) {
					if (type === 'doc_saved') completed += 1;
					page.send(message);
				}
			}, delay);
			timers.add(timer);
		});
	});
	return {
		get starts() { return starts; },
		get completed() { return completed; },
		setDelay(ms: number) { delay = ms; },
		/** Hold save replies until release(), for a cue that must outlast a slow page rather than a fixed delay. */
		hold() { gate = new Promise<void>(resolve => { openGate = resolve; }); },
		release() { gate = undefined; openGate?.(); },
		async cut() {
			blocked = true;
			for (const timer of timers) clearTimeout(timer);
			timers.clear();
			for (const socket of sockets.splice(0)) { await socket.server.close(); await socket.page.close(); }
		},
		restore() { blocked = false; },
	};
}

/** Record every animation frame, not merely the final DOM assertion. */
export async function observeSaveCue(page: Page) {
	await page.evaluate(() => {
		const samples: Array<{ at: number; cue: string; visible: boolean; stale: boolean; disconnected: boolean }> = [];
		const sample = () => {
			const cue = document.querySelector<HTMLElement>('.save-ghost');
			const style = cue ? getComputedStyle(cue) : null;
			samples.push({ at: performance.now(), cue: cue?.textContent ?? '',
				visible: Boolean(cue && style?.display !== 'none' && style?.visibility !== 'hidden'),
				stale: Boolean(document.querySelector('.stale-control')),
				disconnected: document.body.textContent?.includes('Disconnected') ?? false });
			(window as any).__saveCueSamples = samples;
			requestAnimationFrame(sample);
		};
		sample();
	});
}
export async function cueSamples(page: Page): Promise<Array<{ at: number; cue: string; visible: boolean; stale: boolean; disconnected: boolean }>> {
	return page.evaluate(() => (window as any).__saveCueSamples ?? []);
}
export async function expectQuietSave(page: Page) {
	const frames = await cueSamples(page);
	expect(frames.filter(frame => frame.visible && frame.cue !== ''), 'no cue appeared on any animation frame').toEqual([]);
	await expect(page.locator('.save-ghost')).toHaveCount(0);
}
