import path from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { holdPersonBatches, installWorkspace, openThread, seedHeldComments, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel, turnOffDocumentComments } from '../support/heldComments';

/**
 * The document toolbar's "N comments · N unread" count shows whole, inside the toolbar and clear of the other controls,
 * at 320, 360 and 393 px phones and at 900 and 1440, with one-, two- and three-digit counts. The tool cluster breaks into rows only in PE's order, and never clips, overlaps or shrinks a control.
 */

const TEXT = '# Toolbar count probe\n\nA paragraph of ordinary prose to hold a comment.\n';
const COUNTS = [{ threads: 1, unread: 1 }, { threads: 12, unread: 3 }, { threads: 120, unread: 45 }];
const CASES = [
	{ width: 320, scheme: 'light' as const }, { width: 360, scheme: 'light' as const },
	{ width: 393, scheme: 'light' as const }, { width: 393, scheme: 'dark' as const },
	// A window under 768 px whose canvas is wider than 600 px: the tablet range where only the switch has a 44 px target and the other tools are 28 px.
	{ width: 700, scheme: 'light' as const },
	{ width: 900, scheme: 'light' as const }, { width: 1440, scheme: 'light' as const },
	// The receipt state: every comment sent and three of them resolved, so the first group holds the count and "Resolved · 3" and no Send.
	{ width: 320, scheme: 'light' as const, resolved: 3 },
];
const FRAMES = process.env.CORSIA_FRAMES_DIR;
// The rows the ordinary counts make at each width, for worst-sweep.json (the desktop project runs these before the worst-content test).
const ordinaryRows: Array<Record<string, unknown>> = [];

