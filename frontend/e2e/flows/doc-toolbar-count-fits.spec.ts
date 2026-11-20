import path from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
import { test } from '../support/test';
import { expect, type Page, type Route } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { closeDetails, documentBarHost, expectBarHitAreas, expectOneRow, expectTouchTargets, openDetails } from '../support/documentBar';
import { raiseAsk } from '../support/reviewAsk';
import { closeComments, openComments } from '../support/commentDrawer';
import { holdPersonBatches, installWorkspace, openThread, seedHeldComments, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel, turnOffDocumentComments } from '../support/heldComments';

/**
 * Every count the document's bar shows reads whole, inside the bar and clear of its neighbours, on one row: the Comments button
 * (the unread number on the phone header, "N unread" on desktop, the total otherwise; its name says both) and the amber count beside Details (what behind it needs
 * you). At 320, 360 and 393 px phones, 700, 900 and 1440, with one-, two- and three-digit comment counts. What used to share the
 * toolbar (Send, Resolved, Deliver immediately, reviewers, Request review, History) is in Details; its switch keeps a whole hit area there.
 */

const TEXT = '# Toolbar count probe\n\nA paragraph of ordinary prose to hold a comment.\n';
const COUNTS = [{ threads: 1, unread: 1 }, { threads: 12, unread: 3 }, { threads: 120, unread: 45 }];
const CASES = [
	{ width: 320, scheme: 'light' as const }, { width: 360, scheme: 'light' as const },
	{ width: 393, scheme: 'light' as const }, { width: 393, scheme: 'dark' as const },
	// A window under 768 px whose canvas is wider than 600 px.
	{ width: 700, scheme: 'light' as const },
	{ width: 900, scheme: 'light' as const }, { width: 1440, scheme: 'light' as const },
	// The receipt state: every comment sent and three of them resolved, so Details holds "Resolved · 3" and no Send.
	{ width: 320, scheme: 'light' as const, resolved: 3 },
];
const FRAMES = process.env.CORSIA_FRAMES_DIR;
/** What the Comments button shows in the compact phone header or desktop bar. */
const countText = (threads: number, unread: number, phoneHeader: boolean) => unread ? `${unread}${phoneHeader ? '' : ' unread'}` : `${threads}`;
/** Its accessible name carries both, and what waits for Send. */
const countName = (threads: number, unread: number) => new RegExp(`^${threads} ${threads === 1 ? 'comment' : 'comments'}${unread ? `, ${unread} unread` : ''}(?:, \\d+ waiting for Send)?$`);
/** The bar's controls are 44 px touch targets in a window up to 768 px and 36 px with a pointer (DocViewerStatus.svelte, `--bar-row`). */
const barMin = (page: Page) => (page.viewportSize()?.width ?? 0) <= 768 ? 44 : 36;

/** The document's content read answers Overdue, so Details carries one thing that needs you: its amber count. */
async function answerOverdue(page: Page) {
	await page.route(/\/api\/files\/content\?/, async (route: Route) => {
		if (route.request().method() !== 'GET') return route.fallback();
		const response = await route.fetch();
		const verified = new Date(Date.now() - 44 * 86_400_000).toISOString();
		await route.fulfill({ response, json: { ...(await response.json()), freshness: { class: 'reference', verified_at: verified, age_days: 44, due_at: new Date(Date.now() - 14 * 86_400_000).toISOString(), state: 'overdue', owner_name: 'Structured Plans' } } });
	});
}

/** The bar's two counts, measured where a person sees them. */
async function measureBar(page: Page) {
	return documentBarHost(page).evaluate((bar) => {
		const box = (element: Element) => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
		const comments = bar.querySelector<HTMLElement>('[data-comments-entry]')!;
		const details = bar.querySelector<HTMLElement>('[data-document-details]')!;
		const needs = details.querySelector<HTMLElement>('.bar-needs');
		const inner = comments.querySelector<HTMLElement>('span:last-of-type');
		return {
			bar: box(bar), comments: box(comments), details: box(details), needs: needs ? box(needs) : null,
			name: bar.querySelector('[data-testid="header-document-title"]') ? box(bar.querySelector('[data-testid="header-document-title"]')!) : null,
			controls: [...bar.querySelectorAll<HTMLElement>('button, [role="button"], .participant-slot')].filter((el) => el.getBoundingClientRect().width > 0).map((el) => ({ name: el.getAttribute('aria-label') ?? el.getAttribute('title') ?? el.className, box: box(el) })),
			commentsText: comments.innerText.replace(/\s+/g, ' ').trim(), commentsName: comments.getAttribute('aria-label'),
			phoneHeader: !!comments.closest('.shell-header') && window.innerWidth <= 768,
			needsText: needs?.innerText.trim() ?? null, detailsName: details.getAttribute('aria-label'),
			// Nothing inside either button is cut: the button holds its text, and the text span holds its own.
			clipped: [comments, details, ...(inner ? [inner] : []), ...(needs ? [needs] : [])].filter((element) => element.scrollWidth > element.clientWidth + 0.5 && getComputedStyle(element).display !== 'inline').map((element) => element.className),
			pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
			viewport: { width: window.innerWidth },
		};
	});
}
type BarMeasured = Awaited<ReturnType<typeof measureBar>>;

