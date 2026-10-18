import { expect, test, type Page, type TestInfo, type WebSocketRoute } from '@playwright/test';

import { externalLease } from './fixtures/external-agent-lease';
import {
	captureConsoleErrors,
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_SESSION_TOKEN,
} from './support/corsia';
import type { HostAccessExternalAgentException, HostAccessLease } from '../src/lib/api/hostAccess';

const WARNING = 'External agents run outside CORSIA’s managed context and controls. Their instructions, skills and tools may differ, and CORSIA may not fully observe their actions, costs or results. Human approval is required for this exception, even when automatic approval is enabled.';
const UPDATED_COMMAND = "claude -p 'Analyze the refreshed disposable sample report' --add-dir /tmp/refreshed-independent-sample";
const UPDATED_DIRECTORY = '/tmp/corsia-external-sample/' + 'refreshed-disposable-analysis-inputs-'.repeat(8);
const viewports = [
	{ name: 'mobile', width: 393, height: 844 },
	{ name: 'desktop', width: 1280, height: 844 },
] as const;

type Presentation = {
	replace(next: HostAccessLease): void;
	releaseRejection(): void;
	response(): HostAccessLease | null;
	listedEmptyAfterRejection(): boolean;
	decisions: Array<{ action: string; body: unknown }>;
};

function externalException(lease: HostAccessLease): HostAccessExternalAgentException {
	const exception = lease.requested_policy_exceptions[0];
	if (!exception || exception.policy !== 'external_agent') {
		throw new Error('External-agent fixture is malformed');
	}
	return exception;
}

function leaseFor(threadId: string, title: string): HostAccessLease {
	return { ...externalLease(threadId), root_thread_title: title };
}

function refreshedLease(initial: HostAccessLease): HostAccessLease {
	return {
		...initial,
		id: 'external-request-refreshed',
		application_hash: 'c'.repeat(64),
		revision: initial.revision + 1,
		updated_at: '2026-10-01T12:01:00Z',
		requested_policy_exceptions: [{
			...externalException(initial),
			command: UPDATED_COMMAND,
			working_directory: UPDATED_DIRECTORY,
		}],
	};
}

async function selectThread(page: Page, title: string) {
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		const closeDrawer = page.getByRole('button', { name: 'Close drawer' });
		if (!await closeDrawer.isVisible()) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await expect(closeDrawer).toBeVisible();
	}
	const row = page.locator('.thread-item').filter({ hasText: title });
	await expect(row).toBeVisible();
	await row.click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
}

async function routeHostAccessRefresh(page: Page) {
	let socket: WebSocketRoute | null = null;
	await page.routeWebSocket((url) => url.pathname === '/ws', (client) => {
		const server = client.connectToServer();
		socket = client;
		client.onMessage((payload) => { server.send(payload); });
		server.onMessage((payload) => { client.send(payload); });
	});
	return {
		async waitForConnection() {
			await expect.poll(() => socket !== null, { timeout: 10_000 }).toBe(true);
		},
		nudge() {
			if (!socket) throw new Error('Live CORSIA WebSocket was not captured');
			socket.send(JSON.stringify({ type: 'host_access_changed' }));
		},
	};
}

