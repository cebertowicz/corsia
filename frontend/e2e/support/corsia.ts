import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';

function requireTestToken(): string {
	const value = process.env.CORSIA_E2E_TOKEN;
	if (!value) {
		throw new Error(
			'CORSIA_E2E_TOKEN is not set. Seed a disposable test account and export its API token before running e2e.',
		);
	}
	return value;
}

function requireTestSessionToken(): string {
	const value = process.env.CORSIA_E2E_SESSION_TOKEN;
	if (!value) {
		throw new Error(
			'CORSIA_E2E_SESSION_TOKEN is not set. Seed a disposable signed-in session before running e2e.',
		);
	}
	return value;
}

export const TEST_TOKEN = requireTestToken();

export const TEST_SESSION_TOKEN = requireTestSessionToken();

export async function openAppByUrl(page: Page, { timeout = 20_000 }: { timeout?: number } = {}) {
	await page.goto(`/?token=${TEST_TOKEN}`);
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout });
}

export async function loginByUrl(
	page: Page,
	{ waitForComposer = true }: { waitForComposer?: boolean } = {},
) {
	await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
	if (waitForComposer) {
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.getByRole('button', { name: 'New chat' }).first().click();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	}
}

/**
 * Runs one page load and waits out the first-run landing it can trigger. A fresh
 * workspace answers /api/corsia-agent with first_run_pending, and on every load the
 * app then opens the conductor thread, which closes the phone sidebar. Opening the
 * rail before that lands races it: the sidebar closes under the click and the
 * shell header covers the rail. The page's own /api/corsia-agent response is not read,
 * because a response from the page being left can match and its body is gone once
 * the navigation replaces it. Off the root the landing's composer is its proof. At
 * the root the composer is up before the landing, so a caller whose load lands
 * there passes `lands` and waits for the conductor's messages instead.
 */
export async function loadPastFirstRunLanding(page: Page, load: () => Promise<unknown>, { lands = false } = {}) {
	const conductor = await page.request.get(`/api/corsia-agent?token=${TEST_TOKEN}`);
	expect(conductor.ok(), 'the conductor state is readable').toBeTruthy();
	const state = await conductor.json();
	const firstRunPending = state.first_run_pending === true;
	const conductorId: unknown = state.target?.thread_id;
	if (firstRunPending && lands) expect(conductorId, 'the first-run conductor is named').toBeTruthy();
	const landed = firstRunPending && lands
		? page.waitForResponse((response) => new URL(response.url()).pathname === `/api/threads/${conductorId}/messages`)
		: null;
	await load();
	if (landed) await landed;
	else if (firstRunPending) await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
}

export async function openSettings(page: Page) {
	const settingsButton = page.locator('button[title="Settings"]').first();
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await expect(settingsButton).toBeVisible();
	}
	await settingsButton.click();
	await expect(page.getByRole('button', { name: 'Providers' })).toBeVisible();
}

export async function saveEvidenceScreenshot(
	page: Page,
	testInfo: TestInfo,
	name: string,
	options: { fullPage?: boolean } = {},
) {
	await page.screenshot({
		path: testInfo.outputPath(`${name}.png`),
		fullPage: options.fullPage ?? true,
		animations: 'disabled',
	});
}

// Every E2E-created thread declares its own model; the backend has no global
// default model to borrow.
export function e2eModel(): string {
	const model =
		process.env.CORSIA_E2E_MODEL ?? (process.env.CORSIA_E2E_MOCK === '1' ? 'mock/test-model' : undefined);
	if (!model) {
		throw new Error(
			'CORSIA_E2E_MODEL must be set: the test harness passes an explicit model on each created thread.',
		);
	}
	return model;
}

