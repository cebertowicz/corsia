// PROTOTYPE (uncommitted): r6 evidence on the document canvas. Stacks of 2 to 7 comments on one phrase with mixed read and unread: is the unread underline the one painted (pixels), and does a press on the seven-comment stack keep its text above the sheet.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from './support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = process.env.R6_OUT ?? '/tmp/b827-r6doc';
const TH = (process.env.TH ?? 'light') as 'light' | 'dark';
const OPT = process.env.OPT ?? 'cur';

type Stack = { id: string; phrase: string; count: number; unread: number[] };
const STACKS: Stack[] = [];
for (let depth = 2; depth <= 7; depth++) {
	STACKS.push({ id: `s${depth}-first`, phrase: `window ${depth}f`, count: depth, unread: [0] });
	STACKS.push({ id: `s${depth}-last`, phrase: `window ${depth}l`, count: depth, unread: [depth - 1] });
}
STACKS.push({ id: 's7-mid', phrase: 'window 7m', count: 7, unread: [2, 4] });
STACKS.push({ id: 's7-allread', phrase: 'window 7r', count: 7, unread: [] });
const PRESS = { id: 'press7', phrase: 'every channel on the same day', count: 7, unread: [1, 4] };
const ALL = [...STACKS, PRESS];
const DOC = [
	'# Stacks', '',
	...STACKS.flatMap((s) => [`Stack of ${s.count} on the rollout ${s.phrase}, ${s.id}, before Friday.`, '']),
	'Land the announcement with ' + PRESS.phrase + ', and tell each partner team before the public post goes out so nobody hears it second hand.', '',
	'| Milestone | Owner |', '| --- | --- |', '| Design freeze | Priya Raman |', '',
].join('\n');

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
	const docPath = path.join(workspace!, folder, 'stacks.md');
	mkdirSync(path.dirname(docPath), { recursive: true });
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const unreadRoots: string[] = [];
	for (const s of ALL) {
		const from = DOC.indexOf(s.phrase);
		expect(from, s.id).toBeGreaterThan(-1);
		expect(DOC.indexOf(s.phrase, from + 1), `${s.id} unique`).toBe(-1);
		for (let j = 0; j < s.count; j++) {
			const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: `Question ${j + 1} about ${s.id}`, anchor_text: s.phrase, from_off: from, to_off: from + s.phrase.length, delivery_policy: 'immediate' } });
			expect(root.ok(), await root.text()).toBeTruthy();
			if (s.unread.includes(j)) unreadRoots.push((await root.json()).comment.id as string);
		}
	}
	// A table cell with three comments, the last unread.
	{
		const phrase = 'Priya Raman', from = DOC.indexOf(phrase);
		for (let j = 0; j < 3; j++) {
			const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: `Cell question ${j + 1}`, anchor_text: phrase, from_off: from, to_off: from + phrase.length, delivery_policy: 'immediate' } });
			expect(root.ok(), await root.text()).toBeTruthy();
			if (j === 2) unreadRoots.push((await root.json()).comment.id as string);
		}
	}
	sqlite([
		["UPDATE doc_comments SET author='Jordan Lee' WHERE author='e2e'", []],
		...unreadRoots.map((id, i): [string, Array<string | number>] => ["INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Maya Chen', 'agent', ?, ?, strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'))", [artifactId, id, threadId, `Answer ${i + 1}: yes, keep it.`]]),
	]);
	return { docPath, artifactId };
}

