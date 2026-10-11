import fs from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';
import { syntheticGif, syntheticPng } from '../support/syntheticImages';

const WIDTHS = [1440, 393];
const REMOTE_HOST = 'images.example.invalid';
const SOURCE = {
	png: '![Mock screen](shots/screen.png)',
	gif: '![Animated render](shots/render.gif)',
	missing: '![Gone](shots/gone.png)',
	remote: `![Remote](https://${REMOTE_HOST}/remote.png)`,
};
const DOC = [
	'# Design walkthrough', '', 'Intro paragraph before the first image.', '', SOURCE.png, '',
	'Text between the images that a comment can anchor on.', '', SOURCE.gif, '', SOURCE.missing, '', SOURCE.remote, '', 'Closing paragraph.', '',
].join('\n');

test.describe('@flow:doc-inline-images @surface:documents markdown images render inside a document', () => {
	test.use({ hasTouch: true });
	for (const width of WIDTHS) {
		test(`draws relative images in the document canvas at ${width}px without contacting a web server`, async ({ page, request }, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
			await page.setViewportSize({ width, height: width > 768 ? 900 : 852 });
			const stamp = Date.now();
			const sessionTitle = `inline-images-${width}-${stamp}`;
			const sessionId = await createSession(request, sessionTitle);
			const tok = encodeURIComponent(TEST_TOKEN);

			const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
			const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
			const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
			expect(workspaceRoot).toEqual(expect.any(String));

			const dir = path.join(workspaceRoot, `inline-images-${stamp}`);
			const docPath = path.join(dir, 'walkthrough.md');
			fs.mkdirSync(path.join(dir, 'shots'), { recursive: true });
			fs.writeFileSync(path.join(dir, 'shots', 'screen.png'), syntheticPng(640, 400));
			fs.writeFileSync(path.join(dir, 'shots', 'render.gif'), syntheticGif(320, 200));
			fs.writeFileSync(docPath, DOC);

			const outsideRequests: string[] = [];
			await page.route(`**://${REMOTE_HOST}/**`, (route) => { outsideRequests.push(route.request().url()); return route.abort(); });
			page.on('request', (req) => { if (req.url().includes(REMOTE_HOST)) outsideRequests.push(req.url()); });

			try {
				const registered = await request.post(`/api/files/register?token=${tok}`, {
					data: { path: docPath, thread_id: sessionId },
				});
				expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
				const artifact = (await registered.json()).artifact;

				await page.addInitScript(
					({ threadId, artifactId, tabPath }) => {
						localStorage.setItem(
							'corsia:artifactWorkspace:v1',
							JSON.stringify({
								threads: {
									[threadId]: {
										tabs: [{
											instanceId: 'tab-doc', artifactId, path: tabPath, title: 'walkthrough.md',
											loadedRevision: null, latestRevision: null, status: 'loading',
										}],
										activeTabId: 'tab-doc',
										visible: true,
										activeSurface: 'artifact',
									},
								},
								splitWidth: null,
							}),
						);
					},
					{ threadId: sessionId, artifactId: artifact.id, tabPath: docPath },
				);

				await loginByUrl(page);
				if (width <= 768) {
					await page.getByRole('button', { name: 'Toggle sidebar' }).click();
				}
				await page.getByText(sessionTitle).first().click();

				const images = page.locator('.cm-md-image img');
				await expect(images).toHaveCount(2);
				const note = page.locator('.cm-md-image-note');
				await expect(note).toHaveText('Gone · Image not found');

				const content = page.locator('.cm-content');
				for (const alt of ['Mock screen', 'Animated render']) {
					const image = page.getByRole('img', { name: alt });
					await expect(image).toBeVisible();
					await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);
					const natural = await image.evaluate((el: HTMLImageElement) => el.naturalWidth);
					const box = (await image.boundingBox())!;
					const column = (await content.boundingBox())!;
					// Scaled down to the text column, never drawn larger than the file.
					expect(box.width, alt).toBeLessThanOrEqual(natural + 0.5);
					expect(box.x + box.width, alt).toBeLessThanOrEqual(column.x + column.width + 0.5);
				}
				expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

				// The source is hidden with the caret away; the web URL is a link and was never requested.
				await expect(content).not.toContainText('![Mock screen]');
				await expect(page.locator('.cm-md-link', { hasText: 'Remote' })).toBeVisible();
				await expect(page.locator(`img[src*="${REMOTE_HOST}"]`)).toHaveCount(0);

				for (const scheme of ['dark', 'light'] as const) {
					await page.emulateMedia({ colorScheme: scheme });
					await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
					await expect(images.first()).toBeVisible();
					await saveEvidenceScreenshot(page, testInfo, `doc-inline-images-${width}-${scheme}`, { fullPage: false });
				}

				// A click selects the image whole, with a selected state, and its markdown never shows.
				// On the phone width the image is tapped, so the touch path is proven, not only a mouse click.
				if (width <= 768) await images.first().tap();
				else await images.first().click();
				await saveEvidenceScreenshot(page, testInfo, `doc-inline-images-${width}-selected`, { fullPage: false });
				await expect(page.locator('.cm-md-image-selected img')).toHaveCount(1);
				await expect(page.getByRole('button', { name: 'Comment', exact: true }).last()).toBeVisible();
				await expect(page.locator('.cm-md-image-selected img')).toHaveAttribute('alt', 'Mock screen');
				await expect(content).not.toContainText('![Mock screen]');
				await expect(content).not.toContainText('shots/screen.png');
				await expect(images.first()).toBeVisible();

				// The image is one unit for the caret: arrows step over it, and the source stays hidden throughout.
				await page.keyboard.press('ArrowDown');
				await expect(page.locator('.cm-md-image-selected')).toHaveCount(0);
				await expect(content).not.toContainText('![Mock screen]');
				await page.keyboard.press('ArrowUp');
				await page.keyboard.press('ArrowUp');
				await expect(content).not.toContainText('![Mock screen]');
				await expect(images.first()).toBeVisible();

				// Backspace right after the image removes the whole image from the document.
				await page.getByText('Text between the images').click({ position: { x: 2, y: 2 } });
				await page.keyboard.press('Home');
				await page.keyboard.press('ArrowUp');
				await page.keyboard.press('Backspace');
				await expect(images).toHaveCount(1);
				await expect(page.getByRole('img', { name: 'Mock screen' })).toHaveCount(0);
				await expect(content).not.toContainText('![Mock screen]');

				await note.scrollIntoViewIfNeeded();
				await expect(page.locator('.cm-md-link', { hasText: 'Remote' })).toBeInViewport();
				await saveEvidenceScreenshot(page, testInfo, `doc-inline-images-${width}-missing-and-remote`, { fullPage: false });

				expect(outsideRequests, outsideRequests.join('\n')).toEqual([]);
			} finally {
				fs.rmSync(dir, { recursive: true, force: true });
			}
		});
	}
});
