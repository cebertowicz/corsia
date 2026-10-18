import { expect, test, type Page } from '@playwright/test';
import {
	assistantMessages,
	captureConsoleErrors,
	createSession,
	saveEvidenceScreenshot,
	sendChatMessage,
	TEST_TOKEN,
	userMessages,
	waitForWsConnected,
} from '../support/corsia';
import { releaseMockGate, seedMockResponses } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

type MessagesAddedReceipt = {
	sessionId: string;
	messages: Array<Record<string, unknown>>;
};

function watchMessagesAdded(page: Page): MessagesAddedReceipt[] {
	const receipts: MessagesAddedReceipt[] = [];
	page.on('websocket', (ws) => {
		ws.on('framereceived', (frame) => {
			let data: { type?: unknown; thread_id?: unknown; messages?: unknown };
			try {
				data = JSON.parse(String(frame.payload));
			} catch {
				return;
			}
			if (data.type !== 'messages_added' || typeof data.thread_id !== 'string' || !Array.isArray(data.messages)) {
				return;
			}
			receipts.push({
				sessionId: data.thread_id,
				messages: data.messages.filter(
					(message): message is Record<string, unknown> => typeof message === 'object' && message !== null,
				),
			});
		});
	});
	return receipts;
}

test.describe('@flow:chat @surface:chat same-boundary mid-turn Send', () => {
	// Focus is on by default and replaces the live reasoning header; this spec covers the ordinary live reply.
	test.afterEach(async ({ request }) => {
		await setFocusMode(request, true);
	});

	test('batches B and C after A while waiting input stays below the live reply', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		const sessionTitle = `e2e same-boundary batch ${Date.now()}`;
		const completionGate = `wave-h-turn-a-${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);
		await setFocusMode(request, false);
		const messagesAddedReceipts = watchMessagesAdded(page);

		await page.goto(`/?token=${TEST_TOKEN}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-item').filter({ hasText: sessionTitle }).click({ timeout: 10_000 });
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
		await waitForWsConnected(page);

		await seedMockResponses(request, [
			{
				content: 'DONE A',
				reasoning_content: 'Let me count slowly to ten...',
				completion_gate: completionGate,
				finish_reason: 'stop',
			},
			{
				content: 'DONE B+C',
				finish_reason: 'stop',
			},
		]);

		const messageA = 'Count slowly to ten in your head, then say "DONE A".';
		const messageB = 'After A, say "DONE B".';
		const messageC = 'In that same successor response, also say "DONE C".';

		await sendChatMessage(page, messageA);
		await expect(userMessages(page)).toHaveCount(1, { timeout: 10_000 });
		await expect(page.locator('.live-reasoning-header span')).toHaveText('Thinking...', { timeout: 10_000 });

		await sendChatMessage(page, messageB);
		await expect(userMessages(page)).toHaveCount(2);
		await sendChatMessage(page, messageC);
		await expect(userMessages(page)).toHaveCount(3);
		const pendingRegion = page.locator('.messages > .pending-input-region');
		const pendingRows = pendingRegion.locator('.message.user');
		await expect(pendingRegion).toHaveCount(1, { timeout: 10_000 });
		await expect(pendingRows).toHaveCount(2);
		await expect(pendingRows.nth(0)).toContainText(messageB);
		await expect(pendingRows.nth(1)).toContainText(messageC);
		await expect(pendingRows.nth(0).locator('.delivery-status')).toHaveText('Sending when ready…');
		await expect(pendingRows.nth(1).locator('.delivery-status')).toHaveText('Sending when ready…');
		await expect(page.locator('.live-reasoning-header span')).toHaveText('Thinking...');

		const liveActivity = page.locator('.messages > .message.assistant:has(.live-reasoning-header)');
		// Waiting inputs have no transcript position, so they render after the
		// delivered transcript and its live output (#1314): a message sent
		// mid-turn waits below the reply being written — where delivery will
		// land it (#1087) — instead of above the in-flight output.
		expect(await liveActivity.evaluate(activity => {
			const region = activity.parentElement?.querySelector('.pending-input-region');
			if (!region) return false;
			return Boolean(activity.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING);
		})).toBe(true);
		expect(await pendingRegion.evaluate(region => getComputedStyle(region).backgroundImage)).toBe('none');
		const bubblePaint = await pendingRows.nth(0).locator('.user-bubble').evaluate(bubble => {
			const accentProbe = document.createElement('div');
			accentProbe.style.backgroundColor = 'var(--accent)';
			document.body.appendChild(accentProbe);
			const accentColor = getComputedStyle(accentProbe).backgroundColor;
			accentProbe.remove();
			return {
				backgroundImage: getComputedStyle(bubble).backgroundImage,
				backgroundColor: getComputedStyle(bubble).backgroundColor,
				accentColor,
			};
		});
		expect(bubblePaint.backgroundImage).toBe('none');
		expect(bubblePaint.backgroundColor).toBe(bubblePaint.accentColor);
		expect(await page.locator('.messages').evaluate(messages => {
			const live = messages.querySelector('.live-reasoning-header')?.closest('.message.assistant');
			const pending = messages.querySelector('.pending-input-region');
			if (!live || !pending) return false;
			return [...messages.querySelectorAll('.message.user')].every(userMessage =>
				pending.contains(userMessage)
					// A waiting row renders below the live reply it will follow.
					? Boolean(live.compareDocumentPosition(userMessage) & Node.DOCUMENT_POSITION_FOLLOWING)
					// A delivered row keeps its sequence position above it.
					: Boolean(userMessage.compareDocumentPosition(live) & Node.DOCUMENT_POSITION_FOLLOWING),
			);
		})).toBe(true);
		await expect(pendingRegion.locator('.message.assistant')).toHaveCount(0);

		await page.evaluate(() => {
			const messages = document.querySelector('.messages');
			if (!messages) throw new Error('Missing messages container');
			const pendingCounts = [messages.querySelectorAll('.pending-input-region .message.user').length];
			const observer = new MutationObserver(() => {
				const pendingCount = messages.querySelectorAll('.pending-input-region .message.user').length;
				if (pendingCounts.at(-1) !== pendingCount) pendingCounts.push(pendingCount);
			});
			observer.observe(messages, { childList: true, subtree: true });
			(window as typeof window & {
				__waveHPendingCounts?: number[];
				__waveHPendingObserver?: MutationObserver;
			}).__waveHPendingCounts = pendingCounts;
			(window as typeof window & {
				__waveHPendingObserver?: MutationObserver;
			}).__waveHPendingObserver = observer;
		});

		// The batch publishes twice: once claimed (queued, no sequence), once
		// settled (delivered, sequenced). The durable-order proof is the
		// settled receipt, so that is the one this must pin.
		// A passive context digest rides in the next batch as its own row when a context source changed
		// since the thread last saw it, so the pin is on B and C, which must stay adjacent and in order.
		const isDigest = (message: Record<string, unknown>) => String(message.content).startsWith('[PASSIVE CONTEXT DIGEST]');
		const matchingReceipts = () => messagesAddedReceipts.filter(receipt => {
			const inputs = receipt.messages.filter(message => !isDigest(message));
			return receipt.sessionId === sessionId
				&& inputs.length === 2
				&& inputs[0]?.content === messageB
				&& inputs[1]?.content === messageC
				&& receipt.messages.every(message => message.delivery_state === 'delivered');
		});
		await releaseMockGate(request, completionGate);
		try {
			await expect.poll(matchingReceipts).toHaveLength(1);
		} catch (error) {
			// Name every batch this page received, so a missing settled receipt shows what arrived instead.
			const seen = messagesAddedReceipts
				.filter(receipt => receipt.sessionId === sessionId)
				.map(receipt => receipt.messages.map(message => `${String(message.content).slice(0, 24)}:${String(message.delivery_state)}:${String(message.seq)}`));
			throw new Error(`${(error as Error).message}\nmessages_added batches received for this session: ${JSON.stringify(seen)}`);
		}
		const [batchReceipt] = matchingReceipts();
		if (!batchReceipt) throw new Error('Missing B/C messages_added receipt after release');
		const batchInputs = batchReceipt.messages.filter(message => !isDigest(message));
		expect(batchInputs.map(message => message.content)).toEqual([messageB, messageC]);
		const [receiptB, receiptC] = batchInputs;
		if (!receiptB || !receiptC) throw new Error('Expected two B/C messages_added rows');
		const bSeq = Number(receiptB.seq);
		const cSeq = Number(receiptC.seq);
		const bRevision = Number(receiptB.delivery_revision);
		const cRevision = Number(receiptC.delivery_revision);
		expect(bSeq).toBeGreaterThan(0);
		// B and C are adjacent in the order; the only rows that may sit between them are the batch's own digest rows.
		const positionB = batchReceipt.messages.indexOf(receiptB);
		const positionC = batchReceipt.messages.indexOf(receiptC);
		const between = batchReceipt.messages.slice(positionB + 1, positionC);
		expect(between.every(isDigest)).toBe(true);
		expect(cSeq).toBe(bSeq + 1 + between.length);
		expect(bRevision).toBeGreaterThan(0);
		expect(cRevision).toBe(bRevision);

		await expect(assistantMessages(page).filter({ hasText: 'DONE A' })).toHaveCount(1, { timeout: 15_000 });
		await expect(assistantMessages(page).filter({ hasText: 'DONE B+C' })).toHaveCount(1, { timeout: 15_000 });
		await expect(assistantMessages(page)).toHaveCount(2);
		await expect(pendingRegion).toHaveCount(0);
		await expect(page.locator('.delivery-status')).toHaveCount(0);
		await page.waitForTimeout(250);
		await expect(assistantMessages(page)).toHaveCount(2);

		const pendingCounts = await page.evaluate(() => {
			const state = window as typeof window & {
				__waveHPendingCounts?: number[];
				__waveHPendingObserver?: MutationObserver;
			};
			state.__waveHPendingObserver?.disconnect();
			return state.__waveHPendingCounts;
		});
		expect(pendingCounts).toEqual([2, 0]);

		await page.reload();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-item').filter({ hasText: sessionTitle }).click({ timeout: 10_000 });
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
		await waitForWsConnected(page);
		// The compact context-event chip renders as its own system row; the
		// durable conversation it proves is the five user/assistant rows.
		await expect(page.locator('.messages > .message:not(.system)')).toHaveCount(5);
		await expect(assistantMessages(page)).toHaveCount(2);

		const durableRows = await page.locator('.messages > .message:not(.system)').evaluateAll(rows => rows.map(row => ({
			role: row.classList.contains('assistant') ? 'assistant' : 'user',
			text: row.textContent ?? '',
		})));
		const durableOrder = [
			durableRows.findIndex(row => row.role === 'user' && row.text.includes(messageA)),
			durableRows.findIndex(row => row.role === 'assistant' && row.text.includes('DONE A')),
			durableRows.findIndex(row => row.role === 'user' && row.text.includes(messageB)),
			durableRows.findIndex(row => row.role === 'user' && row.text.includes(messageC)),
			durableRows.findIndex(row => row.role === 'assistant' && row.text.includes('DONE B+C')),
		];
		expect(durableOrder.every(index => index >= 0)).toBe(true);
		expect([...new Set(durableOrder)]).toHaveLength(5);
		expect(durableOrder).toEqual([...durableOrder].sort((left, right) => left - right));
		await expect(userMessages(page)).toHaveCount(3);
		await expect(userMessages(page).nth(0)).toContainText(messageA);
		await expect(userMessages(page).nth(1)).toContainText(messageB);
		await expect(userMessages(page).nth(2)).toContainText(messageC);

		await expect(page.getByText(/turn (cancelled|canceled)/i)).toHaveCount(0);
		await expect(page.locator('[role="alert"]:has-text("cancel")')).toHaveCount(0);
		expect(consoleErrors).toEqual([]);

		await saveEvidenceScreenshot(page, testInfo, 'mid-turn-cancel-and-replace');
	});
});
