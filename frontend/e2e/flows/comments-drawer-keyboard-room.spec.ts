import { expect, test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { comment, open, seed } from '../support/reviewFixtures';
import { settleMotion, settleRect } from '../support/sheetSettle';

/**
 * On a phone, while a Reply has focus (the soft keyboard is up) the comment list's footer scrolls with the list, so the list keeps the
 * room; when focus leaves, the footer goes back below it. The page asks for `interactive-widget=resizes-content`, where a browser shrinks
 * the whole layout with the keyboard: `innerHeight` and the visual viewport fall together and no gap between them shows. Resizing the
 * window here is that shape, so the footer must follow focus, not a measured gap.
 */

const TAG = '@flow:comments-drawer-keyboard-room @surface:documents';
const SHEET = '[data-adaptive-drawer] .drawer-panel';
const FULL = 852;
const KEYBOARD = 336;

test.use({ viewport: { width: 393, height: FULL }, hasTouch: true, isMobile: true });
test.beforeEach(async ({}, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'The phone sheet is measured with touch in the mobile project');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(300_000);
});

test(`${TAG} the list's footer scrolls with it while a Reply has focus, with a layout that resizes for the keyboard`, async ({ page, request }) => {
	const doc = await seed(request, 'Keyboard room');
	for (let i = 0; i < 12; i += 1) await comment(request, doc, `Note ${i + 1}: check this step.`, (i % 12) + 1);
	await open(page, doc, 'phone', 'light');
	await page.setViewportSize({ width: 393, height: FULL });
	await settleMotion(page);
	await page.locator('[data-comments-entry]').tap();
	await expect(page.locator(SHEET)).toHaveCount(1);
	await settleRect(page, SHEET);
	await page.waitForTimeout(500);
	/** Whether the footer is inside the scroller (scrolling with the list) rather than fixed below it. */
	const footerScrolls = () => page.evaluate(() => document.querySelector(`[data-adaptive-drawer] .drawer-panel .drawer-foot`)?.parentElement?.classList.contains('drawer-comments') ?? null);
	await expect.poll(footerScrolls, 'at rest the footer is fixed below the list').toBe(false);
	const reply = page.locator(SHEET).getByPlaceholder('Reply…').last();
	await reply.tap();
	await page.setViewportSize({ width: 393, height: FULL - KEYBOARD });
	await settleRect(page, SHEET);
	await expect.poll(footerScrolls, 'with a Reply focused the footer scrolls with the list').toBe(true);
	// Focus leaves the field and the keyboard goes down: the footer is fixed again.
	await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
	await page.setViewportSize({ width: 393, height: FULL });
	await settleRect(page, SHEET);
	await expect.poll(footerScrolls, 'with no field focused the footer is fixed again').toBe(false);
});

test(`${TAG} a tap on the last Reply, then the keyboard, leaves the whole field above the keyboard`, async ({ page, request }) => {
	const doc = await seed(request, 'Keyboard reveal');
	for (let i = 0; i < 12; i += 1) await comment(request, doc, `Note ${i + 1}: check this step.`, (i % 12) + 1);
	await open(page, doc, 'phone', 'light');
	await page.setViewportSize({ width: 393, height: FULL });
	await settleMotion(page);
	await page.locator('[data-comments-entry]').tap();
	await expect(page.locator(SHEET)).toHaveCount(1);
	await settleRect(page, SHEET);
	await page.waitForTimeout(500);
	// A real tap (pointerdown, as a person does), then the layout shrinks for the keyboard while the sheet's own height transition runs.
	await page.locator(SHEET).getByPlaceholder('Reply…').last().tap();
	await page.setViewportSize({ width: 393, height: FULL - KEYBOARD });
	await settleRect(page, SHEET);
	await page.waitForTimeout(800);
	const reading = () => page.evaluate(() => {
		const field = document.activeElement as HTMLElement | null;
		const list = document.querySelector<HTMLElement>('[data-adaptive-drawer] .drawer-panel .drawer-comments');
		if (!field || field.tagName !== 'TEXTAREA' || !list) return null;
		const box = field.getBoundingClientRect(), view = list.getBoundingClientRect();
		return { fieldTop: box.top, fieldBottom: box.bottom, viewTop: view.top, viewBottom: Math.min(view.bottom, window.innerHeight) };
	});
	const seen = await reading();
	expect(seen, 'the tapped Reply keeps focus').not.toBeNull();
	expect(seen!.fieldTop, 'the field starts inside the list').toBeGreaterThanOrEqual(seen!.viewTop - 0.5);
	expect(seen!.fieldBottom, 'the field ends above the keyboard and the list end').toBeLessThanOrEqual(seen!.viewBottom + 0.5);
});