/** The ask this spec raises, in its own words: the Details name repeats them when its card is hidden. */
const ASK_TEXT = 'Is this skill ready?';
const OVERDUE_WORD = 'Overdue';

/** Each count is whole, inside the bar and the window; no control in the bar overlaps another; the page does not scroll sideways. */
async function expectCountsWhole(page: Page, measured: BarMeasured, label: string, { threads, unread, needs, needsWords }: { threads: number; unread: number; needs: number; needsWords: string[] }) {
	expect.soft(measured.commentsText, `${label}: the Comments count reads whole`).toBe(countText(threads, unread, measured.phoneHeader));
	expect.soft(measured.commentsName, `${label}: the Comments button names the total and the unread`).toMatch(countName(threads, unread));
	expect.soft(measured.needsText, `${label}: the amber count beside Details`).toBe(needs ? `${needs}` : null);
	expect.soft(measured.detailsName, `${label}: Details names what waits, not how many`).toBe(`Document details${needsWords.length ? ` · ${needsWords.join(' · ')}` : ''}`);
	expect.soft(measured.needsText ? measured.needsText.trim().length : 0, `${label}: the amber count and the named needs are the same number`).toBe(needsWords.length);
	expect.soft(measured.clipped, `${label}: no count is cut`).toEqual([]);
	for (const [name, control] of [['Comments', measured.comments], ['Details', measured.details], ...(measured.needs ? [['amber count', measured.needs] as const] : [])] as const) {
		expect.soft(control.left, `${label}: ${name} starts inside the bar`).toBeGreaterThanOrEqual(measured.bar.left - 0.5);
		expect.soft(control.right, `${label}: ${name} ends inside the bar`).toBeLessThanOrEqual(measured.bar.right + 0.5);
		expect.soft(control.right, `${label}: ${name} ends inside the window`).toBeLessThanOrEqual(measured.viewport.width + 0.5);
	}
	if (measured.needs) {
		expect.soft(measured.needs.left, `${label}: the amber count sits inside Details`).toBeGreaterThanOrEqual(measured.details.left - 0.5);
		expect.soft(measured.needs.right, `${label}: the amber count sits inside Details`).toBeLessThanOrEqual(measured.details.right + 0.5);
	}
	expect.soft(measured.pageOverflow, `${label}: the page does not scroll sideways`).toBeLessThanOrEqual(1);
	await expectTouchTargets(documentBarHost(page), '[data-comments-entry], [data-document-details]', { min: barMin(page) });
	await expectOneRow(page);
}

/** Waits until the window has the width asked for, no animation is running, and the bar's width holds across two frames. */
async function settled(page: Page, width: number) {
	await expect.poll(() => page.evaluate((expected) => new Promise<string>((resolve) => {
		const read = () => `${window.innerWidth === expected ? 'at' : 'not'}:${Math.round((document.querySelector('[data-comments-entry]')?.getBoundingClientRect().right ?? 0) * 10)}:${document.getAnimations().length}`;
		const first = read();
		requestAnimationFrame(() => requestAnimationFrame(() => { const second = read(); resolve(first === second && second.startsWith('at:') && second.endsWith(':0') ? 'settled' : 'moving'); }));
	}), width), { message: `the bar settles at ${width}`, timeout: 15_000 }).toBe('settled');
}

