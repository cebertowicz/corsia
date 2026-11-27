import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { comment, open, seed, settled, type Doc, type Mode } from '../support/reviewFixtures';
import { openComments } from '../support/commentDrawer';
import { setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';

/**
 * Pressing inside the card of the thread that is already open leaves the page where it is. A long thread scrolls its text
 * out of view while it is read; the next press on a reply used to run the jump to that text and carried the document with it
 * (1440 with the card in the margin, and the phone sheet). A first press on a card that is not open still goes to its text.
 */
const LONG = '# Rollout plan\n\n' + Array.from({ length: 60 }, (_, index) => `Step ${index + 1}: the rollout proceeds to the next ring, and the owner of the ring confirms the checks before the next one starts.\n\n`).join('');
const params = { token: TEST_TOKEN };
const POSITIONS = { top: 3, middle: 30, end: 58 } as const;
const STEADY = 2;

type Seeded = { doc: Doc; root: string; lastReply: string };
async function seedThread(request: APIRequestContext, name: string, position: keyof typeof POSITIONS, count: number): Promise<Seeded> {
	const base = await seed(request, `RJ ${name}`);
	const longPath = base.docPath.replace(/\.md$/, ' long.md');
	const put = await request.put('/api/files/content', { params, data: { path: longPath, text: LONG, thread_id: base.threadId, create: true } });
	expect(put.ok(), await put.text()).toBeTruthy();
	const doc: Doc = { ...base, docPath: longPath, name: base.name.replace(/\.md$/, ' long.md'), text: LONG, artifactId: (await put.json()).artifact.id as string };
	const agents = [await createSession(request, `Agent N ${Math.random().toString(36).slice(2, 6)}`), await createSession(request, `Agent S ${Math.random().toString(36).slice(2, 6)}`)];
	const root = await comment(request, doc, 'Is the owner of this ring confirmed?', POSITIONS[position]);
	let lastReply = root;
	for (let index = 0; index < count; index++) {
		const body = `Reply ${index + 1}: noted, and the owner will confirm the ring before the review of the plan goes ahead.`;
		const created = index % 3 === 2
			? await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params, data: { delivery_policy: 'immediate', body, parent_id: root } })
			: await request.post('/api/delegation/docs/comments', { params, data: { artifact_id: doc.artifactId, as_session_id: agents[index % 2], parent_id: root, body } });
		expect(created.ok(), await created.text()).toBeTruthy();
		const json = await created.json();
		lastReply = (json.comment ?? json).id as string;
	}
	return { doc, root, lastReply };
}

