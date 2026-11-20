import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { beginComment } from '../support/liveCommentProof';
import { at, composer, drag, openThread, seedImage } from '../support/imageRegionComments';

// A failed post says why in one place, decided as it fails: in its box when the box is on screen then (the retry legs
// in comment-retry-once and doc-reply-draft-kept prove that case), otherwise in a toast, with the reason kept so the
// box shows it when it opens again. These legs close the box while the post is still out.

const REASON = "Couldn't post the comment — try again";
const toast = (page: Page) => page.locator('.toast').filter({ hasText: REASON });
const reason = (page: Page) => page.locator('.composer [role="alert"]').filter({ hasText: REASON });

/** The first comment POST is held until `fail()`, which then fails it as a dropped connection; later ones go through. */
async function holdFirstPost(page: Page, artifactId: string) {
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	let posts = 0, seen!: () => void;
	const arrived = new Promise<void>((resolve) => { seen = resolve; });
	await page.route(`**/api/artifacts/${artifactId}/comments*`, async (route) => {
		if (route.request().method() !== 'POST' || posts++ > 0) return route.continue();
		seen();
		await held;
		return route.abort('failed');
	});
	return { arrived, fail: release };
}

const DOC = '# One place\n\nThe router decides where an inbound event goes.\n\nA second paragraph to keep the margin honest.\n';
const QUOTE = 'The router decides';

test.describe('@flow:document-comments a post that fails after its box closed says why in a toast', () => {
	test.beforeEach(({}, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(150_000);
	});

	test('document: closed while posting, a toast; the composer opened again says why', async ({ page, request }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const document = await seedLiveDocument(request, 'one-place-doc', DOC);
		await openLiveDocument(page, document);
		const wire = await holdFirstPost(page, document.artifactId);
		await beginComment(page, QUOTE, 'Name the fallback route');
		await page.locator('.composer').getByRole('button', { name: 'Comment', exact: true }).click();
		await wire.arrived;
		await composer(page).press('Escape');
		await expect(composer(page), 'the box is closed while the post is out').toHaveCount(0);
		wire.fail();
		await expect(toast(page), 'with no box on screen, the toast says why').toBeVisible({ timeout: 30_000 });
		await beginComment(page, QUOTE);
		await expect(composer(page), 'the text is kept').toHaveValue('Name the fallback route');
		await expect(reason(page), 'the box says why once it is open again').toBeVisible();
	});

	test('image: closed while posting, a toast; the box drawn again says why', async ({ page, request }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedImage(page, request, 'one-place-image');
		const wire = await holdFirstPost(page, seeded.artifactId);
		await openThread(page, seeded.sessionTitle, 1440);
		await drag(page, await at(page, 0.2, 0.25), await at(page, 0.45, 0.5));
		await composer(page).fill('The header is too tall');
		await page.keyboard.press('ControlOrMeta+Enter');
		await wire.arrived;
		await composer(page).press('Escape');
		await expect(composer(page), 'the box is closed while the post is out').toHaveCount(0);
		wire.fail();
		await expect(toast(page), 'with no box on screen, the toast says why').toBeVisible({ timeout: 30_000 });
		await drag(page, await at(page, 0.5, 0.55), await at(page, 0.7, 0.75));
		await expect(composer(page), 'the text is kept').toHaveValue('The header is too tall');
		await expect(reason(page), 'the box says why once it is open again').toBeVisible();
	});
});