for (const { width, scheme, resolved } of CASES) {
	test(`@flow:doc-toolbar-count-fits @surface:documents ${width}px ${scheme}${resolved ? ' with Resolved' : ''}: the thread and unread count shows whole`, async ({ page }, info) => {
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
		await loginByUrl(page, { waitForComposer: true });
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByText(title, { exact: true }).first().click();
		await expect(page.locator('.cm-content')).toContainText('ordinary prose');
		await expect(page.getByRole('button', { name: /^1 comment\b/ })).toBeEnabled();
		if (FRAMES) mkdirSync(FRAMES, { recursive: true });

		if (resolved) {
			// Three resolved threads, nothing held: the first group is the count pill and "Resolved · 3", which wraps under the break rule and must stay whole, inside the header and clear of every other control.
			shown.threads = resolved; shown.unread = 0; shown.resolved = resolved;
			await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), artifactId);
			const entry = page.locator('.document-header .resolved-tool');
			await expect(entry).toHaveText(`Resolved · ${resolved}`);
			await expect(page.locator('.document-header .sending-now')).toHaveCount(0);
			await page.waitForTimeout(800);
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			const layout = await measureTools(page);
			const label = `Resolved · ${resolved} at ${width} ${scheme}`;
			expectLayout(layout, label);
			expect.soft(layout.controls.map((control) => control.name), `${label}: the count, Resolved and the second group are all in the header`).toEqual(expect.arrayContaining(['count', 'resolved', 'deliver']));
			expect.soft(await entry.evaluate((element) => element.scrollWidth <= element.clientWidth), `${label}: Resolved is not clipped`).toBe(true);
			if (FRAMES) await page.screenshot({ path: path.join(FRAMES, `${width}-${scheme}-resolved-${resolved}.png`), clip: { x: 0, y: 0, width, height: Math.min(300, layout.tools.bottom + 60) } });
			await info.attach('resolved-receipt', { body: JSON.stringify(layout.rowContents), contentType: 'application/json' });
			await page.unrouteAll({ behavior: 'ignoreErrors' });
			return;
		}
		for (const { threads, unread } of COUNTS) {
			// The server's own answer, with the one real thread copied `threads` times, the first `unread` of them unread; the viewer loads it as it would any list.
			shown.threads = threads; shown.unread = unread;
			await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), artifactId);
			const pill = page.locator('.document-header .comment-status');
			await expect(pill).toHaveText(new RegExp(`${threads}\\s+${threads === 1 ? 'comment' : 'comments'}\\s+·\\s+${unread} unread`), { useInnerText: true });
			// A person sees the settled state: nothing may redraw the pill after the store update.
			await page.waitForTimeout(800);
			await expect(pill).toHaveText(new RegExp(`${threads}\\s+${threads === 1 ? 'comment' : 'comments'}\\s+·\\s+${unread} unread`), { useInnerText: true });
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			const measured = await page.evaluate(() => {
				const box = (element: Element) => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
				const header = document.querySelector('.document-header')!;
				const tools = header.querySelector('.document-tools')!;
				const pillEl = header.querySelector<HTMLElement>('.comment-status')!;
				const text = [...pillEl.querySelectorAll<HTMLElement>('.count, .count-unit, .unread')];
				return {
					header: box(header), tools: box(tools), pill: box(pillEl),
					// The dot after the count is the server's open (unsent) count for this document, not layout: its state is recorded with the frame.
					unsentDot: !!pillEl.querySelector('.unsent-dot'), pillName: pillEl.getAttribute('aria-label'),
					pillScroll: { scrollWidth: pillEl.scrollWidth, clientWidth: pillEl.clientWidth },
					text: text.map((part) => ({ name: part.className, shown: getComputedStyle(part).display !== 'none' && getComputedStyle(part).visibility !== 'hidden', fits: part.scrollWidth <= part.clientWidth || getComputedStyle(part).display === 'inline', ...box(part) })),
					siblings: [...tools.querySelectorAll<HTMLElement>('button, [role="switch"], .save-ghost')]
						.filter((el) => el !== pillEl && !pillEl.contains(el) && el.getBoundingClientRect().width > 0)
						.map((el) => ({ name: el.getAttribute('aria-label') ?? el.className, ...box(el) })),
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
			// Rows follow the break order, from a 329 px header at most two, and nothing clips, overlaps or shrinks.
			const layout = await measureTools(page);
			expectLayout(layout, label);
			ordinaryRows.push({ label: `ordinary content ${threads}/${unread} at ${width}px ${scheme}`, headerContentWidth: layout.headerContentWidth, rows: layout.rows, rowContents: layout.rowContents, groupWidths: layout.groupWidths, labelToToggle: layout.labelToToggle, controlWidths: Object.fromEntries(layout.controls.map((control) => [control.name, Math.round((control.right - control.left) * 10) / 10])) });
			await info.attach(`count-${threads}-${unread}`, { body: JSON.stringify(measured), contentType: 'application/json' });
		}
		// A comments request still in flight when the test ends would fail its route callback with a closed page.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
	});
}

/**
 * The worst header at once: Send 99+, a reviewer group that overflows into "+2", "120 comments · 45 unread" and four
 * readers in the presence row. The tool cluster cannot shrink, so wherever the header is narrow it has to fit inside
 * it: on one row where one row can hold it, and wrapped inside the header where it cannot. Full window from 320 to
 * 1440 px (a fresh load at each phone and tablet width, one desktop load resized across the rest; the Workspace panel closed so the document has the room) and a narrow
 * desktop split at 1440 and 1024, a wide pane (1000 px), and panes that put the header at 720, 760 and 800 px, where the cluster first has to break. The delivery label is the full "Deliver immediately" at every width, the same
 * distance from its switch everywhere, and the cluster breaks only between [Send · count] and the second group.
 */
const PHONE_SWEEP = [...Array.from({ length: 12 }, (_, index) => 320 + index * 40), 393, 700].sort((a, b) => a - b); // 320..760, a fresh load each: the phone layout is chosen at load
const DESKTOP_SWEEP = [...Array.from({ length: 17 }, (_, index) => 800 + index * 40), 900].sort((a, b) => a - b); // 800..1440, resized within one desktop load
const SPLITS = [{ viewport: 1440, split: 360 }, { viewport: 1024, split: 360 }, { viewport: 1920, split: 1000 }];
const FRAME_SIZES = [320, 360, 393, 560, 700, 900];
// A 1920 window with the pane set so the header's content is this wide (measured, asserted): the range where the cluster first has to break.
const HEADER_SWEEP = [{ split: 784, header: 720 }, { split: 824, header: 760 }, { split: 864, header: 800 }];

