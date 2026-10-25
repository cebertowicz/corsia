import { test } from '../support/test';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { resetBackendToSeed } from '../support/backendSeed';
import {
	batchRows, deliveredBatches, DELIVER_IMMEDIATELY_KEY, heldIn, readDeliveryPreference, setDeliveryPreference, turnOffDocumentComments,
} from '../support/heldComments';
import { seedLiveDocument, openLiveDocument, typeAtDocumentEnd } from '../support/liveDocument';
import { beginComment, outsideClose, submitButton } from '../support/liveCommentProof';

// The "Deliver comments immediately" choice belongs to the person: the server keeps it, every open client follows it, and
// the document save path reads it where no client frame can carry the lane.

const mockOnly = (testInfo: TestInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
};

const toggle = (page: Page) => page.getByRole('button', { name: 'Deliver comments immediately', exact: true });

test('@surface:doc-delivery-preference a toggle on one client sets the lane of a comment posted from another', async ({ browser, request }, testInfo) => {
	mockOnly(testInfo);
	test.setTimeout(120_000);
	const stamp = Date.now();
	const quote = `shared line ${stamp}`;
	const document = await seedLiveDocument(request, `preference-${stamp}`, `# Preference ${stamp}\n\n${quote}\n\nsecond line ${stamp}\n`);
	const contexts = [await browser.newContext({ viewport: { width: 1440, height: 1000 } }), await browser.newContext({ viewport: { width: 1440, height: 1000 } })];
	try {
		const [clientA, clientB] = await Promise.all(contexts.map((context) => context.newPage()));
		await openLiveDocument(clientA, document);
		await openLiveDocument(clientB, document);
		await expect(toggle(clientB)).toHaveAttribute('aria-pressed', 'true');

		// Off on A reaches B with no reload; B's comment waits for a Send.
		await turnOffDocumentComments(clientA);
		await expect(toggle(clientB)).toHaveAttribute('aria-pressed', 'false');
		await beginComment(clientB, quote, 'Posted from B while A has delivery off.');
		await submitButton(clientB).click();
		await expect.poll(() => heldIn(document.artifactId, 'document-held')).toBe(1);
		expect(deliveredBatches(document.artifactId, 'document-held')).toBe(0);

		// On again from A: B's next comment goes in the immediate lane; the one already held keeps waiting for its Send.
		await toggle(clientA).click();
		await expect(toggle(clientA)).toHaveAttribute('aria-pressed', 'true');
		await expect(toggle(clientB)).toHaveAttribute('aria-pressed', 'true');
		await outsideClose(clientB);
		await expect(clientB.locator('[data-popover]')).toHaveCount(0);
		await beginComment(clientB, `second line ${stamp}`, 'Posted from B while A has delivery on.');
		await submitButton(clientB).click();
		await expect.poll(() => batchRows(document.artifactId).filter((row) => row.policy === 'immediate').reduce((sum, row) => sum + row.items, 0)).toBe(1);
		expect(heldIn(document.artifactId, 'document-held')).toBe(1);
		expect(await readDeliveryPreference(request)).toEqual({ immediate: true, stored: true });
	} finally {
		await Promise.all(contexts.map((context) => context.close()));
	}
});

test('@surface:doc-delivery-preference a person\'s save is held while delivery is off and immediate while it is on', async ({ page, request }, testInfo) => {
	mockOnly(testInfo);
	const stamp = Date.now();
	await setDeliveryPreference(request, false);
	const held = await seedLiveDocument(request, `edit-held-${stamp}`, `# Edit held ${stamp}\n\nbody line ${stamp}\n`);
	await openLiveDocument(page, held);
	await expect(toggle(page)).toHaveAttribute('aria-pressed', 'false');
	await typeAtDocumentEnd(page, ' edited with delivery off');
	await expect.poll(() => heldIn(held.artifactId, 'document-held')).toBe(1);
	expect(deliveredBatches(held.artifactId, 'document-held')).toBe(0);
	expect(batchRows(held.artifactId).filter((row) => row.policy === 'immediate')).toEqual([]);

	await setDeliveryPreference(request, true);
	const immediate = await seedLiveDocument(request, `edit-immediate-${stamp}`, `# Edit immediate ${stamp}\n\nbody line ${stamp}\n`);
	await openLiveDocument(page, immediate);
	await expect(toggle(page)).toHaveAttribute('aria-pressed', 'true');
	await typeAtDocumentEnd(page, ' edited with delivery on');
	await expect.poll(() => batchRows(immediate.artifactId).filter((row) => row.policy === 'immediate').length).toBeGreaterThan(0);
	expect(heldIn(immediate.artifactId, 'document-held')).toBe(0);
});

// Last in the file: it resets the backend to the seed so the server holds no value for the person.
test('@surface:doc-delivery-preference a browser that chose off before the server kept the choice hands it over once', async ({ page, request }, testInfo) => {
	mockOnly(testInfo);
	await resetBackendToSeed();
	expect(await readDeliveryPreference(request)).toEqual({ immediate: true, stored: false });
	const document = await seedLiveDocument(request, `carry-over-${Date.now()}`, `# Carry over\n\nbody line\n`);
	await page.addInitScript((key) => { if (!sessionStorage.getItem('carried')) { sessionStorage.setItem('carried', '1'); localStorage.setItem(key, 'false'); } }, DELIVER_IMMEDIATELY_KEY);
	await openLiveDocument(page, document);
	await expect(toggle(page)).toHaveAttribute('aria-pressed', 'false');
	await expect.poll(() => readDeliveryPreference(request)).toEqual({ immediate: false, stored: true });
	expect(await page.evaluate((key) => localStorage.getItem(key), DELIVER_IMMEDIATELY_KEY)).toBeNull();
});