for (const { width, scheme, resolved } of CASES) {
	test(`@flow:doc-toolbar-count-fits @surface:documents ${width}px ${scheme}${resolved ? ' with Resolved' : ''}: the bar's counts show whole`, async ({ page }, info) => {
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
		const shown = { threads: 1, unread: 0, resolved: 0 };
		await page.route(new RegExp(`/api/artifacts/${artifactId}/comments(\\?|$)`), async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const response = await route.fetch();
			const body = await response.json();
			const [row] = body.comments;
			body.comments = Array.from({ length: shown.threads }, (_, index) => ({ ...row, id: index === 0 ? row.id : `${row.id}-copy-${index}`, new_comment: index < shown.unread, resolved: index < shown.resolved }));
			await route.fulfill({ response, json: body });
		});
		await answerOverdue(page);
		await loginByUrl(page, { waitForComposer: true });
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByText(title, { exact: true }).first().click();
		await expect(page.locator('.cm-content')).toContainText('ordinary prose');
		await expect(page.getByRole('button', { name: /^1 comment\b/ })).toBeEnabled();
		await expect(page.locator('[data-document-details]')).toHaveAccessibleName('Document details · Overdue');
		if (FRAMES) mkdirSync(FRAMES, { recursive: true });

		if (resolved) {
			// Three resolved threads, nothing held: the drawer holds "Resolved · 3" and no Send, whole and inside the drawer.
			shown.threads = resolved; shown.unread = 0; shown.resolved = resolved;
			await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), artifactId);
			await openComments(page);
			const panel = page.locator('[data-doc-comments-drawer]');
			const entry = panel.locator('[data-resolved-entry]');
			await expect(entry).toHaveText(`Resolved · ${resolved}`);
			await expect(panel.locator('.sending-now')).toHaveCount(0);
			await page.waitForTimeout(800);
			const label = `Resolved · ${resolved} at ${width} ${scheme}`;
			const fit = await panel.evaluate((element) => {
				const panelBox = element.getBoundingClientRect();
				const control = element.querySelector<HTMLElement>('[data-resolved-entry]')!;
				const box = control.getBoundingClientRect();
				return { clipped: control.scrollWidth > control.clientWidth, inside: box.left >= panelBox.left - 0.5 && box.right <= panelBox.right + 0.5, sideways: element.scrollWidth - element.clientWidth };
			});
			expect.soft(fit.clipped, `${label}: Resolved is not clipped`).toBe(false);
			expect.soft(fit.inside, `${label}: Resolved sits inside the drawer`).toBe(true);
			expect.soft(fit.sideways, `${label}: the drawer does not scroll sideways`).toBeLessThanOrEqual(1);
			if (width <= 768) await expectTouchTargets(panel, '[aria-label="Close comments"]', { min: 44 });
			if (FRAMES) await page.screenshot({ path: path.join(FRAMES, `${width}-${scheme}-resolved-${resolved}.png`), fullPage: false });
			await closeComments(page);
			// The Comments button counts open threads, and all three are resolved.
			await expectCountsWhole(page, await measureBar(page), label, { threads: 0, unread: 0, needs: 1, needsWords: [OVERDUE_WORD] });
			await page.unrouteAll({ behavior: 'ignoreErrors' });
			return;
		}
		// With nothing unread, the bar's height: the counts below must not change it.
		shown.threads = 1; shown.unread = 0;
		await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), artifactId);
		await expect(page.locator('[data-comments-entry]')).toHaveAccessibleName(countName(1, 0));
		await page.waitForTimeout(800);
		const idleHeight = await documentBarHost(page).evaluate((bar) => bar.getBoundingClientRect().height);
		for (const { threads, unread } of [{ threads: 1, unread: 0 }, ...COUNTS]) {
			// The server's own answer, with the one real thread copied `threads` times, the first `unread` of them unread; the viewer loads it as it would any list.
			shown.threads = threads; shown.unread = unread;
			await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), artifactId);
			const pill = page.locator('[data-comments-entry]');
			const phoneHeader = await pill.evaluate((el) => !!el.closest('.shell-header') && window.innerWidth <= 768);
			await expect(pill).toHaveText(countText(threads, unread, phoneHeader), { useInnerText: true });
			// A person sees the settled state: nothing may redraw the button after the store update.
			await page.waitForTimeout(800);
			await expect(pill).toHaveText(countText(threads, unread, phoneHeader), { useInnerText: true });
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			const measured = await measureBar(page);
			const label = `${threads}/${unread} at ${width} ${scheme}`;
			if (FRAMES) await page.screenshot({ path: path.join(FRAMES, `${width}-${scheme}-${threads}-${unread}.png`), clip: { x: 0, y: 0, width, height: Math.min(300, measured.bar.bottom + 60) } });
			await expectCountsWhole(page, measured, label, { threads, unread, needs: 1, needsWords: [OVERDUE_WORD] });
			expect.soft(measured.bar.bottom - measured.bar.top, `${label}: the bar keeps its height when the count changes`).toBeCloseTo(idleHeight, 0);
			await info.attach(`count-${threads}-${unread}`, { body: JSON.stringify(measured), contentType: 'application/json' });
		}
		// A comments request still in flight when the test ends would fail its route callback with a closed page.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
	});
}

/**
 * The worst content at once: Send 99+, a reviewer group that overflows into "+2", "120 comments, 45 unread", four readers and
 * Overdue. The bar's counts stay whole on one row at every width from 320 to 1440 px (a fresh load at each phone and tablet width,
 * one desktop load resized across the rest; the Workspace panel closed so the document has the room), in a narrow desktop split
 * at 1440 and 1024, a wide pane (1000 px), and the pane open and closed. At each, Details opens with Send, the reviewers and the
 * delivery switch whole inside it, and the switch's hit area lands every tap on the switch and overlaps nothing.
 */
const PHONE_SWEEP = [...Array.from({ length: 12 }, (_, index) => 320 + index * 40), 375, 393, 700].sort((a, b) => a - b); // 320..760, a fresh load each: the phone layout is chosen at load
const DESKTOP_SWEEP = [...Array.from({ length: 17 }, (_, index) => 800 + index * 40), 900].sort((a, b) => a - b); // 800..1440, resized within one desktop load
const SPLITS = [{ viewport: 1440, split: 360 }, { viewport: 1024, split: 360 }, { viewport: 1920, split: 1000 }];
const FRAME_SIZES = [320, 360, 375, 393, 560, 700, 900];

