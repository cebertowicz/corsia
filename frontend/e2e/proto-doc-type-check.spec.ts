// PROTOTYPE: where typing lands after a press on commented text in the document canvas, recorded the same way on origin/dev and on D so the two can be compared.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from './support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(process.env.CORSIA_E2E_WORKSPACE ?? '/tmp/b827-out', 'typecheck');

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
	{ id: 'table-cell-single', title: 'One thread in a table cell', quote: 'Priya Raman' },
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
const LABEL = process.env.FOCUS_LABEL ?? 'D';
const RUN = Date.now().toString(36);
const MANY = 'onboarding flow';
const LONG_NAMES = ['Alexandrina Wolfeschlegelsteinhausenberger', 'Bartholomew Featherstonehaugh-Cholmondeley', 'Christopher Vandenberghe-Oosterhuis'];

for (const { width, touch } of [{ width: 393, touch: true }, { width: 900, touch: false }, { width: 1440, touch: false }]) {
	test(`type check ${LABEL} ${width}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(240_000);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, `Q4 launch plan focus ${LABEL}-${width}`);
		const { docPath, artifactId } = await seed(request, threadId, `focus-${RUN}-${LABEL}-${width}`);
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
		await stage(page, threadId, docPath, `Q4 launch plan focus ${LABEL}-${width}`, width, false);
		const close = page.getByRole('button', { name: 'Close comments', exact: true });
		if (await close.count()) await close.first().click();
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
		await expect.poll(() => page.locator('[data-comment-new]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
		await page.waitForTimeout(1500);
		await page.route('**/comments/*/viewed', (route) => route.abort());
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
			await page.waitForTimeout(500);
		};
		const press = async (quote: string, offset: number) => {
			await centre(quote);
			const spot = await page.evaluate(({ quote, offset }) => {
				const content = document.querySelector('.doc .cm-content') as HTMLElement;
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const at = (n.textContent ?? '').indexOf(quote.split(' ')[0]); if (at > -1) { const r = document.createRange(); r.setStart(n, at + offset); r.setEnd(n, at + offset + 2); const q = r.getBoundingClientRect(); return { x: q.left + q.width / 2, y: q.top + q.height / 2 }; } }
				return null;
			}, { quote, offset });
			expect(spot, `${quote} found`).not.toBeNull();
			if (touch) await page.touchscreen.tap(spot!.x, spot!.y); else await page.mouse.click(spot!.x, spot!.y);
			await page.waitForTimeout(1200);
		};
		const where = () => page.evaluate(() => {
			const a = document.activeElement as HTMLElement | null;
			if (!a) return { el: 'none', inDialog: false };
			const label = a.getAttribute('aria-label') ?? (a.textContent ?? '').trim().slice(0, 24);
			const dialog = a.closest('[role="dialog"], [data-popover]');
			return { el: `${a.tagName.toLowerCase()}${a.className && typeof a.className === 'string' ? '.' + a.className.trim().split(/\s+/)[0] : ''}[${label}]`, inDialog: !!dialog, dialogName: dialog?.getAttribute('aria-label') ?? null, insideEditor: !!a.closest('.cm-content') };
		});
		const surfaceUp = () => page.evaluate(() => [...document.querySelectorAll('[data-adaptive-drawer] .drawer-panel, [data-popover]')].some((e) => e.getClientRects().length > 0));
		const docText = () => page.evaluate(() => (document.querySelector('.doc .cm-content') as HTMLElement).textContent?.length ?? 0);
		const docUp = () => page.evaluate(() => !!document.querySelector('.doc .cm-content') && (document.querySelector('.doc .cm-content') as HTMLElement).getClientRects().length > 0);
		const reopen = async () => {
			if (await docUp()) return;
			await stage(page, threadId, docPath, `Q4 launch plan focus ${LABEL}-${width}`, width, false);
			const c = page.getByRole('button', { name: 'Close comments', exact: true });
			if (await c.count()) await c.first().click();
			await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
			await page.waitForTimeout(1500);
		};
		const result: Record<string, unknown> = {};
		mkdirSync(out, { recursive: true });
		const fresh = async () => {
			await stage(page, threadId, docPath, `Q4 launch plan focus ${LABEL}-${width}`, width, false);
			const c = page.getByRole('button', { name: 'Close comments', exact: true });
			if (await c.count()) await c.first().click();
			await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
			await expect.poll(() => page.locator('[data-comment-new]').count(), { timeout: 30_000 }).toBeGreaterThan(0);
			await page.waitForTimeout(1200);
		};
		// The editor draws only what is near the viewport: scroll until the pressed text is drawn.
		const reveal = async (quote: string) => {
			for (let i = 0; i < 40; i++) {
				const done = await page.evaluate((key) => {
					const content = document.querySelector('.doc .cm-content') as HTMLElement;
					const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
					for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n.textContent ?? '').includes(key)) { (n.parentElement as HTMLElement).scrollIntoView({ block: 'center' }); return true; }
					let scroller: HTMLElement | null = null;
					for (let e: HTMLElement | null = content; e; e = e.parentElement) { const o = getComputedStyle(e).overflowY; if ((o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1) { scroller = e; break; } }
					if (scroller) scroller.scrollTop = scroller.scrollTop + scroller.clientHeight * 0.6 > scroller.scrollHeight ? 0 : scroller.scrollTop + scroller.clientHeight * 0.6;
					return false;
				}, quote.slice(0, 14));
				if (done) break;
				await page.waitForTimeout(250);
			}
			await page.waitForTimeout(500);
		};
		// Where the pressed text is on screen: the centre of two characters at the offset into the quote.
		const spotOf = (quote: string, offset: number) => page.evaluate(({ quote, offset }) => {
			const content = document.querySelector('.doc .cm-content') as HTMLElement | null;
			if (!content) return null;
			const key = quote.slice(0, 14);
			const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
			for (let n = walker.nextNode(); n; n = walker.nextNode()) {
				const at = (n.textContent ?? '').indexOf(key);
				if (at < 0) continue;
				const r = document.createRange(); r.setStart(n, at + offset); r.setEnd(n, at + offset + 2);
				(n.parentElement as HTMLElement).scrollIntoView({ block: 'center' });
				const q = r.getBoundingClientRect();
				return { x: q.left + q.width / 2, y: q.top + q.height / 2 };
			}
			return null;
		}, { quote, offset });
		// What has focus, and where the typed marker is in the page: the block it sits in (a line, a table cell, a field) and the text of that block.
		const typedWhere = (key: string, mark: string) => page.evaluate(({ key, mark }) => {
			const a = document.activeElement as HTMLElement | null;
			const label = a ? (a.getAttribute('aria-label') ?? (a.textContent ?? '').trim().slice(0, 24)) : '';
			const el = a ? `${a.tagName.toLowerCase()}${typeof a.className === 'string' && a.className ? '.' + a.className.trim().split(/\s+/)[0] : ''}[${label}]` : 'none';
			const content = document.querySelector('.doc .cm-content') as HTMLElement | null;
			const found: string[] = [];
			if (content) {
				const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) {
					if (!(n.textContent ?? '').includes(mark)) continue;
					const block = (n.parentElement?.closest('td, th, li, .cm-line') ?? n.parentElement) as HTMLElement;
					found.push(`${block.tagName.toLowerCase()}: ${(block.textContent ?? '').trim().slice(0, 90)}`);
				}
			}
			const fields = [...document.querySelectorAll('textarea, input, [contenteditable="true"]')].filter((f) => !f.closest('.doc .cm-content') && ((f as HTMLInputElement).value ?? f.textContent ?? '').includes(mark)).map((f) => `${f.tagName.toLowerCase()}[${f.getAttribute('aria-label') ?? f.getAttribute('placeholder') ?? ''}]`);
			const bodyWithQQ = (document.body.innerText ?? '').split(mark).length - 1;
			return { el, inEditor: !!a?.closest('.cm-content'), inSurface: !!a?.closest('[role="dialog"], [data-popover], [data-adaptive-drawer]'), docBlocks: found, otherFields: fields, qqCount: bodyWithQQ, pressedLineHasKey: found.some((f) => f.includes(key)) };
		}, { key, mark });
		const CASESHOT: [string, string, number][] = [
			['single-long', 'In scope: the redesigned first-run screens, the guided project setup, and the updated help pages', 20],
			['one-word', 'partner', 2],
			['table-single', 'Priya Raman', 2],
			['table-two', 'Marcus', 2],
			['list-two', 'escalation', 2],
			['many', MANY, 3],
		];
		let caseNo = 0;
		for (const [name, quote, offset] of CASESHOT) {
			caseNo += 1;
			const mark = `zq${caseNo}x`;
			await fresh();
			await page.route('**/comments/*/viewed', (route) => route.abort());
			await reveal(quote);
			let spot = await spotOf(quote, offset);
			await page.waitForTimeout(500);
			spot = await spotOf(quote, offset);
			if (!spot) { result[name] = { error: 'pressed text not found on screen' }; continue; }
			if (touch) await page.touchscreen.tap(spot.x, spot.y); else await page.mouse.click(spot.x, spot.y);
			await page.waitForTimeout(1200);
			const afterPress = await typedWhere(quote.slice(0, 14), mark);
			const up = await surfaceUp();
			await page.keyboard.type(mark);
			// The save follows the typing by about 0.7 s; the saved file is the one place every outcome shows (the editor draws only what is near the viewport).
			await page.waitForTimeout(3000);
			const afterType = await typedWhere(quote.slice(0, 14), mark);
			const saved = (await import('node:fs')).readFileSync(docPath, 'utf8');
			const at = saved.indexOf(mark);
			const lineStart = saved.lastIndexOf('\n', at - 1) + 1;
			const nl = saved.indexOf('\n', at);
			const savedLine = at < 0 ? '' : saved.slice(lineStart, nl < 0 ? undefined : nl);
			const key = quote.slice(0, 14);
			const cleanLine = savedLine.replace(mark, '');
			const landed = at >= 0 ? (cleanLine.includes(key) || (cleanLine.includes('|') && cleanLine.includes(quote.split(' ')[0])) ? 'at the press point (same line or table row as the pressed text)' : 'elsewhere in the document')
				: afterType.otherFields.length ? 'in a field outside the document' : 'nowhere (not in the saved document or a field)';
			result[name] = { focusAfterPress: afterPress.el, focusInSurface: afterPress.inSurface, surfaceUp: up, landed, lineWithTyping: savedLine.slice(0, 110), otherFields: afterType.otherFields, focusAfterType: afterType.el };
		}
		writeFileSync(path.join(out, `type-${LABEL}-${width}.json`), JSON.stringify(result, null, 1));
		console.log('CHECK TYPE ' + LABEL + ' ' + width + ' ' + JSON.stringify(Object.fromEntries(Object.entries(result).map(([k, v]) => [k, (v as { landed?: string; error?: string }).landed ?? (v as { error?: string }).error]))).slice(0, 1500));
	});
}
