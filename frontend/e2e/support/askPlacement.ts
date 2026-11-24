import { expect, type Page } from '@playwright/test';
import type { Mode } from './reviewFixtures';

/** Where a document's ask shows: on a phone, a card docked to the canvas's bottom edge; on a larger screen, its row in the document header. */
export type AskForm = 'docked' | 'header';
/** The form a case's mode expects. It comes from the case, never from what the page drew, so a wrong placement fails instead of being followed. */
export const askFormOf = (mode: Mode): AskForm => (mode === 'phone' ? 'docked' : 'header');
/** The ask is in the form the case expects and not in the other: docked means the card is visible and there is no header ask row; header means the row is visible and there is no card. */
export async function expectAskPlacement(page: Page, form: AskForm) {
	const card = page.locator('[data-ask-card]'), row = page.locator('.review-bar.ask');
	if (form === 'docked') {
		await expect(card, 'a phone docks the ask as a card on the canvas').toBeVisible();
		await expect(row, 'a phone has no ask row in the header').toHaveCount(0);
	} else {
		await expect(row, 'a larger screen shows the ask as a row in the header').toBeVisible();
		await expect(card, 'a larger screen has no docked ask card').toHaveCount(0);
	}
}
