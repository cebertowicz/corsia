// PROTOTYPE (uncommitted): the real document canvas with unread comment threads, dev placement vs the dot-under placement.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from './support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(process.env.CORSIA_E2E_WORKSPACE ?? '/tmp/b827-out', 'signout');

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
	'- Keep the rollback owner available until Monday.', '',
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

async function stage(page: Page, threadId: string, docPath: string, threadTitle: string, width: number) {
	await page.addInitScript(({ threadId, docPath }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
		threads: { [threadId]: { tabs: [{ instanceId: 'proto-sign', artifactId: null, path: docPath, title: 'launch-plan.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'proto-sign', visible: true, activeSurface: 'artifact' } }, splitWidth: 420,
	})), { threadId, docPath });
	await loginByUrl(page);
	if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, threadTitle).click();
	await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
}

const RUN = Date.now().toString(36);
type Variant = 'before' | 'tint';
const VARS: Variant[] = ['before', 'tint'];
const LABELS: Record<Variant, string> = { before: 'dev (origin/dev 3e3d2ea9a)', tint: 'D: tinted highlight' };

for (const theme of ['light', 'dark'] as const) for (const width of [393, 900, 1440]) {
	test(`proto doc new sign ${theme} ${width}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(360_000);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: theme });
		const title = `Launch plan review ${RUN}-${theme}-${width}`;
		const threadId = await createSession(request, title);
		const { docPath } = await seed(request, threadId, `proto-${theme}-${width}`);
		await stage(page, threadId, docPath, title, width);
		// Close the comments sheet or column if it came up with the document, as a reader would before reading.
		const close = page.getByRole('button', { name: 'Close comments', exact: true });
		if (await close.count()) await close.first().click();
		await expect.poll(() => page.locator('[data-highlight-dot]').count(), { timeout: 20_000 }).toBeGreaterThan(0);
		console.log(`page build: ${await page.evaluate(() => document.querySelector('.version-overlay')?.textContent?.replace(/\s+/g, ' ').trim())}`);
		// No build strip in a frame: the stamp is read above, then hidden the way the other frame specs do.
		await page.evaluate(async () => {
			const { coreSettings } = await import('/src/lib/stores/settings.ts');
			coreSettings.update((current) => ({ ...current, features: { ...current?.features, debug_version_overlay: false } }));
		});
		await expect(page.locator('.version-overlay')).toHaveCount(0);
		// A later settings reload can bring the stamp back; keep it out of every frame.
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });

		// The editor draws a sign once its highlight is near the viewport and under load that takes a moment: wait until the shown signs and unread tints stop changing.
		const settle = async () => {
			let last = '', same = 0;
			for (let i = 0; i < 40 && same < 4; i++) {
				const now = await page.evaluate(() => `${[...document.querySelectorAll<HTMLElement>('[data-highlight-dot]')].filter((h) => getComputedStyle(h).display !== 'none').length}/${document.querySelectorAll('.cm-comment-anchor[data-unread]').length}`);
				same = now === last ? same + 1 : 0; last = now;
				await page.waitForTimeout(200);
			}
		};
		// dev's look is emulated on the same page: its word is drawn (the hosts are in the same place, only hidden in D) and no highlight carries the unread attribute. D's look is the app's own.
		const setVariant = async (variant: Variant) => {
			await page.evaluate(async (v) => {
				// Dev's look is a style override, so the editor redrawing its marks cannot undo it: the read highlight's colours on an unread one, and the word drawn by the hosts.
				document.getElementById('emu-dev')?.remove();
				if (v === 'before') {
					const st = document.createElement('style');
					st.id = 'emu-dev';
					st.textContent = `.cm-comment-anchor[data-unread] { background-color: color-mix(in srgb, var(--amber) 13%, transparent) !important; border-bottom: 2px solid color-mix(in srgb, var(--amber) 42%, transparent) !important; } .cm-md-table td.cm-comment-anchor[data-unread], .cm-md-table th.cm-comment-anchor[data-unread] { background-color: transparent !important; border-bottom-width: 1px !important; box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--amber) 42%, transparent) !important; } [data-highlight-dot] { opacity: 1 !important; }`;
					document.head.append(st);
				}
				// The sign hosts are made visible by the style override above, so a host the editor redraws is shown too.
				window.dispatchEvent(new Event('resize'));
				await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
			}, variant);
			await settle();
		};

		// Everything is measured in the page: text boxes of the document, every sign's box, and the highlight a sign belongs to.
		const measure = (quote: string) => page.evaluate(async (quote) => {
			await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const textRects: { l: number; t: number; r: number; b: number; asc: number }[] = [];
			// From a text box's top to the top of its tallest letters (b, d, f, h, k, l, t), for that text's own font.
			const ascCache = new Map<string, number>();
			const ctx = document.createElement('canvas').getContext('2d')!;
			const ascOf = (el: Element | null) => {
				const cs = getComputedStyle(el ?? content);
				const font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
				if (!ascCache.has(font)) { ctx.font = font; const m = ctx.measureText('bdfhklt'); ascCache.set(font, m.fontBoundingBoxAscent - m.actualBoundingBoxAscent); }
				return ascCache.get(font)!;
			};
			const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
			let target: DOMRect[] = [];
			let found = false;
			let anchorNode: Node | null = null;
			for (let n = walker.nextNode(); n; n = walker.nextNode()) {
				const value = n.textContent ?? '';
				if (!value.trim()) continue;
				const range = document.createRange(); range.selectNodeContents(n);
				const asc = ascOf(n.parentElement);
				for (const q of range.getClientRects()) if (q.width > 0.5) textRects.push({ l: q.left, t: q.top, r: q.right, b: q.bottom, asc });
				const at = value.indexOf(quote.slice(0, 24));
				if (at > -1 && !anchorNode) anchorNode = n;
				if (!found && at > -1 && quote.length <= 24) { const r = document.createRange(); r.setStart(n, at); r.setEnd(n, at + quote.length); target = [...r.getClientRects()]; found = true; }
			}
			// A quote split over several text nodes (nested highlights): the node holding its first word anchors the lookup.
			if (!anchorNode) { const w = quote.split(' ')[0]; const wk = document.createTreeWalker(content, NodeFilter.SHOW_TEXT); for (let n = wk.nextNode(); n; n = wk.nextNode()) if ((n.textContent ?? '').includes(w)) { anchorNode = n; break; } }
			if (!found) {
				// A highlight that spans several text nodes: the marks that paint it.
				const marks = [...content.querySelectorAll<HTMLElement>('[data-comment-new], [data-comment-ids]')].filter((el) => (el.textContent ?? '').length && quote.includes((el.textContent ?? '').trim().slice(0, 12)));
				target = marks.flatMap((m) => [...m.getClientRects()]);
			}
			// Line boxes of the editor (one per document line, and per table cell): where each line sits and how tall it is. Text nodes split when a decoration changes, so their count is not a signal.
			const signature = [...content.querySelectorAll('.cm-line, td, th')].map((el) => { const q = el.getBoundingClientRect(); return `${Math.round(q.left * 10) / 10},${Math.round(q.top * 10) / 10},${Math.round(q.width * 10) / 10},${Math.round(q.height * 10) / 10},${(el.textContent ?? '').trim().slice(0, 14)}|${getComputedStyle(el).borderBottomWidth}|${el.className.slice(0, 40)}`; }).join('|');
			const last = target.at(-1);
			type Sign = { box: number[]; mode: string; anchor: string; touchesText: boolean; overlapPx2: number; gap: number; gapGlyph: number; gapBelowHighlight: number | null; rightEdgeDelta: number | null; clearanceToNextText: number | null; inkClearanceRing: number | null; inkClearanceDot: number | null; insideClip: boolean; signsAtThisBox: number; opacity: string };
			let best: null | Sign = null;
			const scrollerRect = (content.closest('.cm-scroller') as HTMLElement).getBoundingClientRect();
			const r1 = (v: number) => Math.round(v * 10) / 10;
			const visibleBoxes: number[][] = [];
			for (const host of document.querySelectorAll<HTMLElement>('[data-highlight-dot]')) {
				if (getComputedStyle(host).display === 'none' || getComputedStyle(host).opacity === '0') continue;
				const el = [...host.querySelectorAll<HTMLElement>('.ct-new')].find((x) => x.getClientRects().length);
				if (!el) continue;
				const bb = el.getBoundingClientRect();
				visibleBoxes.push([bb.left, bb.top]);
			}
			for (const host of document.querySelectorAll<HTMLElement>('[data-highlight-dot]')) {
				if (getComputedStyle(host).display === 'none' || getComputedStyle(host).opacity === '0') continue;
				const el = [...host.querySelectorAll<HTMLElement>('.ct-new')].find((x) => x.getClientRects().length);
				if (!el || !last) continue;
				const b = el.getBoundingClientRect();
				const dx = Math.max(last.left - b.right, b.left - last.right, 0), dy = Math.max(last.top - b.bottom, b.top - last.bottom, 0);
				const gap = Math.hypot(dx, dy);
				// B's sign belongs to the highlight it sits under: another highlight's sign is not this one (it is hidden at a cut edge).
				if (host.dataset.mode === 'under' && gap > 30) continue;
				if (best && gap >= best.gap) continue;
				// B's 1px page-colour ring counts as part of the sign when it is checked against text.
				const ring = host.dataset.mode === 'under' ? 1 : 0;
				const box = { l: b.left - ring, t: b.top - ring, r: b.right + ring, b: b.bottom + ring };
				let worst = 0;
				for (const t of textRects) { const w = Math.min(box.r, t.r) - Math.max(box.l, t.l), h = Math.min(box.b, t.b) - Math.max(box.t, t.t); if (w > 0.5 && h > 0.5) worst = Math.max(worst, w * h); }
				// The highlight the placement used (its painted box, underline included), as the prototype recorded it.
				const ref = host.dataset.ref ? host.dataset.ref.split(',').map(Number) : null;
				// Next text below: nearest top edge of a text box not wholly above the sign and sharing its columns; ink clearance adds the gap down to the tallest letters.
				let clearance: number | null = null, ink: number | null = null;
				for (const t of textRects) if (t.b > b.top && t.r > box.l && t.l < box.r) { const c = t.t - box.b; if (clearance === null || c < clearance) clearance = c; if (ink === null || c + t.asc < ink) ink = c + t.asc; }
				best = { box: [b.left, b.top, b.width, b.height].map(r1), mode: host.dataset.mode ?? '', anchor: host.dataset.anchor ?? '', touchesText: worst > 0, overlapPx2: r1(worst), gap: r1(gap), gapGlyph: r1(b.top - last.bottom), gapBelowHighlight: ref ? r1(b.top - ref[3]) : null, rightEdgeDelta: ref ? r1(ref[2] - b.right) : null, clearanceToNextText: clearance === null ? null : r1(clearance), inkClearanceRing: ink === null ? null : r1(ink), inkClearanceDot: ink === null ? null : r1(ink + ring), insideClip: box.t >= Math.max(scrollerRect.top, 0) - 0.01 && box.b <= Math.min(scrollerRect.bottom, innerHeight) + 0.01, signsAtThisBox: visibleBoxes.filter((v) => Math.abs(v[0] - b.left) < 0.6 && Math.abs(v[1] - b.top) < 0.6).length, opacity: getComputedStyle(el).opacity };
			}

			// Option D: the mark is the highlight itself. Its own box, compared with every other text line, and the colours it draws.
			let tint: Record<string, unknown> | null = null;
			{
				const t0 = target[0]; const anchorEl = ((anchorNode as Node | null)?.parentElement?.closest('.cm-comment-anchor') ?? (t0 ? document.elementsFromPoint(t0.left + Math.min(6, t0.width / 2), t0.top + t0.height / 2).find((e) => e.matches('.cm-comment-anchor')) : null) ?? null) as HTMLElement | null;
				if (anchorEl) {
					const probe = document.createElement('span'); document.body.append(probe);
					const rgba = (css: string) => { probe.style.color = css; const c = getComputedStyle(probe).color; const cv = document.createElement('canvas'); cv.width = cv.height = 1; const x = cv.getContext('2d')!; x.clearRect(0, 0, 1, 1); x.fillStyle = c; x.fillRect(0, 0, 1, 1); const d = x.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
					const pageBg = rgba('var(--bg-primary)');
					const over = (c: number[]) => [0, 1, 2].map((i) => c[i] * c[3] + pageBg[i] * (1 - c[3]));
					const lum = (c: number[]) => { const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
					const contrast = (css: string) => { const a = lum(over(rgba(css))), b = lum(pageBg); return Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 10) / 10; };
					const isCell = anchorEl.matches('td, th');
					const csa = getComputedStyle(anchorEl);
					const boxRects = isCell ? [anchorEl.getBoundingClientRect()] : [...anchorEl.getClientRects()];
					const lastBox = boxRects.at(-1)!;
					let ov = 0, clr: number | null = null;
					for (const r of boxRects) for (const t of textRects) {
						const mid = (t.t + t.b) / 2;
						if (mid >= r.top && mid <= r.bottom) continue;
						const w = Math.min(r.right, t.r) - Math.max(r.left, t.l), h = Math.min(r.bottom, t.b) - Math.max(r.top, t.t);
						if (w > 0.5 && h > 0.5) ov = Math.max(ov, w * h);
					}
					for (const t of textRects) if (t.t > lastBox.top + lastBox.height / 2 && t.r > lastBox.left && t.l < lastBox.right && !anchorEl.contains(null)) { const c = t.t + t.asc - lastBox.bottom; if (clr === null || c < clr) clr = c; }
					const shadowColour = isCell ? (csa.boxShadow.match(/rgba?\([^)]*\)|color\([^)]*\)/)?.[0] ?? null) : null;
					const underlineColour = isCell ? shadowColour : csa.borderBottomColor;
					tint = {
						present: anchorEl.hasAttribute('data-unread'), isCell, lines: boxRects.length,
						areaPx2: Math.round(boxRects.reduce((a, r) => a + r.width * r.height, 0) * 10) / 10,
						underline: isCell ? csa.boxShadow : `${csa.borderBottomWidth} ${csa.borderBottomStyle} ${csa.borderBottomColor}`,
						underlineContrast: underlineColour ? contrast(underlineColour) : null, washColor: csa.backgroundColor,
						overlapPx2: Math.round(ov * 10) / 10, inkClearanceBelow: clr === null ? null : Math.round(clr * 10) / 10,
						unreadSpansHere: document.elementsFromPoint(lastBox.right - 2, lastBox.top + lastBox.height / 2).filter((e) => e.matches('.cm-comment-anchor[data-unread]')).length,
					};
					probe.remove();
				}
			}
			const union = target.length ? { l: Math.min(...target.map((q) => q.left)), t: Math.min(...target.map((q) => q.top)), r: Math.max(...target.map((q) => q.right)), b: Math.max(...target.map((q) => q.bottom)) } : null;
			const cr = content.getBoundingClientRect();
			return { found: target.length > 0, signature, sign: best, tint, union, content: { l: cr.left, r: cr.right } };
		}, quote);

		const results: Record<string, unknown>[] = [];
		const crops: { id: string; title: string; shots: Record<string, string> }[] = [];
		for (const c of CASES) {
			// Scroll the highlight to the middle of the viewport; every variant is read at this same scroll position.
			await setVariant('before');
			await page.evaluate((quote) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n.textContent ?? '').includes(quote.slice(0, 12))) { (n.parentElement as HTMLElement).scrollIntoView({ block: 'center' }); return; }
			}, c.quote);
			await page.waitForTimeout(500);
			await settle();
			// The editor draws only the lines near the viewport, so a sign exists once its highlight has been scrolled to.
			await expect.poll(async () => (await measure(c.quote)).sign !== null, { timeout: 10_000, message: `${c.id} has its sign` }).toBe(true);
			const shots: Record<string, string> = {};
			const per: Record<string, any> = {};
			for (const variant of VARS) {
				await setVariant(variant);
				const m = await measure(c.quote);
				const aria = await page.locator('body').ariaSnapshot();
				const signs = aria.split('\n').filter((l) => /img "New (reply|comment)"/.test(l)).length;
				const info = await page.evaluate(() => {
					const hosts = [...document.querySelectorAll<HTMLElement>('[data-highlight-dot]')].filter((h) => getComputedStyle(h).display !== 'none');
					return { hosts: hosts.length, modes: hosts.reduce((a: Record<string, number>, h) => { a[h.dataset.mode ?? ''] = (a[h.dataset.mode ?? ''] ?? 0) + 1; return a; }, {}), focusable: hosts.flatMap((h) => [...h.querySelectorAll('[tabindex],a,button')]).length, names: [...new Set(hosts.flatMap((h) => [...h.querySelectorAll('[role="img"]')].map((e) => e.getAttribute('aria-label'))))] };
				});
				per[variant] = { ...m, ariaSigns: signs, ...info };
				if (m.union) {
					const vw = page.viewportSize()!.width, vh = page.viewportSize()!.height;
					const x = Math.max(0, Math.floor(Math.min(m.content.l, m.union.l) - 8)), right = Math.min(vw, Math.ceil(m.content.r + 8));
					const y = Math.max(0, Math.floor(m.union.t - 38)), bottom = Math.min(vh, Math.ceil(m.union.b + 38));
					shots[variant] = (await page.screenshot({ clip: { x, y, width: Math.max(10, right - x), height: Math.max(10, bottom - y) }, scale: 'css' })).toString('base64');
				}
			}
			expect(VARS.every((v) => per[v].found), `${c.id} found`).toBeTruthy();
			results.push({ case: c.id, width, theme, ...Object.fromEntries(VARS.map((v) => [v, { sign: per[v].sign, tint: per[v].tint, ariaSigns: per[v].ariaSigns, hostsShown: per[v].hosts, modes: per[v].modes, focusable: per[v].focusable, names: per[v].names }])), textLinesMoved: VARS.map((v) => per[v].signature !== per.before.signature), movedDetail: (['tint'] as const).map((v) => { const a = (per.before.signature as string).split('|'), z = (per[v].signature as string).split('|'); return { v, n: [a.length, z.length], diff: a.map((x, i) => [i, x, z[i]]).filter(([, x, y]) => x !== y).slice(0, 4) }; }) });
			crops.push({ id: c.id, title: c.title, shots });
		}
		mkdirSync(out, { recursive: true });
		const bg = theme === 'dark' ? '#0a0a0b' : '#ffffff', fg = theme === 'dark' ? '#c8c8ce' : '#3a3a40', line = theme === 'dark' ? '#3a3a40' : '#c8c8ce';
		const labels = [LABELS.before, LABELS.tint];
		// dev on the left, B on the right, each under its own label strip: no label is ever drawn over a frame.
		// Frames show the document pane only: no sidebar, chat composer or model label around it.
		const docShot = async () => { const c = await page.evaluate(() => { const d = document.querySelector('.doc')!.getBoundingClientRect(); const l = Math.max(0, Math.floor(d.left) - 1), r = Math.min(innerWidth, Math.ceil(d.right) + 1); return { x: l, y: 0, width: r - l, height: innerHeight }; }); return page.screenshot({ clip: c }); };
		const composite = async (file: string, shots: string[], labs: string[] = labels) => {
			const sheet = await page.context().newPage();
			const dims = await Promise.all(shots.map((s) => sheet.evaluate((b64) => new Promise<number[]>((res) => { const i = new Image(); i.onload = () => res([i.width, i.height]); i.src = `data:image/png;base64,${b64}`; }), s)));
			await sheet.setViewportSize({ width: dims.reduce((a, d) => a + d[0], 0) + 16 * (shots.length + 1), height: Math.max(...dims.map((d) => d[1])) + 70 });
			await sheet.setContent(`<body style="margin:0;padding:12px;background:${bg};font:13px -apple-system,sans-serif;color:${fg};display:flex;gap:16px;align-items:flex-start">${shots.map((s, i) => `<div><div style="padding:0 0 6px;font-weight:600">${labs[i]}</div><img style="display:block;outline:1px solid ${line}" src="data:image/png;base64,${s}"></div>`).join('')}</body>`);
			await sheet.screenshot({ path: file, fullPage: true });
			await sheet.close();
		};
		// The sheet of cases: a title strip above each case, dev beside B.
		const sheet = await page.context().newPage();
		await sheet.setViewportSize({ width: width === 393 ? 860 : Math.min(2 * width + 80, 2200), height: 600 });
		await sheet.setContent(`<body style="margin:0;padding:12px;background:${bg};font:13px -apple-system,sans-serif;color:${fg}">${crops.map((c) => `<div style="margin:0 0 18px"><div style="padding:0 0 6px;font-weight:600">${c.title}</div><div style="display:flex;gap:16px;align-items:flex-start"><div><div style="padding:0 0 4px">${labels[0]}</div><img style="display:block;outline:1px solid ${line}" src="data:image/png;base64,${c.shots.before}"></div><div><div style="padding:0 0 4px">${labels[1]}</div><img style="display:block;outline:1px solid ${line}" src="data:image/png;base64,${c.shots.tint}"></div></div></div>`).join('')}</body>`);
		await sheet.waitForLoadState('load');
		await sheet.screenshot({ path: path.join(out, `doc-cases-${width}-${theme}.png`), fullPage: true });
		await sheet.close();

		// Whole-screen frames at 1x, dev beside B, at two scroll positions that show most of the highlights.
		const scrollCenter = async (quote: string) => {
			await page.evaluate((quote) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n.textContent ?? '').includes(quote.slice(0, 12))) { (n.parentElement as HTMLElement).scrollIntoView({ block: 'center' }); return; }
			}, quote);
			await page.waitForTimeout(500);
		};
		const views: Record<string, unknown> = {};
		for (const [name, quote] of [['scope', CASES[2].quote], ['table', CASES[4].quote], ['list', CASES[5].quote]] as const) {
			await setVariant('before'); await scrollCenter(quote);
			const a = (await docShot()).toString('base64');
			for (const v of ['tint'] as const) {
				await setVariant(v);
				const b = (await docShot()).toString('base64');
				await composite(path.join(out, `doc-view-${name}-${v}-${width}-${theme}.png`), [a, b], [LABELS.before, LABELS[v]]);
			}
			views[name] = true;
		}

		// A highlight's last line half cut by the bottom of the scroller, then by its top: where does each placement put its sign?
		const cuts: Record<string, unknown> = {};
		// The bottom cut uses the list item (low in the document, so it can be scrolled to the bottom edge at every width); the top cut uses the long Audience highlight.
		for (const cut of ['bottom', 'top'] as const) {
			const quote = cut === 'bottom' ? CASES[5].quote : CASES[3].quote, shots: string[] = [], per: Record<string, unknown> = {};
			// The last line of the highlight: the last text box of its closing words, with the clip box the scroller draws in.
			const lastLine = (arg: { quote: string }) => page.evaluate(({ quote }) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement, scroller = content.closest('.cm-scroller') as HTMLElement;
				const clip = scroller.getBoundingClientRect();
				const headerBottom = document.querySelector('.shell-header')?.getBoundingClientRect().bottom ?? 0;
				const top = Math.max(0, headerBottom, clip.top), bottom = Math.min(innerHeight, clip.bottom);
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				let rect: DOMRect | null = null;
				const tail = quote.slice(-10);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(tail); if (at > -1) { const r = document.createRange(); r.setStart(n, at); r.setEnd(n, at + tail.length); rect = [...r.getClientRects()].at(-1) ?? null; } }
				return { mid: rect ? rect.top + rect.height / 2 : null, top, bottom };
			}, arg);
			for (const variant of ['before', 'tint'] as const) {
				await setVariant(variant);
				await scrollCenter(quote);
				const at = await lastLine({ quote });
				expect(at.mid, `${cut} cut: highlight found`).not.toBeNull();
				const want = cut === 'bottom' ? at.bottom : at.top;
				await page.evaluate((delta) => { const s = (document.querySelector('.doc .cm-content') as HTMLElement).closest('.cm-scroller') as HTMLElement; s.scrollTop += delta; }, at.mid! - want);
				await page.waitForTimeout(500);
				const landed = await lastLine({ quote });
				// The last line must be half cut by the edge, or this frame shows nothing about the cut.
				expect(Math.abs(landed.mid! - want), `${cut} cut landed (middle of the last line at ${landed.mid}, edge ${want})`).toBeLessThan(4);
				const m = await measure(quote);
				per[variant] = { edge: want, lastLineMid: Math.round(landed.mid! * 10) / 10, sign: m.sign, tint: m.tint, union: m.union };
				shots.push((await docShot()).toString('base64'));
			}
			cuts[cut] = per;
			await composite(path.join(out, `doc-cut-${cut}-${width}-${theme}.png`), shots);
		}
		// A press on text two unread threads cover: where it lands, what opens, and what a press on a row and the way back do.
		await setVariant('tint');
		const pressAt = async (quote: string, offset: number, settleMs = 900) => {
			await scrollCenter(quote);
			const spot = await page.evaluate(({ quote, offset }) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(quote); if (at > -1) { const r = document.createRange(); r.setStart(n, at + offset); r.setEnd(n, at + offset + 2); const q = r.getBoundingClientRect(); return { x: q.left + q.width / 2, y: q.top + q.height / 2 }; } }
				return null;
			}, { quote, offset });
			expect(spot, `${quote} found`).not.toBeNull();
			await page.mouse.click(spot!.x, spot!.y);
			await page.waitForTimeout(settleMs);
		};
		const state = () => page.evaluate(() => ({
			cards: [...document.querySelectorAll<HTMLElement>('.ct')].filter((e) => e.getClientRects().length).map((e) => ({ focused: e.classList.contains('focused'), news: e.querySelectorAll('.ct-new').length, text: (e.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 60) })),
			buttons: [...document.querySelectorAll<HTMLElement>('[role="dialog"] button, .canvas-comment-drawer button, [data-adaptive-drawer] button')].filter((e) => e.getClientRects().length).map((e) => ((e.getAttribute('aria-label') ?? '') + ' | ' + (e.textContent ?? '')).replace(/\s+/g, ' ').trim().slice(0, 50)),
		}));
		const press: Record<string, unknown> = {};
		const frame = async (name: string, label: string) => composite(path.join(out, `doc-${name}-${width}-${theme}.png`), [(await docShot()).toString('base64')], [label]);
		const closeList = async () => {
			await page.keyboard.press('Escape'); await page.waitForTimeout(500);
			const left = (await state()).cards.length;
			if (left) { const close = page.getByRole('button', { name: /^Close/ }).first(); if (await close.count()) await close.click(); await page.waitForTimeout(500); }
		};
		// The seen-stamp is held back for these frames so each row's New sign shows as the list arrives; in use it clears once the row has been on screen, as on every card.
		await page.route('**/comments/*/viewed', (route) => route.abort());
		// A press on the text two threads end at: the list, as it arrives (each row's own sign) and after the open has marked them seen.
		await pressAt('escalation', 2, 200);
		press.list = await state();
		await frame('list', 'D: a press on text two threads cover opens the list, each thread with its own sign');
		await page.waitForTimeout(1200);
		press.listSettled = await state();
		// A press on a row: that thread takes focus and its text scrolls into view; the list stays.
		await page.locator('.ct').filter({ hasText: 'inside a list item' }).first().click({ position: { x: 40, y: 18 } });
		await page.waitForTimeout(700);
		press.row = await state();
		await frame('list-row', 'D: pressing a row focuses that thread, the list stays');
		await closeList();
		press.afterClose = await state();
		// The same in a table cell.
		await pressAt('Marcus', 2, 200);
		press.table = await state();
		await frame('list-table', 'D: a press in a table cell two threads cover opens the list');
		await closeList();
		// One thread only: it opens alone.
		await pressAt('rollback owner', 3, 700);
		press.single = await state();
		await frame('list-single', 'D: a press on text one thread covers opens that thread');
		writeFileSync(path.join(out, `doc-${width}-${theme}.json`), JSON.stringify({ cases: results, cuts, views, press }, null, 1));
	});
}
