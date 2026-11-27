/**
 * Build frames for the Activity decision view, paired with the approved frames by name (d-new, d-new-other, d-new-refused,
 * t-new, m-new, r-new, at 1440 and 393). Real app on the disposable mock E2E instance; Activity's read is supplied in the
 * wire shape. Frames go to ACTIVITY_OUT; without it the spec only asserts.
 */
import path from 'node:path';
import type { APIRequestContext, Page } from '@playwright/test';
import { test, expect } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { raiseAsk } from '../support/reviewAsk';

process.env.TZ = 'America/Los_Angeles';

const OUT = process.env.ACTIVITY_OUT;
const tok = () => encodeURIComponent(TEST_TOKEN);

test.use({ timezoneId: 'America/Los_Angeles', locale: 'en-US', colorScheme: 'dark', seedPerTest: true });
test.beforeEach(({}, info) => test.skip(info.project.name === 'chromium-mobile', 'each frame sets its own viewport'));


async function workspaceRoot(request: APIRequestContext): Promise<string> {
	const tree = await request.get(`/api/files/tree?token=${tok()}`);
	expect(tree.ok(), await tree.text()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const root = roots.find((r) => r.kind === 'workspace')?.path;
	expect(root).toEqual(expect.any(String));
	return root!;
}

async function writeDoc(request: APIRequestContext, root: string, threadId: string, name: string, text: string) {
	const created = await request.put(`/api/files/content?token=${tok()}`, {
		data: { path: path.join(root, name), text, thread_id: threadId, create: true },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).artifact.id as string;
}


const LABELS = ['CORSIA Conductor', 'Packaging & Onboarding', 'Ecosystem', 'Governance & Scopes'];

async function seedWorld(request: APIRequestContext) {
	const threads: Record<string, string> = {};
	for (const label of LABELS) threads[label] = await createSession(request, label);
	const root = await workspaceRoot(request);
	const stamp = Date.now().toString(36);
	const credentials = await writeDoc(request, root, threads['Governance & Scopes'], `credentials-design-${stamp}.md`,
		'# Agents sign in with your saved passwords\n\n**Approve the design?** An agent asks before it uses a saved password. You approve once per site for a set time.\n');
	raiseAsk(threads['Governance & Scopes'], credentials, 'Ready for review');
	return { threads, credentials, now: Date.now() };
}
type World = Awaited<ReturnType<typeof seedWorld>>;

const src = (w: World, label: string) => ({ thread_id: w.threads[label], thread_label: label, project_id: null, project_label: null, parent_reviewed: false });

const TITLE = 'Keep an internal notice out of a new person’s first chat?';

function items(w: World) {
	const at = (min: number) => new Date(w.now - min * 60_000).toISOString();
	const rank = { position: 1, reason: '0.5.0: a new person’s first chat gets a stray extra reply', ranked_by: w.threads['CORSIA Conductor'], ranked_at: at(60) };
	return [
		{
			key: 'decision:notice', external_id: 'decision:notice', renderer: 'decision', kind: 'decision', title: TITLE, raised_at: at(45),
			source: src(w, 'CORSIA Conductor'), open: null, rank,
			detail: {
				id: 'decision-notice', plan_id: 'plan-frames', node_id: 'node-decision', source_node_id: 'node-source', thread_id: w.threads['CORSIA Conductor'],
				title: TITLE,
				context: 'Goal: a stranger’s first hour with 0.5.0 holds up. Soon after first launch, CORSIA posts a ‘Corrections Classifier Activated’ notice in the person’s first chat. It’s an internal measurement step they don’t need, and their agent spends a model turn answering it.',
				tradeoff: 'Waking the lane takes a few turns, one PR and a review, with some risk of a late merge. Shipping as is costs nothing, but every new person sees an internal notice they can’t make sense of.',
				recommendation: 'wake', rationale: 'The first chat is the first thing a stranger sees, and the fix is small enough that it won’t hold up the publish.',
				other_enabled: true, order_key: 0, revision: 1, raised_at: at(45), source: src(w, 'CORSIA Conductor'),
				options: [
					{ id: 'wake', label: 'Wake Context, Memory & Learning for this one fix', detail: 'One small PR. It lands on dev today if it’s ready, and 0.5.0 doesn’t wait for it. The lane goes back to paused afterwards.' },
					{ id: 'ship', label: 'Ship 0.5.0 with it, fix it after', detail: 'It goes on the known-issues list. Every new install sees the notice and spends an extra turn on it until the fix ships.' },
				],
			},
		},
		{
			key: 'review:credentials', external_id: 'review:credentials', renderer: 'review', kind: 'review', title: 'Approve or change how agents sign in to sites with your saved passwords.', raised_at: at(120),
			source: { ...src(w, 'Governance & Scopes'), parent_reviewed: true }, open: { artifact_id: w.credentials }, rank: { ...rank, position: 2 }, detail: { artifact_id: w.credentials },
		},
		{
			key: 'task:sign-build', external_id: 'task:sign-build', renderer: 'task', kind: 'task', title: 'Sign the 0.5.0 build: enter the code Apple sends you when asked.', raised_at: at(180),
			source: { ...src(w, 'Packaging & Onboarding'), parent_reviewed: true }, open: null, rank: { ...rank, position: 3 },
			detail: { plan_id: 'plan-packaging', node_id: 'node-sign', revision: 4,
				description: '1. Packaging & Onboarding starts the signing. Your Mac shows an Apple sign-in.\n2. Sign in with your Apple ID and enter the six-digit code Apple sends to your phone.\n3. Press Done. The agent checks the build is signed before it counts.' },
		},
		{
			key: 'message:intel-mac', external_id: 'held-intel', renderer: 'held-reply', kind: 'message', title: 'Send Ecosystem’s reply on the public issue asking whether CORSIA runs on an Intel Mac?', raised_at: at(240),
			source: { ...src(w, 'Ecosystem'), parent_reviewed: true }, open: null, rank: { ...rank, position: 4 },
			detail: { id: 'held-intel', thread_id: w.threads['Ecosystem'], title: 'Send Ecosystem’s reply on the public issue asking whether CORSIA runs on an Intel Mac?',
				text: 'Thanks for asking. We haven’t tried 0.5.0 on an Intel Mac yet; we’ll post here once we have.',
				recipient: 'the public issue “Does CORSIA run on an Intel Mac?”', provider: 'GitHub', error: null, raised_at: at(240), source: src(w, 'Ecosystem') },
		},
	];
}

async function serve(page: Page, w: World, list: unknown[]) {
	await page.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
		status: 200, contentType: 'application/json',
		body: JSON.stringify({ count: list.length, items: list, ranker: { thread_id: w.threads['CORSIA Conductor'], label: 'CORSIA Conductor' } }),
	}));
}

