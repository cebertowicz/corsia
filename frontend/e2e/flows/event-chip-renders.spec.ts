
import { expect, test } from '@playwright/test';
import {
    captureConsoleErrors,
    createSession,
    TEST_TOKEN,
    waitForWsConnected,
    watchSessionSubscriptions,
} from '../support/corsia';
import { injectMessage } from '../support/mock';

test.describe('@flow:event-chip @surface:chat event_chip renders in conversation view', () => {
    test(
        'injected event_chip message renders as .ev-chip with summary and link',
        async ({ page, request }, testInfo) => {
            const consoleErrors = captureConsoleErrors(page);
            const subscriptions = watchSessionSubscriptions(page);

            const sessionTitle = `e2e event-chip-renders ${Date.now()}`;
            const sessionId = await createSession(request, sessionTitle);

            await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
            await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({
                timeout: 20_000,
            });

            if ((page.viewportSize()?.width ?? 0) <= 768) {
                await page.getByRole('button', { name: 'Toggle sidebar' }).click();
            }

            await page.getByText(sessionTitle).click({ timeout: 10_000 });
            await waitForWsConnected(page);
            await subscriptions.waitForSubscribed(sessionId);

            await injectMessage(
                request,
                sessionId,
                {
                    role: 'system',
                    content: 'o/r#42 opened: Fix the thing',
                    metadata: {
                        type: 'event_chip',
                        event_type: 'webhook.github.pr_opened',
                        summary: 'o/r#42 opened: Fix the thing',
                        url: 'https://github.com/o/r/pull/42',
                    },
                },
                TEST_TOKEN,
            );

            const chip = page.locator('.ev-chip').first();
            await expect(chip).toBeVisible({ timeout: 10_000 });

            await expect(chip.locator('.ev-summary')).toContainText('o/r#42 opened');

            await expect(chip).toHaveAttribute('href', 'https://github.com/o/r/pull/42');
            await expect(chip.locator('.ev-link')).toBeVisible();

            const messageList = page.locator('.messages, [data-testid="message-list"]').first();
            await expect(messageList).not.toContainText('"event_type"', { timeout: 3_000 });

            expect(consoleErrors).toEqual([]);

            await page.screenshot({
                path: testInfo.outputPath('event-chip-renders.png'),
                fullPage: true,
                animations: 'disabled',
            });
        },
    );
});