async function stage(page: Page, threadId: string, docPath: string, threadTitle: string, width: number) {
	await page.addInitScript(({ threadId, docPath }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
		threads: { [threadId]: { tabs: [{ instanceId: 'proto-sign', artifactId: null, path: docPath, title: 'stacks.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'proto-sign', visible: true, activeSurface: 'artifact' } }, splitWidth: 420,
	})), { threadId, docPath });
	await loginByUrl(page);
	if (width <= 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, threadTitle).click();
	await expect(page.locator('.doc .cm-content')).toContainText('Stacks');
}

test.use({ hasTouch: true });

for (const width of [320, 393, 900, 1440]) {
	test(`stack doc ${TH} ${OPT} ${width}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(300_000);
		const tag = `${TH}-${OPT}-${width}`;
		await page.setViewportSize({ width, height: width === 320 ? 3000 : 2400 });
		await page.emulateMedia({ colorScheme: TH });
		const threadId = await createSession(request, `Stacks ${tag}`);
		const { docPath } = await seed(request, threadId, `stack-${tag}-${Date.now().toString(36)}`);
		await stage(page, threadId, docPath, `Stacks ${tag}`, width);
		const close = page.getByRole('button', { name: 'Close comments', exact: true });
		if (await close.count()) await close.first().click();
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' + (OPT === 'opt1' ? ' .cm-comment-anchor[data-unread] { border-bottom-width: 2px !important; }' : '') });
		await expect.poll(() => page.locator('.cm-comment-anchor[data-unread]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
		await page.waitForTimeout(1500);
		await page.route('**/comments/*/viewed', (route) => route.abort());
		mkdirSync(out, { recursive: true });
		const result: Record<string, unknown> = {};

		// 1. Every stack's underline, by pixels.
		const buf = await page.screenshot();
		writeFileSync(path.join(out, `dstack-${tag}.png`), buf);
		result.stacks = await page.evaluate(async ({ phrases, b64 }) => {
			const img = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
			const cv = document.createElement('canvas'); cv.width = img.width; cv.height = img.height;
			const cx = cv.getContext('2d')!; cx.drawImage(img, 0, 0);
			const px = (x: number, y: number) => Array.from(cx.getImageData(Math.round(x), Math.round(y), 1, 1).data).slice(0, 3);
			const probe = document.createElement('span'); document.body.append(probe);
			const rgb = (css: string) => { probe.style.color = css; return getComputedStyle(probe).color.match(/[\d.]+/g)!.map(Number).slice(0, 3); };
			const dist = (a: number[], b: number[]) => Math.max(...a.map((v, i) => Math.abs(v - b[i])));
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const res: Record<string, unknown> = {};
			for (const [id, phrase] of phrases) {
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				let node: Text | null = null, at = -1;
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const i = (n.textContent ?? '').indexOf(phrase); if (i > -1) { node = n as Text; at = i; break; } }
				if (!node) { res[id] = { error: 'phrase not drawn' }; continue; }
				const host = node.parentElement as HTMLElement;
				// The nest of highlight wraps from the text outwards.
				const chain: HTMLElement[] = [];
				for (let e: HTMLElement | null = host; e && e !== content; e = e.parentElement) if (e.classList.contains('cm-comment-anchor')) chain.push(e);
				const letters = chain.reverse().map((s) => (s.hasAttribute('data-unread') ? 'U' : 'r')).join('');
				const unreadSpan = chain.find((s) => s.hasAttribute('data-unread'));
				const rg = document.createRange(); rg.setStart(node, at); rg.setEnd(node, at + phrase.length);
				const rcs = [...rg.getClientRects()]; const rc = rcs[rcs.length - 1] ?? rg.getBoundingClientRect(); // a phrase that wraps: its last line
				const wantLine = unreadSpan ? rgb(getComputedStyle(unreadSpan).borderBottomColor) : null;
				const x = rc.left + rc.width * 0.5;
				const band: string[] = []; let painted = 0;
				for (let y = Math.floor(rc.bottom) - 4; y <= Math.ceil(rc.bottom) + 4; y++) { const c = px(x, y); band.push(`${y - Math.floor(rc.bottom)}:${c.join(',')}`); if (wantLine && dist(c, wantLine) <= 14) painted++; }
				res[id] = { chain: letters, depth: chain.length, anyUnread: !!unreadSpan, wantWidth: unreadSpan ? getComputedStyle(unreadSpan).borderBottomWidth : null, wantLine, lineRowsPainted: painted, wash: px(rc.left + rc.width * 0.5, rc.top + 3), band: band.join(' ') };
			}
			probe.remove();
			return res;
		}, { phrases: ALL.map((s) => [s.id, s.phrase] as [string, string]), b64: buf.toString('base64') });

		// 2. Table cell with three comments, one unread: computed style of its wraps.
		result.cell = await page.evaluate(() => {
			const cells = [...document.querySelectorAll<HTMLElement>('.doc .cm-md-table td, .doc .cm-md-table th')].filter((c) => (c.textContent ?? '').includes('Priya'));
			const td = cells[0];
			const anchors = td ? [td, ...td.querySelectorAll<HTMLElement>('.cm-comment-anchor')] : [];
			return anchors.map((a) => { const s = getComputedStyle(a); return { tag: a.tagName, unread: a.hasAttribute('data-unread'), bg: s.backgroundColor, border: `${s.borderBottomWidth} ${s.borderBottomStyle}`, shadow: s.boxShadow.slice(0, 80) }; });
		});

		// 3. The seven-comment press: where the text sits against the open sheet or popover, per frame.
		await page.setViewportSize({ width, height: width === 393 ? 852 : width === 320 ? 568 : 900 });
		await page.waitForTimeout(800);
		const centre = async () => {
			for (let i = 0; i < 40; i++) {
				const done = await page.evaluate((phrase) => {
					const content = document.querySelector('.doc .cm-content') as HTMLElement;
					const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
					for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n.textContent ?? '').includes(phrase)) { (n.parentElement as HTMLElement).scrollIntoView({ block: 'center' }); return true; }
					let scroller: HTMLElement | null = null;
					for (let e: HTMLElement | null = content; e; e = e.parentElement) { const o = getComputedStyle(e).overflowY; if ((o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1) { scroller = e; break; } }
					if (scroller) scroller.scrollTop = scroller.scrollTop + scroller.clientHeight * 0.6 > scroller.scrollHeight ? 0 : scroller.scrollTop + scroller.clientHeight * 0.6;
					return false;
				}, PRESS.phrase);
				if (done) break;
				await page.waitForTimeout(250);
			}
			await page.waitForTimeout(700);
		};
		await centre();
		const spot = await page.evaluate((phrase) => {
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
			for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(phrase); if (at > -1) { const rg = document.createRange(); rg.setStart(n, at + 'every '.length + 2); rg.setEnd(n, at + 'every '.length + 3); const q = rg.getBoundingClientRect(); return { x: q.left + q.width / 2, y: q.top + q.height / 2 }; } }
			return null;
		}, PRESS.phrase);
		expect(spot).not.toBeNull();
		await page.evaluate((phrase) => {
			(window as unknown as { __s: unknown[] }).__s = []; const t0 = performance.now();
			const tick = () => {
				const vis = (e: Element) => e.getClientRects().length > 0;
				const panel = [...document.querySelectorAll<HTMLElement>('[data-adaptive-drawer] .drawer-panel, [data-popover]')].find(vis);
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const rs = [...content.querySelectorAll<HTMLElement>('.cm-comment-anchor')].filter((e) => (e.textContent ?? '').includes('channel')).flatMap((e) => [...e.getClientRects()]);
				const top = rs.length ? Math.min(...rs.map((q) => q.top)) : null, bottom = rs.length ? Math.max(...rs.map((q) => q.bottom)) : null;
				(window as unknown as { __s: unknown[] }).__s.push({ t: Math.round(performance.now() - t0), sheetTop: panel ? Math.round(panel.getBoundingClientRect().top) : null, textTop: top === null ? null : Math.round(top), textBottom: bottom === null ? null : Math.round(bottom) });
				if (performance.now() - t0 < 2200) requestAnimationFrame(tick);
			};
			requestAnimationFrame(tick); void phrase;
		}, PRESS.phrase);
		if (width >= 900) await page.mouse.click(spot!.x, spot!.y); else await page.touchscreen.tap(spot!.x, spot!.y);
		await page.waitForTimeout(2600);
		result.pressSamples = await page.evaluate(() => {
			const s = (window as unknown as { __s: Array<{ sheetTop: number | null; textTop: number | null; textBottom: number | null }> }).__s;
			const withSheet = s.filter((q) => q.sheetTop !== null && q.textBottom !== null);
			return { frames: s.length, sheetTopSeen: [...new Set(withSheet.map((q) => q.sheetTop))].slice(0, 40), textBottomSeen: [...new Set(withSheet.map((q) => q.textBottom))].slice(0, 40), worstGapAfterFirstSettled: withSheet.length > 10 ? Math.min(...withSheet.slice(10).map((q) => (q.sheetTop as number) - (q.textBottom as number))) : null };
		});
		result.press = await page.evaluate(() => {
			const vis = (e: Element) => e.getClientRects().length > 0;
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const rs = [...content.querySelectorAll<HTMLElement>('.cm-comment-anchor')].filter((e) => (e.textContent ?? '').includes('channel')).flatMap((e) => [...e.getClientRects()]);
			const top = Math.min(...rs.map((q) => q.top)), bottom = Math.max(...rs.map((q) => q.bottom)), left = Math.min(...rs.map((q) => q.left)), right = Math.max(...rs.map((q) => q.right));
			const sheetEl = [...document.querySelectorAll<HTMLElement>('[data-adaptive-drawer] .drawer-panel')].find(vis);
			const popEl = [...document.querySelectorAll<HTMLElement>('[data-popover]')].find(vis);
			const marginEl = document.querySelector<HTMLElement>('.canvas-comment-drawer.margin.on-canvas');
			const el = sheetEl ?? popEl ?? marginEl ?? null;
			const s = el?.getBoundingClientRect() ?? null;
			const overlap = s ? Math.max(0, Math.min(right, s.right) - Math.max(left, s.left)) * Math.max(0, Math.min(bottom, s.bottom) - Math.max(top, s.top)) : null;
			return { surface: sheetEl ? 'sheet' : popEl ? 'popover' : marginEl ? 'margin' : null, innerH: innerHeight, textTop: Math.round(top), textBottom: Math.round(bottom), sheetTop: s ? Math.round(s.top) : null, sheetHeight: s ? Math.round(s.height) : null, gapTextToSurface: s ? Math.round(s.top - bottom) : null, textVisibleAbove: s ? top >= 0 && bottom <= s.top : null, overlapPx2: overlap === null ? null : Math.round(overlap) };
		});
		await page.screenshot({ path: path.join(out, `dpress-${tag}.png`) });
		writeFileSync(path.join(out, `dstack-${tag}.json`), JSON.stringify(result, null, 1));
		const stacks = result.stacks as Record<string, { chain?: string; lineRowsPainted?: number }>;
		console.log(`STACKDOC ${tag} ${JSON.stringify({ press: result.press, worst: (result.pressSamples as { worstGapAfterFirstSettled: number | null }).worstGapAfterFirstSettled, stacks: Object.fromEntries(Object.entries(stacks).map(([k, v]) => [k, `${v.chain}:${v.lineRowsPainted}`])), cell: result.cell })}`);
	});
}