async function shot(page: Page, name: string) {
	await page.evaluate(() => document.fonts.ready);
	await page.waitForTimeout(400);
	if (OUT) await page.screenshot({ path: path.join(OUT, name), animations: 'disabled' });
}

const rows = (page: Page) => page.locator('[data-activity-row]');

async function arrive(page: Page, width: number, height: number) {
	await page.setViewportSize({ width, height });
	await openAppPastLanding(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	if (width <= 768) {
		// Frame-only: the design draws iOS's 34px bottom safe area under the dock; a desktop browser has none, so it is set here
		// so the frames compare at the same height. It is not a product element.
		await page.addStyleTag({ content: ':root { --safe-bottom: 34px; }' });
	}
}

for (const [width, height, tag] of [[1440, 900, '1440'], [393, 852, '393']] as const) {
	test(`Activity decision view at ${tag}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const w = await seedWorld(request);
		await serve(page, w, items(w));
		await arrive(page, width, height);
		await expect(rows(page)).toHaveCount(4);
		const pane = page.locator('.pane-col');
		const open = async (index: number) => {
			// On a phone an item fills the screen; the way back is the header's list link.
			const back = page.locator('button, a').filter({ hasText: /^\s*Your tasks\s*$/ });
			if (tag === '393' && (await back.count()) > 0 && (await back.first().isVisible())) await back.first().click();
			await rows(page).nth(index).click();
			await expect(rows(page).nth(index)).toHaveAttribute('aria-current', 'true');
		};
		if (tag === '393') await open(0);
		await expect(pane.getByRole('button', { name: 'Answer', exact: true })).toBeVisible({ timeout: 15_000 });
		await pane.getByLabel('Wake Context, Memory & Learning for this one fix', { exact: false }).first().check({ force: true });
		await page.mouse.move(width / 2, height - 4);
		await shot(page, `d-new-${tag}.png`);
		await pane.getByRole('radio', { name: /Something else/ }).check({ force: true });
		await pane.getByLabel('Your answer').fill('Wake it, but only after the first-chat test passes on the release build.');
		await shot(page, `d-new-other-${tag}.png`);
		// Answered in another chat first: the grey line, the item stays open.
		await page.route('**/api/plans/decisions/decision-notice/answer', (route) => route.fulfill({
			status: 410, contentType: 'application/json', body: JSON.stringify({ detail: 'This decision was answered in another chat.' }),
		}));
		await pane.getByRole('button', { name: 'Answer', exact: true }).click();
		await expect(pane.getByText(/It stays open here until you pick another item/)).toBeVisible();
		await shot(page, `d-new-refused-${tag}.png`);
		await page.unroute('**/api/plans/decisions/decision-notice/answer');

		await open(2);
		await expect(pane.getByRole('button', { name: 'Done', exact: true })).toBeVisible({ timeout: 15_000 });
		await shot(page, `t-new-${tag}.png`);
		await open(3);
		await expect(pane.getByRole('button', { name: 'Send', exact: true })).toBeVisible({ timeout: 15_000 });
		await shot(page, `m-new-${tag}.png`);
		await open(1);
		await expect(pane.getByRole('button', { name: 'Approve', exact: true })).toBeVisible({ timeout: 15_000 });
		await shot(page, `r-new-${tag}.png`);
	});
}
