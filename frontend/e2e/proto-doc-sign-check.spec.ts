// PROTOTYPE (uncommitted): the document canvas press behaviour of the D sign (list sheet/popover/margin, reveal above the sheet, quote rows, reply field), measured and framed per width.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from './support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(process.env.CORSIA_E2E_WORKSPACE ?? '/tmp/b827-out', 'signcheck');

const P_INTRO = 'This plan covers the Q4 launch of the new onboarding flow: what ships, who owns it, when it happens and what could go wrong.';
const P_GOALS = 'Cut the time from install to first useful result to under ten minutes for a new person. Keep the support queue flat through launch week. Land the announcement with every channel on the same day, and tell each partner team before the public post goes out.';
const P_SCOPE = 'In scope: the redesigned first-run screens, the guided project setup, and the updated help pages. Out of scope: the mobile app, pricing changes and anything that needs a new integration.';
const P_AUDIENCE = 'New people installing on their own machine, most of them trying CORSIA after hearing about it from a colleague. They have a model account already and want a first useful result without reading documentation.';
const DOC = [
	'# Q4 launch plan', '', P_INTRO, '',
	'## Goals', '', P_GOALS, '',
	'## Scope', '', P_SCOPE, '',
	'## Audience', '', P_AUDIENCE, '',
	'## Timeline', '',
	'| Milestone | Owner | Date |', '| --- | --- | --- |',
	'| Design freeze | Priya Raman | Nov 3 |', '| Build complete | Marcus Chen | Nov 17 |', '| Public launch | Dana Okafor | Dec 1 |', '',
	'## Post-launch', '',
	'- Check first-run completion against the dogfood baseline.',
	'- Review the support queue and escalation time.',
	'- Keep the rollback owner available until Monday.',
	'- Read the [launch checklist](https://example.com/checklist) before the cutover.', '',
].join('\n');

type Case = { id: string; title: string; quote: string };
const READ_QUOTE = 'rollback owner';
const CASES: Case[] = [
	{ id: 'one-word-first-line', title: 'One word, first line of a paragraph', quote: 'Cut' },
	{ id: 'one-word-mid', title: 'One word, mid-paragraph', quote: 'partner' },
	{ id: 'long-first-line', title: 'Long highlight, from the first line', quote: 'In scope: the redesigned first-run screens, the guided project setup, and the updated help pages' },
	{ id: 'long-mid', title: 'Long highlight, mid-paragraph', quote: 'most of them trying CORSIA after hearing about it from a colleague. They have a model account already' },
	{ id: 'table-cell', title: 'Inside a table cell', quote: 'Marcus Chen' },
	{ id: 'list-item', title: 'Inside a list item', quote: 'escalation' },
	// A second unread thread that ends where the list item's thread ends.
	{ id: 'same-end', title: 'Two unread threads ending at the same offset', quote: 'queue and escalation' },
	// A second unread thread on the table cell's text.
	// An unread thread right after a real link: the unread sign must never read as a link.
	{ id: 'beside-link', title: 'Unread highlight beside a link', quote: 'before the cutover' },
	{ id: 'table-cell-second', title: 'Second thread in the same table cell', quote: 'Marcus' },
];

function sqlite(statements: Array<[string, Array<string | number>]>): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated synthetic state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', ['import json, sqlite3, sys', 'c = sqlite3.connect(sys.argv[1])', 'for sql, params in json.loads(sys.argv[2]): c.execute(sql, params)', 'c.commit()', 'c.close()'].join('\n'),
		path.join(stateRoot, 'corsia.db'), JSON.stringify(statements),
	], { cwd: repoRoot, encoding: 'utf8' });
}

async function seed(request: APIRequestContext, threadId: string, folder: string) {
	const tree = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = ((await tree.json()).roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')?.path;
	const docPath = path.join(workspace!, folder, 'launch-plan.md');
	mkdirSync(path.dirname(docPath), { recursive: true });
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	for (const c of CASES) {
		const from = DOC.indexOf(c.quote);
		expect(from, c.id).toBeGreaterThan(-1);
		expect(DOC.indexOf(c.quote, from + 1), `${c.id} unique`).toBe(-1);
		const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: `Question about ${c.title.toLowerCase()}`, anchor_text: c.quote, from_off: from, to_off: from + c.quote.length, delivery_policy: 'immediate' } });
		expect(root.ok(), await root.text()).toBeTruthy();
		const rootId = (await root.json()).comment.id as string;
		sqlite([["INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Maya Chen', 'agent', ?, ?, strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'))", [artifactId, rootId, threadId, `Answer on ${c.title.toLowerCase()}.`]]]);
	}
	{
		const from = DOC.indexOf(READ_QUOTE);
		const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Read thread: who covers the rollback?', anchor_text: READ_QUOTE, from_off: from, to_off: from + READ_QUOTE.length, delivery_policy: 'immediate' } });
		expect(root.ok(), await root.text()).toBeTruthy();
	}
	sqlite([["UPDATE doc_comments SET author='Jordan Lee' WHERE author='e2e'", []]]);
	return { docPath, artifactId };
}

