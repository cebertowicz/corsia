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
		(window as any).__saveCueSamples = [];
		const sample = () => {
			const cue = document.querySelector<HTMLElement>('.bar-save');
			const style = cue ? getComputedStyle(cue) : null;
			(window as any).__saveCueSamples.push({ at: performance.now(), cue: cue?.textContent ?? '',
				visible: Boolean(cue && style?.display !== 'none' && style?.visibility !== 'hidden'),
				stale: Boolean(document.querySelector('.stale-control')),
				disconnected: document.body.textContent?.includes('Disconnected') ?? false });
			requestAnimationFrame(sample);
		};
		sample();
	});
}
/** Prove the selector with a slow save before recording the quiet episode. */
export async function proveSaveCue(page: Page, link: Awaited<ReturnType<typeof routeDocumentSaves>>, edit: () => Promise<void>) {
	link.hold();
	await edit();
	await expect(page.locator('.bar-save')).toHaveText('Saving…');
	await expect(page.locator('.bar-save')).toBeVisible();
	link.release();
	await expect(page.locator('.bar-save')).toHaveCount(0, { timeout: 20_000 });
}

export async function expectTextOnlyCue(page: Page, text: string) {
	const cue = page.locator('.bar-save');
	await expect(cue).toHaveCount(1);
	await expect(cue).toHaveText(text);
	await expect(cue).toBeVisible();
	const style = await cue.evaluate(element => {
		const computed = getComputedStyle(element);
		return { background: computed.backgroundColor, shadow: computed.boxShadow,
			borders: ['Top', 'Right', 'Bottom', 'Left'].map(side => ({
				style: computed.getPropertyValue(`border-${side.toLowerCase()}-style`),
				width: parseFloat(computed.getPropertyValue(`border-${side.toLowerCase()}-width`)),
			})) };
	});
	expect(style.background).toBe('rgba(0, 0, 0, 0)');
	expect(style.shadow).toBe('none');
	for (const border of style.borders) expect(border.style === 'none' || border.width === 0).toBe(true);
}

export async function cueSamples(page: Page): Promise<Array<{ at: number; cue: string; visible: boolean; stale: boolean; disconnected: boolean }>> {
	return page.evaluate(() => (window as any).__saveCueSamples ?? []);
}
export async function expectQuietSave(page: Page) {
	const frames = await cueSamples(page);
	expect(frames.filter(frame => frame.visible && frame.cue !== ''), 'no cue appeared on any animation frame').toEqual([]);
	await expect(page.locator('.bar-save')).toHaveCount(0);
}
