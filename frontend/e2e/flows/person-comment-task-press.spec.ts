import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { shoot, useScheme } from '../support/comment-count-probe';

const repo = path.resolve(process.cwd(), '..');

// The person's own presses lower the count, as they clear "New": opening a thread (a card press) and deciding a
// suggestion (Accept, Reject). Reading everything settles the task; a new reply brings it back, after Dismiss all too.
for (const width of [1440, 393]) test(`comment task press walk at ${width}px`, async ({ page, request }, testInfo) => {
	test.setTimeout(240_000);
	// Each read stamp the person's presses send, so the frames' log says which gesture lowered the count.
	page.on('request', (r) => { if (r.url().includes('/viewed') || r.url().includes('/dismiss')) console.log(`PRESS ${width} request ${r.method()} ${new URL(r.url()).pathname}`); });
	await page.setViewportSize({ width, height: 900 });
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const narrow = width <= 768;
	// The phone project presses by touch; the desktop project by mouse, at either width.
	const touch = !!testInfo.project.use.hasTouch;
	const title = `Press walk ${Date.now()}`;
	const threadId = await createSession(request, title);
	const docFolder = path.join(workspace, 'comment-task-press', String(width));
	mkdirSync(docFolder, { recursive: true });
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const save = async (name: string) => {
		const saved = await request.put(`/api/files/content?${query}`, { data: { path: path.join(docFolder, name), text: 'Launch plan details for review.', thread_id: threadId, create: true } });
		expect(saved.ok(), await saved.text()).toBeTruthy();
		return (await saved.json()).artifact.id as string;
	};
	const artifactId = await save('Launch plan.md');
	const seed = (mode: string, artifact = artifactId) => execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', mode, artifact, threadId], {
		cwd: repo, encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});
	// Two agent replies in threads the person started (a reply in their thread) and two suggestions tagging them.
	seed('feedback');
	const press = (target: Locator, position?: { x: number; y: number }) => touch ? target.tap({ position }) : target.click({ position });
	const openThread = async () => {
		await openAppPastLanding(page);
		if (narrow) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
	};
	const openPlan = async () => {
		await openThread();
		if (!narrow) return;
		// A phone shows one surface; reopening the chat brings back the document it last showed, which replaces the chat
		// header (and its plan chip) a moment after the chat paints. Go back to the chat, then open the plan from its chip.
		const chat = page.getByRole('button', { name: 'Chat', exact: true });
		await expect(async () => {
			if (await chat.isVisible()) await chat.click({ timeout: 2000 });
			await page.getByRole('button', { name: /^Open plan —/ }).click({ timeout: 2000 });
			await expect(page.getByRole('dialog', { name: /^Plan —/ })).toBeVisible({ timeout: 2000 });
		}).toPass({ timeout: 30_000 });
	};
	const plan = page.getByRole('list', { name: 'Plan steps' });
	// The file's task while it counts. Once settled it stays in the phone's plan sheet as a Done row, with no count.
	const fileRows = plan.getByRole('listitem').filter({ hasText: 'Launch plan.md' });
	const row = fileRows.filter({ has: page.locator('.count-text') }).first();
	// The row opens its document; on a phone the plan sheet stays up over it until the person closes it (as on dev).
	const openFromRow = async () => {
		await press(row.getByRole('button').first());
		if (narrow) await press(page.getByRole('button', { name: 'Done', exact: true }));
	};
	const bar = page.locator('.review-bar');
	const frame = (name: string) => shoot(page, testInfo, `${width}-press-${name}`, true);
	const log = async (label: string) => console.log(`PRESS ${width} ${label}: bar=${JSON.stringify(await bar.first().innerText({ timeout: 1000 }).catch(() => 'gone'))} row=${JSON.stringify(width > 1024 ? await row.innerText({ timeout: 1000 }).catch(() => 'gone') : 'n/a')}`);
	// The count the person sees falls: the bar in the document always, and the plan row where it shows beside it.
	const expectCount = async (gesture: string, text: string | null, name: string) => {
		if (text) await expect(bar).toContainText(`${text} to review`);
		else await expect(bar).toHaveCount(0);
		if (width > 1024) {
			if (text) await expect(row.locator('.count-text')).toHaveText(text);
			else await expect(row).toHaveCount(0);
		}
		await log(`${gesture} -> ${text ?? 'none'}`);
		await frame(name);
	};

	await openPlan();
	await expect(row).toContainText('2 comments, 2 suggestions');
	await openFromRow();
	// The document opens with the task's bar: what waits, and only Dismiss all. No Done, no Won't do.
	await expect(bar).toContainText('2 comments, 2 suggestions to review');
	await expect(bar.getByRole('button', { name: 'Done', exact: true })).toHaveCount(0);
	await expect(bar.getByRole('button', { name: 'Won’t do' })).toHaveCount(0);
	await expect(bar.getByRole('button', { name: 'Dismiss all' })).toBeVisible();
	await log('opened from the row');
	await frame('1-open');

	// The list of threads shows them all as New; showing them is not opening them, so the count holds.
	await press(page.getByRole('button', { name: /\d+ threads?, \d+ unread/ }).first());
	const cards = page.getByRole('region', { name: /^Comment by / });
	await expect(cards.first()).toBeVisible();
	console.log(`PRESS ${width} cards: ${JSON.stringify(await cards.evaluateAll((all) => all.map((card) => [card.getAttribute('aria-label'), (card as HTMLElement).innerText.slice(0, 80)])))}`);
	console.log(`PRESS ${width} sheet buttons: ${JSON.stringify(await page.getByRole('button').evaluateAll((all) => all.map((b) => (b.getAttribute('aria-label') ?? (b as HTMLElement).innerText).slice(0, 40)).filter(Boolean)))}`);
	await page.waitForTimeout(3000);
	await expect(bar).toContainText('2 comments, 2 suggestions to review');
	await log('list shown 3 s');
	await frame('2-list');

	const accept = (scope: Page | Locator) => scope.getByRole('button', { name: 'Accept', exact: true });
	const commentCards = cards.filter({ hasNot: accept(page) });
	const suggestionCards = cards.filter({ has: accept(page) });
	const back = async () => {
		const toList = page.getByRole('button', { name: /^(All threads|Back)/ }).first();
		if (await toList.isVisible().catch(() => false)) await press(toList);
	};
	// (a) Open one thread by pressing its card's text. The quote above it is the separate "Go to text" control, which only jumps.
	const cardText = (card: Locator) => card.getByText('Please check this point.').first();
	await press(cardText(commentCards.first()));
	await expectCount('card press opened thread 1', '1 comment, 2 suggestions', '3-thread-opened');
	await back();
	// (c) Accept one suggestion in the sheet.
	await press(accept(suggestionCards.first()));
	await expectCount('Accept on suggestion 1', '1 comment, 1 suggestion', '4-accepted');
	await back();
	// (c) Reject the other.
	await press(suggestionCards.first().getByRole('button', { name: 'Reject', exact: true }));
	await expectCount('Reject on suggestion 2', '1 comment', '5-rejected');
	await back();
	// (a) Open the last thread: everything is read, the task settles and its row and bar go.
	await press(cardText(commentCards.filter({ hasText: /New/ }).first()));
	await expectCount('card press opened thread 2', null, '6-all-read');
	await openPlan();
	await expect(row).toHaveCount(0);
	if (narrow) await expect(fileRows.first()).toContainText('Done');
	// The person's Accept and Reject reach the owning agent as its own task about their edits. Wait for it, so every run
	// meets the same plan below. That task never carries the file's count: the file keeps its own task beside it.
	await expect(plan.getByRole('listitem').filter({ hasText: 'The user edited Launch plan' })).toBeVisible({ timeout: 30_000 });
	await frame('7-plan-after-all-read');
	const comments = await (await request.get(`/api/artifacts/${artifactId}/comments?${query}`)).json();
	console.log(`PRESS ${width} decisions: ${JSON.stringify(comments.comments.filter((item: { replacement: string | null }) => item.replacement).map((item: { decision: string | null }) => item.decision))}`);

	// A new reply brings the file's task back beside the agent's edit task; Dismiss all clears it; a later reply brings it back again.
	const counted = plan.getByRole('listitem').filter({ has: page.locator('.count-text') });
	const carrier = async (label: string) => console.log(`PRESS ${width} ${label}: counted rows=${JSON.stringify(await counted.evaluateAll((all) => all.map((item) => (item as HTMLElement).innerText.replace(/\s+/g, ' ').trim())))}`);
	seed('reply');
	await openPlan();
	await expect(counted.locator('.count-text')).toHaveText('1 comment');
	await expect(counted).toContainText('Launch plan.md');
	await expect(counted).not.toContainText('The user edited Launch plan');
	await carrier('resurfaced after read');
	await frame('8-resurfaced-after-read');
	if (!narrow) {
		// The document comes from its tab; Dismiss all is in its bar.
		await press(page.getByRole('tab', { name: 'Launch plan.md' }));
		await expect(bar).toContainText('1 comment to review');
		await press(bar.getByRole('button', { name: 'Dismiss all' }));
		await expect(bar).toHaveCount(0);
	} else {
		// A phone's chat has no tab to the document, so Dismiss all comes from the counted row's menu (long-press or right-click).
		const target = counted.first();
		if (touch) {
			const box = await target.boundingBox();
			if (!box) throw new Error('Counted row has no bounds');
			const session = await page.context().newCDPSession(page);
			const touchPoints = [{ x: box.x + box.width / 2, y: box.y + box.height / 2 }];
			await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints });
			await page.waitForTimeout(600);
			await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
			await session.detach();
		} else await target.click({ button: 'right' });
		await press(page.getByRole('menuitem', { name: 'Dismiss all' }));
		await log('row menu Dismiss all');
	}
	await openPlan();
	await expect(counted).toHaveCount(0);
	seed('reply');
	await openPlan();
	await expect(counted.locator('.count-text')).toHaveText('1 comment');
	await carrier('resurfaced after Dismiss all');
	await frame('9-resurfaced-after-dismiss');

	// An agent's open ask: its own untagged comment, on a document the person neither owns nor is tagged on.
	const askDoc = await save('Rollout notes.md');
	seed('ask', askDoc);
	seed('agent-root', askDoc);
	await openPlan();
	const ask = plan.getByRole('listitem').filter({ hasText: 'Review launch plan' }).first();
	await expect(ask.locator('.count-text')).toHaveText('1 comment');
	await expect(plan.getByRole('listitem').filter({ hasText: 'Rollout notes.md' })).toHaveCount(0);
	await frame('10-open-ask-eligible');
});