const markRead = async (request: APIRequestContext, { doc, root, lastReply }: Seeded) => {
	const done = await request.post(`/api/artifacts/${doc.artifactId}/comments/${root}/viewed`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` }, data: { seen_reply_id: lastReply } });
	expect(done.ok(), await done.text()).toBeTruthy();
};

type Surface = { name: string; mode: Mode; width: number; height: number; touch: boolean };
const DESKTOP: Surface = { name: '1440 card in the margin', mode: 'closed', width: 1440, height: 900, touch: false };
const DOCKED: Surface = { name: '1440 docked list', mode: 'default', width: 1440, height: 900, touch: false };
const PHONE: Surface = { name: '393 sheet', mode: 'phone', width: 393, height: 852, touch: true };
const NARROW: Surface = { name: '320 sheet', mode: 'phone', width: 320, height: 640, touch: true };

type Target = 'body' | 'author' | 'below the fold' | 'Reply field' | 'earlier replies';
const targetOf = (card: Locator, target: Target, count: number): Locator => {
	const last = card.locator('.ct-reply').nth(count - 1);
	if (target === 'author') return last.locator('.ct-msg-author');
	if (target === 'Reply field') return card.locator('.ct-input').first();
	if (target === 'earlier replies') return card.locator('.ct-earlier').first();
	return last.locator('> :nth-child(2)');
};

/** Opens the document on `surface` and presses the comment's text, so the thread's card is the open one. */
async function openThread(page: Page, request: APIRequestContext, surface: Surface, seeded: Seeded) {
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	await open(page, seeded.doc, surface.mode, 'light');
	if (surface.width !== (surface.mode === 'phone' ? 393 : 1440)) await page.setViewportSize({ width: surface.width, height: surface.height });
	if (!surface.touch) await openComments(page);
	await settled(page);
	const anchor = page.locator('.cm-comment-anchor').first();
	const scroller = page.locator('.cm-scroller').first();
	await scroller.evaluate((el) => { el.scrollTop = 0; });
	// The anchors are drawn only near the viewport: bring the line up the way a reader scrolls to it.
	for (let step = 0; step < 80 && !(await anchor.count()); step++) { await scroller.evaluate((el) => { el.scrollTop += el.clientHeight * 0.8; }); await page.waitForTimeout(60); }
	await anchor.waitFor({ timeout: 5_000 });
	await anchor.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	await settled(page);
	const box = (await anchor.boundingBox())!;
	if (surface.touch) await page.touchscreen.tap(box.x + 6, box.y + box.height / 2); else await page.mouse.click(box.x + 6, box.y + box.height / 2);
	await page.waitForTimeout(1500); await settled(page);
	const card = page.locator(`[data-thread-id="${seeded.root}"]`).locator('visible=true').last();
	await expect(card).toBeVisible();
	return { card, scroller, anchor };
}

/** The document's scroll, and where the pressed element sits on the screen: a jump moves one or both. */
const place = async (page: Page, el: Locator) => ({
	doc: await page.locator('.cm-scroller').first().evaluate((node) => node.scrollTop),
	top: (await el.boundingBox())!.y,
});

/** Reads the thread to the target as a reader does (the target brought to the middle, or near the foot), then presses it. */
async function pressAfterReading(page: Page, surface: Surface, el: Locator, target: Target) {
	if (target === 'below the fold') {
		await el.evaluate((node, height) => {
			const scrollerOf = (n: HTMLElement) => { for (let p = n.parentElement; p; p = p.parentElement) if (/auto|scroll/.test(getComputedStyle(p).overflowY) && p.scrollHeight > p.clientHeight + 1) return p; return null; };
			const port = scrollerOf(node) ?? document.scrollingElement!;
			port.scrollTop += node.getBoundingClientRect().bottom - (window.innerHeight - height * 0.08);
		}, surface.height);
	} else await el.evaluate((node) => node.scrollIntoView({ block: 'center', behavior: 'instant' }));
	await page.waitForTimeout(400); await settled(page);
	const before = await place(page, el);
	const box = (await el.boundingBox())!;
	const x = box.x + Math.min(box.width / 2, 120), y = box.y + Math.min(box.height / 2, 12);
	if (surface.touch) await page.touchscreen.tap(x, y); else await page.mouse.click(x, y);
	await page.waitForTimeout(1500); await settled(page);
	return { before, after: await place(page, el) };
}

const stays = (moved: { before: { doc: number; top: number }; after: { doc: number; top: number } }) => {
	expect(Math.abs(moved.after.doc - moved.before.doc), 'the document scrolled').toBeLessThanOrEqual(STEADY);
	expect(Math.abs(moved.after.top - moved.before.top), 'the pressed element moved on the screen').toBeLessThanOrEqual(STEADY);
};

const only = (surface: Surface, info: { project: { name: string } }) => test.skip(info.project.name !== (surface.touch ? 'chromium-mobile' : 'chromium-desktop'));

test.use({ hasTouch: true });

// The cells that jumped: every press on a later reply of a long thread, where the card is in the margin and in the phone sheet.
for (const [surface, count, targets] of [
	[DESKTOP, 8, ['body', 'author', 'below the fold']], [DESKTOP, 20, ['body', 'author', 'below the fold']],
	[PHONE, 2, ['body', 'author', 'below the fold']], [NARROW, 2, ['body', 'author', 'below the fold']],
] as const) {
	for (const target of targets) {
		test(`${surface.name}: pressing the ${target} of a later reply, ${count} replies, leaves the page where it is`, async ({ page, request }, info) => {
			only(surface, info); test.setTimeout(180_000);
			const seeded = await seedThread(request, `${surface.mode} ${count}${target[0]}`, 'middle', count);
			const { card } = await openThread(page, request, surface, seeded);
			stays(await pressAfterReading(page, surface, targetOf(card, target, count), target));
		});
	}
}

test('393 sheet: pressing the body of a later reply, a read thread folded to its last two replies, leaves the page where it is', async ({ page, request }, info) => {
	only(PHONE, info); test.setTimeout(180_000);
	const seeded = await seedThread(request, 'phone folded', 'top', 8);
	await markRead(request, seeded);
	const { card } = await openThread(page, request, PHONE, seeded);
	await expect(card.locator('.ct-earlier').first()).toBeVisible();
	stays(await pressAfterReading(page, PHONE, targetOf(card, 'body', 2), 'body'));
});

// Unchanged: the cells that already held, and the presses that are meant to go to the text.
test('1440 docked list: pressing the body of a later reply leaves the page where it is', async ({ page, request }, info) => {
	only(DOCKED, info); test.setTimeout(180_000);
	const seeded = await seedThread(request, 'docked 8', 'middle', 8);
	const { card } = await openThread(page, request, DOCKED, seeded);
	stays(await pressAfterReading(page, DOCKED, targetOf(card, 'body', 8), 'body'));
});

for (const surface of [DESKTOP, PHONE]) {
	test(`${surface.name}: pressing the Reply field leaves the page where it is`, async ({ page, request }, info) => {
		only(surface, info); test.setTimeout(180_000);
		const count = surface === PHONE ? 2 : 8;
		const seeded = await seedThread(request, `${surface.mode} field`, 'middle', count);
		const { card } = await openThread(page, request, surface, seeded);
		stays(await pressAfterReading(page, surface, targetOf(card, 'Reply field', count), 'Reply field'));
	});
}

test('393 sheet: pressing the comment text opens the sheet with the text above it', async ({ page, request }, info) => {
	only(PHONE, info); test.setTimeout(180_000);
	const seeded = await seedThread(request, 'phone text', 'middle', 2);
	const { anchor } = await openThread(page, request, PHONE, seeded);
	const sheet = page.locator('[data-adaptive-drawer] .drawer-body').first();
	expect((await anchor.boundingBox())!.y + (await anchor.boundingBox())!.height).toBeLessThanOrEqual((await sheet.boundingBox())!.y + 1);
});

test('1440 docked list: pressing a card that is not open goes to its text', async ({ page, request }, info) => {
	only(DOCKED, info); test.setTimeout(180_000);
	const seeded = await seedThread(request, 'docked first', 'middle', 2);
	const end = await comment(request, seeded.doc, 'Is the last ring confirmed?', 58);
	await openThread(page, request, DOCKED, seeded);
	const card = page.locator(`[data-thread-id="${end}"]`).locator('visible=true').last();
	await card.scrollIntoViewIfNeeded();
	const doc = page.locator('.cm-scroller').first();
	const before = await doc.evaluate((node) => node.scrollTop);
	await card.getByText('Is the last ring confirmed?').click();
	await page.waitForTimeout(1500); await settled(page);
	expect(Math.abs((await doc.evaluate((node) => node.scrollTop)) - before), 'the document did not go to the comment').toBeGreaterThan(200);
});

// Unfolding the earlier replies in the phone sheet: the sheet keeps the height it had and the longer list scrolls inside it, so the page stays
// where it is and the commented text stays above the sheet.
const sheetState = (page: Page) => page.evaluate(() => {
	const panel = document.querySelector('[data-adaptive-drawer] .drawer-panel') as HTMLElement, body = panel.querySelector('.drawer-body') as HTMLElement;
	const anchor = document.querySelector('.cm-comment-anchor')!.getBoundingClientRect();
	return { doc: document.querySelector('.cm-scroller')!.scrollTop, sheetHeight: panel.getBoundingClientRect().height, sheetTop: panel.getBoundingClientRect().top, anchorTop: anchor.top, anchorBottom: anchor.bottom, bodyScrolls: body.scrollHeight - body.clientHeight > 1 };
});
for (const [surface, position] of [[PHONE, 'top'], [PHONE, 'middle'], [NARROW, 'top']] as const) {
	test(`${surface.name}: unfolding the earlier replies keeps the sheet's height and the page still, the text ${position} of the document in view`, async ({ page, request }, info) => {
		only(surface, info); test.setTimeout(180_000);
		const seeded = await seedThread(request, `${surface.mode} unfold ${position}`, position, 20);
		await markRead(request, seeded);
		const { card } = await openThread(page, request, surface, seeded);
		const control = targetOf(card, 'earlier replies', 20);
		await expect(control).toBeVisible();
		await page.waitForTimeout(500); await settled(page);
		const before = await sheetState(page);
		await page.touchscreen.tap(...(await control.boundingBox().then((b) => [b!.x + Math.min(b!.width / 2, 60), b!.y + b!.height / 2] as const)));
		await page.waitForTimeout(1500); await settled(page);
		const after = await sheetState(page);
		expect(Math.abs(after.doc - before.doc), 'the document scrolled').toBeLessThanOrEqual(STEADY);
		expect(Math.abs(after.sheetHeight - before.sheetHeight), 'the sheet changed height').toBeLessThanOrEqual(1);
		expect(after.anchorTop, 'the commented text left the screen').toBeGreaterThanOrEqual(0);
		expect(after.anchorBottom, 'the sheet covers the commented text').toBeLessThanOrEqual(after.sheetTop + 1);
		expect(after.bodyScrolls, 'the earlier replies did not scroll inside the sheet').toBe(true);
	});
}

