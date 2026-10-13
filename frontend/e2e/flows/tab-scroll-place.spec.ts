import fs from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';

const FILLER = Array.from({ length: 34 }, (_, i) => `Filler paragraph ${i + 1} keeps this tab long enough to restore.\n`);
const INDEX = [
	'# Index', '',
	...FILLER,
	'Open [the second document](./SECOND.md) from this saved place.', '',
].join('\n');
const SECOND = '# Second document\n\nThis is the second tab.\n';

const query = () => new URLSearchParams({ token: TEST_TOKEN }).toString();
const link = (page: Page) => page.locator('.cm-md-link-open', { hasText: 'the second document' });

type VisiblePlace = { scrollTop: number; line: string; top: number | null };
type ReturnFrame = VisiblePlace & { elapsedMs: number; scrollerVisible: boolean };

async function visiblePlace(scroller: ReturnType<Page['locator']>): Promise<VisiblePlace> {
	return scroller.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const line = [...el.querySelectorAll<HTMLElement>('.cm-line')]
			.find((candidate) => {
				const rect = candidate.getBoundingClientRect();
				return rect.bottom > box.top && rect.top < box.bottom;
			});
		return {
			scrollTop: el.scrollTop,
			line: line?.textContent?.trim() ?? '',
			top: line ? line.getBoundingClientRect().top - box.top : null,
		};
	});
}

/**
 * Records every frame the browser paints after the tab's click event. Each sample is taken just after its frame's paint:
 * an animation-frame callback runs before the editor's own measure in that frame, so it reads state never painted.
 */
async function armReturnSampler(page: Page): Promise<void> {
	await page.evaluate(() => {
		let result: Promise<ReturnFrame[]> | undefined;
		(window as Window & { __tabReturnSamples?: () => Promise<ReturnFrame[]> }).__tabReturnSamples = () => {
			if (!result) throw new Error('tab-return sampler did not start');
			return result;
		};
		const arm = () => {
			document.removeEventListener('click', arm, true);
			result = new Promise<ReturnFrame[]>((resolve) => {
				const frames: ReturnFrame[] = [];
				const startedAt = performance.now();
				const afterPaint = (next: () => void) => requestAnimationFrame(() => {
					const channel = new MessageChannel();
					channel.port1.onmessage = () => next();
					channel.port2.postMessage(null);
				});
				const sample = () => {
					const scroller = document.querySelector<HTMLElement>('.cm-scroller');
					const box = scroller?.getBoundingClientRect();
					const style = scroller && getComputedStyle(scroller);
					const visibleLine = scroller && box
						? [...scroller.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => {
							const rect = candidate.getBoundingClientRect();
							return rect.bottom > box.top && rect.top < box.bottom;
						})
						: undefined;
					frames.push({
						elapsedMs: performance.now() - startedAt,
						scrollTop: scroller?.scrollTop ?? 0,
						line: visibleLine?.textContent?.trim() ?? '',
						top: visibleLine && box ? visibleLine.getBoundingClientRect().top - box.top : null,
						scrollerVisible: Boolean(box && style && style.visibility !== 'hidden' && style.display !== 'none'),
					});
					if (performance.now() - startedAt < 1_000) afterPaint(sample);
					else resolve(frames);
				};
				afterPaint(sample);
			});
		};
		document.addEventListener('click', arm, true);
	});
}

async function returnSamples(page: Page): Promise<ReturnFrame[]> {
	return page.evaluate(() => {
		const samples = (window as Window & { __tabReturnSamples?: () => Promise<ReturnFrame[]> }).__tabReturnSamples;
		if (!samples) throw new Error('tab-return sampler is unavailable');
		return samples();
	});
}

/** At most this many frames after the tab is activated may show no document while its place is put back. */
const MAX_HIDDEN_FRAMES = 1;
/** From its first visible frame the saved line stays within this many pixels of where it was. */
const MAX_DRIFT_PX = 2;

/** Frames that drew no document: the covered stretch while the place is restored, and any blank after it. */
function hiddenFrames(frames: ReturnFrame[]): number {
	return frames.filter((frame) => !frame.scrollerVisible).length;
}

function expectNoReturnPop(frames: ReturnFrame[], before: VisiblePlace): void {
	if (before.top === null) throw new Error('saved document place has no visible line');
	expect(frames.at(-1)?.elapsedMs).toBeGreaterThanOrEqual(1_000);
	expect(hiddenFrames(frames), `hidden frames after activation: ${JSON.stringify(frames.slice(0, 4))}`).toBeLessThanOrEqual(MAX_HIDDEN_FRAMES);
	const firstVisible = frames.findIndex((frame) => frame.scrollerVisible);
	expect(firstVisible).toBeGreaterThanOrEqual(0);
	for (const frame of frames.slice(firstVisible)) {
		const sample = `at ${Math.round(frame.elapsedMs)}ms: ${JSON.stringify(frame)}`;
		expect(frame.scrollerVisible, sample).toBe(true);
		expect(frame.line, sample).toBe(before.line);
		expect(frame.top, sample).not.toBeNull();
		expect(Math.abs(frame.top! - before.top), sample).toBeLessThanOrEqual(MAX_DRIFT_PX);
	}
}

