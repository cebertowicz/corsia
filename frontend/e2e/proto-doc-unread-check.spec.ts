// PROTOTYPE: the unread look of every unread highlight in the document canvas, read after each interaction (open, close, type, resize, scroll).
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from './support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(process.env.CORSIA_E2E_WORKSPACE ?? '/tmp/b827-out', 'unreadcheck');

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
	if (width === 393 || wide) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, threadTitle).click();
	await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
}


test.use({ hasTouch: true });
const RUN = Date.now().toString(36);
const MANY = 'onboarding flow';
const LONG_NAMES = ['Alexandrina Wolfeschlegelsteinhausenberger', 'Bartholomew Featherstonehaugh-Cholmondeley', 'Christopher Vandenberghe-Oosterhuis'];

for (const { width, touch } of [{ width: 393, touch: true }, { width: 900, touch: false }, { width: 1440, touch: false }]) {
	test(`unread check ${width}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(300_000);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, `Q4 launch plan unread ${width}`);
		const { docPath, artifactId } = await seed(request, threadId, `unread-${RUN}-${width}`);
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
		await stage(page, threadId, docPath, `Q4 launch plan unread ${width}`, width, false);
		const close = page.getByRole('button', { name: 'Close comments', exact: true });
		if (await close.count()) await close.first().click();
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
		await expect.poll(() => page.locator('[data-comment-new]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
		await page.waitForTimeout(1500);
		await page.route('**/comments/*/viewed', (route) => route.abort());
		mkdirSync(out, { recursive: true });

		// What is drawn now: every rendered unread highlight (an editor mark or a painted cell), and whether it carries the unread look; the header's count.
		const state = () => page.evaluate(() => {
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const marks = [...content.querySelectorAll<HTMLElement>('[data-comment-new]')];
			const ids = new Set(marks.map((m) => m.dataset.commentNew));
			const tinted = new Set(marks.filter((m) => m.hasAttribute('data-unread') && getComputedStyle(m).borderBottomStyle !== 'none' && getComputedStyle(m).backgroundColor !== 'rgba(0, 0, 0, 0)').map((m) => m.dataset.commentNew));
			const cellIds = [...content.querySelectorAll<HTMLElement>('td[data-comment-ids], th[data-comment-ids], .cm-comment-wrap[data-comment-ids]')].filter((el) => el.hasAttribute('data-unread')).length;
			const header = (document.body.textContent ?? '').match(/(\d+) unread/);
			const hosts = document.querySelectorAll('[data-highlight-dot]').length;
			return { renderedUnread: ids.size, tinted: tinted.size, cellsOrWrapsTinted: cellIds, hosts, headerUnread: header ? Number(header[1]) : null };
		});
		const centre = async (quote: string) => {
			for (let i = 0; i < 40; i++) {
				const done = await page.evaluate((quote) => {
					const content = document.querySelector('.doc .cm-content') as HTMLElement;
					const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
					for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n.textContent ?? '').includes(quote.split(' ')[0])) { (n.parentElement as HTMLElement).scrollIntoView({ block: 'center' }); return true; }
					let scroller: HTMLElement | null = null;
					for (let e: HTMLElement | null = content; e; e = e.parentElement) { const o = getComputedStyle(e).overflowY; if ((o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1) { scroller = e; break; } }
					if (scroller) scroller.scrollTop = scroller.scrollTop + scroller.clientHeight * 0.6 > scroller.scrollHeight ? 0 : scroller.scrollTop + scroller.clientHeight * 0.6;
					return false;
				}, quote);
				if (done) break;
				await page.waitForTimeout(250);
			}
			await page.waitForTimeout(600);
		};
		const spotOf = async (quote: string, offset: number) => {
			await centre(quote);
			return page.evaluate(({ quote, offset }) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(quote.split(' ')[0]); if (at > -1) { const r = document.createRange(); r.setStart(n, at + offset); r.setEnd(n, at + offset + 2); const q = r.getBoundingClientRect(); return { x: q.left + q.width / 2, y: q.top + q.height / 2 }; } }
				return null;
			}, { quote, offset });
		};
		const press = async (quote: string, offset: number) => {
			const spot = await spotOf(quote, offset);
			expect(spot, `${quote} found`).not.toBeNull();
			if (touch) await page.touchscreen.tap(spot!.x, spot!.y); else await page.mouse.click(spot!.x, spot!.y);
			await page.waitForTimeout(1200);
		};
		const closeAll = async () => {
			await page.keyboard.press('Escape'); await page.waitForTimeout(500);
			const btn = page.locator('[data-adaptive-drawer]').getByRole('button', { name: 'Close', exact: true }).first();
			if (await btn.count() && await btn.isVisible()) { await btn.click(); await page.waitForTimeout(600); }
		};
		const clip = async (name: string) => {
			const box = await page.evaluate(() => { const d = document.querySelector('.doc')?.getBoundingClientRect(); const left = d ? Math.max(0, Math.floor(d.left) - 1) : 0, right = d ? Math.min(innerWidth, Math.ceil(d.right) + 1) : innerWidth; return { x: left, y: 0, width: right - left, height: innerHeight }; });
			await page.screenshot({ path: path.join(out, `unread-${name}-${width}.png`), clip: box });
		};
		const steps: Record<string, unknown> = {};
		const record = async (name: string) => { steps[name] = await state(); };
		await centre('Cut'); await record('start');
		await clip('start');
		// Open one thread (a list, then a single) and close it.
		await press('escalation', 2); await record('afterOpenList'); await clip('open-list'); await closeAll(); await record('afterCloseList');
		await press('rollback owner', 3); await record('afterOpenSingle'); await closeAll(); await record('afterCloseSingle');
		await press('Marcus', 2); await record('afterOpenTable'); await closeAll(); await record('afterCloseTable');
		// Three threads on one phrase: how much pressed text stays above the sheet or popover.
		await press(MANY, 3);
		steps.manyGap = await page.evaluate(() => {
			const content = document.querySelector('.doc .cm-content') as HTMLElement;
			const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
			let bottom = -1, top = -1, lineH = 0;
			for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf('onboarding'); if (at > -1) { const r = document.createRange(); r.setStart(n, at); r.setEnd(n, at + 10); const q = r.getBoundingClientRect(); bottom = q.bottom; top = q.top; lineH = q.height; break; } }
			const panel = [...document.querySelectorAll('[data-adaptive-drawer] .drawer-panel, [data-popover]')].find((e) => e.getClientRects().length > 0) as HTMLElement | undefined;
			const pt = panel ? panel.getBoundingClientRect().top : null;
			const above = pt === null ? null : pt - bottom;
			const textAboveSheet = pt === null ? null : Math.max(0, Math.min(top, pt) - (document.querySelector('.doc')?.getBoundingClientRect().top ?? 0));
			return { pressedBottom: Math.round(bottom), pressedTop: Math.round(top), lineHeight: Math.round(lineH), panelTop: pt === null ? null : Math.round(pt), clearBelowPressed: above === null ? null : Math.round(above), panel: panel ? (panel.getAttribute('aria-label') ?? panel.className) : null, textAboveSheetPx: textAboveSheet === null ? null : Math.round(textAboveSheet) };
		});
		await clip('open-many'); await record('afterOpenMany'); await closeAll();
		// Resolve one thread from the single sheet and read the marks again.
		await press('rollback owner', 3);
		const resolve = page.locator('[data-adaptive-drawer], [data-popover]').getByRole('button', { name: /^Resolve/ }).first();
		if (await resolve.count()) { await resolve.click(); await page.waitForTimeout(1200); }
		await closeAll(); await record('afterResolve'); await clip('after-resolve');
		// Type into a highlight: press it, leave the thread, type where the reader tapped.
		await press('Cut', 1); await closeAll();
		await record('beforeTyping');
		await page.keyboard.type('QQ');
		await page.waitForTimeout(900);
		await record('afterTyping');
		await clip('typing');
		// Narrow or widen the window, then scroll away and back.
		await page.setViewportSize({ width: width === 393 ? 420 : width - 60, height: 800 }); await page.waitForTimeout(900); await record('afterResize');
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 }); await page.waitForTimeout(900);
		await centre('Audience'); await centre('Cut'); await record('afterScroll');
		// Every unread thread seen while walking the whole document, against the header.
		const seen = new Set<string>();
		for (let i = 0; i < 16; i++) {
			const ids = await page.evaluate(() => { const c = document.querySelector('.doc .cm-content') as HTMLElement; return [...c.querySelectorAll<HTMLElement>('[data-comment-new]')].filter((m) => m.hasAttribute('data-unread')).map((m) => m.dataset.commentNew ?? ''); });
			for (const id of ids) seen.add(id);
			await page.evaluate(() => { const c = document.querySelector('.doc .cm-content') as HTMLElement; let s: HTMLElement | null = null; for (let e: HTMLElement | null = c; e; e = e.parentElement) { const o = getComputedStyle(e).overflowY; if ((o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1) { s = e; break; } } if (s) s.scrollTop += s.clientHeight * 0.5; });
			await page.waitForTimeout(350);
		}
		steps.walkedUnreadThreads = seen.size;
		steps.headerAtEnd = (await state()).headerUnread;
		writeFileSync(path.join(out, `unread-${width}.json`), JSON.stringify(steps, null, 1));
		console.log('CHECK UNREAD ' + width + ' ' + JSON.stringify(steps));
		// The drawn marks equal what is rendered after every step.
		for (const [name, s] of Object.entries(steps)) {
			if (typeof s !== 'object' || !s) continue;
			const st = s as { renderedUnread: number; tinted: number; hosts: number };
			expect(st.tinted, `${name}: tinted equals rendered unread highlights`).toBe(st.renderedUnread);
		}
	});
}