// "Go to text" on the card already open still takes the document to the text, however far the thread was read and the page left.
// Only the docked list has the button to press: the margin card shows it only while its text shows, and the sheet's thread view has none.
const viewportOf = (page: Page) => page.viewportSize()!;
/** Waits until the comment's anchor is on screen, and above the phone sheet when there is one. */
async function expectAnchorShown(page: Page, surface: Surface) {
	await expect(async () => {
		const box = await page.locator('.cm-comment-anchor').first().boundingBox();
		expect(box, 'the commented text is not on screen').not.toBeNull();
		expect(box!.y).toBeGreaterThanOrEqual(0);
		const limit = surface.touch ? (await page.locator('[data-adaptive-drawer] .drawer-body').first().boundingBox())!.y : viewportOf(page).height;
		expect(box!.y + box!.height, 'the commented text is not above the sheet or inside the screen').toBeLessThanOrEqual(limit + 1);
	}).toPass({ timeout: 8_000 });
}
async function press(page: Page, surface: Surface, el: Locator) {
	await expect(el).toBeVisible();
	const box = (await el.boundingBox())!;
	if (surface.touch) await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2); else await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
	await page.waitForTimeout(1500); await settled(page);
}

test(`${DOCKED.name}: Go to text on the open card, after reading its later replies, takes the document to the text`, async ({ page, request }, info) => {
	only(DOCKED, info); test.setTimeout(180_000);
	const count = 8;
	const seeded = await seedThread(request, `${DOCKED.mode} goto ${DOCKED.width}`, 'middle', count);
	const { card } = await openThread(page, request, DOCKED, seeded);
	const doc = page.locator('.cm-scroller').first();
	await targetOf(card, 'body', count).evaluate((node) => node.scrollIntoView({ block: 'center', behavior: 'instant' }));
	await page.waitForTimeout(400); await settled(page);
	const jump = card.locator('[data-comment-jump]').first();
	await jump.evaluate((node) => node.scrollIntoView({ block: 'nearest', behavior: 'instant' }));
	await doc.evaluate((node) => { node.scrollTop = 0; });
	await page.waitForTimeout(400); await settled(page);
	const before = await doc.evaluate((node) => node.scrollTop);
	await press(page, DOCKED, jump);
	expect(Math.abs((await doc.evaluate((node) => node.scrollTop)) - before), 'the document did not go to the text').toBeGreaterThan(200);
	await expectAnchorShown(page, DOCKED);
});

