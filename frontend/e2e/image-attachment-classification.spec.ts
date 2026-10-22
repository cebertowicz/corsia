import { test } from './support/test';
import { expect } from '@playwright/test';
import {
	chatComposer,
	createSession,
	openAppByUrl,
	saveEvidenceScreenshot,
	sendButton,
	TEST_TOKEN,
	watchSessionSubscriptions,
} from './support/corsia';
import { seedMockResponses } from './support/mock';

test.describe('@surface:chat image attachment classification', () => {
	test('keeps unsupported previews image-typed from composer through transcript', async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');

		await page.route('**/api/models*', async (route) => {
			const response = await route.fetch();
			const body = await response.json();
			body.models = body.models.map((model: { capabilities?: { supports_vision?: boolean } }) => ({
				...model,
				capabilities: model.capabilities
					? { ...model.capabilities, supports_vision: false }
					: model.capabilities,
			}));
			await route.fulfill({ response, json: body });
		});

		const title = `Image attachment ${testInfo.project.name} ${Date.now()}`;
		const threadId = await createSession(request, title);
		const subscriptions = watchSessionSubscriptions(page);
		await openAppByUrl(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.getByText(title, { exact: true }).first().click();
		await subscriptions.waitForSubscribed(threadId);

		await page.locator('input[data-composer-attachment-input]').setInputFiles({
			name: 'photo.tiff',
			mimeType: 'image/tiff',
			buffer: Buffer.from(
				'SUkqAAgAAAAKAAABBAABAAAAAgAAAAEBBAABAAAAAgAAAAIBAwADAAAAhgAAAAMBAwABAAAAAQAAAAYBAwABAAAAAgAAABEBBAABAAAAjAAAABUBAwABAAAAAwAAABYBBAABAAAAAgAAABcBBAABAAAADAAAABwBAwABAAAAAQAAAAAAAAAIAAgACAD/AAD/AAD/AAD/AAA=',
				'base64',
			),
		});

		const composerChip = page.locator('.attachment-chip').filter({ hasText: 'photo.tiff' });
		await expect(composerChip.getByRole('img', { name: 'Image attachment' })).toBeVisible();
		await expect(composerChip.locator('.attachment-thumb')).toHaveCount(0);
		await expect(page.getByText("doesn't support images", { exact: false })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'image-attachment-composer-fallback');

		await seedMockResponses(request, [{ content: 'Attachment received' }]);
		await chatComposer(page).fill('Describe this image');
		await sendButton(page).click();

		const transcript = page.locator('.messages');
		const sentAttachment = transcript
			.locator('.bubble-image[aria-label="Image attachment"]')
			.filter({ hasText: 'photo.tiff' });
		await expect(sentAttachment).toBeVisible();
		await expect(transcript.locator('.bubble-img')).toHaveCount(0);
		await expect(transcript.getByText('Describe this image', { exact: true })).toBeVisible();
		await expect(transcript.getByText('Not delivered:', { exact: false })).toHaveCount(0);
		await expect.poll(async () => {
			const response = await request.get(`/api/threads/${threadId}/messages?token=${TEST_TOKEN}`);
			const body = await response.json();
			return body.messages.some(
				(message: { role?: string; content?: string }) =>
					message.role === 'assistant' && message.content === 'Attachment received',
			);
		}, { timeout: 15_000 }).toBe(true);
		await saveEvidenceScreenshot(page, testInfo, 'image-attachment-transcript-fallback');
	});
});
