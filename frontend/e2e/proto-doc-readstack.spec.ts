// PROTOTYPE (r7): a stack of read threads must paint as one wash, no louder than one read thread, and an unread stack stays the strongest mark. Document canvas: a solo read, a read stack of 7 and of 3, an unread stack of 7 and a solo unread, plus table cells, in the middle of a long document. R7_MODE=before deletes r7's nested-read rule from the live stylesheet, which is exactly r6's paint, so one fixture gives before and after.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from './support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = process.env.R7_OUT ?? '/tmp/b827-r7doc';
const TH = (process.env.TH ?? 'light') as 'light' | 'dark';
const MODE = process.env.R7_MODE ?? 'after';

const filler = (n: number) => Array.from({ length: n }, (_, i) => `Filler paragraph ${i + 1} so the stacks sit in the middle of a long page, with enough words to wrap on a phone and keep every highlight away from the top and the bottom of the document.`).flatMap((line) => [line, '']);
type Case = { id: string; phrase: string; count: number; unread: number[] };
const CASES: Case[] = [
	{ id: 'solo-read', phrase: 'solo read phrase', count: 1, unread: [] },
	{ id: 'read7', phrase: 'read seven phrase', count: 7, unread: [] },
	{ id: 'unread7', phrase: 'unread seven phrase', count: 7, unread: [0] },
	{ id: 'read3', phrase: 'read three phrase', count: 3, unread: [] },
	{ id: 'unread-solo', phrase: 'unread solo phrase', count: 1, unread: [0] },
];
const CELLS = [{ id: 'cell-solo-read', phrase: 'Sam Ortiz', count: 1 }, { id: 'cell-read3', phrase: 'Priya Raman', count: 3 }];
const DOC = [
	'# Read stacks', '', ...filler(5),
	...CASES.flatMap((c) => [`The ${c.id} sample is the ${c.phrase} in this sentence, and it carries on for a while after that.`, '']),
	'| Milestone | Owner |', '| --- | --- |', '| Design freeze | Priya Raman |', '| Beta | Sam Ortiz |', '', ...filler(5),
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
	const docPath = path.join(workspace!, folder, 'readstacks.md');
	mkdirSync(path.dirname(docPath), { recursive: true });
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const unreadRoots: string[] = [];
	for (const c of [...CASES, ...CELLS.map((cell) => ({ ...cell, unread: [] as number[] }))]) {
		const from = DOC.indexOf(c.phrase);
		expect(from, c.id).toBeGreaterThan(-1);
		for (let j = 0; j < c.count; j++) {
			const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: `Question ${j + 1} about ${c.id}`, anchor_text: c.phrase, from_off: from, to_off: from + c.phrase.length, delivery_policy: 'immediate' } });
			expect(root.ok(), await root.text()).toBeTruthy();
			if (c.unread.includes(j)) unreadRoots.push((await root.json()).comment.id as string);
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
		threads: { [threadId]: { tabs: [{ instanceId: 'proto-sign', artifactId: null, path: docPath, title: 'readstacks.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'proto-sign', visible: true, activeSurface: 'artifact' } }, splitWidth: 420,
	})), { threadId, docPath });
	await loginByUrl(page);
	if (width <= 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, threadTitle).click();
	await expect(page.locator('.doc .cm-content')).toContainText('Read stacks');
}

test.use({ hasTouch: true });

