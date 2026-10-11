import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

/**
 * The document canvas's side padding. At phone width with a touch pointer the text keeps the same 24px inset on both
 * sides (the 44px comment marker's hit box reaches over the text's edge instead of widening the left inset), and the
 * document header clears the app header, which is taller by the device's top safe area. A fine pointer at desktop
 * width keeps the one-sided rail: 24px left, nothing right.
 */

const INSET = 24;
const SAFE_TOP = 59;
const TEXT = '# Heading padding probe\n\nA paragraph of ordinary prose that is long enough to wrap onto a second and a third line at phone width, so both edges of the text column are visible.\n\n| Name | Value |\n| --- | --- |\n| alpha | one |\n| beta | two |\n\nTail line here.\n';

// A fine pointer keeps today's layout at any width, narrow windows included.
const WIDTHS = [393, 1440, 560];

for (const width of WIDTHS) {
	test(`@flow:doc-canvas-phone-padding ${width}px: the text keeps equal side insets, markers keep their hit box, and the header clears the app header`, async ({ page }, info) => {
		const mobile = info.project.name.includes('mobile');
		test.skip(mobile !== (width === 393), 'a touch pointer runs the phone width; a fine pointer runs the desktop widths');
		await page.setViewportSize({ width, height: mobile ? 852 : 960 });
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		const request = page.request;
		const params = { token: TEST_TOKEN };
		const title = `phone-padding-${Date.now()}`;
		const threadId = await createSession(request, title);
		const tree = await request.get('/api/files/tree', { params });
		expect(tree.ok()).toBeTruthy();
		const workspace = (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
		const fileName = `${title}.md`;
		const docPath = path.join(workspace, fileName);
		const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: TEXT, thread_id: threadId, create: true } });
		expect(seeded.ok(), await seeded.text()).toBeTruthy();
		const artifactId: string = (await seeded.json()).artifact.id;
		const quoted = 'ordinary prose';
		const from = TEXT.indexOf(quoted);
		const comment = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { body: 'A comment', anchor_text: quoted, from_off: from, to_off: from + quoted.length } });
		expect(comment.ok(), await comment.text()).toBeTruthy();
		await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
			localStorage.setItem('corsia_theme', 'light');
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: {
					tabs: [{ instanceId: 'padding-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'padding-tab', visible: true, activeSurface: 'artifact',
				} }, splitWidth: null,
			}));
		}, { threadId, docPath, fileName, artifactId });
		if (mobile) {
			// The phone's top safe area (a notch) makes the app header taller than its nominal height.
			const session = await page.context().newCDPSession(page);
			await session.send('Emulation.setSafeAreaInsetsOverride' as never, { insets: { top: SAFE_TOP, bottom: 0, left: 0, right: 0 } } as never);
		}
		await loginByUrl(page, { waitForComposer: true });
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByText(title, { exact: true }).first().click();
		await expect(page.locator('.cm-content')).toContainText('Tail line here.');
		await expect(page.getByRole('button', { name: '1 comment', exact: true })).toBeEnabled();
		await expect(page.locator('.cm-comment-marker')).toHaveCount(1);

		const measured = await page.evaluate(() => new Promise<{
			left: number; right: number; padLeft: number; padRight: number;
			marker: { left: number; right: number; top: number; width: number; height: number };
			glyph: { left: number; right: number };
			scrollerLeft: number; gutter: number;
			headerTop: number; appHeaderBottom: number; titleHit: boolean;
		}>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => {
			const line = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => candidate.textContent?.startsWith('A paragraph'))!;
			const rect = line.getBoundingClientRect();
			const scroller = document.querySelector('.cm-scroller')!.getBoundingClientRect();
			const content = getComputedStyle(document.querySelector('.cm-content')!);
			const marker = document.querySelector<HTMLElement>('.cm-comment-marker')!.getBoundingClientRect();
			const glyph = document.querySelector('.cm-comment-marker svg')!.getBoundingClientRect();
			const header = document.querySelector('.document-header')!.getBoundingClientRect();
			const heading = document.querySelector('.document-title')!.getBoundingClientRect();
			const hit = document.elementFromPoint(heading.left + heading.width / 2, heading.top + heading.height / 2);
			resolve({
				left: rect.left - scroller.left, right: scroller.right - rect.right,
				padLeft: Number.parseFloat(content.paddingLeft), padRight: Number.parseFloat(content.paddingRight),
				marker: { left: marker.left, right: marker.right, top: marker.top, width: marker.width, height: marker.height },
				glyph: { left: glyph.left, right: glyph.right },
				scrollerLeft: scroller.left,
				gutter: Number.parseFloat(getComputedStyle(document.querySelector('.leaf-body.document-canvas')!).paddingLeft),
				headerTop: header.top, appHeaderBottom: document.querySelector('.shell-header')!.getBoundingClientRect().bottom,
				titleHit: !!hit?.closest('.document-title'),
			});
		}))));

		if (mobile) {
			expect(measured.padLeft, 'text inset, left').toBe(INSET);
			expect(measured.padRight, 'text inset, right').toBe(INSET);
			// Measured from the scroller's own edges: the canvas can still be settling at a fractional offset, which moves both edges alike.
			expect(Math.abs(measured.left - measured.right), `the left (${measured.left}) and right (${measured.right}) insets match`).toBeLessThanOrEqual(0.5);
			expect(measured.gutter, 'the phone page gutter').toBe(12);
			expect(measured.marker.width, 'marker hit width').toBeCloseTo(44, 0);
			expect(measured.marker.height, 'marker hit height').toBeCloseTo(44, 0);
			expect(measured.glyph.right - measured.scrollerLeft, 'the glyph sits clear of the text').toBeLessThanOrEqual(measured.left);
			expect(measured.glyph.left, 'the glyph is inside the canvas').toBeGreaterThanOrEqual(measured.scrollerLeft);
			expect(measured.marker.right - measured.scrollerLeft, 'the hit box reaches over the text edge').toBeGreaterThan(measured.left);
			expect(measured.appHeaderBottom, 'the app header is taller than its nominal height by the safe area').toBeGreaterThan(64);
			expect(measured.headerTop, 'the document header starts below the app header').toBeGreaterThanOrEqual(measured.appHeaderBottom);
			expect(measured.titleHit, 'the document title is not under the app header').toBe(true);
			// A tap on the part of the box that overlaps the text opens the thread rather than placing a caret.
			await page.mouse.click(measured.marker.right - 4, measured.marker.top + 22);
			await expect(page.getByText('A comment', { exact: true }).first()).toBeVisible();
		} else {
			expect([measured.padLeft, measured.padRight], 'a fine pointer keeps the one-sided rail').toEqual([INSET, 0]);
			expect(measured.marker.width, 'marker hit width').toBeCloseTo(24, 0);
			expect(measured.gutter, 'the page gutter is unchanged').toBe(20);
		}
	});
}