/** The delivery switch's whole row takes taps, while its visible track keeps the standard size. */
async function measureSwitch(page: Page, width: number) {
	const panel = await openDetails(page);
	const reviewerMore = await panel.locator('.reviewer-group .more').textContent();
	await closeDetails(page);
	await openComments(page);
	await settled(page, width);
	const drawer = page.locator('[data-doc-comments-drawer]');
	const measured = await drawer.evaluate((root) => {
		const toggle = root.querySelector<HTMLElement>('.delivery .tg')!;
		toggle.scrollIntoView({ block: 'nearest' });
		const area = toggle.getBoundingClientRect();
		const panelBox = root.getBoundingClientRect();
		const radius = Math.min(parseFloat(getComputedStyle(toggle).borderTopLeftRadius) || 0, area.width / 2, area.height / 2);
		const x = area.left + area.width / 2, y = area.top + area.height / 2, inset = radius + 1;
		const probes: Array<[number, number]> = [[x, y], [area.left + 1, y], [area.right - 1, y], [x, area.top + 1], [x, area.bottom - 1], [area.left + inset, area.top + inset], [area.right - inset, area.top + inset], [area.left + inset, area.bottom - inset], [area.right - inset, area.bottom - inset]];
		const track = getComputedStyle(toggle, '::before');
		const tapMisses = probes.map(([x, y]) => document.elementFromPoint(x, y)).filter((el) => el?.closest('.tg') !== toggle).map((el) => `${el?.tagName}.${el?.className}`);
		const overlapOf = (a: DOMRect, b: DOMRect) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
		const hitOverlaps = [...root.querySelectorAll<HTMLElement>('button, a[href], [role="button"], [role="switch"]')].filter((control) => {
			if (control === toggle || toggle.contains(control) || control.contains(toggle)) return false;
			const box = control.getBoundingClientRect(), scroller = control.closest<HTMLElement>('.drawer-comments');
			if (!scroller) return box.width > 0 && overlapOf(area, box);
			const clip = scroller.getBoundingClientRect();
			const visible = new DOMRect(Math.max(box.left, clip.left), Math.max(box.top, clip.top), Math.max(0, Math.min(box.right, clip.right) - Math.max(box.left, clip.left)), Math.max(0, Math.min(box.bottom, clip.bottom) - Math.max(box.top, clip.top)));
			return visible.width > 0 && visible.height > 0 && overlapOf(area, visible);
		}).map((control) => control.getAttribute('aria-label') ?? control.className);
		const send = root.querySelector<HTMLElement>('.drawer-foot .btn');
		const sendShort = send?.querySelector<HTMLElement>('.send-short');
		const sendShown = sendShort && getComputedStyle(sendShort).display !== 'none' ? sendShort : send?.querySelector<HTMLElement>('.send-full') ?? send;
		return {
			panel: { left: panelBox.left, right: panelBox.right, top: panelBox.top, bottom: panelBox.bottom },
			controls: [...root.querySelectorAll<HTMLElement>('button, a[href], [role="button"], [role="switch"]')].filter((el) => el.getBoundingClientRect().width > 0).map((el) => { const r = el.getBoundingClientRect(); return { name: el.getAttribute('aria-label') ?? el.textContent?.trim() ?? el.className, box: { left: r.left, right: r.right, top: r.top, bottom: r.bottom } }; }),
			track: { width: parseFloat(track.width), height: parseFloat(track.height) },
			area: { width: Math.round(area.width * 10) / 10, height: Math.round(area.height * 10) / 10 },
			tapMisses, hitOverlaps,
			hitOutside: { left: Math.max(0, panelBox.left - area.left), right: Math.max(0, area.right - panelBox.right), top: Math.max(0, -area.top), bottom: Math.max(0, area.bottom - window.innerHeight) },
			sendText: sendShown?.innerText.trim() ?? null, sendName: send?.getAttribute('aria-label') ?? null,
			deliverLabel: toggle.querySelector<HTMLElement>('.row-label')?.textContent?.trim() ?? null,
			sideways: root.scrollWidth - root.clientWidth,
			panelInWindow: panelBox.left >= -0.5 && panelBox.right <= window.innerWidth + 0.5,
		};
	});
	return { ...measured, reviewerMore };
}

