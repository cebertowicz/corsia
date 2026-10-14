import path from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

/**
 * The comment margin marker: each marker sits inside the rail the content padding reserves (24px for a fine
 * pointer, 44px otherwise; at phone width the 44px hit box reaches 20px into the text's edge, the text keeping
 * the 24px inset), its whole hit box opens its own thread or list, no two markers overlap, and its
 * glyph, count and focus ring meet contrast in both themes. Everything here reads public DOM geometry (rects, elementFromPoint, selection, computed style);
 * nothing reaches into CodeMirror's private fields.
 */

const TEXT = 4.5, GRAPHIC = 3;
const FINE_RAIL = 24, COARSE_RAIL = 44;
const THEMES = ['light', 'dark'] as const;
type Theme = (typeof THEMES)[number];

const SINGLES: Array<[string, string]> = [
	['qmhead', '# Margin heading qmhead'],
	['qmalone', 'Alone qmalone line here.'],
];
const DENSE = Array.from({ length: 12 }, (_, i) => `qmd${String(i + 1).padStart(2, '0')}`);
const MANY = Array.from({ length: 12 }, (_, i) => `qmm${String(i + 1).padStart(2, '0')}`);
const LONG_WORD = `qmlongword${'a'.repeat(70)}`;

/** The document under test, one case per paragraph so only the adjacent cases sit within a target height. */
function documentText(): string {
	return [
		SINGLES[0][1], '',
		SINGLES[1][1], '',
		'Pair qmpa and qmpb share a line.', '',
		'Near qmna first.',
		'Near qmnb second.', '',
		'A plain line with no comment on it.', '',
		'- List qmlist item here.',
		'- [ ] Task qmtask item here.', '',
		'```ts',
		'const qmcode = 1;',
		'```', '',
		'Wrapped qmwrap line that keeps going and going well past the end of its row so that it wraps onto a second and a third row at every width the canvas has.', '',
		`${LONG_WORD} tail`, '',
		...DENSE.map((key) => `Dense ${key} line.`), '',
		`Many ${MANY.join(' ')} end.`, '',
		'Last qmlast line.', '',
	].join('\n');
}

const ANCHORS = ['qmhead', 'qmalone', 'qmpa', 'qmpb', 'qmna', 'qmnb', 'qmlist', 'qmtask', 'qmcode', 'qmwrap', 'qmlongword', ...DENSE, ...MANY, 'qmlast'];

type Marker = {
	ids: string[];
	label: string | null;
	text: string;
	left: number; top: number; width: number; height: number;
};

function isMobile(info: TestInfo) {
	return info.project.name.includes('mobile');
}

function railFor(info: TestInfo) {
	return isMobile(info) ? COARSE_RAIL : FINE_RAIL;
}

/** Where the text starts inside the content box: the fine rail's width at both widths, the coarse hit box reaching past it. */
const TEXT_INSET = FINE_RAIL;

/** The two widths under test. `tall` is a viewport high enough that CodeMirror renders every line at once. */
function viewport(info: TestInfo, tall: boolean) {
	return isMobile(info) ? { width: 393, height: tall ? 3600 : 852 } : { width: 1440, height: tall ? 2600 : 960 };
}

type Opened = { threadId: string; title: string; ids: Record<string, string>; byId: Record<string, string>; artifactId: string };

