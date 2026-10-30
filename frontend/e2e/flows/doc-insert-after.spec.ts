import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import { showAllThreads } from '../support/commentDrawer';
import { layouts } from '../support/liveConcurrency';

const text = '# About you\n\n# Work\n\n# Preferences\n';
const facts = [
	{ quote: '# About you', replacement: '\nFirst fact', body: 'First fact', insert_after: true },
	{ quote: '# About you', replacement: '\nSecond fact', body: 'Second fact', insert_after: true },
	{ quote: '# Work', replacement: '\nThird fact', body: 'Third fact', insert_after: true },
	{ quote: '# Work', replacement: '\nFourth fact', body: 'Fourth fact', insert_after: true },
	{ quote: '# Preferences', replacement: '\nFifth fact', body: 'Fifth fact', insert_after: true },
];

for (const [width, height, project] of layouts) {
	test(`insert-after suggestions stay independent at ${width}`, async ({ page, request }, info) => {
		test.skip(info.project.name !== project);
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height });
		await page.emulateMedia({ colorScheme: 'light' });
		const { drawer, ids, row, accept, reject, storedText } = await openSuggestionDocument(page, request, text, facts);
		for (const id of ids) {
			await expect(page.locator(`.cm-suggestion-insert[data-suggestion-id="${id}"]`)).toHaveCount(1);
			await expect(page.locator(`.cm-suggestion-del[data-suggestion-id="${id}"]`)).toHaveCount(0);
		}
		await page.getByRole('button', { name: /^\d+ open threads?\b/ }).click();
		await showAllThreads(drawer);
		await page.screenshot({ path: process.env.CORSIA_INSERT_FRAMES ? path.join(process.env.CORSIA_INSERT_FRAMES, `insert-open-${width}.png`) : info.outputPath(`insert-open-${width}.png`) });
		await accept(ids[0]).click();
		await accept(ids[1]).click();
		await reject(ids[2]).click();
		await expect(row(ids[2]).getByRole('region', { name: /^rejected suggestion by / })).toBeVisible();
		await expect.poll(storedText).toBe('# About you\nFirst fact\nSecond fact\n\n# Work\n\n# Preferences\n');
		await page.screenshot({ path: process.env.CORSIA_INSERT_FRAMES ? path.join(process.env.CORSIA_INSERT_FRAMES, `insert-decided-${width}.png`) : info.outputPath(`insert-decided-${width}.png`) });
	});
}