function expectSwitchWhole(measured: Awaited<ReturnType<typeof measureSwitch>>, label: string) {
	const { track, area, tapMisses } = measured;
	expect.soft(Math.abs(track.width - 30) <= 0.5 && Math.abs(track.height - 17) <= 0.5, `${label}: the switch track is 30 x 17, measured ${track.width} x ${track.height}`).toBe(true);
	expect.soft(area.width >= 43.5 && area.height >= 43.5, `${label}: the hit area is at least 44 x 44, measured ${area.width} x ${area.height}`).toBe(true);
	expect.soft(measured.hitOverlaps, `${label}: the switch's hit area overlaps no other control in Details`).toEqual([]);
	expect.soft(measured.hitOutside, `${label}: the switch's hit area stays inside the drawer and window`).toEqual({ left: 0, right: 0, top: 0, bottom: 0 });
	expect.soft(tapMisses, `${label}: taps across the switch row land on the switch`).toEqual([]);
	expect.soft(measured.deliverLabel, `${label}: the delivery label is the full wording`).toBe('Deliver immediately');
	expect.soft(measured.sendText, `${label}: Send's visible label is whole`).toMatch(/^Send(?: ·)? (?:10\d comments?|99\+)$/);
	expect.soft(measured.sendName, `${label}: Send keeps its own number in its name`).toMatch(/^Send 10\d comments?/);
	expect.soft(measured.reviewerMore, `${label}: the reviewer group overflows into +2`).toBe('+2');
	expect.soft(measured.sideways, `${label}: Details does not scroll sideways`).toBeLessThanOrEqual(1);
	expect.soft(measured.panelInWindow, `${label}: Details sits inside the window`).toBe(true);
}

test('@flow:doc-toolbar-count-fits @surface:documents the bar\'s counts stay whole on one row at every width, worst content at once', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(600_000);
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	const title = `toolbar-worst-${Date.now()}-a-document-title-long-enough-to-prove-the-bar-keeps-its-tools-together`;
	const threadId = await createSession(request, title);
	const doc = await seedTextDocument(request, threadId, title, 'worst');
	await seedHeldComments(request, doc.artifactId, 101);
	// Seeding releases the hold on its way out, and a long sweep would then see the 101 delivered; keep the hold for the whole run and drop it afterwards, so the shared isolated DB keeps no trigger for a later spec.
	holdPersonBatches(true);
	try {
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
		await answerOverdue(page);
		const frames = process.env.CORSIA_FRAMES_DIR;
		if (frames) mkdirSync(frames, { recursive: true });
		const results: Array<{ label: string; bar: BarMeasured; panel: Awaited<ReturnType<typeof measureSwitch>> }> = [];
		const readers = async () => page.evaluate(async (artifactId) => {
			const { recordDocActivity } = await import('/src/lib/docPresence.ts');
			for (let index = 0; index < 4; index++) recordDocActivity({ type: 'doc_activity', artifact_id: artifactId, thread_id: `reader-${index}`, agent: `Reader ${index}`, kind: 'read', span: null, comment_id: null, revision: null, at: new Date().toISOString() });
		}, doc.artifactId);
		const open = async (viewport: number, split: number | null) => {
			await page.setViewportSize({ width: viewport, height: viewport < 700 ? 852 : 900 });
			await installWorkspace(page, threadId, [doc], 0, split ? { splitWidth: split } : {});
			await openThread(page, threadId, title);
			await expect(page.locator('.cm-content')).toContainText('A commentable document.');
			if (!split) await page.evaluate(async () => { const { setPaneCollapsed } = await import('/src/lib/stores/paneCollapsed.ts'); setPaneCollapsed(true, true); });
			await turnOffDocumentComments(page);
			await expect(page.locator('[data-comments-entry]')).toHaveAccessibleName(/^120 comments, 45 unread\b/);
			await readers();
			const panel = await openDetails(page);
			await expect(panel.locator('.reviewer-group .more')).toHaveText('+2');
			await closeDetails(page);
			await openComments(page);
			await expect(page.locator('[data-doc-comments-drawer] .drawer-foot .btn')).toBeVisible();
			await closeComments(page);
		};
		const take = async (viewport: number, split: number | null, variant = '') => {
			await settled(page, viewport);
			const label = (split ? `${viewport}px window, pane ${split}px` : `${viewport}px window`) + variant;
			const measured = await measureBar(page);
			await expectCountsWhole(page, measured, label, { threads: 120, unread: 45, needs: 1, needsWords: [OVERDUE_WORD] });
			if (frames && (split || variant || FRAME_SIZES.includes(viewport))) await page.screenshot({ path: path.join(frames, `worst-${split ? `${viewport}-split-${split}` : viewport}${variant.replace(/\s+/g, '-')}.png`), clip: { x: 0, y: 0, width: viewport, height: Math.max(240, Math.ceil(measured.bar.bottom + 40)) } });
			const details = await measureSwitch(page, viewport);
			expectSwitchWhole(details, label);
			if (frames && (split || variant || FRAME_SIZES.includes(viewport))) await page.screenshot({ path: path.join(frames, `worst-details-${split ? `${viewport}-split-${split}` : viewport}${variant.replace(/\s+/g, '-')}.png`), fullPage: false });
			await closeComments(page);
			results.push({ label, bar: measured, panel: details });
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
		// The Workspace panel closed and open, at the two desktop widths the merge is reviewed at.
		for (const viewport of [1440, 900]) {
			await open(viewport, null);
			await take(viewport, null, ' pane closed');
			await page.evaluate(async () => { const { setPaneCollapsed } = await import('/src/lib/stores/paneCollapsed.ts'); setPaneCollapsed(false, true); });
			await take(viewport, null, ' pane open');
		}
		// The phone's dark theme with the same worst content.
		await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'no-preference' });
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'dark'));
		await open(393, null);
		await take(393, null, ' dark');
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		if (frames) writeFileSync(path.join(frames, 'worst-sweep.json'), JSON.stringify(results, null, 1));
	} finally { holdPersonBatches(false); }
});

