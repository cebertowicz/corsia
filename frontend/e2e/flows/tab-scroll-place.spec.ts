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

type VisiblePlace = { scrollTop: number; line: string };

async function visiblePlace(scroller: ReturnType<Page['locator']>): Promise<VisiblePlace> {
	return scroller.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const line = [...el.querySelectorAll<HTMLElement>('.cm-line')]
			.find((candidate) => {
				const rect = candidate.getBoundingClientRect();
				return rect.bottom > box.top && rect.top < box.bottom;
			});
		return { scrollTop: el.scrollTop, line: line?.textContent?.trim() ?? '' };
	});
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
					await page.getByRole('dialog', { name: 'Open side panel items' })
						.getByRole('button', { name: 'INDEX.md', exact: true }).click();
				} else {
					await page.getByRole('tab', { name: 'INDEX.md' }).click();
				}

				await expect.poll(async () => Math.abs((await visiblePlace(scroller)).scrollTop - before.scrollTop)).toBeLessThanOrEqual(2);
				const after = await visiblePlace(scroller);
				expect(after.line).toBe(before.line);
				fs.writeFileSync(testInfo.outputPath(`tab-scroll-place-${width}.txt`), JSON.stringify({ before, after }));
				await saveEvidenceScreenshot(page, testInfo, `tab-scroll-place-${width}-after`, { fullPage: false });
			} finally {
				fs.rmSync(dir, { recursive: true, force: true });
				await context.close();
			}
		});
	});
}
