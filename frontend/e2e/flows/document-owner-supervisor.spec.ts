import { test } from '../support/test';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { chatComposer, loginByUrl, sendButton, TEST_TOKEN } from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';
import { suggest } from '../support/liveConcurrency';
import type { LiveDocument } from '../support/liveDocument';
import { overlaps, ownerItem, rolePicker, seedOwnedDocument, supervisorItem } from '../support/documentOwnerSupervisor';

/**
 * The person sees who owns and who supervises a document and changes each one on the document, at wide and phone
 * widths: two separate items, each opening a picker for that role only; a pick saves; a failed save says so; the
 * person can take the document over, after which an agent's edit is refused and it suggests instead; and Request
 * review never covers the owner and supervisor items, nor they it.
 */

const box = async (locator: Locator) => (await locator.boundingBox())!;

/** The element at the centre of `locator` is inside it: nothing covers it. */
async function reachable(locator: Locator, step: string) {
	await expect(locator, step).toBeVisible();
	const covered = await locator.evaluate((element) => {
		const r = element.getBoundingClientRect();
		const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
		return hit && (element === hit || element.contains(hit)) ? '' : `${hit?.tagName}.${hit?.className}`;
	});
	expect(covered, step).toBe('');
}

async function atRest(page: Page, touch: boolean, step: string) {
	const owner = ownerItem(page), supervisor = supervisorItem(page);
	await expect(owner).toBeVisible(); await expect(supervisor).toBeVisible();
	const [a, b] = [await box(owner), await box(supervisor)];
	expect(overlaps(a, b), `${step}: items overlap`).toBe(false);
	const viewport = page.viewportSize()!;
	for (const item of [a, b]) {
		expect(item.x, `${step}: item starts off screen`).toBeGreaterThanOrEqual(0);
		expect(item.x + item.width, `${step}: item ends off screen`).toBeLessThanOrEqual(viewport.width + 0.5);
		if (touch) expect(item.height, `${step}: tap target under 44px`).toBeGreaterThanOrEqual(44);
	}
}

type Seeded = Awaited<ReturnType<typeof seedOwnedDocument>>;

async function pick(page: Page, seeded: Seeded, role: 'owner' | 'supervisor', name: string) {
	const item = role === 'owner' ? ownerItem(page) : supervisorItem(page);
	await item.click();
	const picker = rolePicker(page);
	await expect(picker).toHaveAttribute('data-role', role);
	await expect(page.locator('[data-popover] .role-picker')).toHaveCount(1);
	const b = await box(picker), viewport = page.viewportSize()!;
	expect(b.x).toBeGreaterThanOrEqual(0); expect(b.x + b.width).toBeLessThanOrEqual(viewport.width + 0.5);
	await seeded.option(picker, name).click();
	await expect(picker).toHaveCount(0);
}

async function agentEditIsRefused(page: Page, request: APIRequestContext, owner: string, docPath: string) {
	const read = async () => (await (await request.get('/api/files/content', { params: { token: TEST_TOKEN, path: docPath } })).json()).text as string;
	const before = await read();
	const agentPage = await page.context().newPage();
	await agentPage.setViewportSize({ width: 1440, height: 900 });
	await loginByUrl(agentPage);
	await agentPage.locator('.thread-item').filter({ hasText: 'Release planning' }).first().click();
	const stamp = `${Date.now()}`;
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: docPath }) }], finish_reason: 'tool_calls', match_thread: owner },
		{ tool_calls: [{ id: `edit-${stamp}`, name: 'edit_file', arguments: JSON.stringify({ path: docPath, old_string: 'Plan a community garden', new_string: 'Plan a rooftop garden' }) }], finish_reason: 'tool_calls', match_thread: owner },
		{ content: 'done', finish_reason: 'stop', match_thread: owner },
	]);
	const calls = await readMockCallCount(request);
	// The person's own page may be a touch device; the agent's chat is sent with its Send button either way.
	await chatComposer(agentPage).fill('Change the document.');
	await sendButton(agentPage).click();
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBeGreaterThanOrEqual(calls + 3);
	const messages = async () => JSON.stringify(await (await request.get(`/api/threads/${owner}/messages`, { params: { token: TEST_TOKEN } })).json());
	await expect.poll(messages, { timeout: 20_000 }).toContain('edits it directly');
	expect(await read()).toBe(before);
	await agentPage.close();
}

/** Opens Request review and checks it never covers the owner and supervisor items; returns which surface it used. */
async function reviewLeavesOwnershipReachable(page: Page): Promise<'drawer' | 'popover'> {
	await page.locator('.document-tools').getByRole('button', { name: 'Request review', exact: true }).click();
	const reviews = page.locator('[data-review-surface]');
	await expect(reviews).toBeVisible();
	await page.waitForTimeout(400);
	const drawer = page.locator('[data-adaptive-drawer] .drawer-panel');
	const kind = await drawer.count() ? 'drawer' : 'popover';
	const panel = kind === 'drawer' ? drawer : page.locator('[data-popover]').filter({ has: reviews });
	for (const item of [ownerItem(page), supervisorItem(page)]) {
		await expect(item).toBeVisible();
		expect(overlaps(await box(item), await box(panel)), `the review ${kind} covers ${await item.getAttribute('data-role')}`).toBe(false);
	}
	await reachable(page.getByRole('combobox', { name: 'Add reviewers' }), `reviewer search reachable in the ${kind}`);
	// The popover leaves the document's own heading readable: the text of the H1 is never under the card.
	if (kind === 'popover') {
		const heading = await page.locator('.cm-content .cm-md-h1').first().evaluate((element) => {
			const range = document.createRange(); range.selectNodeContents(element);
			const { x, y, width, height } = range.getBoundingClientRect(); return { x, y, width, height };
		});
		expect(overlaps(heading, await box(panel)), 'the review popover covers the document heading').toBe(false);
	}
	// A drawer is a sheet: Close returns the document, and the items take the next tap. The popover leaves them live.
	if (kind === 'drawer') { await page.getByRole('button', { name: 'Close', exact: true }).click(); await expect(reviews).toHaveCount(0); }
	for (const item of [ownerItem(page), supervisorItem(page)]) await reachable(item, `${await item.getAttribute('data-role')} reachable after the review ${kind}`);
	await ownerItem(page).click();
	await expect(rolePicker(page)).toHaveAttribute('data-role', 'owner');
	await ownerItem(page).click();
	await expect(rolePicker(page)).toHaveCount(0);
	if (kind === 'popover') await expect(reviews).toHaveCount(0);
	return kind;
}