/**
 * The amber count beside Details, driven through each real thing it counts, never set by hand. It counts what needs the reader
 * behind Details and nowhere else on screen: Overdue and an ask whose card is hidden under the comment sheet (a touch screen:
 * a phone, or one held sideways). File changed remains visible in the bar at every width and is not counted. The count reads 1
 * for Overdue, rising to 2 when the sheet hides the ask. File changed comes from a second
 * writer: a document in a skill's evals folder is guidance and opens without live editing (an agent saving a file the reader has
 * open), so the save is a change the reader is told about rather than merged into their text.
 */
const NEEDS_CASES = [
	{ name: 'a split canvas on a desktop window', project: 'chromium-desktop', width: 1440, height: 900, split: true, sheet: false, max: 1 },
	{ name: 'a phone (compact canvas)', project: 'chromium-mobile', width: 393, height: 852, split: false, sheet: true, max: 2 },
	{ name: 'a phone held sideways (split canvas)', project: 'chromium-mobile', width: 915, height: 412, split: true, sheet: true, max: 2 },
] as const;

/**
 * A document in a skill's evals folder that the thread owns, with one comment: guidance, so it opens without live editing and another
 * writer's save shows File changed; a document, so an ask can name it (a SKILL.md registers as a skill, which an ask cannot).
 */
async function seedSkillDocument(page: Page, threadId: string, title: string) {
	const params = { token: TEST_TOKEN };
	const tree = await page.request.get('/api/files/tree', { params });
	const workspace = (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
	const folder = path.join(workspace, '.threads', threadId, 'skills', title, 'evals');
	mkdirSync(folder, { recursive: true });
	const docPath = path.join(folder, 'review-notes.md');
	const text = TEXT;
	const seeded = await page.request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifact = (await seeded.json()).artifact;
	const artifactId: string = artifact.id;
	const quoted = 'ordinary prose', from = text.indexOf(quoted);
	const comment = await page.request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { body: 'A comment', anchor_text: quoted, from_off: from, to_off: from + quoted.length, delivery_policy: 'immediate' } });
	expect(comment.ok(), await comment.text()).toBeTruthy();
	return { artifactId, path: docPath, fileName: 'review-notes.md', text };
}

/** The canvas's measured mode, as the bar reads it: compact puts File changed in the bar. */
const canvasCompactNow = (page: Page) => page.evaluate(async () => {
	const { canvasCompact } = await import('/src/lib/stores/artifactCanvas.ts');
	let value = false; canvasCompact.subscribe((current: boolean) => { value = current; })();
	return value;
});

/** Whether comments open in the sheet: the viewer's own rule, a phone or a touch screen too short for a card beside its text. */
const commentsInSheet = (page: Page) => page.evaluate(async () => {
	const { isMobile, isShortTouch } = await import('/src/lib/stores/viewport.ts');
	const read = (store: { subscribe: (run: (value: boolean) => void) => () => void }) => { let value = false; store.subscribe((current) => { value = current; })(); return value; };
	return read(isMobile) || read(isShortTouch);
});

