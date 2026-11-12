import path from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { holdPersonBatches, installWorkspace, openThread, seedHeldComments, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel, turnOffDocumentComments } from '../support/heldComments';

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
		await setDebugVersionOverlay(request, false);
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
		// A comments request still in flight when the test ends would fail its route callback with a closed page.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
	});
}

/**
 * The worst header at once: Send 99+, a reviewer group that overflows into "+2", "120 threads · 45 unread" and four
 * readers in the presence row. The tool cluster cannot shrink, so wherever the header is narrow it has to fit inside
 * it: on one row where one row can hold it, and wrapped inside the header where it cannot. Full window from 320 to
 * 1440 px (a fresh load at each phone and tablet width, one desktop load resized across the rest; the Workspace panel closed so the document has the room) and a narrow
 * desktop split at 1440 and 1024, and a wide pane (1000 px) that holds the full label. The delivery label is short exactly where the header is under the one threshold.
 */
const LABEL_THRESHOLD = 820;
const PHONE_SWEEP = [...Array.from({ length: 12 }, (_, index) => 320 + index * 40), 393].sort((a, b) => a - b); // 320..760, a fresh load each: the phone layout is chosen at load
const DESKTOP_SWEEP = [...Array.from({ length: 17 }, (_, index) => 800 + index * 40), 900].sort((a, b) => a - b); // 800..1440, resized within one desktop load
const SPLITS = [{ viewport: 1440, split: 360 }, { viewport: 1024, split: 360 }, { viewport: 1920, split: 1000 }];
const FRAME_SIZES = [393, 900];

async function measureTools(page: Page) {
	return page.evaluate(() => {
		const header = document.querySelector<HTMLElement>('.document-header')!;
		const tools = header.querySelector<HTMLElement>('.document-tools')!;
		const style = getComputedStyle(header);
		const box = (element: Element) => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
		const outer = header.getBoundingClientRect();
		const content = { left: outer.left + parseFloat(style.paddingLeft), right: outer.right - parseFloat(style.paddingRight) };
		const controls = [...tools.querySelectorAll<HTMLElement>('.sending-now .btn, .delivery-toggle, .comment-status, .reviewer-group, .review-request, .history-tool, .resolved-tool')]
			.filter((control) => control.getBoundingClientRect().width > 0)
			.map((control) => ({ name: control.getAttribute('aria-label') ?? control.className.split(' ')[0], ...box(control) }));
		const label = tools.querySelector<HTMLElement>('.delivery-toggle > span')!;
		const pill = tools.querySelector<HTMLElement>('.comment-status')!;
		const parts = [...pill.querySelectorAll<HTMLElement>('.count, .count-unit, .unread')].map((part) => {
			const partStyle = getComputedStyle(part);
			return { name: part.className.split(' ')[0], shown: partStyle.display !== 'none' && partStyle.visibility !== 'hidden', width: part.getBoundingClientRect().width, fits: part.scrollWidth <= part.clientWidth || partStyle.display === 'inline', ...box(part) };
		});
		const saved = tools.style.cssText;
		tools.style.cssText = 'position:fixed;left:0;top:0;flex-wrap:nowrap;max-inline-size:none;width:max-content';
		const natural = tools.getBoundingClientRect().width;
		tools.style.cssText = saved;
		return {
			natural, rows: new Set(controls.map((control) => Math.round(control.top))).size,
			content, tools: box(tools), controls, parts, labelCompact: getComputedStyle(label).fontSize === '0px', headerContentWidth: content.right - content.left,
			pillText: pill.innerText.replace(/\s+/g, ' ').trim(),
			sendText: tools.querySelector<HTMLElement>('.sending-now .btn')?.textContent?.trim() ?? null,
			reviewerMore: tools.querySelector('.reviewer-group .more')?.textContent ?? null,
		};
	});
}

type Measured = Awaited<ReturnType<typeof measureTools>>;
function expectWholeInsideOneRow(measured: Measured, label: string) {
	expect.soft(measured.sendText, `${label}: Send 99+ is in the row`).toMatch(/^Send 10\d comments?$/);
	expect.soft(measured.reviewerMore, `${label}: the reviewer group overflows into +2`).toBe('+2');
	expect.soft(measured.pillText, `${label}: the count reads whole`).toMatch(/^120 threads · 45 unread$/);
	for (const control of measured.controls) {
		expect.soft(control.left, `${label}: ${control.name} starts inside the header`).toBeGreaterThanOrEqual(measured.content.left - 0.5);
		expect.soft(control.right, `${label}: ${control.name} ends inside the header`).toBeLessThanOrEqual(measured.content.right + 0.5);
	}
	const centres = measured.controls.map((control) => (control.top + control.bottom) / 2);
	const spread = Math.max(...centres) - Math.min(...centres);
	// One row wherever one row can hold the cluster; where it cannot, it wraps inside the header instead of clipping.
	if (measured.natural <= measured.headerContentWidth + 0.5 || measured.headerContentWidth >= LABEL_THRESHOLD) expect.soft(spread, `${label}: the tool cluster is one row`).toBeLessThanOrEqual(2);
	expect.soft(measured.labelCompact, `${label}: the delivery label is short exactly below ${LABEL_THRESHOLD}px`).toBe(measured.headerContentWidth < LABEL_THRESHOLD);
	for (const part of measured.parts) {
		expect.soft(part.shown && part.width > 0 && part.fits, `${label}: ${part.name} is shown and not clipped`).toBe(true);
	}
}

