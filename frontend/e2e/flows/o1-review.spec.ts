import { test } from '../support/test';
import { setDebugVersionOverlay } from '../support/heldComments';
import { seed } from '../contact-sheet/document-owner-supervisor';

test('open ownership design for review', async ({ page, request }) => {
	test.setTimeout(1_800_000);
	await setDebugVersionOverlay(request, false);
	await seed(page, request);
	await page.pause();
});