for (const { name, project, width, height, split, sheet, max } of NEEDS_CASES) {
	test(`@flow:doc-toolbar-count-fits @surface:documents the amber count beside Details counts each real source on ${name}, at most ${max}`, async ({ page }, info) => {
		test.skip(info.project.name !== project, `${project} gives this case its pointer`);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(120_000);
		await page.setViewportSize({ width, height });
		await setDebugVersionOverlay(page.request, false);
		const title = `needs-${width}-${Date.now()}`;
		const threadId = await createSession(page.request, title);
		const doc = await seedSkillDocument(page, threadId, title);
		raiseAsk(threadId, doc.artifactId, ASK_TEXT);
		await answerOverdue(page);
		await installWorkspace(page, threadId, [doc], 0);
		await openThread(page, threadId, title);
		await expect(page.locator('.cm-content')).toContainText('ordinary prose');
		// Held sideways the window is wider than 768 px, so the sidebar sits beside the canvas; closed, the canvas has the room to split.
		if (split && width > 768 && info.project.name.includes('mobile')) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await expect.poll(() => canvasCompactNow(page), { message: `the canvas ${split ? 'splits' : 'is compact'}` }).toBe(!split);
		expect(await commentsInSheet(page), `comments ${sheet ? 'open' : 'do not open'} in the sheet on this screen`).toBe(sheet);
		const details = page.locator('[data-document-details]');
		const card = page.locator('[data-ask-card]');
		const frame = async (state: string) => { if (FRAMES) { mkdirSync(FRAMES, { recursive: true }); await page.screenshot({ path: path.join(FRAMES, `needs-${width}x${height}-${state}.png`), fullPage: false }); } };
		const expectNeeds = async (needs: number, state: string) => {
			// Overdue is always one; the ask joins by its own words when its card is hidden (needs reaches the case's max).
			const needsWords = needs >= 2 ? [OVERDUE_WORD, ASK_TEXT] : [OVERDUE_WORD];
			await expect(details, `${name}, ${state}`).toHaveAccessibleName(`Document details · ${needsWords.join(' · ')}`);
			await page.waitForTimeout(400);
			await expectCountsWhole(page, await measureBar(page), `${name}, ${state}`, { threads: 1, unread: 0, needs, needsWords });
			await frame(state);
		};

		// Overdue alone; the ask's card is on screen, so the ask is not counted.
		await expect(card).toBeVisible();
		await expectNeeds(1, 'overdue, ask on its card');

		// A second writer saves the file. File changed is in the bar at every width and does not join the count.
		const saved = await page.request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: doc.path, text: `${doc.text}\nA line another writer added.\n`, thread_id: threadId } });
		expect(saved.ok(), await saved.text()).toBeTruthy();
		const barReload = documentBarHost(page).locator('[data-file-changed]');
		await expect(barReload).toBeVisible();
		await expect(barReload).toHaveAccessibleName('File changed · Reload');
		await expectNeeds(1, 'overdue, file changed in the bar');

		// The comment sheet covers the ask's card on a touch screen, so the ask joins the count; elsewhere the card stays on screen.
		await page.locator('[data-comments-entry]').click();
		if (sheet) {
			await expect(page.locator('[data-doc-comments-drawer]')).toBeVisible();
			await expectNeeds(max, 'overdue, file changed in the bar, ask under the comment sheet');
		} else {
			await expect(card, 'without a sheet the ask keeps its card').toBeVisible();
			await expectNeeds(max, 'overdue, file changed, comments open beside the text');
		}
		await page.unrouteAll({ behavior: 'ignoreErrors' });
	});
}

/**
 * A document a skill's evals folder holds: guidance by its path alone, so it opens without live editing and another writer's save shows
 * File changed, under any file name.
 */
