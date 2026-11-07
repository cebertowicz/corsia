import { test } from '../support/test';
import { expect } from '@playwright/test';
import { openSuggestionDocument } from '../support/docSuggestions';

/**
 * A suggestion drawn in a table cell must not change how the table shares its width: the cell shows the struck text and, under it,
 * the replacement (no wider than the old text here: a wider one grows its own column by the difference), so the neighbouring Notes column keeps the width and the height it has without the replacement drawn.
 */

const NOTES = 'Waiting on county permit before the crew can start, and the culvert order is still not confirmed with the supplier, so the second week of April is the earliest realistic start.';
const TEXT = `# Trail status\n\n| Section | Lead | Status | Notes |\n| --- | --- | --- | --- |\n| Trailhead signs | Dana | In progress | Replace the faded north sign |\n| Drainage | Marcus | Blocked | ${NOTES} |\n| Bridge | Priya | Done | Decking inspected |\n`;
const ITEMS = [
	{ quote: 'In progress', replacement: 'Under way', body: 'Match the board report.' },
	{ quote: 'Blocked', replacement: 'On hold', body: 'Say what blocks it.' },
];

test('@flow:doc-cell-suggestions a suggestion in a table cell leaves the Notes column its width and height', async ({ page, request }, info) => {
	test.setTimeout(120_000);
	await page.setViewportSize(info.project.name.includes('mobile') ? { width: 393, height: 852 } : { width: 1440, height: 900 });
	await openSuggestionDocument(page, request, TEXT, ITEMS, { splitWidth: 540 });
	const inserts = page.locator('td .cm-suggestion-insert');
	await expect(inserts).toHaveCount(2);
	const notes = () => page.evaluate(() => {
		const cells = [...document.querySelectorAll<HTMLElement>('.cm-content table tbody td:nth-child(4)')];
		return cells.map((cell) => { const r = cell.getBoundingClientRect(); return { left: Math.round(r.left), width: Math.round(r.width), height: Math.round(r.height) }; });
	});
	const withInserts = await notes();
	await page.addStyleTag({ content: 'td .cm-suggestion-insert { display: none !important; }' });
	await expect(page.locator('td .cm-suggestion-insert').first()).toBeHidden();
	const without = await notes();
	expect(withInserts.length).toBe(3);
	// The long Notes cell (second row) keeps its width and so its height; the cell holding the replacement may grow by its own lines only.
	expect(withInserts[1].left).toBe(without[1].left);
	expect(withInserts[1].width).toBe(without[1].width);
	expect(withInserts[1].height).toBe(without[1].height);
	expect(withInserts[2]).toEqual(without[2]);
});
