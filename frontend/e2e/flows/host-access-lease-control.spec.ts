import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';

import {
	captureConsoleErrors,
	createSession,
	loginByUrl,
	openSettings,
	TEST_SESSION_TOKEN,
} from '../support/corsia';

type LeaseState = 'awaiting_user' | 'active' | 'blocked' | 'revoked';

interface Lease {
	id: string;
	root_thread_id: string;
	root_thread_title: string;
	state: LeaseState;
	purpose: string;
	plan: string[];
	requested_duration_seconds: number;
	expected_resources: string | null;
	persistent_effects: string | null;
	rollback_plan: string | null;
	forbidden_effects: string | null;
	requesting_project_id: string | null;
	repo_policy_snapshot: Array<{
		repo_name: string;
		repo_path: string;
		pinned_branch: string | null;
		protect_base_checkout: boolean;
	}>;
	repo_policy_hash: string;
	requested_policy_exceptions: Array<{
		repo_name: string;
		policy: 'branch_pinning' | 'protected_base_checkout';
		reason: string;
	}>;
	application_hash: string;
	revision: number;
	decision_capability: string;
	reviewer_model: string;
	reviewer: {
		model: string;
		display_name: string;
		attestation: string;
	};
	application_review_result: {
		verdict: string;
		attestation: string;
	};
	expires_at: string | null;
	terminal_reason: string | null;
	created_at: string;
	updated_at: string;
}

const reviewedRepoPolicy = {
	requesting_project_id: 'project-home-assistant',
	repo_policy_snapshot: [{
		repo_name: 'home-assistant',
		repo_path: '/srv/home-assistant',
		pinned_branch: 'main',
		protect_base_checkout: true,
	}],
	repo_policy_hash: 'b'.repeat(64),
	requested_policy_exceptions: [{
		repo_name: 'home-assistant',
		policy: 'branch_pinning' as const,
		reason: 'The reviewed recovery requires checking out the rollback branch.',
	}, {
		repo_name: 'home-assistant',
		policy: 'protected_base_checkout' as const,
		reason: 'The reviewed repair must update the registered base checkout.',
	}],
} satisfies Pick<
	Lease,
	| 'requesting_project_id'
	| 'repo_policy_snapshot'
	| 'repo_policy_hash'
	| 'requested_policy_exceptions'
>;

const reviewedRequest: Lease = {
	id: 'lease-e2e-1',
	root_thread_id: 'thread-home-assistant',
	root_thread_title: 'Home Assistant',
	state: 'awaiting_user',
	purpose: 'Repair the bounded media bridge configuration.',
	plan: [
		'Inspect the current service configuration and logs.',
		'Apply only the reviewed media bridge correction.',
		'Verify service health and preserve rollback evidence.',
	],
	requested_duration_seconds: 300,
	expected_resources: 'Home Assistant host and its media bridge service',
	persistent_effects: 'One reviewed service configuration update',
	rollback_plan: 'Restore the captured configuration and restart the service',
	forbidden_effects: 'No unrelated hosts, credentials, or services',
	...reviewedRepoPolicy,
	application_hash: 'hash-e2e-1',
	revision: 4,
	decision_capability: 'decision-e2e-1',
	reviewer_model: 'anthropic/claude-opus',
	reviewer: {
		model: 'anthropic/claude-opus',
		display_name: 'Claude Opus',
		attestation: 'The request is bounded to the stated repair and rollback plan.',
	},
	application_review_result: {
		verdict: 'allow',
		attestation: 'The request is bounded to the stated repair and rollback plan.',
	},
	expires_at: null,
	terminal_reason: null,
	created_at: '2026-07-18T16:00:00.000Z',
	updated_at: '2026-07-18T16:01:00.000Z',
};