/**
 * The phone sheet's list scrolls up and down only: it is never wider than its box and nothing moves it sideways (a sideways wheel, a script, a touch swipe).
 * Run with this browser's own scrollbar gutter and with overlay scrollbars emulated (`scrollbar-width: none`, no gutter), because the gutter of a classic
 * scrollbar hides any overflow up to its width, while a phone has no gutter: a 4px overhang from the pinned Accept row went unseen with the gutter and moved the list with overlay scrollbars.
 */
for (const width of [393, 320]) {
	for (const scrollbars of ['classic gutter', 'overlay (emulated)'] as const) {
		test(`${TAG} the list never moves sideways, with the Accept row pinned, at ${width} with ${scrollbars} scrollbars`, async ({ page, request }) => {
			const doc = await seed(request, `Sideways ${width}`);
			for (let i = 0; i < 3; i += 1) await comment(request, doc, `Note ${i + 1}: check this step.`, (i % 12) + 1);
			const writer = await createSession(request, `Other writer ${Math.random().toString(36).slice(2, 8)}`);
			for (const step of [8, 9]) {
				const made = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { artifact_id: doc.artifactId, quote: `Step ${step}: the rollout`, replacement: `Phase ${step}: the rollout`, body: 'Use phase.', as_session_id: writer } });
				expect(made.ok(), await made.text()).toBeTruthy();
			}
			await open(page, doc, 'phone', 'light');
			await page.setViewportSize({ width, height: FULL });
			await settleMotion(page);
			await page.locator('[data-comments-entry]').tap();
			await expect(page.locator(SHEET)).toHaveCount(1);
			await settleRect(page, SHEET);
			await page.waitForTimeout(500);
			if (scrollbars !== 'classic gutter') await page.addStyleTag({ content: `${SHEET} .drawer-comments { scrollbar-width: none; scrollbar-gutter: auto; }` });
			const list = page.locator(`${SHEET} .drawer-comments`);
			const reading = () => list.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, offsetWidth: el.offsetWidth, pinned: !!el.querySelector('.list-accept-all.in-scroller.pinned') }));
			/** A sideways wheel, a script and a sideways touch swipe, each read back on its own. */
			const pushSideways = async () => {
				const box = (await list.boundingBox())!;
				const x = box.x + box.width / 2, y = box.y + box.height / 2;
				const left = () => list.evaluate((el) => el.scrollLeft);
				await page.mouse.move(x, y);
				await page.mouse.wheel(120, 0);
				await page.waitForTimeout(300);
				const wheel = await left();
				await list.evaluate((el) => { el.scrollLeft = 40; });
				const script = await left();
				await list.evaluate((el) => { el.scrollLeft = 0; });
				const cdp = await page.context().newCDPSession(page);
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x + 80, y, id: 0 }] });
				for (let step = 1; step <= 8; step += 1) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + 80 - (160 * step) / 8, y, id: 0 }] });
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
				await cdp.detach();
				await page.waitForTimeout(400);
				const touch = await left();
				await list.evaluate((el) => { el.scrollLeft = 0; });
				return { wheel, script, touch };
			};
			const atRest = await reading();
			expect(atRest.pinned, 'the Accept row is pinned in the list at rest').toBe(true);
			expect(atRest.scrollWidth, 'at rest the list is no wider than its box').toBe(atRest.clientWidth);
			expect(await pushSideways(), 'at rest nothing moves the list sideways').toEqual({ wheel: 0, script: 0, touch: 0 });
			await page.locator(SHEET).locator('[data-discussion-original]').first().tap();
			await expect(page.locator(SHEET)).toHaveClass(/\bhalf\b/);
			await page.waitForTimeout(700);
			await settleRect(page, SHEET);
			const atHalf = await reading();
			expect(atHalf.scrollWidth, 'at half detent the list is no wider than its box').toBe(atHalf.clientWidth);
			expect(await pushSideways(), 'at half detent nothing moves the list sideways').toEqual({ wheel: 0, script: 0, touch: 0 });
		});
	}
}
