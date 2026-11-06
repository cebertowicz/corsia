import { expect, test } from './support/test';
import {
	chatComposer, createSession, openAppPastLanding, sendButton, sidebarThread,
	TEST_TOKEN, waitForWsConnected,
} from './support/corsia';
import { seedMockResponses } from './support/mock';
import { setFocusMode } from './support/focusMode';

const TITLE = 'Reconnect resend';
const SENTENCE = 'Ship the reconnect build tonight';
const REPLY = 'Shipping it now.';

/**
 * A send whose frame the socket never carried is delivered after the reconnect.
 *
 * The person typed one message; the half-dead socket swallowed its frame and
 * then closed. The new socket carries the same frame again; the server stores
 * nothing under its id, so it admits it. Two things have to come true at once: the
 * message lands exactly once, and its row stops waiting for a send that already
 * happened.
 */
test.describe('a chat send the socket swallowed', () => {
	test.describe.configure({ timeout: 180_000 });
	test.use({ seedPerTest: true, viewport: { width: 1440, height: 900 } });
	// Focus hides finished work, and this journey reads the reply it produced.
	test.beforeEach(async ({ request }) => { await setFocusMode(request, false); });
	test.afterEach(async ({ request }) => { await setFocusMode(request, true); });

	test('replays the lost frame once on the new socket', async ({ page, request }) => {
		test.skip(test.info().project.name !== 'chromium-desktop', 'One composed desktop journey.');
		let swallowed = false;
		await page.routeWebSocket(/\/ws(\?|$)/, socket => {
			const server = socket.connectToServer();
			server.onMessage(fromServer => socket.send(fromServer));
			socket.onMessage(fromClient => {
				if (!swallowed && typeof fromClient === 'string' && fromClient.includes(SENTENCE)) {
					// Half-dead connection: the frame leaves the client and never
					// reaches the server, and the socket closes right behind it.
					swallowed = true;
					socket.close();
					return;
				}
				server.send(fromClient);
			});
		});

		const thread = await createSession(request, TITLE);
		await openAppPastLanding(page);
		await sidebarThread(page, TITLE).click();
		await waitForWsConnected(page);
		await seedMockResponses(request, [{ content: REPLY, finish_reason: 'stop' }]);

		await chatComposer(page).fill(SENTENCE);
		await sendButton(page).click();
		await expect.poll(() => swallowed, { timeout: 30_000 }).toBe(true);

		// The row is answered: the reply the person sent that message for arrives.
		await expect(page.locator('.messages').getByText(REPLY, { exact: true }))
			.toBeVisible({ timeout: 60_000 });
		// And the message itself left the waiting region: nothing about it is
		// still claims-a-send, which is what "Sending forever" looked like.
		await expect(
			page.locator('.pending-input-region').getByText(SENTENCE, { exact: true }),
		).toHaveCount(0);

		const history = await request.get(`/api/threads/${thread}/messages?token=${TEST_TOKEN}`);
		expect(history.ok()).toBeTruthy();
		const stored = (await history.json()).messages.filter(
			(message: { role: string; content: string }) =>
				message.role === 'user' && message.content === SENTENCE,
		);
		expect(stored).toHaveLength(1);
	});
});
