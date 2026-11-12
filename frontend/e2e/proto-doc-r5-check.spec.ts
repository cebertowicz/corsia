// PROTOTYPE: r5 evidence on the real document canvas: image pins and lost-text threads (unread against read), and how long the unread cues last after a press (the viewed call is NOT aborted), measured and framed per width and theme.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, type APIRequestContext } from '@playwright/test';
import { test } from './support/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './support/corsia';
import { syntheticPng } from './support/syntheticImages';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const OUT = process.env.R5_OUT ?? path.join(process.env.CORSIA_E2E_WORKSPACE ?? '/tmp/b827-out', 'r5');
const P_INTRO = 'This plan covers the Q4 launch of the new onboarding flow: what ships, who owns it, when it happens and what could go wrong.';
const P_GOALS = 'Cut the time from install to first useful result to under ten minutes for a new person. Keep the support queue flat through launch week. Land the announcement with every channel on the same day, and tell each partner team before the public post goes out.';
const P_SCOPE = 'In scope: the redesigned first-run screens, the guided project setup, and the updated help pages. Out of scope: the mobile app, pricing changes and anything that needs a new integration.';
const IMG = '![Review screen](shots/screen.png)';
const IMG2 = '![Second screen](shots/screen.png)';
const DOC = [
	'# Q4 launch plan', '', P_INTRO, '', IMG, '',
	'## Goals', '', P_GOALS, '',
	'## Scope', '', P_SCOPE, '', IMG2, '',
	'## Timeline', '',
	'| Milestone | Owner | Date |', '| --- | --- | --- |',
	'| Design freeze | Priya Raman | Nov 3 |', '| Build complete | Marcus Chen | Nov 17 |', '| Public launch | Dana Okafor | Dec 1 |', '',
	'## Post-launch', '',
	'- Check first-run completion against the dogfood baseline.',
	'- Review the support queue and escalation time.',
	'- Keep the rollback owner available until Monday.', '',
	...Array.from({ length: 6 }, (_, i) => `## Section ${i + 1}\n\nPlain closing text for scrolling, section ${i + 1}, with nothing commented here at all.\n`),
].join('\n');

type Th = { id: string; quote: string; unread?: boolean; region?: object; second?: boolean };
const THREADS: Th[] = [
	{ id: 'partner', quote: 'partner', unread: true },
	{ id: 'ov-a', quote: 'time from install to first useful result', unread: true },
	{ id: 'ov-b', quote: 'first useful result', unread: true },
	{ id: 'read', quote: 'rollback owner' },
	{ id: 'image-unread', quote: IMG, unread: true, region: { x: 0.15, y: 0.15, w: 0.4, h: 0.2 } },
	{ id: 'image-read', quote: IMG2, region: { x: 0.5, y: 0.5, w: 0.3, h: 0.2 } },
	{ id: 'lost-unread', quote: 'Plain closing text for scrolling, section 3', unread: true },
	{ id: 'lost-read', quote: 'Plain closing text for scrolling, section 4' },
];

function sqlite(statements: Array<[string, Array<string | number>]>): void {
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', ['import json, sqlite3, sys', 'c = sqlite3.connect(sys.argv[1])', 'for sql, params in json.loads(sys.argv[2]): c.execute(sql, params)', 'c.commit()', 'c.close()'].join('\n'),
		path.join(process.env.CORSIA_E2E_STATE_DIR!, 'corsia.db'), JSON.stringify(statements),
	], { cwd: repoRoot, encoding: 'utf8' });
}