async function seedEvalDocument(page: Page, threadId: string, skill: string, fileName: string) {
	const tree = await page.request.get(`/api/files/tree?token=${TEST_TOKEN}`);
	const workspace = (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
	const folder = path.join(workspace, '.threads', threadId, 'skills', skill, 'evals');
	mkdirSync(folder, { recursive: true });
	const docPath = path.join(folder, fileName);
	const seeded = await page.request.put(`/api/files/content?token=${TEST_TOKEN}`, { data: { path: docPath, text: TEXT, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { artifactId: (await seeded.json()).artifact.id as string, path: docPath, fileName, text: TEXT };
}

/** The app bar's document name as drawn, its box, and the bar's reload mark beside it. */
const restingName = (page: Page) => documentBarHost(page).evaluate((bar) => {
	const title = bar.querySelector<HTMLElement>('[data-testid="header-document-title"]')!;
	const shown = title.querySelector<HTMLElement>('[data-testid="middle-name-shown"]');
	const reload = bar.querySelector<HTMLElement>('[data-file-changed]');
	const box = (element: Element) => { const rect = element.getBoundingClientRect(); return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }; };
	return {
		text: (shown ?? title).textContent?.trim() ?? '',
		overflows: shown ? shown.scrollWidth > shown.clientWidth + 0.5 : false,
		title: box(title), reload: reload ? box(reload) : null, viewport: innerWidth,
	};
});

/**
 * The resting name on a phone with File changed on: at 393 px a typical name shows whole beside the reload mark, and a long one is cut in
 * the middle, keeping its start and its suffix (the extension and the character before it), on the one row.
 */
for (const { label, fileName, whole } of [
	{ label: 'a typical name shows whole', fileName: 'launch-plan.md', whole: true },
	{ label: 'a long name is cut in the middle with its suffix kept', fileName: '2026-11-16-documents-header-design-review-pack.md', whole: false },
] as const) {
	test(`@flow:doc-toolbar-count-fits @surface:documents at 393 with File changed on, ${label}`, async ({ page }, info) => {
		test.skip(info.project.name !== 'chromium-mobile', 'A phone gives the bar its touch sizes');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 393, height: 852 });
		await setDebugVersionOverlay(page.request, false);
		const title = `name-${whole ? 'typical' : 'long'}-${Date.now()}`;
		const threadId = await createSession(page.request, title);
		const doc = await seedEvalDocument(page, threadId, title, fileName);
		await installWorkspace(page, threadId, [doc], 0);
		await openThread(page, threadId, title);
		await expect(page.locator('.cm-content')).toContainText('ordinary prose');
		await expect.poll(() => canvasCompactNow(page), { message: 'the phone canvas is compact' }).toBe(true);
		const saved = await page.request.put(`/api/files/content?token=${TEST_TOKEN}`, { data: { path: doc.path, text: `${doc.text}\nA line another writer added.\n`, thread_id: threadId } });
		expect(saved.ok(), await saved.text()).toBeTruthy();
		await expect(documentBarHost(page).locator('[data-file-changed]')).toBeVisible();
		await page.waitForTimeout(400);
		await expectOneRow(page);
		const measured = await restingName(page);
		if (FRAMES) { mkdirSync(FRAMES, { recursive: true }); await page.screenshot({ path: path.join(FRAMES, `name-393-${whole ? 'typical' : 'long'}-file-changed.png`), fullPage: false }); }
		await info.attach('resting-name', { body: JSON.stringify(measured, null, 2), contentType: 'application/json' });
		expect(measured.overflows, `${fileName}: the drawn name is not clipped`).toBe(false);
		if (whole) expect(measured.text, 'the typical name shows whole').toBe(fileName);
		else {
			const suffix = fileName.slice(-('.md'.length + 1));
			expect(measured.text, 'the long name is cut').not.toBe(fileName);
			expect(measured.text, 'the cut is in the middle, keeping the suffix').toMatch(new RegExp(`^${fileName.slice(0, 1)}.*…${suffix.replace('.', '\\.')}$`));
		}
		// Beside the reload mark: on its left, apart from it, and both inside the screen.
		expect(measured.reload, 'the reload mark is in the bar').not.toBeNull();
		expect(measured.title.right, 'the name ends before the reload mark').toBeLessThanOrEqual(measured.reload!.left + 0.5);
		expect(measured.title.left).toBeGreaterThanOrEqual(-0.5);
		expect(measured.reload!.right).toBeLessThanOrEqual(measured.viewport + 0.5);
		await expectBarHitAreas(page);
	});
}

/**
 * History is offered, so Details holds the count's Resolved, Deliver immediately, Request review and History at once. At every width
 * from 360 to 430 px, one pixel at a time (the range where the old toolbar first had to wrap), the bar's counts stay whole on one
 * row; at 360, 393 and 430 Details opens with History in it. The document's own History flag is answered true on the content read,
 * so the sweep does not depend on a Git history in the disposable workspace.
 */
test(`@flow:doc-toolbar-count-fits @surface:documents every width from 360 to 430 px keeps the bar's counts whole, with History in Details`, async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(300_000);
	await setDebugVersionOverlay(request, false);
	const title = `toolbar-history-${Date.now()}`;
	const threadId = await createSession(request, title);
	const doc = await seedTextDocument(request, threadId, title, 'history');
	const shown = { threads: 123, unread: 45, resolved: 3 }; // the count is the 120 open threads; 3 more are resolved
	await page.route(new RegExp(`/api/artifacts/${doc.artifactId}/comments(\\?|$)`), async (route) => {
		if (route.request().method() !== 'GET') return route.fallback();
		const response = await route.fetch();
		const body = await response.json();
		const row = body.comments[0] ?? { id: 'probe', body: 'A comment', anchor_text: 'commentable', from_off: 2, to_off: 13, author: 'You', author_kind: 'user', replies: [] };
		body.comments = Array.from({ length: shown.threads }, (_, index) => ({ ...row, id: `${row.id}-copy-${index}`, new_comment: index >= shown.resolved && index < shown.resolved + shown.unread, resolved: index < shown.resolved }));
		await route.fulfill({ response, json: body });
	});
	await page.route(/\/api\/files\/content\?/, async (route) => {
		if (route.request().method() !== 'GET') return route.fallback();
		const response = await route.fetch();
		await route.fulfill({ response, json: { ...(await response.json()), history: true } });
	});
	await page.setViewportSize({ width: 430, height: 900 });
	await installWorkspace(page, threadId, [doc], 0);
	await openThread(page, threadId, title);
	await expect(page.locator('[data-comments-entry]')).toHaveAccessibleName(/^120 comments, 45 unread\b/);
	if (FRAMES) mkdirSync(FRAMES, { recursive: true });
	for (let width = 430; width >= 360; width--) {
		await page.setViewportSize({ width, height: 900 });
		await settled(page, width);
		const label = `History at ${width} px`;
		await expectCountsWhole(page, await measureBar(page), label, { threads: 120, unread: 45, needs: 0, needsWords: [] });
		if ([360, 393, 430].includes(width)) {
			const panel = await openDetails(page);
			await expect(panel.locator('.history-tool')).toBeVisible();
			await expect(panel.locator('.review-request')).toBeVisible();
			expect.soft(await panel.evaluate((element) => element.scrollWidth - element.clientWidth), `${label}: Details does not scroll sideways`).toBeLessThanOrEqual(1);
			if (FRAMES) await page.screenshot({ path: path.join(FRAMES, `history-${width}.png`), fullPage: false });
			await closeDetails(page);
			await openComments(page);
			await expect(page.locator('[data-resolved-entry]')).toHaveText('Resolved · 3');
			await expect(page.getByRole('button', { name: 'Deliver immediately', exact: true })).toBeVisible();
			await closeComments(page);
		}
	}
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});