for (const width of [393, 1440]) {
	test(`read stack ${TH} ${MODE} ${width}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(300_000);
		const tag = `${TH}-${MODE}-${width}`;
		await page.setViewportSize({ width, height: 1900 });
		await page.emulateMedia({ colorScheme: TH });
		const threadId = await createSession(request, `Read stacks ${tag}`);
		const { docPath } = await seed(request, threadId, `readstack-${tag}-${Date.now().toString(36)}`);
		await stage(page, threadId, docPath, `Read stacks ${tag}`, width);
		const close = page.getByRole('button', { name: 'Close comments', exact: true });
		if (await close.count()) await close.first().click();
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
		await expect.poll(() => page.locator('.cm-comment-anchor[data-unread]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
		await page.waitForTimeout(1500);
		await page.route('**/comments/*/viewed', (route) => route.abort());
		// "before" is r6's paint: the page without r7's nested-read rule.
		const removed = await page.evaluate((mode) => {
			if (mode !== 'before') return -1;
			let n = 0;
			for (const sheet of [...document.styleSheets]) {
				let rules: CSSRuleList;
				try { rules = sheet.cssRules; } catch { continue; }
				for (let i = rules.length - 1; i >= 0; i--) {
					const rule = rules[i];
					if (rule instanceof CSSStyleRule && /^\.cm-comment-anchor:not\(\[data-unread\]\) \.cm-comment-anchor:not\(\[data-unread\]/.test(rule.selectorText)) { sheet.deleteRule(i); n++; }
				}
			}
			return n;
		}, MODE);
		if (MODE === 'before') expect(removed).toBeGreaterThan(0);
		if (MODE !== 'before') {
			const present = await page.evaluate(() => [...document.styleSheets].some((sheet) => { try { return [...sheet.cssRules].some((rule) => rule instanceof CSSStyleRule && /^\.cm-comment-anchor:not\(\[data-unread\]\) \.cm-comment-anchor:not\(\[data-unread\]/.test(rule.selectorText)); } catch { return false; } }));
			expect(present, 'r7 rule is in the live stylesheet').toBe(true);
		}
		await page.waitForTimeout(500);
		mkdirSync(out, { recursive: true });

		const targets = [...CASES.map((c) => ({ id: c.id, phrase: c.phrase })), ...CELLS.map((c) => ({ id: c.id, phrase: c.phrase }))];
		const buf = await page.screenshot();
		const b64 = buf.toString('base64');
		const result = await page.evaluate(async ({ targets, b64 }) => {
			const img = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
			const cv = document.createElement('canvas'); cv.width = img.width; cv.height = img.height;
			const cx = cv.getContext('2d')!; cx.drawImage(img, 0, 0);
			const px = (x: number, y: number) => Array.from(cx.getImageData(Math.round(x), Math.round(y), 1, 1).data).slice(0, 3);
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const res: Record<string, unknown> = {};
			let top = Infinity, bottom = 0;
			for (const { id, phrase } of targets) {
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				let node: Text | null = null, at = -1;
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const i = (n.textContent ?? '').indexOf(phrase); if (i > -1) { node = n as Text; at = i; break; } }
				if (!node) { res[id] = { error: 'phrase not drawn' }; continue; }
				const host = node.parentElement as HTMLElement;
				const chain: HTMLElement[] = [];
				for (let e: HTMLElement | null = host; e && e !== content; e = e.parentElement) if (e.classList.contains('cm-comment-anchor')) chain.push(e);
				chain.reverse(); // outermost first
				const rg = document.createRange(); rg.setStart(node, at); rg.setEnd(node, at + phrase.length);
				const rc = [...rg.getClientRects()].pop() ?? rg.getBoundingClientRect();
				top = Math.min(top, rc.top); bottom = Math.max(bottom, rc.bottom);
				const cellEl = host.closest('td, th') as HTMLElement | null;
				const paint = (e: HTMLElement) => { const s = getComputedStyle(e); return { unread: e.hasAttribute('data-unread'), tag: e.tagName.toLowerCase(), bg: s.backgroundColor, borderStyle: s.borderBottomStyle, borderWidth: s.borderBottomWidth, borderColor: s.borderBottomColor, boxShadow: s.boxShadow === 'none' ? 'none' : s.boxShadow.slice(0, 80) }; };
				const x = rc.left + rc.width * 0.5;
				res[id] = { depth: chain.length, chain: chain.map((e) => (e.hasAttribute('data-unread') ? 'U' : 'r')).join(''), layers: chain.map(paint), cell: cellEl ? paint(cellEl) : null, washPx: px(rc.left + rc.width * 0.5, rc.top + 3), bandBelow: [0, 1, 2, 3].map((d) => px(x, Math.floor(rc.bottom) - 1 + d)), box: { left: Math.round(rc.left), top: Math.round(rc.top), right: Math.round(rc.right), bottom: Math.round(rc.bottom) } };
			}
			res.__region = { top: Math.round(top), bottom: Math.round(bottom) };
			return res;
		}, { targets, b64 });
		const region = result.__region as { top: number; bottom: number };
		await page.screenshot({ path: path.join(out, `readstack-${tag}.png`), clip: { x: 0, y: Math.max(0, region.top - 60), width, height: Math.min(1900, region.bottom - region.top + 120) } });
		writeFileSync(path.join(out, `readstack-${tag}.json`), JSON.stringify(result, null, 1));
		const brief = Object.fromEntries(Object.entries(result).filter(([k]) => k !== '__region').map(([k, v]) => { const r = v as { chain: string; layers: Array<{ bg: string; borderStyle: string }>; washPx: number[] }; return [k, `${r.chain} wash=${r.washPx.join(',')} layers=${r.layers.map((l) => `${l.bg}/${l.borderStyle}`).join(' | ')}`]; }));
		console.log(`READSTACK ${tag} ${JSON.stringify(brief)}`);
	});
}
