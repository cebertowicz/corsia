import { test } from '../support/test';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { saveEvidenceScreenshot } from '../support/corsia';
import { openSuggestionDocument } from '../support/docSuggestions';
import { waitForPanelsToSettle } from '../support/sidebar';

/**
 * A press on a comment's highlight opens that comment's card, wherever the highlight sits. The one-word comment here lies inside the
 * longer quote of an agent's suggestion: the press is in both highlights, and it is the word's comment the reader pressed, so it is that
 * card that opens (on a phone a tap in a suggestion's struck text opens no card, so a tap on the word opened nothing). The reverse
 * nesting, a suggestion on one word inside a longer comment, opens the suggestion on desktop (the innermost) and the comment on a
 * phone, where a suggestion's card does not open by tap: no nesting leaves a press that opens nothing.
 */

const TAG = '@flow:doc-highlight-press @surface:documents';
const TEXT = ['# Handover notes', '', 'Handover notes say the reviewer signs off on timing before it ships.', '', 'Nothing else on this page carries a comment, it only gives the page some length.', ''].join('\n');
const WORD = 'Which reviewer?';
const SUGGESTION = 'Say a reviewer instead.';
const COMMENT = { quote: 'reviewer', body: WORD, author: 'person' as const };
const ITEMS = [COMMENT, { quote: 'the reviewer signs', replacement: 'a reviewer signs', body: SUGGESTION }];
const LONG = 'Is this the right sign-off?';
const REVERSE = [{ quote: 'the reviewer signs', body: LONG, author: 'person' as const }, { quote: 'reviewer', replacement: 'reviewers', body: SUGGESTION }];

test.beforeEach(({}, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Size and touch are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
});

/** The middle of the word's highlight, read from the real layout. */
const wordCentre = (page: Page) => page.evaluate(() => {
	const anchor = [...document.querySelectorAll('.cm-comment-anchor')].find((el) => el.textContent === 'reviewer')!;
	const box = anchor.getBoundingClientRect();
	return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
});

/** The middle of a suggestion's struck word, read from the real layout. */
const struckCentre = (page: Page) => page.evaluate(() => {
	const box = [...document.querySelectorAll('.cm-suggestion-del')].find((el) => el.textContent === 'reviewer')!.getBoundingClientRect();
	return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
});

/** The page build, as the header shows it, logged before a frame is taken. */
async function logBuild(page: Page, testInfo: TestInfo) {
	const stamp = (await page.locator('body').innerText()).match(/b [0-9a-f]{7}\S*\s+·\s+f [0-9a-f]{7}\S*/)?.[0] ?? 'no stamp';
	console.log(`build stamp before ${testInfo.title}: ${stamp}`);
}

async function open(page: Page, request: Page['request'], panel: 'open' | 'closed', items = [COMMENT]) {
	await openSuggestionDocument(page, request, TEXT, items, { splitWidth: 540 });
	await expect(page.locator('.cm-comment-anchor', { hasText: /^reviewer$/ })).toBeVisible();
	await waitForPanelsToSettle(page);
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	const shown = await page.evaluate(() => { const pane = document.querySelector('.pane'); return !!pane && getComputedStyle(pane).display !== 'none' && pane.getBoundingClientRect().width > 0; });
	if ((panel === 'open') !== shown) { await toggle.click(); await waitForPanelsToSettle(page); }
}

const card = (page: Page) => page.getByRole('dialog').filter({ hasText: WORD });

test.describe(`${TAG} desktop`, () => {
	test('a press on the word opens its comment, not the suggestion around it, at 1440 wide', async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await open(page, request, 'closed', ITEMS);
		const { x, y } = await wordCentre(page);
		await page.mouse.click(x, y);
		await expect(card(page), 'the word comment opens').toBeVisible();
		await expect(page.getByText(SUGGESTION), 'not the suggestion card').toHaveCount(0);
		await logBuild(page, testInfo);
		await saveEvidenceScreenshot(page, testInfo, 'press-1440-suggestion');
	});

	test('a press on a suggestion\'s word inside a longer comment opens the suggestion, at 1440 wide', async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await openSuggestionDocument(page, request, TEXT, REVERSE, { splitWidth: 540 });
		await expect(page.locator('.cm-suggestion-del', { hasText: /^reviewer$/ })).toBeVisible();
		const { x, y } = await struckCentre(page);
		await page.mouse.click(x, y);
		await expect(page.getByText(SUGGESTION), 'the innermost, the suggestion, opens').toBeVisible();
		await expect(page.getByText(LONG)).toHaveCount(0);
		await logBuild(page, testInfo);
		await saveEvidenceScreenshot(page, testInfo, 'press-1440-reverse');
	});

	// At 900 an open Workspace panel is a drawer that slides the page left under a close scrim, so only its closed state is pressed there.
	for (const [width, height, panel] of [[1440, 900, 'open'], [1440, 900, 'closed'], [1200, 800, 'closed'], [900, 800, 'closed']] as const) {
		test(`a press on the word opens its comment at ${width} wide, panel ${panel}`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			await page.setViewportSize({ width, height });
			await open(page, request, panel);
			const { x, y } = await wordCentre(page);
			await page.mouse.click(x, y);
			await expect(card(page), 'the word comment opens').toBeVisible();
			await logBuild(page, testInfo);
			await saveEvidenceScreenshot(page, testInfo, `press-${width}-panel-${panel}`);
		});
	}
});

test.describe(`${TAG} phone`, () => {
	test.use({ viewport: { width: 393, height: 852 }, hasTouch: true, isMobile: true });

	test('a tap on a one-word highlight inside a suggestion opens its comment in the sheet', async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		await openSuggestionDocument(page, request, TEXT, ITEMS, { splitWidth: 540 });
		await expect(page.locator('.cm-comment-anchor', { hasText: /^reviewer$/ })).toBeVisible();
		const { x, y } = await wordCentre(page);
		await page.touchscreen.tap(x, y);
		const sheet = page.locator('.drawer-panel');
		await expect(sheet.getByText(WORD), 'the word\'s comment opens in the sheet').toBeVisible();
		await expect(sheet.getByText(SUGGESTION)).toHaveCount(0);
		await logBuild(page, testInfo);
		await saveEvidenceScreenshot(page, testInfo, 'tap-393');
	});

	test('a tap on a suggestion\'s word inside a longer comment opens the comment in the sheet', async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		await openSuggestionDocument(page, request, TEXT, REVERSE, { splitWidth: 540 });
		await expect(page.locator('.cm-suggestion-del', { hasText: /^reviewer$/ })).toBeVisible();
		const { x, y } = await struckCentre(page);
		await page.touchscreen.tap(x, y);
		const sheet = page.locator('.drawer-panel');
		await expect(sheet.getByText(LONG), 'the comment around the suggestion opens in the sheet').toBeVisible();
		await logBuild(page, testInfo);
		await saveEvidenceScreenshot(page, testInfo, 'tap-393-reverse');
	});
});