const longHostAccessPurpose = [
	'Repair an intermittent media bridge failure that began after the most recent Home Assistant service update.',
	'Inspect the current bridge configuration, service journal, process state, and the bounded host networking facts needed to distinguish a configuration regression from a transient discovery failure.',
	'Compare the live configuration with the captured pre-update backup and record every material difference before changing the host.',
	'Confirm that the configured device names, discovery endpoints, ports, and service account ownership match the reviewed deployment plan.',
	'Validate local DNS resolution and the single service route used by the bridge without probing unrelated hosts or networks.',
	'If the evidence identifies the reviewed configuration mismatch, update only that setting and preserve a copy of the exact prior file for rollback.',
	'Restart only the media bridge service, then verify its process health, journal output, discovery registration, and Home Assistant integration status.',
	'Do not rotate credentials, install packages, upgrade the operating system, change firewall policy, or modify any unrelated automation.',
	'If the service does not recover after the bounded correction, restore the captured configuration immediately and restart the same service once.',
	'Preserve the before-and-after service status and relevant journal excerpts so the result can be independently reviewed.',
	'Treat any unexpected permission boundary, missing backup, unrelated service failure, or broader network symptom as a stop condition rather than expanding scope.',
	'Leave unrelated host files, containers, integrations, users, devices, and long-running processes untouched throughout the repair.',
	'Confirm at the end that the bridge remains healthy after a second discovery interval and that the intended device is visible to Home Assistant.',
	'Report the exact setting changed, the verification performed, whether rollback was required, and any residual risk that needs a separate request.',
].join(' ');

/**
 * Opening Settings mounts the providers view, whose load asks for host resources, the settings model inventory and the model
 * catalog together. A reload that lands while those are in flight aborts them and the view logs "harness inventory failed", so
 * a test that reloads after opening Settings first waits for that load to be answered. Call it before opening Settings. On a phone
 * Settings opens at its section list and the providers view is not mounted yet, so there is no such load to wait for.
 */
function providersLoadAnswered(page: Page): Promise<unknown> {
	const pathOf = (url: string) => new URL(url).pathname;
	let loadStarted = false;
	page.on('request', (request) => {
		if (pathOf(request.url()) === '/api/system/host-resources') loadStarted = true;
	});
	const answered = (match: (path: string) => boolean) => page.waitForResponse((response) => match(pathOf(response.url())));
	// The plain model catalog is also read when Settings opens, before the load starts, so only a read after it counts.
	const settled = Promise.all([
		answered((path) => path === '/api/system/host-resources'),
		answered((path) => path === '/api/settings/models'),
		answered((path) => loadStarted && path === '/api/models'),
	]);
	settled.catch(() => {});
	return settled;
}

async function selectThread(page: Page, title: string) {
	const mobile = (page.viewportSize()?.width ?? 0) <= 768;
	const composer = page.getByRole('textbox', { name: 'Message CORSIA' });
	if (mobile) {
		const sidebarToggle = page.getByRole('button', { name: 'Toggle sidebar' });
		await expect(sidebarToggle).toBeVisible();
		await sidebarToggle.click();
	}
	const row = page.locator('.thread-item').filter({ hasText: title });
	await expect(row).toBeVisible();
	await row.click();
	await expect(composer).toBeVisible();
	if (mobile) {
		const main = page.locator('main.main');
		await expect(main).not.toHaveClass(/\bopen\b/);
		await expect.poll(async () => {
			const box = await main.boundingBox();
			return box === null ? null : Math.round(box.x);
		}).toBe(0);
	}
}

/**
 * A sticky footer in the drawer's scroller must sit on the scroller's bottom edge wherever the content is scrolled. The scroller's own
 * bottom padding opens a strip under a sticky footer that the content scrolls through, so the footer floats above the sheet's edge. Returns
 * whether the content overflowed at all: with nothing to scroll there is no strip to see.
 */