/** Seeds the document with one thread per anchor (plus an orphan if asked), opens it in the canvas and waits until it is loaded and still. */
async function openDocument(page: Page, info: TestInfo, { comments = true, orphan = false, readOnly = false, tall = true, size }: { comments?: boolean; orphan?: boolean; readOnly?: boolean; tall?: boolean; size?: { width: number; height: number } } = {}): Promise<Opened> {
	await page.setViewportSize(size ?? viewport(info, tall));
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
	const request = page.request;
	const params = { token: TEST_TOKEN };
	const title = `margin-marker-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const tree = await request.get('/api/files/tree', { params });
	expect(tree.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const text = documentText();
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const ids: Record<string, string> = {};
	const byId: Record<string, string> = {};
	const add = async (anchor: string, body: string, quoted = anchor) => {
		const from = text.indexOf(quoted);
		const created = await request.post(`/api/artifacts/${artifactId}/comments`, {
			params, data: { body, anchor_text: quoted, from_off: from < 0 ? 0 : from, to_off: (from < 0 ? 0 : from) + quoted.length },
		});
		expect(created.ok(), await created.text()).toBeTruthy();
		const id: string = (await created.json()).comment.id;
		ids[anchor] = id;
		byId[id] = anchor;
	};
	if (comments) {
		for (const anchor of ANCHORS) {
			expect(text.indexOf(anchor), `setup: ${anchor} is in the document`).toBeGreaterThanOrEqual(0);
			await add(anchor, `Thread on ${anchor}`);
		}
	}
	if (orphan) await add('qmorphan', 'Thread whose text is gone', 'text that is no longer in the document');
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'marker-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'marker-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	if (readOnly) {
		await page.route('**/api/files/content**', async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			const response = await route.fetch();
			await route.fulfill({ response, json: { ...(await response.json()), writable: false } });
		});
		// A live document takes its writability from the opened frame, not from the file read.
		await page.routeWebSocket((url) => url.pathname === '/ws', (socket) => {
			const server = socket.connectToServer();
			socket.onMessage((payload) => server.send(payload));
			server.onMessage((payload) => {
				let frame: Record<string, unknown> | null = null;
				try { frame = JSON.parse(String(payload)); } catch { /* Forward non-JSON unchanged. */ }
				socket.send(frame?.type === 'doc_opened' ? JSON.stringify({ ...frame, writable: false }) : payload);
			});
		});
	}
	// A canvas opened at its own size goes straight to the sidebar's thread list: at a 200% zoom the New chat button sits under the open drawer's
	// close layer, which the document needs no new chat to avoid.
	await loginByUrl(page, { waitForComposer: !size });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-content')).toBeVisible();
	await expect(page.locator('.cm-content')).toContainText(tall ? 'Last qmlast line.' : 'Margin heading qmhead');
	const expectedCount = (comments ? ANCHORS.length : 0) + (orphan ? 1 : 0);
	if (expectedCount) {
		await expect(page.getByRole('button', { name: new RegExp(`^${expectedCount} open thread${expectedCount === 1 ? '' : 's'}\\b`) })).toBeEnabled();
	}
	await settle(page);
	return { threadId, title, ids, byId, artifactId };
}

/** Waits until nothing animates, no ancestor of the editor is transformed, and the editor's geometry is steady across frames. */
async function settle(page: Page) {
	await expect.poll(() => page.locator('.cm-editor').evaluate(async (node) => {
		const transformed: string[] = [];
		for (let current: Element | null = node; current; current = current.parentElement) {
			const style = getComputedStyle(current);
			if ((style.transform !== 'none' && !new DOMMatrixReadOnly(style.transform).isIdentity) || style.scale !== 'none') transformed.push(current.tagName);
		}
		const scroller = node.querySelector('.cm-scroller')!;
		const before = `${scroller.scrollHeight}:${node.getBoundingClientRect().top}`;
		await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		const after = `${scroller.scrollHeight}:${node.getBoundingClientRect().top}`;
		return { transformed, running: document.getAnimations().filter((a) => a.playState === 'running').length, steady: before === after };
	}), { message: 'setup: the canvas is loaded, still and untransformed' }).toEqual({ transformed: [], running: 0, steady: true });
}

async function markers(page: Page): Promise<Marker[]> {
	return page.evaluate(() => [...document.querySelectorAll<HTMLElement>('.cm-comment-marker')].map((marker) => {
		const rect = marker.getBoundingClientRect();
		return {
			ids: (marker.dataset.commentIds ?? '').split(',').filter(Boolean),
			label: marker.getAttribute('aria-label'),
			text: (marker.textContent ?? '').trim(),
			left: rect.left, top: rect.top, width: rect.width, height: rect.height,
		};
	}));
}

/** Brings marker `index` to rest at 40% of the editor's height and returns the editor's own geometry beside it. */
async function reveal(page: Page, index: number) {
	return page.evaluate(async (index) => {
		const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const marker = document.querySelectorAll<HTMLElement>('.cm-comment-marker')[index];
		const box = scroller.getBoundingClientRect();
		const rect = marker.getBoundingClientRect();
		scroller.scrollTop += rect.top + rect.height / 2 - (box.top + box.height * 0.4);
		await frame();
		await frame();
		const content = document.querySelector('.cm-content')!.getBoundingClientRect();
		const now = marker.getBoundingClientRect();
		const point = (x: number, y: number) => {
			const hit = document.elementFromPoint(x, y)?.closest<HTMLElement>('.cm-comment-marker');
			// A marker under the sheet or its modal backdrop is covered by design; neither surface exposes the canvas beneath it.
			const covered = !hit && !!document.elementFromPoint(x, y)?.closest('.canvas-comment-drawer.sheet, [data-adaptive-drawer]');
			return { x, y, hit: hit?.dataset.commentIds ?? null, covered };
		};
		const cx = now.left + now.width / 2, cy = now.top + now.height / 2;
		return {
			rect: { left: now.left, top: now.top, width: now.width, height: now.height },
			content: { left: content.left },
			clip: { left: box.left, right: box.right, top: box.top, bottom: box.bottom },
			ids: marker.dataset.commentIds ?? '',
			points: [
				point(cx, cy),
				point(now.left + 1, now.top + 1), point(now.right - 1, now.top + 1),
				point(now.left + 1, now.bottom - 1), point(now.right - 1, now.bottom - 1),
			],
		};
	}, index);
}

/** The cluster rule of the design, applied by hand to the rendered lines: the oracle the markers are compared with. */
async function expectedClusters(page: Page, target: number): Promise<string[][]> {
	return page.evaluate(({ keys, target }) => {
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const origin = scroller.getBoundingClientRect().top - scroller.scrollTop;
		const lines = [...document.querySelectorAll<HTMLElement>('.cm-line')].map((line) => {
			const text = line.textContent ?? '';
			const found = keys.filter((key) => text.includes(key)).sort((a, b) => text.indexOf(a) - text.indexOf(b));
			return { top: line.getBoundingClientRect().top - origin, keys: found };
		}).filter((line) => line.keys.length).sort((a, b) => a.top - b.top);
		const clusters: Array<{ anchor: number; keys: string[] }> = [];
		for (const line of lines) {
			const current = clusters.at(-1);
			if (current && line.top - current.anchor < target) current.keys.push(...line.keys);
			else clusters.push({ anchor: line.top, keys: [...line.keys] });
		}
		return clusters.map((cluster) => cluster.keys);
	}, { keys: ANCHORS, target });
}

function keysOf(opened: Opened, marker: { ids: string[] }) {
	return marker.ids.map((id) => opened.byId[id]);
}

/**
 * What is open now: the drawer, and the threads whose seeded text ("Thread on <key>") is visible in a thread list.
 * Thread cards carry no id attribute outside the drawer, so threads are told apart by their text and mapped back to ids.
 */
async function surfaceOpen(page: Page, ids: Record<string, string> = {}) {
	const found = await page.evaluate((keys) => {
		const lists = [...document.querySelectorAll<HTMLElement>('[aria-label="Comment threads"]')].filter((list) => list.getClientRects().length);
		const text = lists.map((list) => list.textContent ?? '').join('\n');
		return { keys: keys.filter((key) => text.includes(`Thread on ${key}`)), drawer: !!document.querySelector('[data-doc-comments-drawer]') };
	}, ANCHORS);
	return { cards: found.keys.map((key) => ids[key] ?? key), drawer: found.drawer };
}

/** The id of the history entry a phone's open sheet or pane has pushed, or null when there is none. */
const historyLayer = (page: Page) => page.evaluate(() => (history.state as { backLayer?: number } | null)?.backLayer ?? null);

async function closeSurface(page: Page, layerBefore?: number | null) {
	await page.keyboard.press('Escape');
	// The drawer leaves through its outro: it is gone shortly after Escape closes it, and only a drawer still showing then needs its close button.
	const drawer = page.locator('[data-doc-comments-drawer]');
	await drawer.waitFor({ state: 'detached', timeout: 1500 }).catch(() => undefined);
	if (await drawer.count()) {
		await page.getByRole('button', { name: 'Close comments', exact: true }).click();
	}
	await expect.poll(() => surfaceOpen(page).then((s) => s.cards.length + (s.drawer ? 1 : 0))).toBe(0);
	// A phone's sheet pushes a history entry and pops it asynchronously; the next open waits for that pop, as a user's next tap would.
	if (layerBefore !== undefined) await expect.poll(() => historyLayer(page), { message: 'the sheet\'s history entry is gone' }).toBe(layerBefore);
}

const fmt = (n: number) => Math.round(n * 100) / 100;

/**
 * The numeric packet for one viewport and theme: for every marker, its hit box and what its centre and four corners opened.
 * Rows are printed for the run log and attached as JSON.
 */
async function probeEveryMarker(page: Page, info: TestInfo, opened: Opened, theme: Theme, { click = true } = {}) {
	const rail = railFor(info);
	const all = await markers(page);
	const label = `${isMobile(info) ? '393' : '1440'} ${theme}`;
	const rows: string[] = [];
	const boxes: Array<{ top: number; left: number; width: number; height: number; scroll: number }> = [];
	for (let index = 0; index < all.length; index += 1) {
		const seen = await reveal(page, index);
		const ids = seen.ids.split(',');
		const keys = ids.map((id) => opened.byId[id]);
		const scroll = await page.locator('.cm-scroller').evaluate((node) => node.scrollTop);
		const { rect, clip, content } = seen;
		boxes.push({ ...rect, scroll });
		expect.soft(rect.left, `${label} marker ${keys}: inside the rail (left)`).toBeGreaterThanOrEqual(content.left - 0.5);
		expect.soft(rect.left + rect.width, `${label} marker ${keys}: inside the rail (right)`).toBeLessThanOrEqual(content.left + rail + 0.5);
		expect.soft(rect.left, `${label} marker ${keys}: inside the clip`).toBeGreaterThanOrEqual(clip.left);
		expect.soft(rect.top + rect.height, `${label} marker ${keys}: inside the clip (bottom)`).toBeLessThanOrEqual(clip.bottom);
		expect.soft(rect.width, `${label} marker ${keys}: width`).toBeGreaterThanOrEqual(rail);
		expect.soft(rect.height, `${label} marker ${keys}: height`).toBeGreaterThanOrEqual(rail);
		const names = ['centre', 'top-left', 'top-right', 'bottom-left', 'bottom-right'];
		const opens: string[] = [];
		let current = seen;
		const layerBefore = await historyLayer(page);
		for (const [i, name] of names.entries()) {
			// Each corner is aimed with geometry read after the last close: focus returning to the marker may have moved the canvas.
			const point = current.points[i];
			expect.soft(point.hit, `${label} marker ${keys}: ${name} reaches its own marker`).toBe(seen.ids);
			if (!click) continue;
			await page.mouse.click(point.x, point.y);
			const message = `${label} marker ${keys}: ${name} opens something (hit ${point.hit}, at ${fmt(point.x)},${fmt(point.y)})`;
			await expect.poll(() => surfaceOpen(page, opened.ids).then((s) => s.drawer || s.cards.length > 0), { message }).toBe(true);
			const surface = await surfaceOpen(page, opened.ids);
			if (ids.length === 1) expect.soft(surface.cards, `${label} marker ${keys}: ${name} opens its thread`).toContain(ids[0]);
			else expect.soft(surface.drawer, `${label} marker ${keys}: ${name} opens the list`).toBe(true);
			opens.push(`${name}:${ids.length === 1 ? surface.cards.includes(ids[0]) : surface.drawer}`);
			await closeSurface(page, layerBefore);
			current = await reveal(page, index);
		}
		rows.push(`MARKER|${label}|${keys.join('+')}|box ${fmt(rect.left)},${fmt(rect.top)} ${fmt(rect.width)}x${fmt(rect.height)}|rail ${fmt(content.left)}..${fmt(content.left + rail)}|${opens.join(' ')}`);
	}
	// Document-space boxes: no two may intersect.
	const space = boxes.map((box, i) => ({ i, left: box.left, right: box.left + box.width, top: box.top + box.scroll, bottom: box.top + box.scroll + box.height }));
	for (let a = 0; a < space.length; a += 1) {
		for (let b = a + 1; b < space.length; b += 1) {
			const overlap = space[a].left < space[b].right && space[b].left < space[a].right && space[a].top < space[b].bottom - 0.01 && space[b].top < space[a].bottom - 0.01;
			expect.soft(overlap, `${label} markers ${a} and ${b} do not intersect`).toBe(false);
		}
	}
	await info.attach(`hit-boxes-${label.replace(' ', '-')}.txt`, { body: rows.join('\n'), contentType: 'text/plain' });
	return all.length;
}

async function setTheme(page: Page, theme: Theme) {
	await page.emulateMedia({ colorScheme: theme });
	await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
}

/** WCAG contrast of one property of the element over every background up to the first opaque one. */
async function contrast(page: Page, selector: string, property: 'color' | 'stroke' | 'outline') {
	return page.locator(selector).first().evaluate((element, property) => {
		const context = document.createElement('canvas').getContext('2d', { willReadFrequently: true })!;
		const rgba = (colour: string) => {
			context.clearRect(0, 0, 1, 1);
			context.fillStyle = colour;
			context.fillRect(0, 0, 1, 1);
			const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
			return [r / 255, g / 255, b / 255, a / 255];
		};
		const over = ([r, g, b, a]: number[], under: number[]) => [r * a + under[0] * (1 - a), g * a + under[1] * (1 - a), b * a + under[2] * (1 - a), 1];
		const luminance = (rgb: number[]) => rgb.slice(0, 3).map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
			.reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
		const own = getComputedStyle(element);
		const colour = property === 'stroke' ? own.stroke : property === 'outline' ? own.outlineColor : own.color;
		const foreground = rgba(colour);
		const layers: number[][] = [];
		let opacity = 1;
		for (let node: Element | null = element; node; node = node.parentElement) {
			const style = getComputedStyle(node);
			if (style.backgroundImage !== 'none' || style.filter !== 'none' || style.backdropFilter !== 'none') return Number.NaN;
			opacity *= Number.parseFloat(style.opacity);
			const background = rgba(style.backgroundColor);
			background[3] *= Number.parseFloat(style.opacity);
			if (background[3] > 0) layers.unshift(background);
			if (background[3] === 1) break;
		}
		if (layers[0]?.[3] !== 1) return Number.NaN;
		const behind = layers.reduce((under, layer) => over(layer, under));
		const front = over([...foreground.slice(0, 3), foreground[3] * opacity], behind);
		const [light, dark] = [luminance(front), luminance(behind)].sort((a, b) => b - a);
		return (light + 0.05) / (dark + 0.05);
	}, property);
}

test.describe('@flow:doc-margin-marker', () => {
	test('@flow:doc-margin-marker every marker is visible in the rail, sized, apart, and opens its own ids from its centre and corners', async ({ page }, info) => {
		test.setTimeout(480_000);
		const opened = await openDocument(page, info, { orphan: true });
		const rail = railFor(info);
		// Visibility first: a marker that exists but cannot be seen or reached is the defect.
		const all = await markers(page);
		expect(all.length, 'there are markers at all').toBeGreaterThan(0);
		await reveal(page, 0);
		const first = await page.evaluate(() => {
			const marker = document.querySelector<HTMLElement>('.cm-comment-marker')!;
			const rect = marker.getBoundingClientRect();
			const scroller = document.querySelector('.cm-scroller')!.getBoundingClientRect();
			const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
			return { reachable: marker.contains(hit), insideClip: rect.left >= scroller.left && rect.right <= scroller.right && rect.width > 0 };
		});
		expect(first, 'the first marker is on screen and is what a click at its centre reaches').toEqual({ reachable: true, insideClip: true });
		// Shape of the cluster set against the design's rule.
		const expected = await expectedClusters(page, rail);
		expect(all.map((marker) => keysOf(opened, marker)), 'markers are the design\'s clusters, each id once, in document order').toEqual(expected);
		expect(expected.flat().sort(), 'no thread lost or duplicated across clusters').toEqual([...ANCHORS].sort());
		for (const marker of all) {
			const many = marker.ids.length > 1;
			expect.soft(marker.label, `aria-label for ${keysOf(opened, marker)}`).toBe(many ? `Open ${marker.ids.length} comments` : 'Open comment');
			expect.soft(marker.text, `a count shows its number, a single thread shows no text: ${keysOf(opened, marker)}`).toBe(many ? String(marker.ids.length) : '');
		}
		const sameLinePair = all.find((marker) => keysOf(opened, marker).join() === 'qmpa,qmpb');
		expect(sameLinePair, 'the same-line pair is one count marker').toBeTruthy();
		const many = all.find((marker) => marker.ids.length === MANY.length);
		expect(many?.text, 'a two-digit count shows both digits').toBe('12');
		const orphanRow = page.locator('.comment-status:has(.unplaced-dot)');
		await expect(orphanRow, 'the orphan stays flagged in the top status row').toBeVisible();
		expect(all.flatMap((marker) => marker.ids), 'the orphan never has a margin marker').not.toContain(opened.ids.qmorphan);
		if (isMobile(info)) {
			expect(DENSE.length / 2, 'on a coarse pointer dense lines group in anchor-bounded pairs').toBe(all.filter((marker) => marker.ids.length === 2 && DENSE.includes(keysOf(opened, marker)[0])).length);
		} else {
			expect(all.filter((marker) => DENSE.includes(keysOf(opened, marker)[0])).map((marker) => marker.ids.length), 'on a fine pointer each dense line has its own marker').toEqual(DENSE.map(() => 1));
		}
		let count = 0;
		for (const theme of THEMES) {
			await setTheme(page, theme);
			count = await probeEveryMarker(page, info, opened, theme);
			await saveEvidenceScreenshot(page, info, `margin-marker-${theme}`, { fullPage: false });
		}
		expect(count).toBe(all.length);
	});

	test('@flow:doc-margin-marker a point in the first glyph and one pixel past the rail place the caret and drag-select, opening nothing', async ({ page }, info) => {
		test.setTimeout(180_000);
		await openDocument(page, info);
		const rail = railFor(info);
		const target = await page.evaluate(() => {
			const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
			const line = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => candidate.textContent?.startsWith('Alone qmalone'))!;
			scroller.scrollTop += line.getBoundingClientRect().top - (scroller.getBoundingClientRect().top + scroller.clientHeight * 0.3);
			return new Promise<{ glyph: { x: number; y: number }; content: number; y: number }>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => {
				const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
				const text = walker.nextNode()!;
				const range = document.createRange();
				range.setStart(text, 0);
				range.setEnd(text, 1);
				const glyph = range.getBoundingClientRect();
				const content = document.querySelector('.cm-content')!.getBoundingClientRect().left;
				const box = line.getBoundingClientRect();
				resolve({ glyph: { x: glyph.left + glyph.width / 2, y: glyph.top + glyph.height / 2 }, content, y: box.top + box.height / 2 });
			})));
		});
		const cases = [
			// A touch pointer's hit box reaches into the text's edge on purpose, so its first glyph belongs to the marker.
			...(rail <= TEXT_INSET ? [{ name: 'inside the first glyph of a flush-left line', x: target.glyph.x, y: target.glyph.y }] : []),
			{ name: '1px right of the rail', x: target.content + rail + 1, y: target.y },
		];
		for (const { name, x, y } of cases) {
			expect.soft(x, `${name}: the point is right of the rail`).toBeGreaterThan(target.content + rail);
			const hit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('.cm-comment-marker') !== null, { x, y });
			expect(hit, `${name}: no marker is under the point`).toBe(false);
			await page.mouse.click(x, y);
			const caret = await page.evaluate(() => {
				const selection = getSelection();
				return { collapsed: !!selection?.isCollapsed, line: (selection?.anchorNode?.parentElement?.closest('.cm-line')?.textContent ?? '').slice(0, 14), focused: !!document.activeElement?.closest('.cm-content') };
			});
			expect.soft(caret, `${name}: the caret is placed in that line`).toEqual({ collapsed: true, line: 'Alone qmalone ', focused: true });
			expect.soft(await surfaceOpen(page), `${name}: nothing opened`).toEqual({ cards: [], drawer: false });
			await page.mouse.move(x, y);
			await page.mouse.down();
			await page.mouse.move(x + 90, y, { steps: 6 });
			await page.mouse.up();
			const selected = await page.evaluate(() => ({ text: getSelection()?.toString() ?? '', line: (getSelection()?.anchorNode?.parentElement?.closest('.cm-line')?.textContent ?? '').slice(0, 14) }));
			expect.soft(selected.text.length, `${name}: dragging selects text`).toBeGreaterThan(0);
			expect.soft(selected.line, `${name}: the selection starts on that line`).toBe('Alone qmalone ');
			expect.soft(await surfaceOpen(page), `${name}: the drag opened nothing`).toEqual({ cards: [], drawer: false });
		}
	});

	test('@flow:doc-margin-marker the first line never reflows, markers stay right across scroll, zoom and the drawer, and a read-only canvas still shows them', async ({ page, context }, info) => {
		test.setTimeout(300_000);
		const opened = await openDocument(page, info);
		const rail = railFor(info);
		// Each line is measured as it is and again as a bare copy of the same text at the same width, in the same
		// document, so nothing about the pane or the comments can differ between the two but the rail itself.
		const geometry = () => page.evaluate(() => {
			const scroller = document.querySelector<HTMLElement>('.cm-scroller')!.getBoundingClientRect();
			const lines = [...document.querySelectorAll<HTMLElement>('.cm-line')];
			const bare = (line: HTMLElement) => {
				const style = getComputedStyle(line);
				const copy = document.createElement('div');
				for (const property of ['font-family', 'font-size', 'font-weight', 'font-style', 'letter-spacing', 'line-height', 'padding-left', 'padding-right', 'padding-top', 'padding-bottom', 'white-space', 'word-break', 'overflow-wrap', 'tab-size', 'text-indent']) {
					copy.style.setProperty(property, style.getPropertyValue(property));
				}
				copy.style.cssText += `;box-sizing:border-box;position:absolute;visibility:hidden;top:0;left:0;width:${line.getBoundingClientRect().width}px`;
				copy.textContent = line.textContent;
				document.querySelector('.cm-scroller')!.append(copy);
				const height = copy.getBoundingClientRect().height;
				copy.remove();
				return height;
			};
			const wrapped = lines.find((line) => line.textContent?.startsWith('Wrapped qmwrap'))!;
			const first = lines[0];
			const firstRect = first.getBoundingClientRect();
			return {
				firstLeft: firstRect.left - scroller.left, firstTop: firstRect.top - scroller.top,
				firstHeight: firstRect.height, firstBare: bare(first),
				wrappedHeight: wrapped.getBoundingClientRect().height, wrappedBare: bare(wrapped),
				markersInLines: document.querySelectorAll('.cm-line .cm-comment-marker').length,
			};
		});
		const withMarkers = await geometry();
		const control = await context.newPage();
		await openDocument(control, info, { comments: false });
		expect(await markers(control), 'the control canvas has no markers').toEqual([]);
		await control.close();
		expect(withMarkers.markersInLines, 'no marker sits inside a line, so none adds to its advance').toBe(0);
		expect(withMarkers.firstTop, 'the first line starts at the content top').toBe(4);
		expect(withMarkers.firstHeight, 'the first line is as tall as the same text with no marker').toBe(withMarkers.firstBare);
		expect(withMarkers.wrappedHeight, 'a wrapped line wraps as the same text at the same width would with no marker').toBe(withMarkers.wrappedBare);
		expect(withMarkers.firstLeft, 'the text starts at the text inset, whatever the hit box reaches over').toBeCloseTo(TEXT_INSET, 0);

		// The drawer narrows the canvas: markers re-seat and stay clustered the same way.
		const before = (await markers(page)).map((marker) => keysOf(opened, marker));
		await page.getByRole('button', { name: new RegExp(`^${ANCHORS.length} open threads\\b`) }).click();
		await expect(page.locator('[data-doc-comments-drawer]')).toBeVisible();
		await settle(page);
		const withDrawer = await markers(page);
		expect(withDrawer.map((marker) => keysOf(opened, marker)), 'the drawer does not change the clusters').toEqual(before);
		// A canvas too narrow for a side column gets the documented modal AdaptiveDrawer. Its portaled backdrop blocks the canvas;
		// a non-modal drawer would leave markers above its sheet reachable.
		const modalDrawer = await page.locator('[data-adaptive-drawer]').count() > 0;
		expect(modalDrawer, 'this narrow document canvas uses the documented modal drawer').toBe(true);
		let reachable = 0;
		for (let index = 0; index < withDrawer.length; index += 1) {
			const seen = await reveal(page, index);
			const centre = seen.points[0];
			if (centre.hit === seen.ids) reachable += 1;
			else expect.soft(centre.covered, `drawer open: marker ${index} is reachable or covered by the sheet only`).toBe(true);
			expect.soft(seen.rect.left + seen.rect.width, `drawer open: marker ${index} stays in the rail`).toBeLessThanOrEqual(seen.content.left + rail + 0.5);
		}
		expect(reachable, 'the modal drawer backdrop protects the canvas').toBe(0);
		await page.getByRole('button', { name: 'Close comments', exact: true }).click();
		await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);

		// Scroll and zoom each get a canvas of their own at that size from the start, as a reload would give, rather than
		// resizing this one: a mobile page resized mid-test is a harness state no user is ever in.
		const sized = async (size: { width: number; height: number }) => {
			const fresh = await context.newPage();
			const freshOpened = await openDocument(fresh, info, { size, tall: false });
			return { fresh, freshOpened };
		};
		// Scroll: at an ordinary height the editor shows part of the document, and every marker is still reachable once scrolled to.
		const ordinary = await sized(viewport(info, false));
		const scrollGroups = () => markers(ordinary.fresh).then((found) => found.map((marker) => keysOf(ordinary.freshOpened, marker)));
		await expect.poll(scrollGroups, { message: 'the scroll canvas has rendered its marker groups' }).toEqual(before);
		const scrolled = await markers(ordinary.fresh);
		expect(scrolled.map((marker) => keysOf(ordinary.freshOpened, marker)), 'scrolling regroups nothing').toEqual(before);
		for (let index = 0; index < scrolled.length; index += 1) {
			const seen = await reveal(ordinary.fresh, index);
			expect.soft(seen.points[0].hit, `scrolled: marker ${index} is reachable`).toBe(seen.ids);
			expect.soft(seen.rect.left + seen.rect.width, `scrolled: marker ${index} stays in the rail`).toBeLessThanOrEqual(seen.content.left + rail + 0.5);
		}
		expect(await ordinary.fresh.evaluate(() => document.querySelector('.cm-scroller')!.scrollLeft), 'no horizontal scroll').toBe(0);
		// The canvas column never grows past its pane: a wide header button used to widen it and push the editor sideways.
		expect(await ordinary.fresh.evaluate(() => {
			const doc = document.querySelector<HTMLElement>('.doc')!;
			const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
			return { doc: doc.scrollWidth - doc.clientWidth, scroller: scroller.scrollWidth - scroller.clientWidth, page: document.documentElement.scrollWidth - window.innerWidth };
		}), 'the canvas, the editor and the page have nothing to scroll sideways').toEqual({ doc: 0, scroller: 0, page: 0 });
		await ordinary.fresh.close();
		// Zoom: a 200% zoom of this viewport is half the CSS width; the rail stays 24 or 44 CSS px.
		const size = viewport(info, false);
		const zoom = await sized({ width: Math.round(size.width / 2), height: Math.round(size.height / 2) });
		const zoomed = await markers(zoom.fresh);
		expect(zoomed.length, 'markers survive a zoom').toBeGreaterThan(0);
		for (let index = 0; index < zoomed.length; index += 1) {
			const seen = await reveal(zoom.fresh, index);
			expect.soft(seen.points[0].hit, `zoomed: marker ${index} is reachable`).toBe(seen.ids);
			expect.soft(seen.rect.width, `zoomed: marker ${index} keeps its size`).toBeGreaterThanOrEqual(rail);
		}
		const zoomedIds = zoomed.flatMap((marker) => marker.ids).sort();
		expect(zoomedIds, 'zoomed: every anchored id appears once').toEqual(Object.values(zoom.freshOpened.ids).sort());
		expect(await zoom.fresh.evaluate(() => document.querySelector('.cm-scroller')!.scrollLeft), 'zoomed: no horizontal scroll').toBe(0);
		await zoom.fresh.close();
	});

	test('@flow:doc-margin-marker a read-only canvas shows the same markers and opens them', async ({ page }, info) => {
		test.setTimeout(120_000);
		const opened = await openDocument(page, info, { readOnly: true });
		await expect(page.getByText('Read-only', { exact: false })).toBeVisible();
		const all = await markers(page);
		expect(all.length).toBeGreaterThan(0);
		const single = all.findIndex((marker) => marker.ids.length === 1);
		const seen = await reveal(page, single);
		expect(seen.points[0].hit).toBe(seen.ids);
		await page.mouse.click(seen.points[0].x, seen.points[0].y);
		await expect.poll(() => surfaceOpen(page, opened.ids).then((s) => s.cards)).toContain(seen.ids);
	});

	test('@flow:doc-margin-marker Tab, Enter and Space reach markers and return focus, and focus follows its id when clusters regroup', async ({ page }, info) => {
		test.setTimeout(240_000);
		const opened = await openDocument(page, info);
		const all = await markers(page);
		const single = all.findIndex((marker) => marker.ids.length === 1);
		const singleIds = all[single].ids.join(',');
		const active = () => page.evaluate(() => (document.activeElement as HTMLElement | null)?.dataset?.commentIds ?? null);
		await reveal(page, single);
		const buttons = page.locator('.cm-comment-marker');
		for (const key of ['Enter', 'Space'] as const) {
			await buttons.nth(single).focus();
			expect(await active(), `${key}: the marker takes focus`).toBe(singleIds);
			await page.keyboard.press(key === 'Space' ? ' ' : 'Enter');
			await expect.poll(() => surfaceOpen(page, opened.ids).then((s) => s.cards), `${key} opens the thread`).toContain(all[single].ids[0]);
			await page.keyboard.press('Escape');
			await expect.poll(() => surfaceOpen(page).then((s) => s.cards.length)).toBe(0);
			expect(await active(), `${key}: focus returns to the marker`).toBe(singleIds);
		}
		const count = all.findIndex((marker) => marker.ids.length > 1);
		await buttons.nth(count).focus();
		await page.keyboard.press('Enter');
		await expect(page.locator('[data-doc-comments-drawer]')).toBeVisible();
		await closeSurface(page);
		expect(await active(), 'a count marker gets focus back too').toBe(all[count].ids.join(','));
		// Tab order is document order.
		await buttons.nth(0).focus();
		await page.keyboard.press('Tab');
		expect(await active(), 'Tab moves to the next marker in document order').toBe(all[1].ids.join(','));
		await page.keyboard.press('Shift+Tab');
		expect(await active(), 'Shift+Tab moves back').toBe(all[0].ids.join(','));

		// Regroup: squeeze every line, then spread them. Focus stays with the id it was on.
		const now = await markers(page);
		const nearIndex = now.findIndex((marker) => keysOf(opened, marker).includes('qmna'));
		// A count marker stands for its first thread, so the thread the focus follows is the first id of the marker it is on.
		const nearId = now[nearIndex].ids[0];
		await buttons.nth(nearIndex).focus();
		expect(await active(), `the marker holding qmna (${keysOf(opened, now[nearIndex])}) has focus before the regroup`).toBe(now[nearIndex].ids.join(','));
		for (const lineHeight of ['10px', '90px']) {
			await page.addStyleTag({ content: `.cm-line { line-height: ${lineHeight} !important; }` });
			await expect.poll(async () => (await markers(page)).map((marker) => marker.ids.length).join(','), { message: `markers regroup at ${lineHeight}` }).not.toBe(all.map((marker) => marker.ids.length).join(','));
			await settle(page);
			const holder = await page.evaluate(() => (document.activeElement as HTMLElement | null)?.dataset?.commentIds ?? '');
			expect(holder.split(','), `after ${lineHeight} lines focus is on the marker holding the thread it was on (${opened.byId[nearId]}; focus is on ${keysOf(opened, { ids: holder.split(',') })}; markers ${(await markers(page)).map((marker) => keysOf(opened, marker).join('+')).join(' | ')})`).toContain(nearId);
			const labels = (await markers(page)).flatMap((marker) => marker.ids);
			expect(labels.sort(), `after ${lineHeight} lines every id appears once`).toEqual(Object.values(opened.ids).sort());
		}
	});

	test('@flow:doc-margin-marker the count has 4.5:1 text, the glyph and focus ring 3:1, in both themes, and reduced motion removes transitions', async ({ page }, info) => {
		test.setTimeout(180_000);
		await openDocument(page, info);
		const environment = await page.evaluate(() => ({
			coarse: matchMedia('(any-pointer: coarse)').matches,
			fine: matchMedia('(any-pointer: fine)').matches,
			touchPoints: navigator.maxTouchPoints,
		}));
		if (isMobile(info)) expect(environment.coarse, 'the phone fixture\'s real matchMedia says coarse').toBe(true);
		else expect(environment.coarse, 'the desktop fixture\'s real matchMedia says not coarse').toBe(false);
		const allBoxes = await markers(page);
		expect(allBoxes.every((marker) => marker.width >= railFor(info)), `markers use the ${railFor(info)}px rail the pointer environment selects`).toBe(true);
		const countSelector = '.cm-comment-marker[data-comment-ids*=","] .cm-comment-count';
		const glyphSelector = '.cm-comment-marker:not([data-comment-ids*=","]) svg';
		const twoDigitSelector = '.cm-comment-marker .cm-comment-count:text-matches("^[0-9]{2}")';
		expect(await page.locator(twoDigitSelector).count(), 'the dense and many-on-a-line cases make a two-digit count').toBeGreaterThan(0);
		expect(await page.locator('.cm-comment-marker:has(.cm-comment-count:text-matches("^[0-9]{2}"))').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('aria-label') === `Open ${node.getAttribute('data-comment-ids')?.split(',').length} comments`)), 'two-digit counts name their comments').not.toContain(false);
		for (const theme of THEMES) {
			await setTheme(page, theme);
			expect.soft(await contrast(page, countSelector, 'color'), `${theme}: count text contrast`).toBeGreaterThanOrEqual(TEXT);
			expect.soft(await contrast(page, glyphSelector, 'stroke'), `${theme}: glyph contrast`).toBeGreaterThanOrEqual(GRAPHIC);
			expect.soft(await contrast(page, twoDigitSelector, 'color'), `${theme}: two-digit count text contrast`).toBeGreaterThanOrEqual(TEXT);
			// A key press first, so a programmatic focus counts as keyboard focus and shows its ring.
			await page.keyboard.press('Shift');
			await page.locator('.cm-comment-marker:not([data-comment-ids*=","])').first().focus();
			await expect(page.locator('.cm-comment-marker:focus-visible')).toHaveCount(1);
			const ring = await contrast(page, '.cm-comment-marker:focus-visible', 'outline');
			expect.soft(ring, `${theme}: focus ring contrast`).toBeGreaterThanOrEqual(GRAPHIC);
			await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		}
		await page.emulateMedia({ reducedMotion: 'reduce' });
		const motion = await page.locator('.cm-comment-marker').first().evaluate((marker) => {
			const style = getComputedStyle(marker);
			return { property: style.transitionProperty, animation: style.animationName };
		});
		expect(motion, 'reduced motion: the marker has no transition or animation').toEqual({ property: 'none', animation: 'none' });
	});
});