async function installPresentation(
	page: Page,
	threadId: string,
	initial: HostAccessLease,
): Promise<Presentation> {
	let lease = initial;
	let terminal = false;
	let response: HostAccessLease | null = null;
	let listedEmpty = false;
	let release: (() => void) | null = null;
	const decisions: Array<{ action: string; body: unknown }> = [];

	await page.route('**/api/host-access/leases**', async (route) => {
		const request = route.request();
		expect(request.headers().authorization).toBe(`Bearer ${TEST_SESSION_TOKEN}`);
		const url = new URL(request.url());
		const action = url.pathname.match(/\/(approve|reject|revoke)$/)?.[1];
		if (request.method() === 'POST' && action) {
			decisions.push({ action, body: request.postDataJSON() });
			expect(action).toBe('reject');
			response = { ...lease, state: 'rejected', terminal_reason: 'Rejected by user.' };
			terminal = true;
			await new Promise<void>((resolve) => { release = resolve; });
			await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) });
			return;
		}
		const leases = url.searchParams.get('root_thread_id') === threadId && !terminal ? [lease] : [];
		if (terminal && url.searchParams.get('root_thread_id') === threadId) listedEmpty = leases.length === 0;
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ leases }) });
	});

	return {
		replace(next) { lease = next; },
		releaseRejection() {
			if (!release) throw new Error('Rejection request did not reach the lease route');
			release();
		},
		response: () => response,
		listedEmptyAfterRejection: () => listedEmpty,
		decisions,
	};
}