// The phone list: a press inside the open card keeps the sheet's height and the document where they are; Go to text on it goes to the text.
async function openPhoneList(page: Page, request: APIRequestContext, name: string) {
	const seeded = await seedThread(request, name, 'middle', 2);
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	await open(page, seeded.doc, 'phone', 'light');
	await openComments(page);
	await settled(page);
	const drawer = page.locator('[data-doc-comments-drawer]');
	const card = drawer.locator(`[data-thread-id="${seeded.root}"]`).first();
	const jump = card.locator('[data-comment-jump]').first();
	await press(page, PHONE, jump);
	await expect(card).toHaveClass(/focused/);
	const doc = page.locator('.cm-scroller').first();
	await doc.evaluate((node) => { node.scrollTop = 0; });
	await page.waitForTimeout(400); await settled(page);
	return { card, jump, doc, drawer };
}

/** The open sheet is at half height, so the card's row can sit below its edge: scroll it into the sheet's view before pressing (the press is by position). */
const bring = (el: Locator) => el.evaluate((node) => node.scrollIntoView({ block: 'center', behavior: 'instant' }));

test('393 phone list: pressing inside the open card keeps the sheet height and the document where they are', async ({ page, request }, info) => {
	only(PHONE, info); test.setTimeout(180_000);
	const { card, doc, drawer } = await openPhoneList(page, request, 'list press');
	await bring(card.getByText('Is the owner of this ring confirmed?')); await page.waitForTimeout(400); await settled(page);
	const before = { doc: await doc.evaluate((node) => node.scrollTop), sheet: (await drawer.boundingBox())!.height };
	await press(page, PHONE, card.getByText('Is the owner of this ring confirmed?'));
	expect(Math.abs((await doc.evaluate((node) => node.scrollTop)) - before.doc), 'the document scrolled').toBeLessThanOrEqual(STEADY);
	expect(Math.abs((await drawer.boundingBox())!.height - before.sheet), 'the sheet changed height').toBeLessThanOrEqual(STEADY);
});