for (const width of [1440, 393]) {
	const touch = width <= 768;
	const height = touch ? 852 : 900;
	test.describe(`@flow:tab-scroll-place @surface:documents tab place survives at ${width}px`, () => {
		test(`returns to the same document line and scroll offset at ${width}px`, async ({ browser, request }, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
			const context = await browser.newContext({
				baseURL: testInfo.project.use.baseURL,
				viewport: { width, height },
				hasTouch: touch,
				recordVideo: { dir: testInfo.outputPath('video'), size: { width, height } },
			});
			const page = await context.newPage();
			const sessionTitle = `tab-scroll-place-${width}-${Date.now()}`;
			const sessionId = await createSession(request, sessionTitle);

			const tree = await (await request.get(`/api/files/tree?${query()}`)).json();
			const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
			const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
			expect(workspaceRoot).toEqual(expect.any(String));
			const dir = path.join(workspaceRoot, sessionTitle);
			const indexPath = path.join(dir, 'INDEX.md');
			fs.mkdirSync(dir, { recursive: true });
			fs.writeFileSync(indexPath, INDEX);
			fs.writeFileSync(path.join(dir, 'SECOND.md'), SECOND);

			try {
				const registered = await request.post(`/api/files/register?${query()}`, {
					data: { path: indexPath, thread_id: sessionId },
				});
				expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
				const artifact = (await registered.json()).artifact;
				await page.addInitScript(
					({ threadId, artifactId, tabPath }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
						threads: {
							[threadId]: {
								tabs: [{
									instanceId: 'tab-index', artifactId, path: tabPath, title: 'INDEX.md',
									loadedRevision: null, latestRevision: null, status: 'loading',
								}],
								activeTabId: 'tab-index', visible: true, activeSurface: 'artifact',
							},
						},
						splitWidth: null,
					})),
					{ threadId: sessionId, artifactId: artifact.id, tabPath: indexPath },
				);

				await loginByUrl(page);
				if (touch) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
				await page.getByText(sessionTitle).first().click();
				const scroller = page.locator('.cm-scroller');
				await expect(scroller).toBeVisible();
				await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; });
				await expect(link(page)).toBeInViewport();
				const before = await visiblePlace(scroller);
				expect(before.scrollTop).toBeGreaterThan(0);
				expect(before.line).not.toBe('');
				await saveEvidenceScreenshot(page, testInfo, `tab-scroll-place-${width}-before`, { fullPage: false });

				if (touch) await link(page).tap();
				else await link(page).click();
				await expect(page.getByText('This is the second tab.')).toBeVisible();
				if (touch) {
					await page.getByRole('button', { name: /Tabs \(2\)/ }).click();
					await armReturnSampler(page);
					await page.getByRole('dialog', { name: 'Open side panel items' })
						.getByRole('button', { name: 'INDEX.md', exact: true }).click();
				} else {
					await armReturnSampler(page);
					await page.getByRole('tab', { name: 'INDEX.md' }).click();
				}

				const frames = await returnSamples(page);
				const report = { before, hiddenFrames: hiddenFrames(frames), maxHiddenFrames: MAX_HIDDEN_FRAMES, maxDriftPx: MAX_DRIFT_PX, frames };
				fs.writeFileSync(testInfo.outputPath(`tab-scroll-place-${width}.json`), JSON.stringify(report));
				expectNoReturnPop(frames, before);
				await expect.poll(async () => {
					const current = await visiblePlace(scroller);
					return current.line === before.line && current.top !== null && before.top !== null
						&& Math.abs(current.top - before.top) <= MAX_DRIFT_PX;
				}).toBe(true);
				const after = await visiblePlace(scroller);
				expect(after.line).toBe(before.line);
				expect(after.top).not.toBeNull();
				expect(Math.abs(after.top! - before.top!)).toBeLessThanOrEqual(MAX_DRIFT_PX);
				fs.writeFileSync(testInfo.outputPath(`tab-scroll-place-${width}.json`), JSON.stringify({ ...report, after }));
				await saveEvidenceScreenshot(page, testInfo, `tab-scroll-place-${width}-after`, { fullPage: false });
			} finally {
				fs.rmSync(dir, { recursive: true, force: true });
				await context.close();
			}
		});
	});
}
