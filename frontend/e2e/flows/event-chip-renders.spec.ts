
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { captureConsoleErrors, createSession, openAppPastLanding, TEST_TOKEN, waitForWsConnected, watchSessionSubscriptions, sidebarThread } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

test.describe('@flow:event-chip @surface:chat event_chip renders in conversation view', () => {
    // Focus is on by default and shows an event only as a delivery line; this spec covers the full chip row.
    test.afterEach(async ({ request }) => {
        await setFocusMode(request, true);
    });

    test(
        'injected event_chip message renders as .ev-chip with summary and link',
        async ({ page, request }, testInfo) => {
            const consoleErrors = captureConsoleErrors(page);
            const subscriptions = watchSessionSubscriptions(page);

            const sessionTitle = `e2e event-chip-renders ${Date.now()}`;
            const sessionId = await createSession(request, sessionTitle);
            await setFocusMode(request, false);

            await openAppPastLanding(page);

            if ((page.viewportSize()?.width ?? 0) <= 768) {
                await page.getByRole('button', { name: 'Toggle sidebar' }).click();
            }

            await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
            await waitForWsConnected(page);
            await subscriptions.waitForSubscribed(sessionId);

            await injectMessage(
                request,
                sessionId,
                {
                    role: 'system',
                    content: 'o/r#42 opened: Fix the thing',
                    metadata: {
                        // SystemMessageRow.svelte renders EventChip for `activity_receipt` and `context_event`; `event_chip` is retired.
                        type: 'activity_receipt',
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