async function stage(page: Page, threadId: string, docPath: string, threadTitle: string, width: number, wide = false) {
	await page.addInitScript(({ threadId, docPath, wide }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
		threads: { [threadId]: { tabs: [{ instanceId: 'proto-sign', artifactId: null, path: docPath, title: 'launch-plan.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'proto-sign', visible: true, activeSurface: 'artifact' } }, splitWidth: wide ? 820 : 420,
	})), { threadId, docPath, wide });
	await loginByUrl(page);
	// Wide: the sidebar folded away, so the document pane is 660 px or more and the margin column has room.
	// The thread is picked before the sidebar folds: clicking it while the sidebar animates away left the click outside the viewport.
	if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, threadTitle).click();
	await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
	if (wide) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
}

test.use({ hasTouch: true });

// Seven threads on one phrase, each by a long name: the list's worst case for width and height.
const MANY = 'onboarding flow';
const LONG_NAMES = ['Alexandrina Wolfeschlegelsteinhausenberger', 'Bartholomew Featherstonehaugh-Cholmondeley', 'Christopher Vandenberghe-Oosterhuis', 'Dominique Papadopoulos-Constantinidis', 'Evangeline Rutherford-Montgomerie', 'Ferdinand Oluwaseun Adebayo-Nwachukwu', 'Guadalupe Hernandez-Villanueva'];
const RUN = Date.now().toString(36);
const CONFIGS: Array<{ theme: 'light' | 'dark'; width: number; wide: boolean }> = [];
for (const theme of ['light', 'dark'] as const) for (const width of [393, 900, 1440]) CONFIGS.push({ theme, width, wide: false });
for (const theme of ['light', 'dark'] as const) CONFIGS.push({ theme, width: 1440, wide: true });

for (const { theme, width, wide } of CONFIGS) {
	test(`sign check ${theme} ${width}${wide ? ' wide' : ''}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(240_000);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: theme });
		const tag = wide ? `${theme}-wide${width}` : `${theme}-${width}`;
		page.on('console', (m) => { const t = m.text(); if (/^(REVEAL|NOTE)/.test(t)) console.log(t); });
		const threadId = await createSession(request, `Q4 launch plan ${tag}`);
		const { docPath, artifactId } = await seed(request, threadId, `check-${RUN}-${tag}`);
		// Seven threads on one phrase, each root by a long name, each with an unread agent reply.
		const from = DOC.indexOf(MANY);
		for (let i = 0; i < LONG_NAMES.length; i++) {
			const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: `Many ${i + 1}: a question about the flow`, anchor_text: MANY, from_off: from, to_off: from + MANY.length, delivery_policy: 'immediate' } });
			expect(root.ok(), await root.text()).toBeTruthy();
			const rootId = (await root.json()).comment.id as string;
			sqlite([
				["UPDATE doc_comments SET author=? WHERE id=?", [LONG_NAMES[i], rootId]],
				["INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Maya Chen', 'agent', ?, ?, strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'))", [artifactId, rootId, threadId, `Answer ${i + 1} on the flow.`]],
			]);
		}
		await stage(page, threadId, docPath, `Q4 launch plan ${tag}`, width, wide);
		const close = page.getByRole('button', { name: 'Close comments', exact: true });
		if (await close.count()) await close.first().click();
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
		await expect.poll(() => page.locator('[data-highlight-dot]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
		await expect.poll(() => page.locator('.cm-comment-anchor[data-unread]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
		await page.waitForTimeout(1500);
		await page.route('**/comments/*/viewed', (route) => route.abort());

		// The editor draws only the lines near the viewport: walk the document until the text is drawn, then centre it.
		const centre = async (quote: string) => {
			for (let i = 0; i < 40; i++) {
				const done = await page.evaluate((quote) => {
					const content = document.querySelector('.doc .cm-content') as HTMLElement;
					const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
					for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n.textContent ?? '').includes(quote)) { (n.parentElement as HTMLElement).scrollIntoView({ block: 'center' }); return true; }
					let scroller: HTMLElement | null = null;
					for (let e: HTMLElement | null = content; e; e = e.parentElement) { const o = getComputedStyle(e).overflowY; if ((o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1) { scroller = e; break; } }
					if (scroller) scroller.scrollTop = scroller.scrollTop + scroller.clientHeight * 0.6 > scroller.scrollHeight ? 0 : scroller.scrollTop + scroller.clientHeight * 0.6;
					return false;
				}, quote);
				if (done) break;
				await page.waitForTimeout(250);
			}
			await page.waitForTimeout(500);
		};
		const press = async (quote: string, offset: number) => {
			await centre(quote);
			const spot = await page.evaluate(({ quote, offset }) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(quote); if (at > -1) { const r = document.createRange(); r.setStart(n, at + offset); r.setEnd(n, at + offset + 2); const q = r.getBoundingClientRect(); return { x: q.left + q.width / 2, y: q.top + q.height / 2 }; } }
				return null;
			}, { quote, offset });
			expect(spot, `${quote} found`).not.toBeNull();
			if (wide) await page.mouse.click(spot!.x, spot!.y); else await page.touchscreen.tap(spot!.x, spot!.y);
			await page.waitForTimeout(1200);
		};
		const measure = (quote: string, offset: number) => page.evaluate(({ quote, offset }) => {
			const vis = (e: Element) => e.getClientRects().length > 0;
			const box = (r: DOMRect | null | undefined) => r ? { l: Math.round(r.left), t: Math.round(r.top), r: Math.round(r.right), b: Math.round(r.bottom) } : null;
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			let line: DOMRect | null = null;
			const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
			for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(quote); if (at > -1) { const r = document.createRange(); r.setStart(n, at + offset); r.setEnd(n, at + offset + 2); line = r.getClientRects()[0] ?? null; break; } }
			const sheetEl = [...document.querySelectorAll<HTMLElement>('[data-adaptive-drawer] .drawer-panel')].find(vis);
			const popEl = [...document.querySelectorAll<HTMLElement>('[data-popover]')].find(vis);
			const marginEl = document.querySelector<HTMLElement>('.canvas-comment-drawer.margin.on-canvas');
			const surfEl = sheetEl ?? popEl ?? marginEl ?? null;
			const surface = sheetEl ? 'sheet' : popEl ? 'popover' : marginEl ? 'margin' : null;
			const sr = surfEl?.getBoundingClientRect() ?? null;
			const overlap = line && sr ? Math.max(0, Math.min(line.right, sr.right) - Math.max(line.left, sr.left)) * Math.max(0, Math.min(line.bottom, sr.bottom) - Math.max(line.top, sr.top)) : null;
			const cards = surfEl ? [...surfEl.querySelectorAll<HTMLElement>('.ct')].filter(vis) : [];
			const inputs = surfEl ? [...surfEl.querySelectorAll<HTMLElement>('.ct-input')].filter(vis) : [];
			let worstClip = 0, worstGap = Infinity;
			for (const input of inputs) {
				const ir = input.getBoundingClientRect(), card = input.closest('.ct')?.getBoundingClientRect();
				if (card) worstGap = Math.min(worstGap, Math.round((card.bottom - ir.bottom) * 10) / 10);
				for (let a: HTMLElement | null = input.parentElement; a && surfEl && surfEl.contains(a); a = a.parentElement) {
					const o = getComputedStyle(a).overflowY;
					if (o === 'visible') continue;
					const ar = a.getBoundingClientRect();
					worstClip = Math.max(worstClip, Math.round((ir.bottom - ar.bottom) * 10) / 10);
				}
			}
			const actions = cards[0] ? [...cards[0].querySelectorAll<HTMLElement>('button[aria-label]')].filter(vis).filter((b) => /agree|resolve|thumb|like/i.test(b.getAttribute('aria-label') ?? '')).map((b) => { const r = b.getBoundingClientRect(); return { label: b.getAttribute('aria-label'), l: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height) }; }) : [];
			return {
				surface, surfaceRect: box(sr), pressed: box(line), overlapPx2: overlap === null ? null : Math.round(overlap),
				gapPressedToSurface: line && sr ? Math.round(sr.top >= line.bottom ? sr.top - line.bottom : line.top - sr.bottom) : null,
				title: surfEl?.querySelector('h2')?.textContent ?? null,
				name: surfEl?.closest('[role="dialog"]')?.getAttribute('aria-label') ?? surfEl?.getAttribute('aria-label') ?? null,
				cards: cards.length, quotes: cards.filter((c) => c.querySelector('.quote')).length, news: cards.reduce((n, c) => n + c.querySelectorAll('.ct-new').length, 0),
				replyInputs: inputs.length, replyClippedPx: worstClip, replyGapToCardBottom: worstGap === Infinity ? null : worstGap,
				longestName: Math.max(0, ...cards.map((c) => Math.round(c.scrollWidth - c.clientWidth))),
				sheetScrolls: sheetEl ? (() => { const b = sheetEl.querySelector<HTMLElement>('.drawer-body'); return b ? b.scrollHeight > b.clientHeight : false; })() : null,
				actions,
			};
		}, { quote, offset });
		const frame = async (name: string) => {
			const clip = await page.evaluate(() => {
				const doc = document.querySelector('.doc')?.getBoundingClientRect();
				const left = doc ? Math.max(0, Math.floor(doc.left) - 1) : 0, right = doc ? Math.min(innerWidth, Math.ceil(doc.right) + 1) : innerWidth;
				return { x: left, y: 0, width: right - left, height: innerHeight };
			});
			await page.screenshot({ path: path.join(out, `check-${name}-${tag}.png`), clip });
		};
		const closeAll = async () => {
			await page.keyboard.press('Escape'); await page.waitForTimeout(500);
			const btn = page.locator('[data-adaptive-drawer]').getByRole('button', { name: 'Close', exact: true }).first();
			if (await btn.count() && await btn.isVisible()) { await btn.click(); await page.waitForTimeout(600); }
		};
		mkdirSync(out, { recursive: true });
		const result: Record<string, unknown> = {};
		for (const [name, quote, offset] of [['table', 'Marcus', 2], ['list', 'escalation', 2], ['single', 'rollback owner', 3], ['many', MANY, 3]] as const) {
			await press(quote, offset);
			result[name] = await measure(quote, offset);
			await frame(name);
			if (name === 'many') {
				// The last card's reply field must be reachable: scroll the list to its end and look again.
				await page.evaluate(() => { for (const el of document.querySelectorAll<HTMLElement>('[data-adaptive-drawer] .drawer-body, [data-popover] .drawer-comments, [data-popover]')) el.scrollTop = el.scrollHeight; });
				await page.waitForTimeout(500);
				result.manyEnd = await measure(quote, offset);
				await frame('many-end');
			}
			await closeAll();
		}
		// F1: a real link and an unread highlight side by side, measured in the page.
		await centre('before the cutover');
		result.linkBeside = await page.evaluate(() => {
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const probe = document.createElement('span'); document.body.append(probe);
			const rgba = (css: string) => { probe.style.color = css; const c = getComputedStyle(probe).color; const cv = document.createElement('canvas'); cv.width = cv.height = 1; const x = cv.getContext('2d')!; x.clearRect(0, 0, 1, 1); x.fillStyle = c; x.fillRect(0, 0, 1, 1); const d = x.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
			const lum = (c: number[]) => { const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
			const ratio = (a: string, b: string) => { const x = lum(rgba(a)), y = lum(rgba(b)); return Math.round(((Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)) * 100) / 100; };
			const link = [...content.querySelectorAll<HTMLElement>('.cm-md-link')].find((e) => (e.textContent ?? '').includes('launch checklist'));
			const unread = [...content.querySelectorAll<HTMLElement>('.cm-comment-anchor[data-unread]')].find((e) => (e.textContent ?? '').includes('cutover'));
			if (!link || !unread) return { found: { link: !!link, unread: !!unread } };
			const l = getComputedStyle(link), u = getComputedStyle(unread);
			const linkLine = l.textDecorationColor, unreadLine = u.borderBottomColor;
			const out = { linkText: l.color, linkUnderline: `${l.textDecorationLine} ${l.textDecorationColor}`, unreadText: u.color, unreadUnderline: `${u.borderBottomWidth} ${u.borderBottomStyle} ${unreadLine}`, unreadWash: u.backgroundColor, underlineVsLinkText: ratio(unreadLine, l.color), underlineVsLinkUnderline: ratio(unreadLine, linkLine), unreadInkVsLinkInk: ratio(u.color, l.color), unreadUnderlineVsOwnInk: ratio(unreadLine, u.color) };
			probe.remove();
			return out;
		});
		{
			const clip = await page.evaluate(() => { const c = document.querySelector('.doc .cm-content') as HTMLElement; const t = [...c.querySelectorAll<HTMLElement>('.cm-md-link')].find((e) => (e.textContent ?? '').includes('launch checklist')); const r = t?.getBoundingClientRect(); const d = document.querySelector('.doc')!.getBoundingClientRect(); return r ? { x: Math.max(0, Math.floor(d.left)), y: Math.max(0, Math.floor(r.top - 50)), width: Math.ceil(d.width), height: 130 } : null; });
			if (clip) await page.screenshot({ path: path.join(out, `check-link-${tag}.png`), clip });
		}
		writeFileSync(path.join(out, `check-${tag}.json`), JSON.stringify(result, null, 1));
		console.log(`CHECK ${tag} ${JSON.stringify(Object.fromEntries(Object.entries(result).map(([k, v]) => { const m = v as Record<string, unknown>; return [k, [m.surface, m.title, m.cards, m.quotes, m.overlapPx2, m.gapPressedToSurface, m.replyClippedPx]]; })))}`);
	});
}