async function measureTools(page: Page) {
	return page.evaluate(() => {
		const header = document.querySelector<HTMLElement>('.document-header')!;
		const tools = header.querySelector<HTMLElement>('.document-tools')!;
		const style = getComputedStyle(header);
		const box = (element: Element) => { const r = element.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
		const outer = header.getBoundingClientRect();
		const content = { left: outer.left + parseFloat(style.paddingLeft), right: outer.right - parseFloat(style.paddingRight) };
		const NAMED: Array<[string, string]> = [['send', '.sending-now .btn'], ['count', '.comment-status'], ['resolved', '.resolved-tool'], ['deliver', '.delivery-toggle'], ['reviewers', '.reviewer-group'], ['request', '.review-request'], ['history', '.history-tool']];
		const controls = NAMED.flatMap(([name, selector]) => [...tools.querySelectorAll<HTMLElement>(selector)].map((control) => ({ name, ...box(control) })))
			.filter((control) => control.right > control.left)
			.sort((a, b) => a.left - b.left);
		// A row is the controls whose centres sit together; rows are listed top to bottom, each left to right.
		const rowsOf = [...controls].sort((a, b) => (a.top + a.bottom) - (b.top + b.bottom)).reduce<Array<{ centre: number; names: Array<{ name: string; left: number }> }>>((rows, control) => {
			const centre = (control.top + control.bottom) / 2;
			const row = rows[rows.length - 1];
			if (row && Math.abs(row.centre - centre) <= 10) row.names.push({ name: control.name, left: control.left });
			else rows.push({ centre, names: [{ name: control.name, left: control.left }] });
			return rows;
		}, []).map((row) => row.names.sort((a, b) => a.left - b.left).map((item) => item.name));
		const label = tools.querySelector<HTMLElement>('.delivery-toggle > span')!;
		// The gap a person sees: from the label's right edge to the switch. The switch's button is its track; the touch hit area is a span inside it.
		const toggleButton = tools.querySelector<HTMLElement>('.delivery-toggle .tg')!;
		const button = toggleButton.getBoundingClientRect();
		const labelToToggle = button.left - label.getBoundingClientRect().right;
		const hitArea = toggleButton.querySelector<HTMLElement>('.hit')!.getBoundingClientRect();
		const inView = hitArea.top >= 0 && hitArea.bottom <= window.innerHeight && hitArea.left >= 0 && hitArea.right <= window.innerWidth;
		if (!inView) toggleButton.scrollIntoView({ block: 'center' });
		const area = toggleButton.querySelector<HTMLElement>('.hit')!.getBoundingClientRect();
		// A tap on each corner (one pixel in) and the centre of the hit area lands on the switch.
		const probes: Array<[number, number]> = [[area.left + 1, area.top + 1], [area.right - 1, area.top + 1], [area.left + 1, area.bottom - 1], [area.right - 1, area.bottom - 1], [area.left + area.width / 2, area.top + area.height / 2]];
		const tapMisses = probes.map(([x, y]) => document.elementFromPoint(x, y)).filter((el) => el?.closest('.tg') !== toggleButton).map((el) => `${el?.tagName}.${el?.className}`);
		// The hit area is one more control: it overlaps no other control, stays inside the header (never past its bottom padding into the document), and a point just inside each edge of Send and the count lands on that control, not on the switch.
		const overlapOf = (a: DOMRect, b: DOMRect) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
		const hitOverlaps = [...header.querySelectorAll<HTMLElement>('button, a[href], [role="button"], [role="switch"]')].filter((control) => control !== toggleButton && !toggleButton.contains(control) && !control.contains(toggleButton) && control.getBoundingClientRect().width > 0 && overlapOf(area, control.getBoundingClientRect())).map((control) => control.getAttribute('aria-label') ?? control.className);
		const hitOutside = { above: Math.max(0, outer.top - area.top), below: Math.max(0, area.bottom - outer.bottom) };
		const edgeMisses = (['.sending-now .btn', '.comment-status'] as const).flatMap((selector) => {
			const control = tools.querySelector<HTMLElement>(selector);
			if (!control) return [];
			const r = control.getBoundingClientRect();
			const points: Array<[number, number]> = [];
			for (const fraction of [0.25, 0.75]) points.push([r.left + 1, r.top + r.height * fraction], [r.right - 1, r.top + r.height * fraction], [r.left + r.width * fraction, r.top + 1], [r.left + r.width * fraction, r.bottom - 1]);
			return points.map(([x, y]) => document.elementFromPoint(x, y)).filter((el) => el?.closest(selector) !== control).map((el) => `${selector} edge hit ${el?.tagName}.${el?.className}`);
		});
		let canvas: HTMLElement | null = header;
		while (canvas && !getComputedStyle(canvas).containerName.split(' ').includes('doc-canvas')) canvas = canvas.parentElement;
		const touchMeant = window.innerWidth <= 768 || (canvas?.getBoundingClientRect().width ?? Infinity) <= 600;
		const pill = tools.querySelector<HTMLElement>('.comment-status')!;
		const parts = [...pill.querySelectorAll<HTMLElement>('.count, .count-unit, .unread')].map((part) => {
			const partStyle = getComputedStyle(part);
			return { name: part.className.split(' ')[0], shown: partStyle.display !== 'none' && partStyle.visibility !== 'hidden', width: part.getBoundingClientRect().width, fits: part.scrollWidth <= part.clientWidth || partStyle.display === 'inline', ...box(part) };
		});
		// Each group's width on one line: what the header has to hold for that group not to wrap.
		const groupWidth = (selector: string) => {
			const group = tools.querySelector<HTMLElement>(selector);
			if (!group) return 0;
			const kept = group.style.cssText;
			group.style.cssText = 'position:fixed;left:0;top:0;flex-wrap:nowrap;max-inline-size:none;width:max-content';
			const width = group.getBoundingClientRect().width;
			group.style.cssText = kept;
			return Math.round(width * 10) / 10;
		};
		const groupWidths = { first: groupWidth('.tool-send'), second: groupWidth('.tool-more'), deliver: groupWidth('.tool-deliver'), review: groupWidth('.tool-review') };
		// The whole cluster on one line: its two groups and the gap between them.
		const natural = groupWidths.first + groupWidths.second + (parseFloat(getComputedStyle(tools).columnGap) || 0);
		const overlaps = controls.flatMap((one, index) => controls.slice(index + 1).filter((other) => Math.min(one.right, other.right) - Math.max(one.left, other.left) > 0.5 && Math.min(one.bottom, other.bottom) - Math.max(one.top, other.top) > 0.5).map((other) => `${one.name}/${other.name}`));
		const sendButton = tools.querySelector<HTMLElement>('.sending-now .btn');
		return {
			natural, rows: rowsOf.length, rowContents: rowsOf, labelToToggle: Math.round(labelToToggle * 10) / 10,
			deliverLabel: label.textContent?.trim() ?? null,
			names: { send: sendButton?.getAttribute('aria-label') ?? null, count: pill.getAttribute('aria-label') },
			overlaps, minControlHeight: Math.min(...controls.filter((control) => control.name !== 'deliver').map((control) => control.bottom - control.top)),
			toggleHit: { button: { width: Math.round(button.width * 10) / 10, height: Math.round(button.height * 10) / 10 }, area: { width: Math.round(area.width * 10) / 10, height: Math.round(area.height * 10) / 10 }, tapMisses, hitOverlaps, hitOutside, edgeMisses, touchMeant },
			headerOverflow: header.scrollWidth - header.clientWidth, pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
			content, tools: box(tools), controls, parts, headerContentWidth: content.right - content.left, groupWidths, expectedGap: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--chip-inline-padding')),
			pillText: pill.innerText.replace(/\s+/g, ' ').trim(),
			sendText: tools.querySelector<HTMLElement>('.sending-now .btn')?.innerText.trim() ?? null,
			reviewerMore: tools.querySelector('.reviewer-group .more')?.textContent ?? null,
		};
	});
}

type Measured = Awaited<ReturnType<typeof measureTools>>;
/** The rows the cluster has to make, by PE's break order: nothing breaks while it fits; then between [Send · count] and the rest; then inside the rest between [Deliver immediately · switch] and [reviewers · Request review · History]; and between Send and the count only when [Send · count] alone is wider than the header. */
function expectedRows(measured: Measured): string[][] {
	const present = new Set(measured.controls.map((control) => control.name));
	const first = ['send', 'count', 'resolved'].filter((name) => present.has(name));
	const deliver = ['deliver'].filter((name) => present.has(name));
	const review = ['reviewers', 'request', 'history'].filter((name) => present.has(name));
	const room = measured.headerContentWidth + 0.5;
	if (measured.natural <= room) return [[...first, ...deliver, ...review]];
	const sendAlone = measured.groupWidths.first > room && first[0] === 'send' && first.length > 1;
	const rows = sendAlone ? [['send'], first.slice(1)] : [first];
	rows.push(...(measured.groupWidths.second > room && deliver.length && review.length ? [deliver, review] : [[...deliver, ...review]]));
	return rows.filter((row) => row.length);
}

/** Layout rules that hold for any content: the cluster stays inside the header, breaks only where PE ruled, keeps the label one step from its switch and every hit target whole. */
function expectLayout(measured: Measured, label: string) {
	for (const control of measured.controls) {
		expect.soft(control.left, `${label}: ${control.name} starts inside the header`).toBeGreaterThanOrEqual(measured.content.left - 0.5);
		expect.soft(control.right, `${label}: ${control.name} ends inside the header`).toBeLessThanOrEqual(measured.content.right + 0.5);
	}
	expect.soft(measured.overlaps, `${label}: no control overlaps another`).toEqual([]);
	expect.soft(measured.headerOverflow, `${label}: the header does not scroll sideways`).toBeLessThanOrEqual(1);
	expect.soft(measured.pageOverflow, `${label}: the page does not scroll sideways`).toBeLessThanOrEqual(1);
	expect.soft(measured.minControlHeight, `${label}: no control but the switch row is shorter than the toolbar's 28 px`).toBeGreaterThanOrEqual(27.5);
	// The switch's button stays its 30 x 17 track, so the label keeps its gap; its hit area is the track itself or a 44 x 44 touch size, and a tap on each corner and the centre lands on the switch.
	const { button: toggleBox, area: hitArea, tapMisses } = measured.toggleHit;
	expect.soft(Math.abs(toggleBox.width - 30) <= 0.5 && Math.abs(toggleBox.height - 17) <= 0.5, `${label}: the switch button is its 30 x 17 track, measured ${toggleBox.width} x ${toggleBox.height}`).toBe(true);
	expect.soft((Math.abs(hitArea.width - 30) <= 0.5 && Math.abs(hitArea.height - 17) <= 0.5) || (hitArea.width >= 43.5 && hitArea.height >= 43.5), `${label}: the switch hit area is its track or the 44 px touch size, measured ${hitArea.width} x ${hitArea.height}`).toBe(true);
	expect.soft(measured.toggleHit.touchMeant ? hitArea.width >= 43.5 && hitArea.height >= 43.5 : Math.abs(hitArea.width - 30) <= 0.5 && Math.abs(hitArea.height - 17) <= 0.5, `${label}: the hit area is 44 x 44 where a touch target is meant (a window to 768 px or a canvas to 600 px) and the track elsewhere, measured ${hitArea.width} x ${hitArea.height}`).toBe(true);
	expect.soft(measured.toggleHit.hitOverlaps, `${label}: the switch's hit area overlaps no other control`).toEqual([]);
	expect.soft(measured.toggleHit.hitOutside, `${label}: the switch's hit area stays inside the header, none of it above or below`).toEqual({ above: 0, below: 0 });
	expect.soft(measured.toggleHit.edgeMisses, `${label}: points just inside each edge of Send and the count land on those controls, not the switch`).toEqual([]);
	expect.soft(tapMisses, `${label}: taps on the four corners and the centre of the switch's hit area land on the switch`).toEqual([]);
	// The label is the full "Deliver immediately", the normal chip padding from its switch at every width.
	expect.soft(measured.deliverLabel, `${label}: the delivery label is the full wording`).toBe('Deliver immediately');
	expect.soft(Math.abs(measured.labelToToggle - measured.expectedGap), `${label}: the label is ${measured.expectedGap}px from the switch, measured ${measured.labelToToggle}`).toBeLessThanOrEqual(0.5);
	const expected = expectedRows(measured);
	expect.soft(measured.rowContents, `${label}: the rows follow the break order (header ${measured.headerContentWidth}, groups ${JSON.stringify(measured.groupWidths)})`).toEqual(expected);
	// A header of 329 (a 393 phone) or wider holds the cluster in at most two rows, whatever the content.
	if (measured.headerContentWidth >= 329) expect.soft(measured.rows, `${label}: at most two control rows from a 329 px header`).toBeLessThanOrEqual(2);
	// When Send is split from the count, the count pill leads the next row; Send keeps its own number in its own name.
	if (measured.rowContents[0]?.length === 1 && measured.rowContents[0][0] === 'send') expect.soft(measured.rowContents[1]?.[0], `${label}: the count pill leads the row after Send`).toBe('count');
}

function expectWholeInsideOneRow(measured: Measured, label: string) {
	expect.soft(measured.sendText, `${label}: Send's visible label is in the row, whole`).toMatch(/^Send (?:10\d comments?|99\+)$/);
	expect.soft(measured.names.send, `${label}: Send keeps its own number in its name`).toMatch(/^Send 10\d comments?/);
	expect.soft(measured.names.count, `${label}: the count pill keeps its own name`).toMatch(/^120 comments, 45 unread/);
	expect.soft(measured.reviewerMore, `${label}: the reviewer group overflows into +2`).toBe('+2');
	expect.soft(measured.pillText, `${label}: the count reads whole`).toMatch(/^120 comments · 45 unread$/);
	expectLayout(measured, label);
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
				await expect(page.locator('.document-header .comment-status')).toContainText('120 comments');
				await readers();
				await expect(page.locator('.document-header .reviewer-group .more')).toHaveText('+2');
			};
			await load();
			await expect(page.locator('.document-header .sending-now .btn')).toBeVisible();
		};
		const take = async (viewport: number, split: number | null, header?: number, variant = '') => {
			// Measure once the window has the width asked for, no animation is running, and the header's width holds across two frames.
			await expect.poll(() => page.evaluate((expected) => new Promise<string>((resolve) => {
				const read = () => `${window.innerWidth === expected ? 'at' : 'not'}:${Math.round(document.querySelector('.document-header')!.getBoundingClientRect().width * 10)}:${document.getAnimations().length}`;
				const first = read();
				requestAnimationFrame(() => requestAnimationFrame(() => { const second = read(); resolve(first === second && second.startsWith('at:') && second.endsWith(':0') ? 'settled' : 'moving'); }));
			}), viewport), { message: 'the header layout settles', timeout: 15_000 }).toBe('settled');
			const label = (header ? `header ${header}px (${viewport}px window, pane ${split}px)` : split ? `${viewport}px window, pane ${split}px` : `${viewport}px window`) + variant;
			const measured = await measureTools(page);
			results.push({ label, ...measured });
			expectWholeInsideOneRow(measured, label);
			if (header) expect.soft(Math.abs(measured.headerContentWidth - header), `${label}: the header's content is ${header}px wide, measured ${measured.headerContentWidth}`).toBeLessThanOrEqual(0.5);
			// Tall enough for every row the cluster makes.
			const cropHeight = Math.max(240, Math.ceil(measured.tools.bottom + 40));
			const crop = header ? { x: Math.max(0, measured.content.left - 24), y: 0, width: measured.content.right - measured.content.left + 48, height: cropHeight } : { x: 0, y: 0, width: viewport, height: cropHeight };
			if (frames && (header || split || variant || FRAME_SIZES.includes(viewport))) await page.screenshot({ path: path.join(frames, `worst-${header ? `header-${header}` : split ? `${viewport}-split-${split}` : viewport}${variant.replace(/\s+/g, '-')}.png`), clip: crop });
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
		for (const { split, header } of HEADER_SWEEP) {
			await open(1920, split);
			await take(1920, split, header);
		}
		// The Workspace panel closed and open, at the two desktop widths the merge is reviewed at.
		for (const viewport of [1440, 900]) {
			await open(viewport, null);
			await take(viewport, null, undefined, ' pane closed');
			await page.evaluate(async () => { const { setPaneCollapsed } = await import('/src/lib/stores/paneCollapsed.ts'); setPaneCollapsed(false, true); });
			await take(viewport, null, undefined, ' pane open');
		}
		// The phone's dark theme with the same worst content.
		await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'no-preference' });
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'dark'));
		await open(393, null);
		await take(393, null, undefined, ' dark');
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		if (frames) writeFileSync(path.join(frames, 'worst-sweep.json'), JSON.stringify([...ordinaryRows, ...results.map(({ label, headerContentWidth, controls, tools, ...rest }) => ({ label, ...rest, headerContentWidth, controlWidths: Object.fromEntries(controls.map((control) => [control.name, Math.round((control.right - control.left) * 10) / 10])), unfit: [rest.groupWidths.first > headerContentWidth + 0.5 && 'first group', rest.groupWidths.second > headerContentWidth + 0.5 && 'second group'].filter(Boolean), toolsLeft: tools.left, controlLeft: Math.min(...controls.map((control) => control.left)) }))], null, 1));
	} finally { holdPersonBatches(false); }
});

