import { test, expect } from '@playwright/test';
import { longPressOn } from '../support/tableCells';

test('plain browser touch selection control', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile');
	await page.setContent('<div contenteditable="true" style="font: 20px sans-serif; padding: 40px">a long word here</div>');
	const editable = page.locator('[contenteditable]');
	await editable.tap();
	await longPressOn(editable, 'long');
	await expect.poll(() => page.evaluate(() => String(getSelection()))).toBe('long');
});
