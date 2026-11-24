import { expect, test } from '../support/test';
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