/**
 * History is offered, so the row holds the count, "Resolved", "Deliver immediately", Request review and History at once. At every
 * width from 360 to 430 px, one pixel at a time (the range where History's icon first shows and the row first has to wrap), the
 * row stays inside the header with the comment icon whole. The document's own History flag is answered true on the content read, so the sweep does not depend on a Git history in the disposable workspace.
 */
test(`@flow:doc-toolbar-count-fits @surface:documents every width from 360 to 430 px keeps the row with History inside the header`, async ({ page, request }, info) => {
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
	await expect(page.locator('.document-header .comment-status')).toContainText('120 comments');
	await expect(page.locator('.document-header .resolved-tool')).toHaveText('Resolved · 3');
	await expect(page.locator('.document-header .history-tool')).toBeVisible();
	if (FRAMES) mkdirSync(FRAMES, { recursive: true });
	const rowsSeen: Array<[number, number]> = [];
	for (let width = 430; width >= 360; width--) {
		await page.setViewportSize({ width, height: 900 });
		await expect.poll(() => page.evaluate((expected) => new Promise<string>((resolve) => {
			const read = () => `${window.innerWidth === expected ? 'at' : 'not'}:${Math.round(document.querySelector('.document-header')!.getBoundingClientRect().width * 10)}:${document.getAnimations().length}`;
			const first = read();
			requestAnimationFrame(() => requestAnimationFrame(() => { const second = read(); resolve(first === second && second.startsWith('at:') && second.endsWith(':0') ? 'settled' : 'moving'); }));
		}), width), { message: `the header settles at ${width}`, timeout: 15_000 }).toBe('settled');
		const label = `History row at ${width} px`;
		const measured = await measureTools(page);
		const names = measured.controls.map((control) => control.name);
		expect.soft(names, `${label}: the count, Resolved, Deliver immediately, Request review and History are all in the row`).toEqual(expect.arrayContaining(['count', 'resolved', 'deliver', 'request', 'history']));
		expect.soft(measured.pillText, `${label}: the count reads whole`).toMatch(/^120 comments · 45 unread$/);
		expectLayout(measured, label);
		rowsSeen.push([width, measured.rows]);
		if (FRAMES && [380, 381, 393].includes(width)) await page.screenshot({ path: path.join(FRAMES, `history-${width}.png`), clip: { x: 0, y: 0, width, height: Math.min(300, Math.ceil(measured.tools.bottom + 60)) } });
	}
	await info.attach('rows-by-width', { body: JSON.stringify(rowsSeen), contentType: 'application/json' });
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});
