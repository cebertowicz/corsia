import { test } from '../support/test';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';

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
		// Approve is the one solid action; Reject is today's secondary button beside it.
		await expect(dialog.getByRole('button', { name: 'Reject' })).toHaveClass(/\bsecondary\b/);
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
		await expect(dialog.locator('.risk-grid section').first().locator('.label')).toHaveText('Duration');
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
		// Reject sits beside Approve, not across the footer from it: the app's button gap, a finger's width on a phone.
		const besideGap = approveBoxAtRest!.x - (rejectBoxAtRest!.x + rejectBoxAtRest!.width);
		const expectedGap = viewport!.width <= 720 ? 44 : 12;
		expect(besideGap).toBeGreaterThanOrEqual(expectedGap);
		expect(besideGap).toBeLessThanOrEqual(expectedGap + 1);
		// The pinned head keeps 12px under its title at every width; both pinned strips end at a hairline
		// over the scrolling content, as the decision drawer's do.
		await expect(dialog.locator('.decision-head')).toHaveCSS('padding-bottom', '12px');
		await expect(dialog.locator('.decision-head')).toHaveCSS('border-bottom-width', '1px');
		await expect(dialog.locator('.decision-actions')).toHaveCSS('border-top-width', '1px');
		// The keyboard reaches Reject, then Approve, and each shows a focus ring.
		await dialog.getByRole('button', { name: 'Reject' }).focus();
		await page.keyboard.press('Shift+Tab');
		await page.keyboard.press('Tab');
		await expect(dialog.getByRole('button', { name: 'Reject' })).toBeFocused();
		await page.keyboard.press('Tab');
		await expect(dialog.getByRole('button', { name: 'Approve' })).toBeFocused();
		for (const name of ['Approve', 'Reject']) {
			if (name === 'Reject') await page.keyboard.press('Shift+Tab');
			const ring = await dialog.getByRole('button', { name }).evaluate((element) => {
				const style = getComputedStyle(element);
				return { visible: element.matches(':focus-visible'), style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
			});
			expect(ring, `${name} shows a focus ring`).toMatchObject({ visible: true });
			expect(ring.style, `${name} outline`).not.toBe('none');
			expect(ring.width, `${name} outline width`).toBeGreaterThan(0);
		}

		expect(await footerSitsOnScrollerEdge(dialog, '.decision-actions'), 'the long purpose overflows the sheet').toBe(true);

		await dialog.getByRole('button', { name: 'Approve' }).click();
		await expect(page.locator('.active-chip .countdown')).toContainText(':');
		await expect(page.locator('.active-chip')).not.toContainText(ownerTitle);
		const hostAccessDisclosure = page.getByRole('button', { name: 'Host access', exact: true });
		const revokeHostAccess = page.getByRole('button', { name: 'Revoke host access' });
		await expect(hostAccessDisclosure).toBeVisible();
		await expect(revokeHostAccess).toHaveCount(0);
		if (viewport!.width <= 768) {
			const composer = page.getByRole('textbox', { name: 'Message CORSIA' });
			// The solid material is the trigger's; the wrapper only lays it out.
			const activeChip = page.locator('.active-chip-trigger');
			const chipMaterial = await activeChip.evaluate((element) => {
				const solidProbe = document.createElement('div');
				solidProbe.style.background = 'var(--bg-secondary)';
				document.body.appendChild(solidProbe);
				const solidBackground = getComputedStyle(solidProbe).backgroundColor;
				solidProbe.remove();
				return {
					actualBackground: getComputedStyle(element).backgroundColor,
					solidBackground,
				};
			});
			expect(chipMaterial.actualBackground).toBe(chipMaterial.solidBackground);

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
		// Revoke ends where a request sheet's Approve ends: the content's right edge.
		expect(Math.abs(revokeBoxAtRest!.x + revokeBoxAtRest!.width - (activeBodyBox!.x + activeBodyBox!.width))).toBeLessThanOrEqual(1);
		await expect(activeDialog.locator('.decision-head')).toHaveCSS('padding-bottom', '12px');
		await expect(activeDialog.locator('.decision-head')).toHaveCSS('border-bottom-width', '1px');
		await expect(activeDialog.locator('.active-drawer-actions')).toHaveCSS('border-top-width', '1px');
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

	const SCHEME = 'https:' + '//';
	const LONG_SITE = `${SCHEME}accounts.northwind-regional-supply-cooperative.example.com`;
	const LONG_SITE_SHOWN = 'accounts.northwind-regional-supply-cooperative.example.com';

	function fillRequest(threadId: string, n: number, overrides: Record<string, unknown> = {}) {
		return {
			id: `fill-e2e-${n}`, kind: 'fill', state: 'awaiting_user', thread_id: threadId, root_thread_id: threadId,
			agent: 'Bookkeeper', site: LONG_SITE, domain: 'example.com', username: 'accounts-payable@example.com', item_label: null,
			revision: 1, application_hash: `hash-fill-e2e-${n}`, decision_capability: `decision-fill-e2e-${n}`,
			max_duration_seconds: 1800, requested_duration_seconds: null, duration_seconds: null, standing: false, expires_at: null,
			created_at: `2026-07-18T16:0${n}:00.000Z`, updated_at: `2026-07-18T16:0${n}:00.000Z`,
			...overrides,
		};
	}

	/** The entry's site line: one line, nothing clipped. */
	async function siteLine(shown: Locator) {
		return shown.evaluate((element) => ({
			fits: element.scrollWidth <= element.clientWidth,
			width: element.clientWidth,
			lines: element.getClientRects().length,
			height: element.getBoundingClientRect().height,
			lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight) || 0,
		}));
	}

	/** The card's row: the site ends before Review, and Review stays inside the card and the screen. */
	async function expectRowFits(page: Page) {
		const row = await page.locator('.pending-request-trigger').first().evaluate((button) => {
			const box = (selector: string) => button.querySelector(selector)!.getBoundingClientRect();
			const entry = button.getBoundingClientRect();
			return { siteRight: box('.pending-site').right, actionLeft: box('.pending-action').left,
				actionRight: box('.pending-action').right, entryRight: entry.right, screen: window.innerWidth,
				ownerWidth: box('.pending-owner').width, entryWidth: entry.width };
		});
		expect(row.siteRight, JSON.stringify(row)).toBeLessThanOrEqual(row.actionLeft);
		expect(row.actionRight, JSON.stringify(row)).toBeLessThanOrEqual(row.entryRight + 0.5);
		expect(row.entryRight, JSON.stringify(row)).toBeLessThanOrEqual(row.screen);
	}

	test('fits a long saved-login site on one line on a phone, cut from the front between whole labels with the domain whole', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 393, height: 852 });
		const title = `Saved login entry ${testInfo.project.name}-${Date.now()}`;
		const threadId = await createSession(request, title);
		const fill = fillRequest(threadId, 1);
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
		await expect(shown).toHaveAttribute('title', LONG_SITE);
		await expect.poll(() => shown.textContent()).not.toBe(LONG_SITE_SHOWN);
		const label = (await shown.textContent()) ?? '';
		// No scheme; one cut, at the front; what follows it is whole labels ending in the whole domain.
		expect(label, label).not.toContain(SCHEME);
		expect(label.startsWith('…'), label).toBe(true);
		expect(label.match(/…/g), label).toHaveLength(1);
		const tail = label.slice(1);
		expect(tail === 'example.com' || LONG_SITE_SHOWN.endsWith(`.${tail}`), label).toBe(true);
		const box = await siteLine(shown);
		expect(box.fits, JSON.stringify(box)).toBe(true);
		expect(box.lines).toBe(1);
		if (box.lineHeight) expect(box.height).toBeLessThanOrEqual(box.lineHeight + 1);
		await expectRowFits(page);
	});

	test('keeps Review on screen on a phone when the agent name is long', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 393, height: 852 });
		const title = `Saved login long name phone ${testInfo.project.name}-${Date.now()}`;
		const threadId = await createSession(request, title);
		const agent = 'Quarterly supplier reconciliation and accounts payable follow-up for the regional cooperative';
		const fill = fillRequest(threadId, 1, { agent });
		await page.route('**/api/host-access/leases**', async (route) => {
			const root = new URL(route.request().url()).searchParams.get('root_thread_id');
			await route.fulfill({
				status: 200, contentType: 'application/json',
				body: JSON.stringify({ leases: root === threadId ? [fill] : [] }),
			});
		});
		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, title);

		await expect(page.locator('.pending-request-trigger .pending-action')).toBeVisible();
		await expectRowFits(page);
	});

	for (const viewport of [{ width: 1440, height: 900 }, { width: 393, height: 852 }]) {
		test(`cuts a long agent name before the site at ${viewport.width} px, so the registrable domain stays whole`, async ({ page, request }, testInfo) => {
			await page.setViewportSize(viewport);
			const title = `Saved login long name ${viewport.width} ${testInfo.project.name}-${Date.now()}`;
			const threadId = await createSession(request, title);
			const agent = 'Quarterly supplier reconciliation and accounts payable follow-up for the regional cooperative';
			const fill = fillRequest(threadId, 1, { agent });
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
			await expect(page.locator('.pending-request-trigger .pending-owner')).toHaveText(agent);
			// fitLabel sets the label after a frame; wait until the site's minimum is measured.
			await expect.poll(() => shown.evaluate((element) => (element as HTMLElement).style.getPropertyValue('--label-min'))).not.toBe('');
			const label = (await shown.textContent()) ?? '';
			// Whole, or cut once at the front between whole labels, and always ending in the whole domain.
			expect(label === LONG_SITE_SHOWN || (label.startsWith('…') && LONG_SITE_SHOWN.endsWith(`.${label.slice(1)}`)), label).toBe(true);
			expect(label.endsWith('example.com'), label).toBe(true);
			const box = await siteLine(shown);
			expect(box.fits, JSON.stringify(box)).toBe(true);
			// The name gives way first but stays readable: on screen, one line, cut at its end.
			const name = await page.locator('.pending-request-trigger .pending-owner').evaluate((element) => ({
				width: element.clientWidth, height: element.getBoundingClientRect().height,
				lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight) || 0,
			}));
			expect(name.width, JSON.stringify(name)).toBeGreaterThan(40);
			if (name.lineHeight) expect(name.height, JSON.stringify(name)).toBeLessThanOrEqual(name.lineHeight + 1);
			await expect(page.locator('.pending-request-trigger .pending-action')).toBeVisible();
			await expectRowFits(page);
			await page.screenshot({ path: testInfo.outputPath(`fill-entry-long-name-${viewport.width}.png`), animations: 'disabled' });
		});
	}

	/**
	 * What the saved-login card must hold at any width, stale or not: "Review →" and the stale badge wholly inside
	 * the card and the screen, the site ending in its whole registrable domain, nothing scrolled or clipped, and
	 * the agent's name never an empty column.
	 */
	async function expectEntryHolds(page: Page, tail: string) {
		const shown = page.locator('.pending-request-trigger .pending-site');
		await expect.poll(() => shown.evaluate((element) => (element as HTMLElement).style.getPropertyValue('--label-min'))).not.toBe('');
		const label = (await shown.textContent()) ?? '';
		expect(label.endsWith(tail), label).toBe(true);
		expect(label === tail || label.endsWith(`.${tail}`) || label === `…${tail}`, label).toBe(true);
		const card = await page.locator('.pending-request-trigger').first().evaluate((button) => {
			const box = (element: Element | null) => {
				if (!element) return null;
				const { left, right, top, bottom, width } = element.getBoundingClientRect();
				return { left, right, top, bottom, width };
			};
			const site = button.querySelector('.pending-site') as HTMLElement;
			return {
				entry: box(button), action: box(button.querySelector('.pending-action')), pill: box(button.querySelector('.pill')),
				owner: box(button.querySelector('.pending-owner')), screen: window.innerWidth,
				row: { scroll: button.scrollWidth, client: button.clientWidth },
				site: { scroll: site.scrollWidth, client: site.clientWidth },
				layout: { stacked: button.hasAttribute('data-stacked'), row: button.getAttribute('style'), site: site.getAttribute('style') },
			};
		});
		const detail = JSON.stringify(card);
		for (const part of [card.action, card.pill]) {
			if (!part) continue;
			expect(part.left, detail).toBeGreaterThanOrEqual(card.entry!.left - 0.5);
			expect(part.right, detail).toBeLessThanOrEqual(card.entry!.right + 0.5);
			expect(part.top, detail).toBeGreaterThanOrEqual(card.entry!.top - 0.5);
			expect(part.bottom, detail).toBeLessThanOrEqual(card.entry!.bottom + 0.5);
			expect(part.right, detail).toBeLessThanOrEqual(card.screen);
		}
		expect(card.action, detail).not.toBeNull();
		expect(card.row.scroll, detail).toBeLessThanOrEqual(card.row.client);
		expect(card.site.scroll, detail).toBeLessThanOrEqual(card.site.client);
		expect(card.owner!.width, detail).toBeGreaterThan(0);
		return card;
	}

	/** One fill in the entry, served from the list read; `stale` then fails the next read, as a dropped connection does. */
	async function showFill(page: Page, request: APIRequestContext, title: string, overrides: Record<string, unknown>, stale = false) {
		const threadId = await createSession(request, title);
		const fill = fillRequest(threadId, 1, overrides);
		let reads = 0;
		await page.route('**/api/host-access/leases**', async (route) => {
			if (route.request().method() === 'POST') {
				await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'lease revision is stale' }) });
				return;
			}
			const root = new URL(route.request().url()).searchParams.get('root_thread_id');
			if (root === threadId && stale && reads++ > 0) {
				await route.abort('failed');
				return;
			}
			await route.fulfill({
				status: 200, contentType: 'application/json',
				body: JSON.stringify({ leases: root === threadId ? [fill] : [] }),
			});
		});
		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, title);
		await expect(page.locator('.pending-request-trigger .pending-site')).toBeVisible();
		if (!stale) return;
		// A Reject the server refuses as changed makes the control read the list again; that read fails, so the request is out of date.
		await page.locator('.pending-request-trigger').click();
		await page.getByRole('dialog').getByRole('button', { name: 'Reject' }).click();
		await expect(page.getByRole('dialog').getByRole('button', { name: 'Reject' })).toBeEnabled();
		await page.keyboard.press('Escape');
		await expect(page.getByRole('dialog')).toHaveCount(0);
		await expect(page.locator('.pending-request-trigger .pill')).toBeVisible();
	}

	const LONG_AGENT = 'Quarterly supplier reconciliation and accounts payable follow-up for the regional cooperative';

	test('keeps Review and the out-of-date badge inside the card on a phone, with a long agent name', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 393, height: 852 });
		await showFill(page, request, `Saved login stale phone ${testInfo.project.name}-${Date.now()}`, { agent: LONG_AGENT }, true);
		await expectEntryHolds(page, 'example.com');
		await page.screenshot({ path: testInfo.outputPath('fill-entry-stale-393.png') });
	});

	test('puts a long registrable domain on its own line on a phone, whole, with Review inside the card', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 393, height: 852 });
		// A registrable domain is one label under a public suffix, so a long one is a long label before ".com".
		const domain = 'northwind-regional-supply-cooperative-accounts.com';
		await showFill(page, request, `Saved login long domain ${testInfo.project.name}-${Date.now()}`, {
			agent: LONG_AGENT, site: `${SCHEME}login.${domain}`, domain,
		});
		await expectEntryHolds(page, domain);
		await page.screenshot({ path: testInfo.outputPath('fill-entry-long-domain-393.png') });
	});

	test('keeps a site with a port whole to its port on a phone, with Review inside the card', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 393, height: 852 });
		// A saved login's site is an https origin: it can carry a port other than 443, never a path.
		await showFill(page, request, `Saved login port ${testInfo.project.name}-${Date.now()}`, {
			agent: LONG_AGENT, site: `${LONG_SITE}:8443`,
		});
		await expectEntryHolds(page, 'example.com:8443');
	});

	test('keeps a short agent name and the site on one row when both fit whole', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const title = `Saved login one row ${testInfo.project.name}-${Date.now()}`;
		const threadId = await createSession(request, title);
		const fill = fillRequest(threadId, 1);
		await page.route('**/api/host-access/leases**', async (route) => {
			const root = new URL(route.request().url()).searchParams.get('root_thread_id');
			await route.fulfill({
				status: 200, contentType: 'application/json',
				body: JSON.stringify({ leases: root === threadId ? [fill] : [] }),
			});
		});
		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, title);

		const name = page.locator('.pending-request-trigger .pending-owner');
		const shown = page.locator('.pending-request-trigger .pending-site');
		await expect.poll(() => shown.textContent()).toBe(LONG_SITE_SHOWN);
		const [nameBox, siteBox] = await Promise.all([name.boundingBox(), shown.boundingBox()]);
		expect(Math.abs(siteBox!.y - nameBox!.y), 'one row').toBeLessThan(nameBox!.height);
		expect(siteBox!.x).toBeGreaterThan(nameBox!.x + nameBox!.width - 1);
		await expectRowFits(page);
	});

	test('shows the next waiting request in the entry as soon as the first is decided', async ({ page, request }, testInfo) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const title = `Saved login next request ${testInfo.project.name}-${Date.now()}`;
		const threadId = await createSession(request, title);
		const first = fillRequest(threadId, 1);
		const secondSite = `${SCHEME}billing.example.org`;
		const second = fillRequest(threadId, 2, { agent: 'Payroll helper', site: secondSite, domain: 'example.org' });
		let waiting = [first, second];
		const rejected: string[] = [];
		await page.route('**/api/host-access/leases**', async (route) => {
			const url = new URL(route.request().url());
			if (route.request().method() === 'POST') {
				const match = /\/fills\/([^/]+)\/reject$/.exec(url.pathname);
				expect(match, url.pathname).toBeTruthy();
				rejected.push(match![1]);
				const decided = waiting.find((item) => item.id === match![1])!;
				waiting = waiting.filter((item) => item.id !== match![1]);
				await route.fulfill({
					status: 200, contentType: 'application/json',
					body: JSON.stringify({ ...decided, state: 'denied', revision: 2 }),
				});
				return;
			}
			const root = url.searchParams.get('root_thread_id');
			await route.fulfill({
				status: 200, contentType: 'application/json',
				body: JSON.stringify({ leases: root === threadId ? waiting : [] }),
			});
		});
		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, title);

		const entry = page.locator('.pending-request-trigger');
		await expect(entry).toHaveCount(1);
		await expect(entry.locator('.pending-owner')).toHaveText('Bookkeeper');
		await entry.click();
		const dialog = page.getByRole('dialog');
		await dialog.getByRole('button', { name: 'Reject' }).click();
		await expect.poll(() => rejected).toEqual(['fill-e2e-1']);
		// The next request takes the entry's place without a reload.
		await expect(entry.locator('.pending-owner')).toHaveText('Payroll helper');
		await expect(entry.locator('.pending-site')).toHaveAttribute('title', secondSite);
		await expect(entry.locator('.pending-site')).toHaveText('billing.example.org');
	});

	for (const viewport of [{ width: 1440, height: 900 }, { width: 393, height: 852 }]) {
		test(`starts on the agent's asked-for time, approves until turned off, and shows no chip for it at ${viewport.width} px`, async ({ page, request }, testInfo) => {
			await page.setViewportSize(viewport);
			const title = `Saved login standing ${viewport.width} ${testInfo.project.name}-${Date.now()}`;
			const threadId = await createSession(request, title);
			let items: Record<string, unknown>[] = [fillRequest(threadId, 1, { requested_duration_seconds: 900 })];
			const decisions: { action: string; body: unknown }[] = [];
			await page.route('**/api/host-access/leases**', async (route) => {
				const url = new URL(route.request().url());
				if (route.request().method() === 'POST') {
					const match = /\/fills\/([^/]+)\/approve$/.exec(url.pathname);
					expect(match, url.pathname).toBeTruthy();
					decisions.push({ action: 'approve', body: route.request().postDataJSON() });
					const approved = { ...items[0], state: 'active', revision: 2, standing: true, duration_seconds: null, expires_at: null };
					// As the server does: the chat's lease list never carries a standing grant.
					items = [];
					await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(approved) });
					return;
				}
				const root = url.searchParams.get('root_thread_id');
				await route.fulfill({
					status: 200, contentType: 'application/json',
					body: JSON.stringify({ leases: root === threadId ? items : [] }),
				});
			});
			await loginByUrl(page, { waitForComposer: false });
			await selectThread(page, title);

			await page.locator('.pending-request-trigger').click();
			const dialog = page.getByRole('dialog');
			// One select named Duration, in time order, starting on the agent's asked-for time.
			const select = dialog.getByRole('combobox', { name: 'Duration' });
			await expect(select.locator('option')).toHaveText(['5 minutes', '15 minutes', '30 minutes', 'Until I turn it off']);
			await expect(select).toHaveValue('900');
			// One line, the full width of the sheet's content column, Field's own chevron, and a finger-sized target on touch.
			const box = await select.evaluate((el) => {
				const style = getComputedStyle(el);
				const column = el.closest('.decision-body')!;
				const columnStyle = getComputedStyle(column);
				const inner = column.getBoundingClientRect().width - parseFloat(columnStyle.paddingLeft) - parseFloat(columnStyle.paddingRight);
				return {
					width: el.getBoundingClientRect().width, inner, height: el.getBoundingClientRect().height,
					chevron: style.backgroundImage, radius: style.borderTopLeftRadius,
					coarse: matchMedia('(pointer: coarse)').matches,
					touchTarget: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--touch-target')) || 44,
				};
			});
			expect(Math.abs(box.width - box.inner), JSON.stringify(box)).toBeLessThanOrEqual(1);
			expect(box.chevron, JSON.stringify(box)).toContain('url(');
			if (box.coarse) expect(box.height, JSON.stringify(box)).toBeGreaterThanOrEqual(box.touchTarget);
			await expect(select).toBeInViewport({ ratio: 1 });
			await expect(dialog.getByText('CORSIA fills in the password on this site for the agent. The agent never sees it.', { exact: true })).toBeVisible();
			await expect(dialog).not.toContainText('revoke');
			await page.screenshot({ path: testInfo.outputPath(`requested-15-${viewport.width}.png`), animations: 'disabled' });

			await select.selectOption('until_revoked');
			await expect(select).toHaveValue('until_revoked');
			// No screen ends a standing grant yet, so the sheet never says it can be revoked.
			await expect(dialog).not.toContainText('revoke');
			await page.screenshot({ path: testInfo.outputPath(`until-turned-off-${viewport.width}.png`), animations: 'disabled' });
			await dialog.getByRole('button', { name: 'Approve' }).click();
			await expect.poll(() => decisions.length).toBe(1);
			expect(decisions[0]).toEqual({
				action: 'approve', body: { revision: 1, application_hash: 'hash-fill-e2e-1', until_revoked: true },
			});

			// A standing grant has no chip: no lock, no countdown and no drawer in the chat.
			await expect(page.locator('.pending-request-trigger')).toHaveCount(0);
			await expect(page.locator('.active-chip')).toHaveCount(0);
			await page.screenshot({ path: testInfo.outputPath(`after-standing-${viewport.width}.png`), animations: 'disabled' });
		});
	}
});
