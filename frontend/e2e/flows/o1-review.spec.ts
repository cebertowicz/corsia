import { test } from '../support/test';
import { setDebugVersionOverlay, isolatedDb } from '../support/heldComments';
import { seed } from '../contact-sheet/document-owner-supervisor';

test('open ownership design for review', async ({ page, request }) => {
	test.setTimeout(1_800_000);
	await setDebugVersionOverlay(request, false);
	isolatedDb(['connection.execute("UPDATE models SET display_name=? WHERE id=?", ("Claude Sonnet 5.5", "mock-test-model"))']);
	await seed(page, request);
	await page.pause();
});