export async function createSession(request: APIRequestContext, title: string) {
	const response = await request.post(`/api/threads?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { title, model: e2eModel() },
	});
	expect(response.ok()).toBeTruthy();
	const body = await response.json();
	expect(body.thread_id).toEqual(expect.any(String));
	return body.thread_id as string;
}


export function chatComposer(page: Page) {
	return page.getByRole('textbox', { name: 'Message CORSIA' });
}

export function sendButton(page: Page) {
	return page.locator('button.send-btn');
}

export async function sendChatMessage(page: Page, text: string) {
	const composer = chatComposer(page);
	await expect(composer).toBeVisible();
	await composer.click();
	await composer.fill(text);
	const isTouch = (page.viewportSize()?.width ?? 0) <= 768;
	if (isTouch) {
		await sendButton(page).click();
	} else {
		await page.keyboard.press('Enter');
	}
}

export async function waitForWsConnected(page: Page) {
	const isMockMode = !!process.env.CORSIA_E2E_MOCK;

	await expect(page.locator('.model-chip-name').first()).toBeVisible({ timeout: 20_000 });
	await waitForModelListLoaded(page);
}

/**
 * Waits for the composer's model chip to name a loaded model, i.e. the model list has loaded. Unlike
 * waitForWsConnected it does not require the chip to be visible: on a phone a restored document covers the chat.
 * This is not proof of a live session.
 */
export async function waitForModelListLoaded(page: Page) {
	await expect(page.locator('.model-chip-name').first()).toHaveText(/^(?!Select Model$)\S.*$/, { timeout: 30_000 });
}

export interface SessionSubscribeWatcher {
	waitForSubscribed(sessionId: string, timeoutMs?: number): Promise<void>;
}

// The backend delivers a session's live events (e.g. injected test messages)
// only to sockets already registered in its per-session broadcast list, which
// happens when the server replies to the client's `subscribe_session` frame
// with `session_snapshot`. A test that injects a message right after clicking
// into a session races that handshake — the broadcast is best-effort with no
// replay, so a message sent before the reply lands is silently dropped.
//
// Must be called BEFORE page.goto(): the frame listener has to attach inside
// the 'websocket' event callback itself (synchronously, no await) because on
// a fast local/mock connection the whole connect→auth→subscribe→snapshot
// handshake can complete before a `page.waitForEvent('websocket')` promise
// round-trips back through CDP — by then the early frames are already gone.
export function watchSessionSubscriptions(page: Page): SessionSubscribeWatcher {
	const subscribed = new Set<string>();
	const waiters: Array<{ sessionId: string; resolve: () => void }> = [];

	page.on('websocket', (ws) => {
		ws.on('framereceived', (frame) => {
			let data: { type?: string; thread_id?: string };
			try {
				data = JSON.parse(String(frame.payload));
			} catch {
				return;
			}
			if (data.type !== 'session_snapshot' || !data.thread_id) return;
			subscribed.add(data.thread_id);
			for (const waiter of waiters.filter((w) => w.sessionId === data.thread_id)) {
				waiter.resolve();
			}
		});
	});

	return {
		waitForSubscribed(sessionId: string, timeoutMs = 10_000): Promise<void> {
			if (subscribed.has(sessionId)) return Promise.resolve();
			return new Promise<void>((resolve, reject) => {
				const entry = {
					sessionId,
					resolve: () => {
						clearTimeout(timer);
						resolve();
					},
				};
				const timer = setTimeout(() => {
					waiters.splice(waiters.indexOf(entry), 1);
					reject(new Error(`Timed out waiting for session_snapshot for session ${sessionId}`));
				}, timeoutMs);
				waiters.push(entry);
			});
		},
	};
}

export function captureFailedRequests(page: Page, predicate: (url: string) => boolean): string[] {
	const failed: string[] = [];
	page.on('requestfailed', (req) => {
		const url = req.url();
		if (predicate(url)) failed.push(url);
	});
	page.on('response', (resp) => {
		const url = resp.url();
		if (predicate(url) && resp.status() >= 400) {
			failed.push(`${url} [${resp.status()}]`);
		}
	});
	return failed;
}

export function lastUserMessage(page: Page) {
	return page.locator('.message.user').last();
}

export function lastAssistantMessage(page: Page) {
	return page.locator('.message.assistant').last();
}

export function userMessages(page: Page) {
	return page.locator('.message.user');
}

export function assistantMessages(page: Page) {
	return page.locator('.message.assistant');
}

export function captureConsoleErrors(page: Page): string[] {
	const errors: string[] = [];
	page.on('console', (msg) => {
		if (msg.type() === 'error') {
			errors.push(msg.text());
		}
	});
	page.on('pageerror', (err) => {
		errors.push(`pageerror: ${err.message}`);
	});
	return errors;
}