async function assertInitialViewport(
	page: Page,
	dialog: ReturnType<Page['getByRole']>,
	exception: HostAccessExternalAgentException,
) {
	const viewport = page.viewportSize()!;
	const warning = dialog.locator('.external-warning');
	const command = dialog.getByText('Command', { exact: true });
	// The review slides in on open; its bounds are read where it comes to rest.
	await expect.poll(async () => {
		const rest = await dialog.boundingBox();
		return !!rest && rest.y >= 0 && rest.y + rest.height <= viewport.height;
	}, 'the review rests inside the viewport').toBe(true);
	const [warningBox, commandBox, rejectBox, approveBox, footerBox, headBox] = await Promise.all([
		warning.boundingBox(),
		command.boundingBox(),
		dialog.getByRole('button', { name: 'Reject' }).boundingBox(),
		dialog.getByRole('button', { name: 'Approve' }).boundingBox(),
		dialog.locator('.decision-actions').boundingBox(),
		dialog.locator('.decision-head').boundingBox(),
	]);
	await expect(warning).toHaveText(WARNING);
	await expect(dialog.getByText('30 minutes', { exact: true })).toBeVisible();
	await expect(dialog.getByText('Working directory', { exact: true })).toBeVisible();
	await expect(dialog.locator('.policy-exceptions code')).toHaveText([exception.command, exception.working_directory]);
	expect(await warning.evaluate((element) => {
		const range = document.createRange();
		range.selectNodeContents(element);
		return getComputedStyle(element).whiteSpace !== 'nowrap' && range.getClientRects().length > 1;
	})).toBe(true);
	expect(warningBox!.height).toBeGreaterThan(18);
	expect(warningBox!.y).toBeGreaterThanOrEqual(headBox!.y + headBox!.height);
	expect(warningBox!.y + 18).toBeLessThanOrEqual(footerBox!.y);
	expect(commandBox!.y + commandBox!.height).toBeLessThanOrEqual(footerBox!.y);
	for (const box of [commandBox, rejectBox, approveBox]) {
		expect(box).toBeTruthy();
		expect(box!.y).toBeGreaterThanOrEqual(0);
		expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
	}
	expect(Math.abs(rejectBox!.y - approveBox!.y)).toBeLessThanOrEqual(1);
	expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
	expect(await dialog.locator('.drawer-body').evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
}

async function scrollWorkingDirectoryAboveFooter(dialog: ReturnType<Page['getByRole']>) {
	const directory = dialog.locator('.policy-exceptions code').nth(1);
	await directory.evaluate((element) => {
		const body = element.closest<HTMLElement>('.drawer-body');
		if (!body) throw new Error('External-agent drawer body is missing');
		const footer = body.querySelector('.decision-actions');
		if (!footer) throw new Error('External-agent decision footer is missing');
		// Chromium rounds scrollTop to whole pixels; round up to reveal the full end.
		body.scrollTop += Math.ceil(Math.max(0, element.getBoundingClientRect().bottom - footer.getBoundingClientRect().top));
	});
	const [directoryBox, footerBox] = await Promise.all([
		directory.boundingBox(),
		dialog.locator('.decision-actions').boundingBox(),
	]);
	expect(directoryBox).toBeTruthy();
	expect(footerBox).toBeTruthy();
	const headBox = await dialog.locator('.decision-head').boundingBox();
	expect(directoryBox!.y).toBeGreaterThanOrEqual(headBox!.y + headBox!.height);
	expect(directoryBox!.y + directoryBox!.height).toBeLessThanOrEqual(footerBox!.y);
}

async function runJourney(
	page: Page,
	request: Parameters<typeof createSession>[0],
	testInfo: TestInfo,
	viewport: typeof viewports[number],
) {
	await page.setViewportSize(viewport);
	const suffix = `${viewport.name}-${testInfo.project.name}-${Date.now()}`;
	const ownerTitle = `External-agent lease ${suffix}`;
	const threadId = await createSession(request, ownerTitle);
	const initial = leaseFor(threadId, ownerTitle);
	const presentation = await installPresentation(page, threadId, initial);
	const hostAccessSocket = await routeHostAccessRefresh(page);
	const consoleErrors = captureConsoleErrors(page);

	await loginByUrl(page, { waitForComposer: false });
	await hostAccessSocket.waitForConnection();
	await selectThread(page, ownerTitle);
	const trigger = page.getByRole('button', { name: 'Review temporary host access' });
	await trigger.click();
	const dialog = page.getByRole('dialog', { name: 'Review temporary host access' });
	await expect(dialog).toBeVisible();
	await assertInitialViewport(page, dialog, externalException(initial));
	await saveEvidenceScreenshot(page, testInfo, `external-agent-${viewport.name}-initial`, { fullPage: false });

	await scrollWorkingDirectoryAboveFooter(dialog);
	await saveEvidenceScreenshot(page, testInfo, `external-agent-${viewport.name}-directory-end`, { fullPage: false });

	const updated = refreshedLease(initial);
	presentation.replace(updated);
	const refreshedResponse = page.waitForResponse((response) => {
		const url = new URL(response.url());
		return response.request().method() === 'GET'
			&& url.pathname === '/api/host-access/leases'
			&& url.searchParams.get('root_thread_id') === threadId;
	});
	hostAccessSocket.nudge();
	await refreshedResponse;
	await expect(dialog).toHaveCount(0);
	await trigger.click();
	const refreshed = externalException(updated);
	await expect(dialog.locator('.policy-exceptions code')).toHaveText([refreshed.command, refreshed.working_directory]);
	await saveEvidenceScreenshot(page, testInfo, `external-agent-${viewport.name}-refreshed`, { fullPage: false });

	const reject = dialog.getByRole('button', { name: 'Reject' });
	const approve = dialog.getByRole('button', { name: 'Approve' });
	await reject.click();
	await expect(reject).toBeDisabled();
	await expect(approve).toBeDisabled();
	await expect.poll(() => presentation.response()).not.toBeNull();
	presentation.releaseRejection();
	await expect(trigger).toHaveCount(0);
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeFocused();
	expect(presentation.response()).toMatchObject({ state: 'rejected', terminal_reason: 'Rejected by user.' });
	expect(presentation.listedEmptyAfterRejection()).toBe(true);
	expect(presentation.decisions).toEqual([{
		action: 'reject', body: { revision: 2, application_hash: 'c'.repeat(64) },
	}]);
	expect(consoleErrors).toEqual([]);
	await saveEvidenceScreenshot(page, testInfo, `external-agent-${viewport.name}-rejected`, { fullPage: false });
}

test.describe('@flow:host-access @surface:shell external-agent lease', () => {
	for (const viewport of viewports) {
		test(`${viewport.width}x${viewport.height}: discloses, refreshes, and rejects an external-agent lease`, async ({ page, request }, testInfo) => {
			await runJourney(page, request, testInfo, viewport);
		});
	}
});