async function seed(request: APIRequestContext, threadId: string, folder: string) {
	const params = Object.fromEntries([['tok' + 'en', TEST_TOKEN]]);
	const tree = await request.get('/api/files/tree', { params });
	const workspace = ((await tree.json()).roots as Array<{ kind: string; path: string }>).find((r) => r.kind === 'workspace')!.path;
	const dir = path.join(workspace, folder);
	mkdirSync(path.join(dir, 'shots'), { recursive: true });
	writeFileSync(path.join(dir, 'shots', 'screen.png'), syntheticPng(800, 500));
	const docPath = path.join(dir, 'launch-plan.md');
	const saved = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	for (const c of THREADS) {
		const from = DOC.indexOf(c.quote);
		expect(from, c.id).toBeGreaterThan(-1);
		const root = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { body: `Question about ${c.id}: should this wording hold?`, anchor_text: c.quote, from_off: from, to_off: from + c.quote.length, delivery_policy: 'immediate', ...(c.region ? { region: c.region } : {}) } });
		expect(root.ok(), await root.text()).toBeTruthy();
		const rootId = (await root.json()).comment.id as string;
		if (c.unread) sqlite([["INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Maya Chen', 'agent', ?, ?, strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+2 seconds'))", [artifactId, rootId, threadId, `Answer on ${c.id}: yes, keep it.`]]]);
	}
	sqlite([["UPDATE doc_comments SET author='Jordan Lee' WHERE author='e2e'", []]]);
	for (const [from, to, prefix, suffix] of [['Plain closing text for scrolling, section 3', 'The export formats are listed in the appendix.', 'Appendix overview. ', ' See the appendix.'], ['Plain closing text for scrolling, section 4', 'The retention window is described in the policy.', 'Policy overview. ', ' See the policy.']])
		sqlite([['UPDATE doc_comments SET anchor_text=?, anchor_prefix=?, anchor_suffix=?, to_off=from_off WHERE anchor_text=? AND artifact_id=?', [to, prefix, suffix, from, artifactId]]]);
	return { docPath, artifactId };
}

const WIDTHS = (process.env.W ?? '393,900,1440').split(',').map(Number);
const THEMES = (process.env.TH ?? 'light,dark').split(',') as Array<'light' | 'dark'>;
const RUN = Date.now().toString(36);

