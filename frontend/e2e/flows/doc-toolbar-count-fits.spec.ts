import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

/**
 * The document toolbar's "N threads · N unread" count shows whole, inside the toolbar and clear of the other controls,
 * at a phone's 393 px and at 900 and 1440, with one-, two- and three-digit counts. The row stays one row tall.
 */

const TEXT = '# Toolbar count probe\n\nA paragraph of ordinary prose to hold a comment.\n';
const COUNTS = [{ threads: 1, unread: 1 }, { threads: 12, unread: 3 }, { threads: 120, unread: 45 }];
const CASES = [
	{ width: 393, scheme: 'light' as const }, { width: 393, scheme: 'dark' as const },
	{ width: 900, scheme: 'light' as const }, { width: 1440, scheme: 'light' as const },
];
const FRAMES = process.env.CORSIA_FRAMES_DIR;

for (const { width, scheme } of CASES) {
	test(`@flow:doc-toolbar-count-fits @surface:documents ${width}px ${scheme}: the thread and unread count shows whole`, async ({ page }, info) => {
		const mobile = info.project.name.includes('mobile');
		test.skip(mobile !== (width === 393), 'a touch pointer runs the phone width; a fine pointer runs the desktop widths');
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height: mobile ? 852 : 960 });
		await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'no-preference' });
		const request = page.request;
		const params = { token: TEST_TOKEN };
		const title = `toolbar-count-${width}-${scheme}-${Date.now()}`;
		const threadId = await createSession(request, title);
		const tree = await request.get('/api/files/tree', { params });
		const workspace = (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
		const fileName = `${title}.md`;
		const docPath = path.join(workspace, fileName);
		const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: TEXT, thread_id: threadId, create: true } });
		expect(seeded.ok(), await seeded.text()).toBeTruthy();
		const artifactId: string = (await seeded.json()).artifact.id;
		const quoted = 'ordinary prose';
		const from = TEXT.indexOf(quoted);
		const comment = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { body: 'A comment', anchor_text: quoted, from_off: from, to_off: from + quoted.length, delivery_policy: 'immediate' } });
		expect(comment.ok(), await comment.text()).toBeTruthy();
		await page.addInitScript(({ threadId, docPath, fileName, artifactId, scheme }) => {
			localStorage.setItem('corsia_theme', scheme);
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: {
					tabs: [{ instanceId: 'count-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'count-tab', visible: true, activeSurface: 'artifact',
				} }, splitWidth: null,
			}));
		}, { threadId, docPath, fileName, artifactId, scheme });
		const shown = { threads: 1, unread: 0 };
		await page.route(new RegExp(`/api/artifacts/${artifactId}/comments(\\?|$)`), async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const response = await route.fetch();
			const body = await response.json();
			const [row] = body.comments;
			body.comments = Array.from({ length: shown.threads }, (_, index) => ({ ...row, id: index === 0 ? row.id : `${row.id}-copy-${index}`, new_comment: index < shown.unread }));
			await route.fulfill({ response, json: body });
		});
		await loginByUrl(page, { waitForComposer: true });
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByText(title, { exact: true }).first().click();
		await expect(page.locator('.cm-content')).toContainText('ordinary prose');
		await expect(page.getByRole('button', { name: /^1 thread\b/ })).toBeEnabled();
		if (FRAMES) mkdirSync(FRAMES, { recursive: true });

		for (const { threads, unread } of COUNTS) {
			// The server's own answer, with the one real thread copied `threads` times, the first `unread` of them unread; the viewer loads it as it would any list.
			shown.threads = threads; shown.unread = unread;
			await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), artifactId);
			const pill = page.locator('.document-header .comment-status');
			await expect(pill).toHaveText(new RegExp(`${threads}\\s+${threads === 1 ? 'thread' : 'threads'}\\s+·\\s+${unread} unread`), { useInnerText: true });
			// A person sees the settled state: nothing may redraw the pill after the store update.
			await page.waitForTimeout(800);
			await expect(pill).toHaveText(new RegExp(`${threads}\\s+${threads === 1 ? 'thread' : 'threads'}\\s+·\\s+${unread} unread`), { useInnerText: true });
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			const measured = await page.evaluate(() => {
				const box = (element: Element) => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
				const header = document.querySelector('.document-header')!;
				const tools = header.querySelector('.document-tools')!;
				const pillEl = header.querySelector<HTMLElement>('.comment-status')!;
				const text = [...pillEl.querySelectorAll<HTMLElement>('.count, .count-unit, .unread')];
				return {
					header: box(header), tools: box(tools), pill: box(pillEl),
					pillScroll: { scrollWidth: pillEl.scrollWidth, clientWidth: pillEl.clientWidth },
					text: text.map((part) => ({ name: part.className, shown: getComputedStyle(part).display !== 'none' && getComputedStyle(part).visibility !== 'hidden', fits: part.scrollWidth <= part.clientWidth || getComputedStyle(part).display === 'inline', ...box(part) })),
					siblings: [...tools.querySelectorAll<HTMLElement>('button, [role="switch"], .save-ghost')]
						.filter((el) => el !== pillEl && !pillEl.contains(el) && el.getBoundingClientRect().width > 0)
						.map((el) => ({ name: el.getAttribute('aria-label') ?? el.className, ...box(el) })),
					rowHeights: [...tools.children].map((el) => el.getBoundingClientRect().height),
				};
			});
			const label = `${threads}/${unread} at ${width} ${scheme}`;
			if (FRAMES) await page.screenshot({ path: path.join(FRAMES, `${width}-${scheme}-${threads}-${unread}.png`), clip: { x: 0, y: 0, width, height: Math.min(300, measured.header.bottom + 60) } });
			expect.soft(measured.pill.left, `${label}: the pill starts inside the toolbar`).toBeGreaterThanOrEqual(measured.header.left - 0.5);
			expect.soft(measured.pill.right, `${label}: the pill ends inside the toolbar`).toBeLessThanOrEqual(measured.header.right + 0.5);
			expect.soft(measured.pillScroll.scrollWidth, `${label}: the pill clips none of its text`).toBeLessThanOrEqual(measured.pillScroll.clientWidth);
			expect.soft(measured.text.map((part) => part.name.split(' ')[0]), `${label}: the number, its noun and the unread part all exist`).toEqual(['count', 'count-unit', 'unread']);
			for (const part of measured.text) {
				expect.soft(part.shown, `${label}: ${part.name} is displayed`).toBe(true);
				expect.soft(part.right - part.left, `${label}: ${part.name} has width`).toBeGreaterThan(0);
				expect.soft(part.fits, `${label}: ${part.name} is not clipped inside itself`).toBe(true);
				expect.soft(part.left, `${label}: ${part.name} starts inside the pill`).toBeGreaterThanOrEqual(measured.pill.left - 0.5);
				expect.soft(part.right, `${label}: ${part.name} ends inside the pill`).toBeLessThanOrEqual(measured.pill.right + 0.5);
			}
			expect.soft(measured.tools.left, `${label}: the tools start inside the toolbar`).toBeGreaterThanOrEqual(measured.header.left - 0.5);
			for (const other of measured.siblings) {
				const apart = other.right <= measured.pill.left + 0.5 || other.left >= measured.pill.right - 0.5 || other.bottom <= measured.pill.top + 0.5 || other.top >= measured.pill.bottom - 0.5;
				expect.soft(apart, `${label}: ${other.name} does not overlap the pill ${JSON.stringify([other, measured.pill])}`).toBe(true);
			}
			expect.soft(measured.tools.bottom - measured.tools.top, `${label}: the tools stay one row`).toBeLessThanOrEqual(Math.max(...measured.rowHeights) + 1);
			await info.attach(`count-${threads}-${unread}`, { body: JSON.stringify(measured), contentType: 'application/json' });
		}
	});
}