async function footerSitsOnScrollerEdge(dialog: Locator, footerSelector: string): Promise<boolean> {
	const body = dialog.locator('.drawer-body');
	const scrolls = await body.evaluate((el) => el.scrollHeight > el.clientHeight);
	if (!scrolls) return false;
	for (const position of [0, 0.5, 1]) {
		const geometry = await body.evaluate((el, [fraction, selector]) => {
			el.scrollTop = (el.scrollHeight - el.clientHeight) * (fraction as number);
			const footer = el.querySelector(selector as string)!.getBoundingClientRect();
			return { scrollerBottom: el.getBoundingClientRect().bottom, footerBottom: footer.bottom };
		}, [position, footerSelector]);
		expect(geometry.footerBottom, `footer at scroll ${position}: ${JSON.stringify(geometry)}`).toBeCloseTo(geometry.scrollerBottom, 0);
	}
	return true;
}

test.describe('@flow:host-access @surface:shell reviewed host access', () => {
	test('repairs reviewer setup, approves once, and revokes on desktop and mobile', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		const decisions: Array<{ action: string; body: unknown }> = [];
		const settingsPatches: unknown[] = [];
		let baseSettings: Record<string, unknown> | null = null;
		let reviewerConfigured = false;
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const otherTitle = `Other host thread ${suffix}`;
		const ownerTitle = `Host access owner ${suffix}`;
		await createSession(request, otherTitle);
		const ownerSessionId = await createSession(request, ownerTitle);
		const ownedRequest: Lease = {
			...reviewedRequest,
			root_thread_id: ownerSessionId,
			root_thread_title: ownerTitle,
			purpose: longHostAccessPurpose,
		};
		const ownerMessages = Array.from({ length: 24 }, (_, index) => ({
			id: `host-access-message-${index}`,
			thread_id: ownerSessionId,
			role: index % 2 === 0 ? 'user' : 'assistant',
			content: `Host access layout message ${index}`,
			created_at: `2026-07-18T15:${String(index).padStart(2, '0')}:00.000000`,
			metadata: null,
		}));
		let lease: Lease = {
			...ownedRequest,
			state: 'blocked',
			revision: 3,
			terminal_reason: 'No independent reviewer is configured.',
		};

		await page.route('**/api/settings', async (route) => {
			const request = route.request();
			expect(request.headers().authorization).toBe(`Bearer ${TEST_SESSION_TOKEN}`);

			if (request.method() === 'PATCH') {
				const body = request.postDataJSON();
				settingsPatches.push(body);
				reviewerConfigured = true;
				lease = { ...ownedRequest };
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						...(baseSettings ?? {}),
						host_access_reviewer_model: 'mock-test-model',
						host_access_reviewer_model_name: 'Mock Test Model',
						host_access_reviewer_status: 'ready',
						host_access_reviewer_message: null,
					}),
				});
				return;
			}

			const response = await route.fetch();
			baseSettings = await response.json();
			await route.fulfill({
				response,
				contentType: 'application/json',
				body: JSON.stringify({
					...baseSettings,
					host_access_reviewer_model: reviewerConfigured ? 'mock-test-model' : null,
					host_access_reviewer_model_name: reviewerConfigured ? 'Mock Test Model' : null,
					host_access_reviewer_status: reviewerConfigured ? 'ready' : 'unconfigured',
					host_access_reviewer_message: reviewerConfigured
						? null
						: 'Configure a ready model before requesting host access.',
				}),
			});
		});

		await page.route('**/api/host-access/leases**', async (route) => {
			const request = route.request();
			expect(request.headers().authorization).toBe(`Bearer ${TEST_SESSION_TOKEN}`);
			const url = new URL(request.url());
			const action = url.pathname.match(/\/(approve|reject|revoke)$/)?.[1];

			if (request.method() === 'POST' && action) {
				const body = request.postDataJSON();
				decisions.push({ action, body });
				if (action === 'approve') {
					lease = {
						...ownedRequest,
						state: 'active',
						revision: 5,
						expires_at: new Date(Date.now() + 300_000).toISOString(),
						updated_at: '2026-07-18T16:02:00.000Z',
					};
				} else if (action === 'revoke') {
					lease = {
						...lease,
						state: 'revoked',
						revision: 6,
						expires_at: null,
						terminal_reason: 'Revoked by user.',
						updated_at: '2026-07-18T16:03:00.000Z',
					};
				}
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(lease),
				});
				return;
			}

			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				// The control asks for one thread's open leases; the server answers only for that root thread
				// and drops terminal states, which is how a revoked lease leaves the control.
				body: JSON.stringify({
					leases: [lease].filter((item) => {
						const root = url.searchParams.get('root_thread_id');
						if (root && item.root_thread_id !== root) return false;
						return url.searchParams.get('nonterminal_only') !== 'true'
							|| ['reviewing', 'awaiting_user', 'activating', 'active'].includes(item.state);
					}),
				}),
			});
		});
		await page.route(`**/api/threads/${ownerSessionId}/messages**`, async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ messages: ownerMessages, has_more: false, next_cursor: null }),
			});
		});

		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, otherTitle);
		await expect(page.locator('.thread-host-access')).toHaveCount(0);
		await selectThread(page, ownerTitle);
		await expect(page.locator('.thread-host-access')).toHaveCount(0);

		const providersLoaded = (page.viewportSize()?.width ?? 0) > 768 ? providersLoadAnswered(page) : null;
		await openSettings(page);
		await page.getByRole('button', { name: 'System', exact: true }).click();
		await expect(page.getByRole('heading', { name: 'Host Access Review' })).toBeVisible();

		await page.getByLabel('Reviewer model').selectOption('mock-test-model');
		await expect(page.getByText('Ready', { exact: true })).toBeVisible();
		expect(settingsPatches).toEqual([
			{ host_access_reviewer_model: 'mock-test-model' },
		]);

		await providersLoaded;
		await page.reload();
		await selectThread(page, ownerTitle);
		const reviewTrigger = page.getByRole('button', { name: 'Review temporary host access' });
		await expect(reviewTrigger).toBeVisible();
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		// The pending request never steals focus or renders its full body until
		// the user explicitly taps the compact trigger.
		await expect(page.getByText(ownedRequest.purpose)).toHaveCount(0);

		await reviewTrigger.click();
		const dialog = page.getByRole('dialog', { name: 'Review temporary host access' });
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(1);
		await expect(dialog).toBeVisible();
		await expect(dialog.getByText(ownedRequest.purpose)).toBeVisible();
		await expect(dialog.getByText('Repository policy context', { exact: true })).toBeVisible();
		await expect(dialog.getByText('Project: project-home-assistant', { exact: true })).toBeVisible();
		await expect(dialog.getByText('Pinned to main', { exact: true })).toBeVisible();
		await expect(dialog.getByText('Base checkout protected', { exact: true })).toBeVisible();
		await expect(dialog.getByText('home-assistant: Branch pinning', { exact: true })).toBeVisible();
		await expect(dialog.getByText('home-assistant: Protected base checkout', { exact: true })).toBeVisible();
		await expect(dialog.getByRole('button', { name: 'Reject' })).toBeVisible();
		await expect(dialog.getByRole('button', { name: 'Approve' })).toBeVisible();
		// Approve is the one solid action; Reject is a ghost text action.
		await expect(dialog.getByRole('button', { name: 'Reject' })).toHaveClass(/\bghost\b/);
		await expect(dialog.getByRole('button', { name: 'Approve' })).toHaveClass(/\bprimary\b/);
		// Every section is a label over plain text, with no card border or fill.
		const sections = dialog.locator('.risk-grid section, .repo-policy-context, .policy-exceptions, .review');
		expect(await sections.count()).toBeGreaterThanOrEqual(3);
		for (const section of await sections.all()) {
			await expect(section).toHaveCSS('border-top-width', '0px');
			await expect(section).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
		}
		// With no box, the exceptions label keeps the warning colour the pending status uses.
		const warningColor = await dialog.locator('.decision-head .status').evaluate((element) => getComputedStyle(element).color);
		await expect(dialog.locator('.policy-exceptions .exceptions-label')).toHaveCSS('color', warningColor);
		await expect(dialog.locator('.decision-head .status')).toHaveText('Needs approval');
		// The header carries the state; the asked duration is body text, not a second amber.
		await expect(dialog.locator('.risk-grid section').first()).toContainText('Duration');
		await expect(dialog.locator('.risk-grid section').first().locator('p')).not.toHaveCSS('color', warningColor);

		const viewport = page.viewportSize();
		// The sheet slides up from below on open; its bounds are read where it comes to rest.
		await expect.poll(async () => {
			const rest = await dialog.boundingBox();
			return !!rest && rest.x >= 0 && rest.y >= 0 && rest.x + rest.width <= viewport!.width
				&& rest.y + rest.height <= viewport!.height;
		}, 'the review sheet rests inside the viewport').toBe(true);
		const box = await dialog.boundingBox();
		expect(box).toBeTruthy();
		await expect(dialog.locator('.drawer-body')).toHaveCSS('overflow-y', 'auto');
		await expect(dialog.locator('.drawer-body')).toHaveCSS('padding-left', '16px');
		await expect(dialog.locator('.drawer-body')).toHaveCSS('padding-right', '16px');
		const pendingBodyBox = await dialog.locator('.decision-body').boundingBox();
		const pendingActionsBox = await dialog.locator('.decision-actions').boundingBox();
		expect(pendingBodyBox).toBeTruthy();
		expect(pendingActionsBox).toBeTruthy();
		expect(pendingBodyBox!.x - box!.x).toBeGreaterThanOrEqual(15);
		expect(box!.x + box!.width - pendingBodyBox!.x - pendingBodyBox!.width)
			.toBeGreaterThanOrEqual(15);
		expect(Math.abs(pendingActionsBox!.x - pendingBodyBox!.x)).toBeLessThanOrEqual(1);
		expect(Math.abs(pendingActionsBox!.width - pendingBodyBox!.width)).toBeLessThanOrEqual(1);

		// toBeVisible() only proves the control is attached with non-zero size --
		// it does not prove it is reachable without scrolling, which is exactly
		// how a footer buried below long request content survived review. With
		// this long purpose text and zero scroll performed, assert Reject and
		// Approve already sit within the viewport at rest.
		const rejectBoxAtRest = await dialog.getByRole('button', { name: 'Reject' }).boundingBox();
		const approveBoxAtRest = await dialog.getByRole('button', { name: 'Approve' }).boundingBox();
		for (const controlBox of [rejectBoxAtRest, approveBoxAtRest]) {
			expect(controlBox).toBeTruthy();
			expect(controlBox!.y).toBeGreaterThanOrEqual(0);
			expect(controlBox!.y + controlBox!.height).toBeLessThanOrEqual(viewport!.height);
		}
		// Reject sits beside Approve, not across the footer from it; on a phone a finger's width apart.
		const besideGap = approveBoxAtRest!.x - (rejectBoxAtRest!.x + rejectBoxAtRest!.width);
		if (viewport!.width <= 720) {
			expect(besideGap).toBeGreaterThanOrEqual(44);
			expect(besideGap).toBeLessThanOrEqual(48);
		} else {
			expect(besideGap).toBeGreaterThanOrEqual(0);
			expect(besideGap).toBeLessThanOrEqual(16);
		}

		expect(await footerSitsOnScrollerEdge(dialog, '.decision-actions'), 'the long purpose overflows the sheet').toBe(true);

		await dialog.getByRole('button', { name: 'Approve' }).click();
		await expect(page.locator('.active-chip .countdown')).toContainText('min left');
		await expect(page.locator('.active-chip')).not.toContainText(ownerTitle);
		// The chip's words are its name, the time left included.
		const hostAccessDisclosure = page.getByRole('button', { name: /^Host access · \d+ min left$/ });
		const revokeHostAccess = page.getByRole('button', { name: 'Revoke host access' });
		await expect(hostAccessDisclosure).toBeVisible();
		await expect(revokeHostAccess).toHaveCount(0);
		if (viewport!.width <= 768) {
			const composer = page.getByRole('textbox', { name: 'Message CORSIA' });
			// A composer chip: no fill and no border at rest; it sits clear of the messages instead.
			const activeChip = page.locator('.active-chip-trigger');
			const chipMaterial = await activeChip.evaluate((element) => {
				const style = getComputedStyle(element);
				return { background: style.backgroundColor, border: style.borderTopWidth };
			});
			expect(chipMaterial).toEqual({ background: 'rgba(0, 0, 0, 0)', border: '0px' });

			const messages = page.locator('.messages');
			await messages.evaluate((element) => { element.scrollTop = element.scrollHeight; });
			await messages.dispatchEvent('scroll');
			const [chipBox, composerBox, latestMessageBox] = await Promise.all([
				hostAccessDisclosure.boundingBox(),
				composer.boundingBox(),
				page.locator('.message').last().boundingBox(),
			]);
			expect(chipBox!.height).toBeGreaterThanOrEqual(44);
			expect(latestMessageBox!.y + latestMessageBox!.height).toBeLessThanOrEqual(chipBox!.y);
			expect(chipBox!.y + chipBox!.height).toBeLessThanOrEqual(composerBox!.y);

			await messages.evaluate((element) => { element.scrollTop = 0; });
			await messages.dispatchEvent('scroll');
			const scrollToBottom = page.getByRole('button', { name: /^Scroll to (the )?bottom$/ });
			await expect(scrollToBottom).toBeVisible();
			const scrollBox = await scrollToBottom.boundingBox();
			expect(
				chipBox!.x + chipBox!.width <= scrollBox!.x
					|| scrollBox!.x + scrollBox!.width <= chipBox!.x,
			).toBe(true);
			await scrollToBottom.click();

			await composer.focus();
			const [focusedChipBox, focusedComposerBox] = await Promise.all([
				hostAccessDisclosure.boundingBox(),
				composer.boundingBox(),
			]);
			expect(focusedChipBox!.y + focusedChipBox!.height)
				.toBeLessThanOrEqual(focusedComposerBox!.y);
		}
		expect(decisions[0]).toEqual({
			action: 'approve',
			body: { revision: 4, application_hash: 'hash-e2e-1' },
		});

		await selectThread(page, otherTitle);
		await expect(page.locator('.active-chip')).toHaveCount(0);
		await selectThread(page, ownerTitle);
		await expect(hostAccessDisclosure).toBeVisible();

		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		await hostAccessDisclosure.click();
		const activeDialog = page.getByRole('dialog', { name: /Host access/ });
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(1);
		await expect(activeDialog).toBeVisible();
		await expect(revokeHostAccess).toBeVisible();
		expect(decisions).toHaveLength(1);

		await expect.poll(async () => {
			const rest = await activeDialog.boundingBox();
			return !!rest && rest.x >= 0 && rest.x + rest.width <= viewport!.width
				&& rest.y + rest.height <= viewport!.height;
		}, 'the active-lease sheet rests inside the viewport').toBe(true);
		const activeDialogBox = await activeDialog.boundingBox();
		expect(activeDialogBox).toBeTruthy();
		await expect(activeDialog.locator('.drawer-body')).toHaveCSS('overflow-y', 'auto');
		await expect(activeDialog.locator('.drawer-body')).toHaveCSS('padding-left', '16px');
		await expect(activeDialog.locator('.drawer-body')).toHaveCSS('padding-right', '16px');
		const activeBodyBox = await activeDialog.locator('.active-drawer-body').boundingBox();
		const activeActionsBox = await activeDialog.locator('.active-drawer-actions').boundingBox();
		expect(activeBodyBox).toBeTruthy();
		expect(activeActionsBox).toBeTruthy();
		expect(activeBodyBox!.x - activeDialogBox!.x).toBeGreaterThanOrEqual(15);
		expect(activeDialogBox!.x + activeDialogBox!.width - activeBodyBox!.x - activeBodyBox!.width)
			.toBeGreaterThanOrEqual(15);
		expect(Math.abs(activeActionsBox!.x - activeBodyBox!.x)).toBeLessThanOrEqual(1);
		expect(Math.abs(activeActionsBox!.width - activeBodyBox!.width)).toBeLessThanOrEqual(1);
		// scrollIntoViewIfNeeded() would silently paper over an unreachable
		// control by scrolling to it before the assertion ever runs -- that is
		// exactly how the prior regression passed review. Assert Revoke is
		// already within the viewport at rest, with zero scroll performed.
		const revokeBoxAtRest = await revokeHostAccess.boundingBox();
		expect(revokeBoxAtRest).toBeTruthy();
		expect(revokeBoxAtRest!.y).toBeGreaterThanOrEqual(0);
		expect(revokeBoxAtRest!.y + revokeBoxAtRest!.height).toBeLessThanOrEqual(viewport!.height);
		await footerSitsOnScrollerEdge(activeDialog, '.active-drawer-actions');

		// Escape closes the drawer without revoking, and returns focus to the
		// trigger that opened it -- the drawer's own contract, reused as-is.
		await page.keyboard.press('Escape');
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		await expect(hostAccessDisclosure).toBeFocused();
		expect(decisions).toHaveLength(1);

		await hostAccessDisclosure.click();
		await expect(revokeHostAccess).toBeVisible();
		await revokeHostAccess.click();
		await expect(page.locator('.active-chip')).toHaveCount(0);
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeFocused();
		expect(decisions[1]).toEqual({
			action: 'revoke',
			body: { revision: 5, application_hash: 'hash-e2e-1' },
		});
		expect(consoleErrors).toEqual([]);
	});

	test('fits a long saved-login site on one line on a phone, cut between whole segments with the domain whole', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 393, height: 852 });
		const title = `Saved login entry ${testInfo.project.name}-${Date.now()}`;
		const threadId = await createSession(request, title);
		const scheme = 'https:' + '//';
		const site = `${scheme}accounts.northwind-regional-supply-cooperative.example.com`;
		const fill = {
			id: 'fill-e2e-1', kind: 'fill', state: 'awaiting_user', thread_id: threadId, root_thread_id: threadId,
			agent: 'Bookkeeper', site, domain: 'example.com', username: 'accounts-payable@example.com',
			revision: 1, application_hash: 'hash-fill-e2e-1', decision_capability: 'decision-fill-e2e-1',
			max_duration_seconds: 1800, duration_seconds: null, expires_at: null,
			created_at: '2026-07-18T16:00:00.000Z', updated_at: '2026-07-18T16:00:00.000Z',
		};
		await page.route('**/api/host-access/leases**', async (route) => {
			const root = new URL(route.request().url()).searchParams.get('root_thread_id');
			await route.fulfill({
				status: 200, contentType: 'application/json',
				body: JSON.stringify({ leases: root === threadId ? [fill] : [] }),
			});
		});
		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, title);

		const shown = page.locator('.pending-request-trigger .pending-site');
		await expect(shown).toBeVisible();
		await expect(shown).toHaveAttribute('title', site);
		await expect.poll(() => shown.textContent()).not.toBe(site);
		const label = (await shown.textContent()) ?? '';
		const [head, tail, ...more] = label.split('…');
		expect(more, label).toEqual([]);
		// The head is whole segments from the front: the scheme, then whole labels, never part of one.
		expect([scheme, `${scheme}accounts`], label).toContain(head);
		// The tail ends in the whole registrable domain, after whole labels only.
		expect(tail.endsWith('example.com'), label).toBe(true);
		expect(tail === 'example.com' || site.endsWith(`.${tail}`), label).toBe(true);
		// One line, nothing clipped.
		const box = await shown.evaluate((element) => ({
			fits: element.scrollWidth <= element.clientWidth,
			lines: element.getClientRects().length,
			height: element.getBoundingClientRect().height,
			lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight) || 0,
		}));
		expect(box.fits, JSON.stringify(box)).toBe(true);
		expect(box.lines).toBe(1);
		if (box.lineHeight) expect(box.height).toBeLessThanOrEqual(box.lineHeight + 1);
	});
});