for (const [label, width, height] of [['wide', 1440, 1000], ['phone', 393, 852]] as const) {
	test(`@document-owner-supervisor the person changes owner and supervisor separately, takes the document over, and requests review at ${label} width`, async ({ page, request }, info) => {
		const touch = info.project.name.includes('mobile');
		test.skip(touch !== (label === 'phone'), 'wide runs on the desktop project, phone on the mobile one');
		test.setTimeout(180_000);
		await page.setViewportSize({ width, height });
		// A heading long enough to run under the popover's column, so a card placed over it would be caught.
		const seeded = await seedOwnedDocument(page, request, 'Project planning for the community garden and the neighbourhood volunteers who tend it');
		await seeded.open(page);

		// At rest: two plainly labelled items.
		await expect(ownerItem(page)).toHaveAccessibleName('Owner: Release planning');
		await expect(supervisorItem(page)).toHaveAccessibleName('Supervisor: Research coordinator');
		await atRest(page, touch, 'agent owns');

		// Each item opens a picker for its own role only; "You" is offered only as owner.
		await ownerItem(page).click();
		await expect(rolePicker(page)).toHaveAttribute('data-role', 'owner');
		await expect(rolePicker(page).getByRole('option', { name: /^You/ })).toBeVisible();
		await reachable(page.locator('.document-tools').getByRole('button', { name: 'Request review', exact: true }), 'owner picker leaves Request review reachable');
		await supervisorItem(page).click();
		await expect(rolePicker(page)).toHaveAttribute('data-role', 'supervisor');
		await expect(rolePicker(page).getByRole('option', { name: /^You/ })).toHaveCount(0);
		await supervisorItem(page).click();
		await expect(rolePicker(page)).toHaveCount(0);

		// A pick saves at once and is there when the document is opened again.
		await pick(page, seeded, 'supervisor', 'Garden volunteers');
		await expect(supervisorItem(page)).toHaveAccessibleName('Supervisor: Garden volunteers');
		await seeded.open(page);
		await expect(supervisorItem(page)).toHaveAccessibleName('Supervisor: Garden volunteers');

		// A failed save keeps the old owner and says so.
		await page.route('**/api/artifacts/*/ownership', (route) => route.fulfill({ status: 500, body: '{"detail":"boom"}' }));
		await pick(page, seeded, 'owner', 'Planning specialist');
		await expect(ownerItem(page)).toContainText('Couldn’t change');
		await expect(ownerItem(page)).toContainText('Release planning');
		await page.unroute('**/api/artifacts/*/ownership');

		// The person takes the document over; the picked supervisor stays.
		await pick(page, seeded, 'owner', 'You');
		await expect(ownerItem(page)).toHaveAccessibleName('Owner: You');
		await expect(supervisorItem(page)).toHaveAccessibleName('Supervisor: Garden volunteers');
		await atRest(page, touch, 'you own');
		for (const item of [ownerItem(page), supervisorItem(page)]) {
			await item.click();
			await expect(rolePicker(page)).toHaveAttribute('data-role', await item.getAttribute('data-role') ?? '');
			await item.click();
			await expect(rolePicker(page)).toHaveCount(0);
		}

		// Now an agent's edit is refused and it suggests instead; the suggestion shows on the document.
		await agentEditIsRefused(page, request, seeded.owner, seeded.docPath);
		const doc = { artifactId: seeded.artifact, docPath: seeded.docPath } as LiveDocument;
		await suggest(request, doc, seeded.owner, 'Plan a community garden', 'Plan a rooftop garden', 'Suggested wording');
		await expect(page.locator('.cm-suggestion-insert, .cm-suggestion-del').first()).toBeVisible({ timeout: 20_000 });

		// Request review open: it and the owner and supervisor items never cover each other, and both stay reachable.
		expect(await reviewLeavesOwnershipReachable(page)).toBe('drawer');
		if (!touch) {
			// A wider document canvas gets the popover, which opens below the items rather than over them.
			await page.getByRole('button', { name: 'Toggle panel' }).click();
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			expect(await reviewLeavesOwnershipReachable(page)).toBe('popover');
		}
	});
}

test('@document-owner-supervisor the items stay separate tap targets at 320 width', async ({ page, request }, info) => {
	test.skip(!info.project.name.includes('mobile'), 'phone only');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 320, height: 640 });
	const seeded = await seedOwnedDocument(page, request);
	await seeded.open(page);
	await atRest(page, true, '320');
	await expect(ownerItem(page)).toContainText('Release planning');
	await expect(supervisorItem(page)).toContainText('Research coordinator');
});
