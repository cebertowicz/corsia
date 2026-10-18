
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	captureConsoleErrors,
	captureFailedRequests,
	createSession,
	openAppPastLanding,
	saveEvidenceScreenshot,
	TEST_SESSION_TOKEN,
	TEST_TOKEN,
	waitForWsConnected,
} from '../support/corsia';
import { focusSetting } from '../support/focusOutcomeProof';
import { setFocusMode } from '../support/focusMode';
import { seedMockResponses } from '../support/mock';

const BACKEND_PORT = Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110);
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const FLOWS_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(FLOWS_DIR, '../../..');
const PYTHON = path.join(REPO_ROOT, '.venv/bin/python');
const DELEGATE_CLI = path.join(REPO_ROOT, 'backend/scripts/delegate_cli.py');

test.describe('@flow:delegation @surface:chat agent message attribution renders', () => {
	// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
	test.afterEach(async ({ request }) => {
		await setFocusMode(request, true);
	});

	test('delegation send with attribution renders its sender chip', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		const failedApiRequests = captureFailedRequests(page, (url) =>
			url.includes('/api/threads') || url.includes('/api/delegation'),
		);

		const senderTitle = `e2e attribution sender ${Date.now()}`;
		const targetTitle = `e2e attribution target ${Date.now()}`;
		const senderId = await createSession(request, senderTitle);
		const targetId = await createSession(request, targetTitle);

		await seedMockResponses(request, [{ content: 'ok', finish_reason: 'stop' }]);

		const sendResp = await request.post(
			`${BACKEND_URL}/api/delegation/threads/${targetId}/send?token=${encodeURIComponent(TEST_TOKEN)}`,
			{
				data: {
					message: [
						'**run the build** [docs](https://example.com)',
						'',
						'Paragraph content stays inside the message bubble.',
						'',
						'- unordered item one',
						'- unordered item two',
						'',
						'1. ordered item one',
						'2. ordered item two',
						'3. ordered item three',
						'4. ordered item four',
						'5. ordered item five',
						'6. ordered item six',
						'7. ordered item seven',
						'8. ordered item eight',
						'9. ordered item nine',
						'10. ordered item ten',
						'',
						String.fromCharCode(96).repeat(3) + 'ts',
						'const answer = 42;',
						String.fromCharCode(96).repeat(3),
					].join('\n'),
					from_thread_id: senderId,
					from_title: senderTitle,
				},
			},
		);
		expect(sendResp.ok()).toBeTruthy();

		await openAppPastLanding(page);
		// Focus is on by default and hides finished work; this spec reads the raw transcript.
		await focusSetting(page, false);

		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		await page.locator('.thread-item').filter({ hasText: targetTitle }).click({ timeout: 10_000 });
		await waitForWsConnected(page);


		const chip = page.locator('button.agent-attr').first();
		await expect(chip).toBeVisible({ timeout: 10_000 });
		await expect(chip).toContainText(`From ${senderTitle}`);
		const agentRow = page.locator('.message.user.agent-inbound').first();
		await expect(agentRow).toBeVisible({ timeout: 10_000 });
		const markdown = agentRow.locator('.bubble-content.md-content');
			await expect(markdown).toBeVisible();
			await expect(markdown.locator('p', { hasText: 'Paragraph content stays inside the message bubble.' })).toHaveCount(1);
			await expect(markdown.locator('ul li')).toHaveCount(2);
			await expect(markdown.locator('ol li')).toHaveCount(10);
			await expect(markdown.locator('ol li').nth(9)).toHaveText('ordered item ten');
			await expect(markdown.locator('.code-block')).toBeVisible();
			await expect(markdown.locator('strong')).toHaveText('run the build');
		await expect(markdown.locator('a')).toHaveAttribute('href', 'https://example.com');

			const containment = await markdown.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				const viewportWidth = window.innerWidth;
				const children = Array.from(element.querySelectorAll('p, ul, ol, .code-block')).map((child) => {
					const childRect = child.getBoundingClientRect();
					return { left: childRect.left, right: childRect.right };
				});
				return {
					left: rect.left,
					right: rect.right,
					clientWidth: element.clientWidth,
					scrollWidth: element.scrollWidth,
					viewportWidth,
					children,
				};
			});
			expect(containment.left).toBeGreaterThanOrEqual(0);
			expect(containment.right).toBeLessThanOrEqual(containment.viewportWidth + 1);
			expect(containment.scrollWidth).toBeLessThanOrEqual(containment.clientWidth + 1);
			for (const child of containment.children) {
				expect(child.left).toBeGreaterThanOrEqual(containment.left - 1);
				expect(child.right).toBeLessThanOrEqual(containment.viewportWidth + 1);
			}


		await expect(chip.locator('.attr-glyph')).toHaveText('▸');

		await chip.click();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await expect(
			page.locator('.thread-item.active'),
		).toContainText(senderTitle, { timeout: 10_000 });

		await expect(page.locator('.toast.toast--error')).toHaveCount(0);
		expect(consoleErrors).toEqual([]);
		expect(failedApiRequests).toEqual([]);

		await saveEvidenceScreenshot(page, testInfo, 'agent-msg-attribution-renders');
	});

	test('delegate CLI self-id renders receiver attribution and sender breadcrumb', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		const failedApiRequests = captureFailedRequests(page, (url) =>
			url.includes('/api/threads') || url.includes('/api/delegation'),
		);

		const senderTitle = `e2e cli sender ${Date.now()}`;
		const targetTitle = `e2e cli target ${Date.now()}`;
		const senderId = await createSession(request, senderTitle);
		const targetId = await createSession(request, targetTitle);

		await seedMockResponses(request, [{ content: 'ok', finish_reason: 'stop' }]);

		execFileSync(
			PYTHON,
			[
				DELEGATE_CLI,
				'--token',
				TEST_TOKEN,
				'--base',
				BACKEND_URL,
				'send',
				targetId,
				'run the cli-shaped send',
			],
			{
				cwd: REPO_ROOT,
				encoding: 'utf8',
				env: { ...process.env, CORSIA_THREAD_ID: senderId },
			},
		);

		await openAppPastLanding(page);
		await focusSetting(page, false);

		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		await page.locator('.thread-item').filter({ hasText: targetTitle }).click({ timeout: 10_000 });
		await waitForWsConnected(page);

		const attrChip = page.locator('button.agent-attr').first();
		await expect(attrChip).toBeVisible({ timeout: 10_000 });
		await expect(attrChip).toContainText(`From ${senderTitle}`);
		await expect(page.locator('.message.user.agent-inbound').first()).toBeVisible({ timeout: 10_000 });

		await attrChip.click();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await expect(page.locator('.thread-item.active')).toContainText(senderTitle, { timeout: 10_000 });

		const sendChip = page.locator('button.send-chip').first();
		await expect(sendChip).toBeVisible({ timeout: 10_000 });
		await expect(sendChip).toContainText('Sent');
		await expect(sendChip).toContainText(targetTitle);

		await expect(page.locator('.toast.toast--error')).toHaveCount(0);
		expect(consoleErrors).toEqual([]);
		expect(failedApiRequests).toEqual([]);

		await saveEvidenceScreenshot(page, testInfo, 'agent-msg-attribution-cli-shaped');
	});
});