test('@flow:doc-toolbar-count-fits @surface:documents the whole tool cluster stays inside the header at every width, worst content at once', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(600_000);
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	const title = `toolbar-worst-${Date.now()}-a-document-title-long-enough-to-prove-the-header-keeps-its-tools-together`;
	const threadId = await createSession(request, title);
	const doc = await seedTextDocument(request, threadId, title, 'worst');
	await seedHeldComments(request, doc.artifactId, 101);
	// Seeding releases the hold on its way out, and a long sweep would then see the 101 delivered; this state is disposable, so keep the hold for the whole run.
	holdPersonBatches(true);
	const shown = { threads: 120, unread: 45 };
	await page.route(new RegExp(`/api/artifacts/${doc.artifactId}/comments(\\?|$)`), async (route) => {
		if (route.request().method() !== 'GET') return route.fallback();
		const response = await route.fetch();
		const body = await response.json();
		const [row] = body.comments;
		body.comments = Array.from({ length: shown.threads }, (_, index) => ({ ...row, id: index === 0 ? row.id : `${row.id}-copy-${index}`, new_comment: index < shown.unread }));
		await route.fulfill({ response, json: body });
	});
	await page.route(new RegExp(`/api/artifacts/${doc.artifactId}/reviews(\\?|$)`), async (route) => {
		const reviews = Array.from({ length: 5 }, (_, index) => ({
			id: `review-${index}`, reviewer_thread_id: `reviewer-${index}`, reviewer_name: `Reviewer ${index}`, requester_name: 'You',
			created_at: new Date().toISOString(), may_cancel: false, state: 'delivered', outcome: 'open', comment_count: 0,
		}));
		await route.fulfill({ json: { reviews, reviewers: [], taggable_agents: [] } });
	});
	const frames = process.env.CORSIA_FRAMES_DIR;
	if (frames) mkdirSync(frames, { recursive: true });
	const results: Array<{ label: string } & Measured> = [];
	const readers = async () => page.evaluate(async (artifactId) => {
		const { recordDocActivity } = await import('/src/lib/docPresence.ts');
		for (let index = 0; index < 4; index++) recordDocActivity({ type: 'doc_activity', artifact_id: artifactId, thread_id: `reader-${index}`, agent: `Reader ${index}`, kind: 'read', span: null, comment_id: null, revision: null, at: new Date().toISOString() });
	}, doc.artifactId);
	const open = async (viewport: number, split: number | null) => {
		await page.setViewportSize({ width: viewport, height: viewport < 700 ? 852 : 900 });
		await installWorkspace(page, threadId, [doc], 0, split ? { splitWidth: split } : {});
		const load = async () => {
			await openThread(page, threadId, title);
			await expect(page.locator('.cm-content')).toContainText('A commentable document.');
			if (!split) await page.evaluate(async () => { const { setPaneCollapsed } = await import('/src/lib/stores/paneCollapsed.ts'); setPaneCollapsed(true, true); });
			await turnOffDocumentComments(page);
			await expect(page.locator('.document-header .comment-status')).toContainText('120 threads');
			await readers();
			await expect(page.locator('.document-header .reviewer-group .more')).toHaveText('+2');
		};
		await load();
		await expect(page.locator('.document-header .sending-now .btn')).toBeVisible();
	};
	const take = async (viewport: number, split: number | null) => {
		// Measure once the window has the width asked for, no animation is running, and the header's width holds across two frames.
		await expect.poll(() => page.evaluate((expected) => new Promise<string>((resolve) => {
			const read = () => `${window.innerWidth === expected ? 'at' : 'not'}:${Math.round(document.querySelector('.document-header')!.getBoundingClientRect().width * 10)}:${document.getAnimations().length}`;
			const first = read();
			requestAnimationFrame(() => requestAnimationFrame(() => { const second = read(); resolve(first === second && second.startsWith('at:') && second.endsWith(':0') ? 'settled' : 'moving'); }));
		}), viewport), { message: 'the header layout settles', timeout: 15_000 }).toBe('settled');
		const label = split ? `${viewport}px window, pane ${split}px` : `${viewport}px window`;
		const measured = await measureTools(page);
		results.push({ label, ...measured });
		expectWholeInsideOneRow(measured, label);
		if (frames && (split || FRAME_SIZES.includes(viewport))) await page.screenshot({ path: path.join(frames, `worst-${split ? `${viewport}-split-${split}` : viewport}.png`), clip: { x: 0, y: 0, width: viewport, height: 240 } });
	};
	for (const width of PHONE_SWEEP) {
		await open(width, null);
		await take(width, null);
	}
	await open(DESKTOP_SWEEP[0], null);
	for (const width of DESKTOP_SWEEP) {
		await page.setViewportSize({ width, height: 900 });
		await take(width, null);
	}
	for (const { viewport, split } of SPLITS) {
		await open(viewport, split);
		await take(viewport, split);
	}
	await page.unrouteAll({ behavior: 'ignoreErrors' });
	if (frames) writeFileSync(path.join(frames, 'worst-sweep.json'), JSON.stringify(results.map(({ label, headerContentWidth, labelCompact, controls, tools, ...rest }) => ({ label, ...rest, headerContentWidth, labelCompact, toolsLeft: tools.left, controlLeft: Math.min(...controls.map((control) => control.left)) })), null, 1));
});
