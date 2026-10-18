
import { expect, type Locator, test } from '@playwright/test';
import { captureConsoleErrors, createSession, openAppPastLanding, TEST_TOKEN, waitForWsConnected, sidebarThread } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

const LONG_TEXT = Array.from(
    { length: 26 },
    (_, i) => `Line ${i + 1}: ${'readability '.repeat(8)}`,
).join('\n');

async function expectCollapsedThenExpand(row: Locator) {
    const body = row.locator('.long-message-body').first();
    const toggle = row.getByRole('button', { name: 'Show more' }).first();

    await expect(body).toHaveClass(/collapsed/, { timeout: 10_000 });
    await expect(toggle).toBeVisible();

    await toggle.click();
    await expect(row.getByRole('button', { name: 'Show less' })).toBeVisible();
    await expect(body).not.toHaveClass(/collapsed/);
}

test.describe('@flow:long-message-collapse @surface:chat long message readability', () => {
    // Focus is on by default and groups these rows; this spec covers the ordinary row presentation.
    test.afterEach(async ({ request }) => {
        await setFocusMode(request, true);
    });

    test(
        'injected long ordinary rows collapse and expand in chat',
        async ({ page, request }, testInfo) => {
            const consoleErrors = captureConsoleErrors(page);

            const sessionTitle = `e2e long-message-collapse ${Date.now()}`;
            const sessionId = await createSession(request, sessionTitle);
            await setFocusMode(request, false);

            await injectMessage(
                request,
                sessionId,
                { role: 'assistant', content: LONG_TEXT, metadata: {} },
                TEST_TOKEN,
            );
            await injectMessage(
                request,
                sessionId,
                {
                    role: 'user',
                    content: LONG_TEXT,
                    metadata: {
                        sender_kind: 'agent',
                        from_thread: 'sender-thread',
                        from_title: 'Coordinator',
                    },
                },
                TEST_TOKEN,
            );
            await injectMessage(
                request,
                sessionId,
                {
                    role: 'system',
                    content: LONG_TEXT,
                    metadata: {
                        // SystemMessageRow.svelte renders EventChip for `activity_receipt` and `context_event`; `event_chip` is retired.
                        type: 'activity_receipt',
                        event_type: 'webhook.github.pr_comment',
                        summary: LONG_TEXT,
                        url: 'https://github.com/o/r/pull/42#issuecomment-1',
                    },
                },
                TEST_TOKEN,
            );

            await openAppPastLanding(page);

            if ((page.viewportSize()?.width ?? 0) <= 768) {
                await page.getByRole('button', { name: 'Toggle sidebar' }).click();
            }

            await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
            await waitForWsConnected(page);

            const assistantRow = page.locator('.message.assistant').first();
            await expect(assistantRow.locator('.long-message-body')).not.toHaveClass(/collapsed/);
            await expect(assistantRow.getByRole('button', { name: 'Show more' })).toHaveCount(0);

            const agentRow = page.locator('.message.user').filter({ has: page.locator('.agent-attr') }).first();
            await expect(agentRow.locator('.agent-attr')).toContainText('Coordinator');
            await expectCollapsedThenExpand(agentRow);

            const eventRow = page.locator('.message.system').filter({ has: page.locator('.ev-chip') }).first();
            await expect(eventRow.locator('a.ev-chip')).toHaveAttribute(
                'href',
                'https://github.com/o/r/pull/42#issuecomment-1',
            );
            await expectCollapsedThenExpand(eventRow);

            expect(consoleErrors).toEqual([]);

            await page.screenshot({
                path: testInfo.outputPath('long-message-collapse.png'),
                fullPage: true,
                animations: 'disabled',
            });
        },
    );
});
