import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';

const MISSING_TOAST = "Couldn't find the linked workspace document";
const WEB_LINK = 'https://example.invalid/guide/start';
const FILLER = Array.from({ length: 34 }, (_, i) => `Filler paragraph ${i + 1} so the document scrolls.\n`);
const INDEX = [
	'# Index', '',
	'Read [the decisions](./DECISIONS.md) first, then [a web guide](' + WEB_LINK + ') or [a page that is gone](./nope.md).', '',
	...FILLER,
	'Last, [the decisions again](./DECISIONS.md#top) at the bottom.', '',
].join('\n');
const DECISIONS = '# Decisions\n\nThe decisions record body.\n';

const query = () => new URLSearchParams({ token: TEST_TOKEN }).toString();
const link = (page: Page, text: string) => page.locator('.cm-md-link-open', { hasText: text });

type Scheme = 'light' | 'dark';

// Each run is one width in one color scheme, in its own context so its recording is the whole viewport.
for (const width of [1440, 393]) {
	const touch = width <= 768;
	const height = touch ? 852 : 900;
	for (const scheme of ['light', 'dark'] as const) {
		runFlow(width, height, touch, scheme);
	}
}

function runFlow(width: number, height: number, touch: boolean, scheme: Scheme) {
	test.describe(`@flow:doc-links-open @surface:documents a link in a document opens its target (${width}px, ${scheme})`, () => {
		test(`follows a relative link into a second tab and back, at ${width}px in ${scheme}`, async ({ browser, request }, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
			const context = await browser.newContext({
				baseURL: testInfo.project.use.baseURL,
				viewport: { width, height },
				hasTouch: touch,
				colorScheme: scheme,
				recordVideo: { dir: testInfo.outputPath('video'), size: { width, height } },
			});
			const page = await context.newPage();
			const shoot = async (name: string) => {
				await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
				await saveEvidenceScreenshot(page, testInfo, `doc-links-${width}-${name}-${scheme}`, { fullPage: false });
			};
			const stamp = Date.now();
			const sessionTitle = `doc-links-${width}-${scheme}-${stamp}`;
			const sessionId = await createSession(request, sessionTitle);

			const tree = await (await request.get(`/api/files/tree?${query()}`)).json();
			const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
			const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
			expect(workspaceRoot).toEqual(expect.any(String));

			const dir = path.join(workspaceRoot, `doc-links-${width}-${scheme}-${stamp}`);
			const indexPath = path.join(dir, 'INDEX.md');
			fs.mkdirSync(dir, { recursive: true });
			fs.writeFileSync(indexPath, INDEX);
			fs.writeFileSync(path.join(dir, 'DECISIONS.md'), DECISIONS);

			try {
				const registered = await request.post(`/api/files/register?${query()}`, {
					data: { path: indexPath, thread_id: sessionId },
				});
				expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
				const artifact = (await registered.json()).artifact;

				await page.addInitScript(
					({ threadId, artifactId, tabPath }) => {
						// A web link opens in a browser tab; record the call instead of leaving the page under test.
						(window as unknown as { __opened: unknown[][] }).__opened = [];
						window.open = (...args: unknown[]) => { (window as unknown as { __opened: unknown[][] }).__opened.push(args); return null; };
						localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
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
						}));
					},
					{ threadId: sessionId, artifactId: artifact.id, tabPath: indexPath },
				);

				await loginByUrl(page);
				if (touch) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
				await page.getByText(sessionTitle).first().click();

				const content = page.locator('.cm-content');
				const scroller = page.locator('.cm-scroller');
				const press = (target: ReturnType<typeof link>) => (touch ? target.tap() : target.click());
				const goToIndex = async () => {
					if (touch) {
						await page.getByRole('button', { name: /Tabs \(2\)/ }).click();
						await page.getByRole('dialog', { name: 'Open side panel items' }).getByRole('button', { name: 'INDEX.md', exact: true }).click();
					} else {
						await page.getByRole('tab', { name: 'INDEX.md' }).click();
					}
					await expect(content).toBeVisible();
				};

				// At rest: the URL is hidden, the link is styled, and it carries its destination.
				await expect(link(page, 'the decisions').first()).toBeVisible();
				await expect(content).not.toContainText('./DECISIONS.md');
				await expect(link(page, 'the decisions').first()).toHaveAttribute('title', './DECISIONS.md');
				await expect(link(page, 'a web guide')).toHaveAttribute('title', 'example.invalid/guide/start');
				await shoot('rest');

				// The pointer and press feedback the pointer type earns.
				const coarse = await page.evaluate(() => matchMedia('(pointer: coarse)').matches);
				expect(coarse).toBe(touch);
				if (!touch) {
					const first = link(page, 'the decisions').first();
					const underline = () => first.evaluate((el) => getComputedStyle(el).textDecorationColor);
					const restUnderline = await underline();
					await first.hover();
					expect(await first.evaluate((el) => getComputedStyle(el).cursor)).toBe('pointer');
					// The underline strengthens on hover, after its short transition.
					await expect.poll(underline).not.toBe(restUnderline);
					await first.evaluate((el) => Promise.all(el.getAnimations().map((animation) => animation.finished)));
					fs.writeFileSync(testInfo.outputPath(`hover-link-box-${width}-${scheme}.json`),
						JSON.stringify({ restUnderline, hoverUnderline: await underline(), box: await first.boundingBox() }));
					await shoot('hover');
				} else {
					const pressRule = await page.evaluate(() => [...document.styleSheets].some((sheet) => {
						try {
							return [...sheet.cssRules].some((rule) => rule.cssText.includes('(pointer: coarse)') && rule.cssText.includes('.cm-md-link-open:active'));
						} catch { return false; }
					}));
					expect(pressRule).toBe(true);
				}

				// A web link opens in a browser tab; the canvas is unchanged.
				await press(link(page, 'a web guide'));
				await expect.poll(() => page.evaluate(() => (window as unknown as { __opened: unknown[][] }).__opened.length)).toBe(1);
				expect(await page.evaluate(() => (window as unknown as { __opened: unknown[][] }).__opened[0][0])).toBe(WEB_LINK);

				// A target that is not there tells the person, and the canvas stays as it was.
				await press(link(page, 'a page that is gone'));
				const toast = page.getByRole('status').filter({ hasText: MISSING_TOAST });
				await expect(toast).toBeVisible();
				await shoot('missing-toast');
				if (!touch) await expect(page.getByRole('tab')).toHaveCount(1);
				else await expect(page.getByRole('button', { name: /Tabs \(1\)/ })).toBeVisible();
				await toast.getByRole('button').click();
				await expect(toast).toHaveCount(0);

				// Follow the link at the bottom of a scrolled document into a second tab.
				// The editor draws only the lines near the viewport, so scroll first and let the link render.
				await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; });
				const bottom = link(page, 'the decisions again');
				await expect(bottom).toBeInViewport();
				const scrollBefore = await scroller.evaluate((el) => el.scrollTop);
				expect(scrollBefore).toBeGreaterThan(0);
				await press(bottom);
				await expect(page.getByText('The decisions record body.')).toBeVisible();
				if (!touch) {
					await expect(page.getByRole('tab', { name: 'DECISIONS.md' })).toHaveAttribute('aria-selected', 'true');
					await expect(page.getByRole('tab')).toHaveCount(2);
				} else {
					await expect(page.getByRole('button', { name: /Tabs \(2\)/ })).toBeVisible();
					await expect(page.locator('.shell-header')).toContainText('DECISIONS.md');
				}
				await shoot('second-tab');

				// Back on the first document; whether its place was kept is recorded, not asserted.
				await goToIndex();
				const scrollAfter = await scroller.evaluate((el) => el.scrollTop);
				fs.writeFileSync(testInfo.outputPath(`scroll-${width}-${scheme}.txt`),
					`scrollTop before follow: ${scrollBefore}\nscrollTop after return: ${scrollAfter}\n`);
				await shoot('back');

				// Following the same target again focuses its open tab and adds none.
				await scroller.evaluate((el) => { el.scrollTop = 0; });
				await press(link(page, 'the decisions').first());
				await expect(page.getByText('The decisions record body.')).toBeVisible();
				if (!touch) await expect(page.getByRole('tab')).toHaveCount(2);
				else await expect(page.getByRole('button', { name: /Tabs \(2\)/ })).toBeVisible();

				// No click wrote to the document.
				expect(fs.readFileSync(indexPath, 'utf8')).toBe(INDEX);
			} finally {
				fs.rmSync(dir, { recursive: true, force: true });
				// The recording is finalized when the context closes.
				await context.close();
				await page.video()?.saveAs(testInfo.outputPath(`doc-links-${width}-${scheme}-follow-and-return.webm`));
			}
		});
	});
}