// On a phone a tap on a comment's highlight opens its card in the sheet. That open is a read, as a card press is: the
// file task's count falls by one for a one-word highlight and for a longer one. The text matches the fixture's 'highlights'.
test.describe('comment task highlight taps', () => {
	test.use({ viewport: { width: 393, height: 852 }, hasTouch: true, isMobile: true });

	test('comment task highlight taps at 393px', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Size and touch are set by the spec itself');
		test.setTimeout(180_000);
		page.on('request', (r) => { if (r.url().includes('/viewed')) console.log(`TAP request ${r.method()} ${new URL(r.url()).pathname}`); });
		const workspace = process.env.CORSIA_E2E_WORKSPACE;
		const state = process.env.CORSIA_E2E_STATE_DIR;
		const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
		if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
		const title = `Tap walk ${Date.now()}`;
		const threadId = await createSession(request, title);
		const docFolder = path.join(workspace, 'comment-task-tap');
		mkdirSync(docFolder, { recursive: true });
		const query = new URLSearchParams({ token: TEST_TOKEN });
		const saved = await request.put(`/api/files/content?${query}`, { data: { path: path.join(docFolder, 'Tap notes.md'), text: 'Launch plan details for review. The rollout waits on sign-off.', thread_id: threadId, create: true } });
		expect(saved.ok(), await saved.text()).toBeTruthy();
		const artifactId = (await saved.json()).artifact.id as string;
		execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', 'highlights', artifactId, threadId], {
			cwd: repo, encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
		});

		await openAppPastLanding(page);
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
		await page.getByRole('button', { name: /^Open plan —/ }).tap();
		const row = page.getByRole('list', { name: 'Plan steps' }).getByRole('listitem').filter({ hasText: 'Tap notes.md' }).filter({ has: page.locator('.count-text') });
		await expect(row.locator('.count-text')).toHaveText('3 comments');
		await row.getByRole('button').first().tap();
		await page.getByRole('button', { name: 'Done', exact: true }).tap();
		const bar = page.locator('.review-bar');
		await expect(bar).toContainText('3 comments to review');

		const sheet = page.locator('.drawer-panel');
		const light = async (name: string) => {
			await useScheme(page, 'light');
			await page.screenshot({ path: testInfo.outputPath(`393-tap-${name}-light.png`) });
		};
		for (const [quote, body, after, name] of [['details', 'Which details?', '2 comments', '1-one-word'], ['The rollout waits on sign-off', 'Who signs off?', '1 comment', '2-longer']] as const) {
			const anchor = page.locator('.cm-comment-anchor').filter({ hasText: quote }).first();
			await expect(anchor).toBeVisible();
			await light(`${name}-before`);
			// The longer highlight wraps onto a second line at this width, so the middle of its whole box can be past its
			// text: tap the middle of its first line's piece.
			const box = await anchor.evaluate((el) => { const r = el.getClientRects()[0]; return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
			await page.touchscreen.tap(box.x, box.y);
			await expect(sheet.getByText(body), `the tap on "${quote}" opens its card`).toBeVisible();
			await expect(bar).toContainText(`${after} to review`);
			console.log(`TAP "${quote}": bar=${JSON.stringify(await bar.first().innerText())}`);
			await light(`${name}-after`);
			await sheet.getByRole('button', { name: /^Close( comments)?$/ }).tap();
			await expect(sheet).toHaveCount(0);
		}
	});
});
