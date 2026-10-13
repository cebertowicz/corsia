import fs from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';
import { installPaintedFrames, type EachPaintedFrame } from '../support/paintedFrames';
import { syntheticPng } from '../support/syntheticImages';

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
/** `editor` is false while the tab still shows its loading state, before the document's text arrived. */
type ReturnFrame = VisiblePlace & { elapsedMs: number; editor: boolean; scrollerVisible: boolean };

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
 * Records every frame the browser paints after the tab's click event, each as that frame paints it: read in the frame's
 * rendering, after the editor's measure and the page's resize observers (see `installPaintedFrames`). A read in an
 * animation-frame callback comes before the editor's measure, and one in a task after the frame can see a later
 * answer from the server, so either can record a state no frame showed.
 */
async function armReturnSampler(page: Page): Promise<void> {
	await installPaintedFrames(page);
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
				const measure = (): ReturnFrame => {
					const scroller = document.querySelector<HTMLElement>('.cm-scroller');
					const box = scroller?.getBoundingClientRect();
					const style = scroller && getComputedStyle(scroller);
					const visibleLine = scroller && box
						? [...scroller.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => {
							const rect = candidate.getBoundingClientRect();
							return rect.bottom > box.top && rect.top < box.bottom;
						})
						: undefined;
					return {
						elapsedMs: performance.now() - startedAt,
						scrollTop: scroller?.scrollTop ?? 0,
						line: visibleLine?.textContent?.trim() ?? '',
						top: visibleLine && box ? visibleLine.getBoundingClientRect().top - box.top : null,
						editor: Boolean(scroller),
						scrollerVisible: Boolean(box && style && style.visibility !== 'hidden' && style.display !== 'none'),
					};
				};
				// The window ends on the frame whose own reading reached it, so that frame is the last recorded.
				const keep = (frame: ReturnFrame) => {
					frames.push(frame);
					if (frame.elapsedMs < 1_000) return true;
					resolve(frames);
					return false;
				};
				(window as Window & { __eachPaintedFrame?: EachPaintedFrame }).__eachPaintedFrame!(measure, keep);
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

/** Frames that drew the document's editor covered while its place was put back. */
function coveredFrames(frames: ReturnFrame[]): number {
	return frames.filter((frame) => frame.editor && !frame.scrollerVisible).length;
}

/**
 * Frames that showed the tab's loading state while the document's text and live session came from the server. How many
 * depends on how fast the server answers, so they are reported, not bounded; they show no document, so no place.
 */
function loadingFrames(frames: ReturnFrame[]): number {
	return frames.filter((frame) => !frame.editor).length;
}

function expectNoReturnPop(frames: ReturnFrame[], before: VisiblePlace): void {
	if (before.top === null) throw new Error('saved document place has no visible line');
	expect(frames.at(-1)?.elapsedMs).toBeGreaterThanOrEqual(1_000);
	expect(coveredFrames(frames), `covered frames after activation: ${JSON.stringify(frames.slice(0, 4))}`).toBeLessThanOrEqual(MAX_HIDDEN_FRAMES);
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
				const report = {
					before, coveredFrames: coveredFrames(frames), loadingFrames: loadingFrames(frames),
					maxHiddenFrames: MAX_HIDDEN_FRAMES, maxDriftPx: MAX_DRIFT_PX, frames,
				};
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

const wrapped = (tag: string, count: number) => Array.from({ length: count }, (_, i) =>
	`${tag} paragraph ${i + 1}: enough words to wrap across a line or two of the document column as the canvas narrows.\n`).join('\n');
const WALKTHROUGH = ['# Walkthrough', '', wrapped('Walkthrough', 14), '![Mock screen](shots/screen.png)', '', wrapped('After image', 30)].join('\n');
/** Where the image is held below the scroller's top before it is opened. */
const IMAGE_PLACE_PX = 90;
/** Back is sampled for longer than the canvas takes to ease back to its width. */
const BACK_SAMPLE_MS = 1_200;

type ImageFrame = {
	elapsedMs: number;
	scrollerVisible: boolean;
	imageTop: number | null;
	overlay: boolean;
	overlayImage: boolean;
	leafWidth: number | null;
	chatLeft: number | null;
	chatWidth: number | null;
	/** Tabs drawn with a background: the active mark alone, with the pointer resting on the active tab. */
	markedTabs: string[];
	selectedTabs: string[];
};

/** Like the tab-return sampler, for a document image: records every frame painted after the next click. */
async function armImageSampler(page: Page, durationMs: number): Promise<void> {
	await installPaintedFrames(page);
	await page.evaluate((durationMs) => {
		const target = window as Window & { __imageSamples?: Promise<ImageFrame[]> };
		target.__imageSamples = undefined;
		const arm = () => {
			document.removeEventListener('click', arm, true);
			target.__imageSamples = new Promise<ImageFrame[]>((resolve) => {
				const frames: ImageFrame[] = [];
				const startedAt = performance.now();
				const name = (el: HTMLElement) => el.querySelector('[role="tab"]')?.textContent?.trim() ?? '';
				const measure = (): ImageFrame => {
					const scroller = document.querySelector<HTMLElement>('.cm-scroller');
					const box = scroller?.getBoundingClientRect();
					const style = scroller && getComputedStyle(scroller);
					const widget = scroller?.querySelector<HTMLElement>('.cm-md-image');
					const overlay = document.querySelector<HTMLElement>('.image-overlay');
					const chat = document.querySelector<HTMLElement>('.chat-col')?.getBoundingClientRect();
					const leaf = document.querySelector<HTMLElement>('.leaf-col')?.getBoundingClientRect();
					const tabs = [...document.querySelectorAll<HTMLElement>('.tab-wrap')];
					return {
						elapsedMs: performance.now() - startedAt,
						scrollerVisible: Boolean(box && style && style.visibility !== 'hidden' && style.display !== 'none' && box.height > 0),
						imageTop: widget && box ? widget.getBoundingClientRect().top - box.top : null,
						overlay: Boolean(overlay),
						overlayImage: Boolean(overlay?.querySelector('img')),
						leafWidth: leaf?.width ?? null,
						chatLeft: chat?.left ?? null,
						chatWidth: chat?.width ?? null,
						markedTabs: tabs.filter((el) => !['rgba(0, 0, 0, 0)', 'transparent'].includes(getComputedStyle(el).backgroundColor)).map(name),
						selectedTabs: tabs.filter((el) => el.querySelector('[aria-selected="true"]')).map(name),
					};
				};
				const keep = (frame: ImageFrame) => {
					frames.push(frame);
					if (frame.elapsedMs < durationMs) return true;
					resolve(frames);
					return false;
				};
				(window as Window & { __eachPaintedFrame?: EachPaintedFrame }).__eachPaintedFrame!(measure, keep);
			});
		};
		document.addEventListener('click', arm, true);
	}, durationMs);
}

async function imageSamples(page: Page): Promise<ImageFrame[]> {
	return page.evaluate(() => {
		const samples = (window as Window & { __imageSamples?: Promise<ImageFrame[]> }).__imageSamples;
		if (!samples) throw new Error('image sampler did not start');
		return samples;
	});
}

/** Back shows the document with its image where it was on every frame, through the whole width transition. */
function expectBackInPlace(frames: ImageFrame[], savedTop: number): void {
	expect(frames.at(-1)?.elapsedMs).toBeGreaterThanOrEqual(BACK_SAMPLE_MS);
	expect(frames.filter((frame) => !frame.scrollerVisible).length, `hidden frames: ${JSON.stringify(frames.slice(0, 4))}`).toBeLessThanOrEqual(MAX_HIDDEN_FRAMES);
	const firstVisible = frames.findIndex((frame) => frame.scrollerVisible && !frame.overlay);
	expect(firstVisible).toBeGreaterThanOrEqual(0);
	for (const frame of frames.slice(firstVisible)) {
		const sample = `at ${Math.round(frame.elapsedMs)}ms: ${JSON.stringify(frame)}`;
		expect(frame.overlay, sample).toBe(false);
		expect(frame.imageTop, sample).not.toBeNull();
		expect(Math.abs(frame.imageTop! - savedTop), sample).toBeLessThanOrEqual(MAX_DRIFT_PX);
	}
	// The samples outlast the canvas easing back: its width held over the last frames.
	const widths = frames.slice(-5).map((frame) => frame.leafWidth);
	expect(new Set(widths).size, `canvas width still moving: ${JSON.stringify(widths)}`).toBe(1);
}

/**
 * A return to a tab whose image is open shows the image from the first frame, at the canvas width the image had, with
 * that tab marked active, and the chat column beside it never moves.
 */
function expectImageReturn(frames: ImageFrame[], wideLeaf: number, touch: boolean): void {
	expect(frames.length).toBeGreaterThan(0);
	const first = frames[0];
	for (const frame of frames) {
		const sample = `at ${Math.round(frame.elapsedMs)}ms: ${JSON.stringify(frame)}`;
		expect(frame.overlay && frame.overlayImage, sample).toBe(true);
		// A phone shows the canvas alone, with its tabs in a sheet.
		if (touch) continue;
		expect(frame.leafWidth, sample).toBe(wideLeaf);
		expect(frame.selectedTabs, sample).toEqual(['walkthrough.md']);
		expect(frame.markedTabs, sample).toEqual(['walkthrough.md']);
		expect([frame.chatLeft, frame.chatWidth], sample).toEqual([first.chatLeft, first.chatWidth]);
	}
}

for (const width of [1440, 393]) {
	const touch = width <= 768;
	const height = touch ? 852 : 900;
	test.describe(`@flow:tab-scroll-place @surface:documents document image place survives at ${width}px`, () => {
		test(`keeps the document in place on Back from its image and on a return with the image open at ${width}px`, async ({ browser, request }, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
			const context = await browser.newContext({ baseURL: testInfo.project.use.baseURL, viewport: { width, height }, hasTouch: touch });
			const page = await context.newPage();
			const sessionTitle = `tab-image-place-${width}-${Date.now()}`;
			const sessionId = await createSession(request, sessionTitle);
			const tree = await (await request.get(`/api/files/tree?${query()}`)).json();
			const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
			const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
			expect(workspaceRoot).toEqual(expect.any(String));
			const dir = path.join(workspaceRoot, sessionTitle);
			fs.mkdirSync(path.join(dir, 'shots'), { recursive: true });
			fs.writeFileSync(path.join(dir, 'walkthrough.md'), WALKTHROUGH);
			fs.writeFileSync(path.join(dir, 'OTHER.md'), ['# Other', '', wrapped('Other', 6)].join('\n'));
			fs.writeFileSync(path.join(dir, 'shots/screen.png'), syntheticPng(1600, 1000));
			const report: Record<string, unknown> = {};

			try {
				const tabs = [];
				for (const title of ['walkthrough.md', 'OTHER.md']) {
					const registered = await request.post(`/api/files/register?${query()}`, { data: { path: path.join(dir, title), thread_id: sessionId } });
					expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
					tabs.push({
						instanceId: `tab-${title}`, artifactId: (await registered.json()).artifact.id, path: path.join(dir, title), title,
						loadedRevision: null, latestRevision: null, status: 'loading',
					});
				}
				await page.addInitScript(({ threadId, tabs }) => {
					// Seeded once: a reload keeps what the person did since.
					if (sessionStorage.getItem('tab-image-place-seeded')) return;
					sessionStorage.setItem('tab-image-place-seeded', '1');
					localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
						threads: { [threadId]: { tabs, activeTabId: 'tab-walkthrough.md', visible: true, activeSurface: 'artifact' } },
						splitWidth: null,
					}));
				}, { threadId: sessionId, tabs });

				await loginByUrl(page);
				if (touch) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
				await page.getByText(sessionTitle).first().click();
				const scroller = page.locator('.cm-scroller').first();
				await expect(page.locator('.cm-content').first()).toContainText('Walkthrough paragraph 1');
				const docImage = page.locator('.cm-md-image img').first();
				await expect.poll(async () => {
					if (await docImage.count()) return true;
					await scroller.evaluate((el) => { el.scrollTop += 250; });
					return false;
				}).toBe(true);
				await expect.poll(() => docImage.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(1600);
				const imageTop = () => page.locator('.cm-md-image').first().evaluate((el) =>
					el.getBoundingClientRect().top - el.closest('.cm-scroller')!.getBoundingClientRect().top);
				await page.locator('.cm-md-image').first().evaluate((el, place) => {
					const owner = el.closest<HTMLElement>('.cm-scroller')!;
					owner.scrollTop += el.getBoundingClientRect().top - owner.getBoundingClientRect().top - place;
				}, IMAGE_PLACE_PX);
				const back = page.getByRole('button', { name: 'Back to walkthrough.md' });
				const leafWidth = () => page.locator('.leaf-col').evaluate((el) => el.getBoundingClientRect().width);
				/** Opens the image once the editor has finished measuring around it; Back owes the place it had then. */
				const openImage = async () => {
					let last = Number.NaN;
					await expect.poll(async () => {
						const now = await imageTop();
						const settled = now === last;
						last = now;
						return settled;
					}, { intervals: [250] }).toBe(true);
					if (touch) await docImage.tap();
					else await docImage.click();
					await expect(back).toBeVisible();
					// The canvas has finished easing wider.
					let lastWidth = -1;
					await expect.poll(async () => {
						const now = await leafWidth();
						const settled = now === lastWidth;
						lastWidth = now;
						return settled;
					}).toBe(true);
					return last;
				};

				const savedTop = await openImage();
				report.savedTop = savedTop;
				const wideLeaf = await leafWidth();
				report.wideLeaf = wideLeaf;
				await armImageSampler(page, BACK_SAMPLE_MS);
				await back.click();
				const backFrames = await imageSamples(page);
				report.back = backFrames;
				expectBackInPlace(backFrames, savedTop);

				const reopenedTop = await openImage();
				report.reopenedTop = reopenedTop;
				const tabsSheet = () => page.getByRole('dialog', { name: 'Open side panel items' });
				if (touch) {
					await page.getByRole('button', { name: /^Tabs \(2\)/ }).click();
					await tabsSheet().getByRole('button', { name: 'OTHER.md', exact: true }).click();
				} else await page.getByRole('tab', { name: 'OTHER.md', exact: true }).click();
				await expect(page.locator('.cm-content').first()).toContainText('Other paragraph 1');
				if (touch) {
					await page.getByRole('button', { name: /^Tabs \(2\)/ }).click();
					await armImageSampler(page, 1_000);
					await tabsSheet().getByRole('button', { name: 'walkthrough.md', exact: true }).click();
				} else {
					await armImageSampler(page, 1_000);
					await page.getByRole('tab', { name: 'walkthrough.md', exact: true }).click();
				}
				const returnFrames = await imageSamples(page);
				report.return = returnFrames;
				expectImageReturn(returnFrames, wideLeaf, touch);

				await armImageSampler(page, BACK_SAMPLE_MS);
				await back.click();
				const backAfterReturn = await imageSamples(page);
				report.backAfterReturn = backAfterReturn;
				expectBackInPlace(backAfterReturn, reopenedTop);
			} finally {
				fs.writeFileSync(testInfo.outputPath(`tab-image-place-${width}.json`), JSON.stringify(report));
				fs.rmSync(dir, { recursive: true, force: true });
				await context.close();
			}
		});
	});
}