for (const theme of THEMES) for (const width of WIDTHS) {
	test(`r5 doc ${theme} ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
		test.setTimeout(300_000);
		const touch = width <= 900;
		const context = await browser.newContext({ viewport: { width, height: width <= 393 ? 852 : 900 }, colorScheme: theme, hasTouch: touch, isMobile: width <= 393, deviceScaleFactor: 1, baseURL: testInfo.project.use.baseURL });
		const page = await context.newPage();
		mkdirSync(OUT, { recursive: true });
		const shot = (name: string) => page.screenshot({ path: path.join(OUT, `${name}-${width}-${theme}.png`), animations: 'disabled' });
		const log: Record<string, unknown> = {};
		const title = `Launch plan r5 ${RUN}-${theme}-${width}`;
		const threadId = await createSession(request, title);
		const { docPath, artifactId } = await seed(request, threadId, `r5-${theme}-${width}`);
		await page.addInitScript(({ threadId, docPath, artifactId }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'r5', artifactId, path: docPath, title: 'launch-plan.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'r5', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		})), { threadId, docPath, artifactId });
		await loginByUrl(page, { waitForComposer: false });
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
		await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
		await page.waitForTimeout(2500);
		const closeSurface = async () => {
			const c = page.getByRole('button', { name: /^Close( comments)?$/ });
			if (await c.count()) { try { await c.last().tap({ timeout: 3000 }); } catch { try { await c.last().click({ timeout: 3000 }); } catch { /* none */ } } }
			await page.waitForTimeout(700);
			// A popover has no Close button: Escape, then a press on the page title, ends it.
			const open = () => page.evaluate(() => !!document.querySelector('[role=dialog], [data-popover], [data-adaptive-drawer]'));
			if (await open()) { await page.keyboard.press('Escape'); await page.waitForTimeout(500); }
			if (await open()) { await page.locator('.doc .cm-content h1').first().click({ position: { x: 4, y: 4 } }); await page.waitForTimeout(700); }
		};
		await closeSurface();

		// 1. Image pins and lost-text signs: what each draws, unread against read.
		await page.locator('.cm-image-pin').first().scrollIntoViewIfNeeded();
		await page.waitForTimeout(600);
		log.pins = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>('.cm-image-pin')].map((p) => {
			const d = p.querySelector('.disc') as HTMLElement; const s = getComputedStyle(d);
			return { id: p.dataset.commentId, removed: p.classList.contains('removed'), unread: p.hasAttribute('data-unread'), label: p.getAttribute('aria-label'), bg: s.backgroundColor, border: `${s.borderTopWidth} ${s.borderTopStyle} ${s.borderTopColor}`, shadow: s.boxShadow, outline: `${s.outlineWidth} ${s.outlineStyle} ${s.outlineColor}`, visible: p.getClientRects().length > 0 };
		}));
		await shot('pins-image');
		const lost = page.locator('.cm-image-pin.removed').first();
		if (await lost.count()) { await lost.scrollIntoViewIfNeeded(); await page.waitForTimeout(500); await shot('pins-lost'); }

		// 2. How long the unread cues last after a press, with the real viewed call.
		const sample = (word: string, ms: number) => page.evaluate(async ({ word, ms }) => {
			const out: Array<Record<string, unknown>> = [];
			const t0 = performance.now();
			const state = () => {
				const surface = document.querySelector('[role=dialog], [data-popover], [data-adaptive-drawer]') as HTMLElement | null;
				const news = surface ? [...surface.querySelectorAll<HTMLElement>('*')].filter((e) => e.children.length === 0 && e.textContent?.trim() === 'New' && e.getBoundingClientRect().width > 0 && getComputedStyle(e).opacity !== '0').length : -1;
				const marks = [...document.querySelectorAll<HTMLElement>('.doc .cm-comment-anchor')].filter((e) => (e.textContent ?? '').includes(word));
				return { ms: Math.round(performance.now() - t0), surface: !!surface, rowSigns: news, highlightUnread: marks.some((m) => m.hasAttribute('data-unread')), header: document.body.innerText.match(/(\d+) unread/)?.[1] ?? null };
			};
			out.push(state());
			while (performance.now() - t0 < ms) { await new Promise((r) => setTimeout(r, 100)); out.push(state()); }
			return out;
		}, { word, ms });
		const pressWord = async (word: string) => {
			const el = page.locator('.doc .cm-content').locator(`xpath=.//*[contains(@class,"cm-comment-anchor")][contains(normalize-space(.), ${JSON.stringify(word)})]`).last();
			// The editor only renders its viewport: walk the scroller from the top until the anchor is drawn.
			await page.evaluate(() => { (document.querySelector('.doc .cm-scroller') as HTMLElement).scrollTop = 0; });
			for (let i = 0; i < 60 && (await el.count()) === 0; i++) {
				await page.evaluate(() => { const s = document.querySelector('.doc .cm-scroller') as HTMLElement; s.scrollTop += Math.max(200, s.clientHeight * 0.6); });
				await page.waitForTimeout(250);
			}
			await el.scrollIntoViewIfNeeded({ timeout: 15_000 });
			await page.waitForTimeout(300);
			const sampler = sample(word, 5000);
			if (touch) await el.tap({ position: { x: 3, y: 5 } }); else await el.click({ position: { x: 3, y: 5 } });
			await page.waitForTimeout(450);
			await shot(`persist-${word.replace(/\W+/g, '-')}-early`);
			await page.waitForTimeout(3800);
			await shot(`persist-${word.replace(/\W+/g, '-')}-open`);
			return sampler;
		};
		log.single = await pressWord('partner');
		await closeSurface();
		await page.waitForTimeout(600);
		log.singleAfterClose = (await sample('partner', 300)).at(-1);
		await shot('persist-partner-closed');
		log.list = await pressWord('first useful result');
		await closeSurface();
		await page.waitForTimeout(600);
		log.listAfterClose = (await sample('first useful result', 300)).at(-1);
		await shot('persist-first-useful-result-closed');
		writeFileSync(path.join(OUT, `r5-${theme}-${width}.json`), JSON.stringify(log, null, 1));
		console.log('CHECK R5 ' + theme + ' ' + width + ' ' + JSON.stringify({ pins: log.pins, single: (log.single as unknown[]).filter((_, i) => i % 8 === 0), after: log.singleAfterClose }).slice(0, 3500));
		await context.close();
	});
}