test('393 phone list: Go to text on the open card still takes the document to the text', async ({ page, request }, info) => {
	only(PHONE, info); test.setTimeout(180_000);
	const { jump, doc } = await openPhoneList(page, request, 'list goto');
	await bring(jump); await page.waitForTimeout(400); await settled(page);
	const before = await doc.evaluate((node) => node.scrollTop);
	await press(page, PHONE, jump);
	expect(Math.abs((await doc.evaluate((node) => node.scrollTop)) - before), 'the document did not go to the text').toBeGreaterThan(200);
	await expectAnchorShown(page, PHONE);
});

// A short thread's reply and recipient line remain visible as the Agreed line enters the held sheet.
for (const surface of [PHONE, NARROW]) {
	for (const { delayed, reduced } of [{ delayed: false, reduced: false }, { delayed: true, reduced: false }, { delayed: false, reduced: true }]) {
		test(`${surface.name}: Agree keeps the visible reply in view${delayed ? ' when the response is delayed' : ''}${reduced ? ' with reduced motion' : ''}`, async ({ page, request }, info) => {
			only(surface, info); test.setTimeout(180_000);
			if (reduced) await page.emulateMedia({ reducedMotion: 'reduce' });
			const seeded = await seedThread(request, `${surface.mode} short agree ${delayed} ${reduced}`, 'middle', 1);
			const { card, scroller } = await openThread(page, request, surface, seeded);
			const panel = page.locator('.drawer-panel:visible').last();
			const reply = card.locator('.ct-input'), line = card.locator('.ct-goes');
			await expect(line).toBeVisible();
			const geometry = () => panel.evaluate((element) => {
				const body = element.querySelector('.drawer-body')!.getBoundingClientRect();
				const field = element.querySelector('.ct-input')!.getBoundingClientRect();
				const recipient = element.querySelector('.ct-goes')!.getBoundingClientRect();
				return { top: element.getBoundingClientRect().top, clipTop: body.top, clipBottom: Math.min(body.bottom, innerHeight), fieldTop: field.top, fieldBottom: field.bottom, lineBottom: recipient.bottom };
			});
			const before = await geometry(), docBefore = await scroller.evaluate((node) => node.scrollTop);
			expect(before.fieldTop).toBeGreaterThanOrEqual(before.clipTop - 1);
			expect(before.fieldBottom).toBeLessThanOrEqual(before.clipBottom + 1);
			expect(before.lineBottom).toBeLessThanOrEqual(before.clipBottom + 1);
			let release = () => {}, seen = () => {}, continued = () => {};
			const gate = new Promise<void>((resolve) => { release = resolve; });
			const requested = new Promise<void>((resolve) => { seen = resolve; });
			const completed = new Promise<void>((resolve) => { continued = resolve; });
			const route = async (intercept: import('@playwright/test').Route) => {
				if (intercept.request().method() !== 'PATCH') { await intercept.continue(); return; }
				seen(); await gate; await intercept.continue(); continued();
			};
			if (delayed) await page.route('**/api/comments/*', route);
			await page.getByRole('button', { name: 'Agree', exact: true }).locator('visible=true').first().tap();
			if (delayed) {
				await requested;
				expect((await geometry()).lineBottom).toBeCloseTo(before.lineBottom, 0);
				expect(await scroller.evaluate((node) => node.scrollTop)).toBeCloseTo(docBefore, 0);
				release(); await completed; await page.unroute('**/api/comments/*', route);
			}
			await expect(card.locator('.ct-agreed')).toBeVisible();
			await panel.evaluate(async (element) => { await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => {}))); });
			await expect.poll(async () => (await geometry()).lineBottom - (await geometry()).clipBottom).toBeLessThanOrEqual(1);
			const after = await geometry();
			expect(Math.abs(after.top - before.top)).toBeLessThanOrEqual(STEADY);
			expect(Math.abs((await scroller.evaluate((node) => node.scrollTop)) - docBefore)).toBeLessThanOrEqual(STEADY);
			await expect(reply).toBeVisible();
			if (reduced) {
				const body = panel.locator('.drawer-body');
				const bodyBefore = await body.evaluate((node) => node.scrollTop);
				const agent = await createSession(request, 'Later reviewer');
				const workspace = process.env.CORSIA_E2E_WORKSPACE, state = process.env.CORSIA_E2E_STATE_DIR, python = process.env.CORSIA_E2E_BACKEND_PYTHON;
				if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
				// Seed a different author's later reply and reload it: this checks card growth, not live notification delivery.
				execFileSync(python, ['-c', [
					'import runpy, sys',
					'runpy.run_path("frontend/e2e/support/focus-outcomes.py")["assert_disposable_roots"]()',
					'from doceditor import comments',
					'row, created = comments.create_with_status(sys.argv[1], body="A later reply arrives after the agreement.", author="Later reviewer", author_kind="agent", author_thread_id=sys.argv[3], parent_id=sys.argv[2], announce=False)',
					'assert created and row["parent_id"] == sys.argv[2] and row["author_thread_id"] == sys.argv[3]',
				].join('\n'), seeded.doc.artifactId, seeded.root, agent], { cwd: path.resolve(process.cwd(), '..'), env: { ...process.env, PYTHONPATH: 'backend', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state } });
				await page.evaluate(async (artifactId) => (await import('/src/lib/stores/docComments.ts')).loadComments(artifactId), seeded.doc.artifactId);
				await expect(card.getByText('A later reply arrives after the agreement.')).toBeVisible();
				await panel.evaluate(async (element) => { await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => {}))); });
				await settled(page);
				expect(await body.evaluate((node) => node.scrollTop)).toBeCloseTo(bodyBefore, 0);
			}
		});
	}
}

// Agree in the phone sheet's header adds an "Agreed" line to the thread: the sheet keeps the height it had and the line scrolls inside it, as for earlier
// replies, so the page and the Agree control stay where they are (the sheet used to grow, which carried the control up and moved the page).
for (const surface of [PHONE, NARROW]) {
	test(`${surface.name}: pressing Agree keeps the sheet's height, the page and the Agree control where they are`, async ({ page, request }, info) => {
		only(surface, info); test.setTimeout(180_000);
		const seeded = await seedThread(request, `${surface.mode} agree`, 'middle', 2);
		await openThread(page, request, surface, seeded);
		const agree = page.getByRole('button', { name: 'Agree', exact: true }).locator('visible=true').first();
		await expect(agree).toHaveAttribute('aria-pressed', 'false');
		const sheetBefore = await sheetState(page);
		const moved = await pressAfterReading(page, surface, agree, 'body');
		await expect(agree, 'the press did not register').toHaveAttribute('aria-pressed', 'true');
		stays(moved);
		expect(Math.abs((await sheetState(page)).sheetHeight - sheetBefore.sheetHeight), 'the sheet changed height').toBeLessThanOrEqual(1);
	});
}
