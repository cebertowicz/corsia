import { test } from '../support/test';
import { expect } from '@playwright/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import { closeComments, openComments } from '../support/commentDrawer';

/**
 * A canvas wide enough for the margin column opens it by itself once the document's comments load, and the header's comments
 * control toggles. A spec that opens comments after that must not press the control again, or it closes the column it wanted open.
 */
test('@flow:document-comments opening comments on a document whose column opened by itself leaves the card open', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Chromium desktop only.');
	await page.setViewportSize({ width: 2200, height: 900 });
	const { ids: [id] } = await openSuggestionDocument(page, request, '## A\n\nThe commentable words are right here.', [{ quote: 'commentable words', body: 'Hi', author: 'person' }], { splitWidth: 5000 });
	const card = page.locator(`[data-comment-id="${id}"]`);
	const pill = page.locator('.comment-status');
	await expect(card).toBeVisible();
	await expect(pill).toHaveAttribute('aria-expanded', 'true');
	await openComments(page);
	await expect(pill).toHaveAttribute('aria-expanded', 'true');
	await expect(card).toBeVisible();
	await closeComments(page);
	await expect(card).toHaveCount(0);
	await openComments(page);
	await expect(card).toBeVisible();
});
