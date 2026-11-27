/**
 * Build frames for the Activity decision view, paired with the approved frames by name (d-new, d-new-other, d-new-refused,
 * t-new, m-new, r-new, at 1440 and 393). Real app on the disposable mock E2E instance; Activity's read is supplied in the
 * wire shape. Frames go to ACTIVITY_OUT; without it the spec only asserts.
 */
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import type { APIRequestContext, Page } from '@playwright/test';
import { test, expect } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { raiseAsk } from '../support/reviewAsk';
import { mockEmailDraftContext, markEmailDraft, reply } from '../support/emailDraftContext';

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


const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

/** An agent's person step linked to its instructions document (the plan service's `add` as the owning thread), in the isolated mock state. */
function raiseStep(threadId: string, artifactId: string, title: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The step fixture requires isolated E2E mock state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'thread, artifact, title = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'db.commit()',
		'mutate(plan.id, "add", {"title": title, "acceptance_criteria": "Connected", "size": "unknown", "intent": "Connect the shared bank account", "assignee": "user", "instructions_artifact_id": artifact, "description": "Open the document and follow its steps. Already tried: nothing; only you can approve this with the bank."},',
		'  actor=PlanActor(thread, user, None), provenance="e2e.person_step", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
	].join('\n'), threadId, artifactId, title], {
		cwd: path.join(repoRoot, 'backend'), encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}

const BANK_DOC = [
	'# How to connect the account', '',
	'This takes ten minutes. Only you can do it: the bank sends a one-time code to your phone and asks you, not an agent, to approve the connection.', '',
	'### Before you start', '',
	'- Your online banking login for the shared account.', '- Your phone, with the bank’s text messages turned on.', '',
	'### Steps', '',
	'1. Open your bank’s website and sign in.', '2. Choose **Settings**, then **Connected apps**.', '3. Choose **Add an app** and pick **CORSIA** from the list.', '4. Enter the six-digit code the bank texts you.', '5. Choose **Approve** and come back here.', '',
	'### If the code does not arrive', '',
	'Wait two minutes, then ask the bank to send it again. Check that the number the bank has on file is the phone in your hand.', '',
	'### What you will see at the end', '',
	'The bank shows “CORSIA is connected” and lists the shared account. When you press Done, the agent checks the connection itself.', '',
].join('\n');

const DRAFT_DOC = [
	'From: Alex Rivera <alex.rivera@example.com>', 'To: dana.whitfield@example.com', 'Subject: Re: Saturday trail work: meeting point', '',
	'Hi Dana,', '', 'Twelve of us can come Saturday. Could you confirm the meeting spot is still the north trailhead lot, not the church lot?', '',
	'Thanks,', 'Alex', '',
].join('\n');

const LABELS = ['CORSIA Conductor', 'Packaging & Onboarding', 'Ecosystem', 'Governance & Scopes'];

async function seedWorld(request: APIRequestContext) {
	const threads: Record<string, string> = {};
	for (const label of LABELS) threads[label] = await createSession(request, label);
	const root = await workspaceRoot(request);
	const stamp = Date.now().toString(36);
	const credentials = await writeDoc(request, root, threads['Governance & Scopes'], `credentials-design-${stamp}.md`,
		'# Agents sign in with your saved passwords\n\n**Approve the design?** An agent asks before it uses a saved password. You approve once per site for a set time.\n');
	raiseAsk(threads['Governance & Scopes'], credentials, 'Ready for review');
	const conductor = threads['CORSIA Conductor'];
	const bankPath = path.join(root, `connect-the-shared-bank-account-${stamp}.md`);
	const bank = await writeDoc(request, root, conductor, path.basename(bankPath), BANK_DOC);
	raiseStep(conductor, bank, 'Connect the shared bank account');
	const draftPath = path.join(root, `saturday-trail-work-${stamp}.md`);
	const draft = await writeDoc(request, root, conductor, path.basename(draftPath), DRAFT_DOC);
	return { threads, credentials, bank, draft, draftPath, now: Date.now() };
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
				context: 'Goal: a stranger’s first hour with 0.5.0 holds up. Soon after first launch, CORSIA posts a ‘Corrections Classifier Activated’ notice in the person’s first chat. It’s an internal measurement step they don’t need, and their agent spends a model turn answering it. This happens on every new install, once per classifier version, and has since October. Product Experience found it today in a first-send test, and I read the code that sends it. The likely fix is to send the notice as a background note the agent reads at its next turn, with nothing shown in chat and no turn of its own. The code belongs to Context, Memory & Learning, which is paused, so only you can wake it.',
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

/** A phone frame's scroll, as the approved frames are: the options label's top `at`px from the top (the decision frames: 152; the pick-any and long ones: 146, the page's 108 plus the app header the page leaves out), or the end of the pane. */
async function phoneScroll(page: Page, tag: string, to: 'options' | 'end' | 'top', at = 152) {
	if (tag !== '393') return;
	await page.locator('.pane-col .scroll').evaluate((el, { to, at }) => {
		const label = el.querySelector('.opts-label');
		if (to === 'options' && label) el.scrollTop += label.getBoundingClientRect().top - at;
		else el.scrollTop = to === 'end' ? el.scrollHeight : 0;
	}, { to, at });
	await page.waitForTimeout(200);
}

async function openItem(page: Page, tag: string, index: number) {
	const back = page.locator('button, a').filter({ hasText: /^\s*Your tasks\s*$/ });
	if (tag === '393' && (await back.count()) > 0 && (await back.first().isVisible())) await back.first().click({ timeout: 3_000 }).catch(() => {}); // the way back leaves the page once the list is showing
	await rows(page).nth(index).click();
	await expect(rows(page).nth(index)).toHaveAttribute('aria-current', 'true');
}

const VIEWS = [[1440, 900, '1440'], [393, 852, '393']] as const;

const decisionItem = (w: World, spec: { id: string; title: string; context: string; recommendation: string | string[]; pick?: 'any'; rationale?: string; options: Array<{ id: string; label: string; detail: string; rationale?: string }> }) => {
	const base = items(w)[0] as { detail: Record<string, unknown> } & Record<string, unknown>;
	return {
		...base, key: `decision:${spec.id}`, external_id: `decision:${spec.id}`, title: spec.title,
		detail: { ...base.detail, id: `decision-${spec.id}`, title: spec.title, context: spec.context, tradeoff: null, rationale: spec.rationale ?? null, recommendation: spec.recommendation, pick: spec.pick, options: spec.options },
	};
};
const MULTI_ITEM = (w: World) => decisionItem(w, {
	id: 'multi', title: 'Which first-hour checks should 0.5.0 run before it says anything in a new person’s chat?', recommendation: ['a', 'b'], pick: 'any',
	context: 'Each check adds a model turn to the person’s first hour. Pick every one you want on for 0.5.0; the rest stay off until after publish. Together the first two cost 40 seconds on a laptop with the local model, and the third is instant.',
	options: [
		{ id: 'a', label: 'Model download finished', detail: 'Waits for the local model to finish downloading and says so once, in one line.' },
		{ id: 'b', label: 'Connected-provider test message', detail: 'Sends one short test message so a broken key shows up before the first real question.' },
		{ id: 'c', label: 'Instruction-file overlap check', detail: 'Flags a workspace line that a project file contradicts. It runs instantly and says nothing when there is none.' },
	],
});
const LONG_ITEM = (w: World) => decisionItem(w, {
	id: 'long', title: 'Where should the first-hour checks report their result?', recommendation: 'b', rationale: 'A stranger reads one note instead of four interruptions.',
	context: 'Each check finishes at a different time during the person’s first hour. The choice decides whether they see one result or several, and whether any of it sits in the chat they are using.',
	options: [
		{ id: 'a', label: 'In the person’s first chat as one short line per check, grouped under a single heading, with each line expandable for detail and the whole group collapsible once it has been read', detail: 'Every check gets its own line the moment it finishes, so a failed provider key is visible immediately. The heading collapses after the person reads it, and expanding a line shows the full test output, the time it took and the setting that controls it. Costs one chat message per check.' },
		{ id: 'b', label: 'In one summary note when the last check finishes', detail: 'One message, nothing mid-hour.' },
		{ id: 'c', label: 'In Settings only', detail: 'Nothing appears in chat; the person finds it in Settings, under Health.' },
		{ id: 'd', label: 'Only when a check fails', detail: 'Quiet when all is well; a failure appears as one line in chat.' },
		{ id: 'e', label: 'In the agent’s next turn as a background note', detail: 'The agent reads it and mentions it only if it matters.' },
	],
});

for (const [width, height, tag] of VIEWS) {
	test(`Activity decision variants at ${tag}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const w = await seedWorld(request);
		const pane = page.locator('.pane-col');
		const settle = () => page.mouse.move(width / 2, height - 4);
		const scene = async (list: unknown[]) => { await serve(page, w, list); await arrive(page, width, height); await expect(rows(page)).toHaveCount(list.length); await openItem(page, tag, 0); await expect(pane.getByRole('button', { name: 'Answer', exact: true })).toBeVisible({ timeout: 15_000 }); };
		// Pick any: the two recommended options ticked.
		await scene([MULTI_ITEM(w)]);
		await pane.getByRole('checkbox', { name: /Model download finished/ }).check({ force: true });
		await pane.getByRole('checkbox', { name: /Connected-provider test message/ }).check({ force: true });
		await pane.getByLabel('Note (optional)').fill('Keep the notices to one line each.');
		await pane.getByLabel('Note (optional)').blur();
		await settle(); await phoneScroll(page, tag, 'options', 146);
		await shot(page, `d-new-multi-${tag}.png`);
		await page.emulateMedia({ colorScheme: 'light' });
		await shot(page, `d-new-multi-light-${tag}.png`);
		await page.emulateMedia({ colorScheme: 'dark' });
		// Long titles and details.
		await page.unroute(/\/api\/attention\/queue\?(.*&)?view=activity/);
		await serve(page, w, [LONG_ITEM(w)]);
		await page.reload(); await openAppPastLanding(page);
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click().catch(() => {});
		await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
		await openItem(page, tag, 0);
		await expect(pane.getByRole('button', { name: 'Answer', exact: true })).toBeVisible({ timeout: 15_000 });
		await pane.getByRole('radio', { name: /In one summary note/ }).check({ force: true });
		await settle(); await phoneScroll(page, tag, 'options', 146);
		await shot(page, `d-new-long-${tag}.png`);
		await page.emulateMedia({ colorScheme: 'light' });
		await shot(page, `d-new-long-light-${tag}.png`);
		await page.emulateMedia({ colorScheme: 'dark' });
	});

	test(`Activity decision light and top at ${tag}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const w = await seedWorld(request);
		await serve(page, w, items(w));
		await arrive(page, width, height);
		await expect(rows(page)).toHaveCount(4);
		const pane = page.locator('.pane-col');
		await openItem(page, tag, 0);
		await expect(pane.getByRole('button', { name: 'Answer', exact: true })).toBeVisible({ timeout: 15_000 });
		if (tag === '1440') {
			// Keyboard focus ring on an option row, with nothing picked yet.
			await page.keyboard.press('Shift');
			await pane.getByRole('radio', { name: /Ship 0.5.0 with it/ }).focus();
			await shot(page, 'd-new-focus-option-1440.png');
		}
		await pane.getByLabel('Wake Context, Memory & Learning for this one fix', { exact: false }).first().check({ force: true });
		await page.mouse.move(width / 2, height - 4);
		await phoneScroll(page, tag, 'top');
		await shot(page, `d-new-top-${tag}.png`);
		await phoneScroll(page, tag, 'options');
		await page.emulateMedia({ colorScheme: 'light' });
		await shot(page, `d-new-light-${tag}.png`);
		await page.emulateMedia({ colorScheme: 'dark' });
		if (tag === '1440') {
			// Keyboard focus rings: the Answer button, the asker's chat link.
			await page.keyboard.press('Shift');
			await pane.getByRole('button', { name: 'Answer', exact: true }).focus();
			await shot(page, 'd-new-focus-answer-1440.png');
			await pane.getByRole('button', { name: /Open chat with CORSIA Conductor/ }).first().focus();
			await shot(page, 'd-new-link-focus-1440.png');
		}
	});

	test(`Activity task with document and email draft at ${tag}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const w = await seedWorld(request);
		const at = (min: number) => new Date(w.now - min * 60_000).toISOString();
		const conductor = w.threads['CORSIA Conductor'];
		const rank = { position: 1, reason: 'First', ranked_by: conductor, ranked_at: at(60) };
		const taskDoc = { key: 'task:bank', external_id: 'task:bank', renderer: 'task', kind: 'task', title: 'Connect the shared bank account', raised_at: at(180), source: { ...src(w, 'CORSIA Conductor'), parent_reviewed: true }, open: { artifact_id: w.bank }, rank, detail: { plan_id: 'plan-bank', node_id: 'node-bank', revision: 1, description: null } };
		const draft = { key: 'review:draft', external_id: 'review:draft', renderer: 'review', kind: 'review', title: 'Send: Saturday trail work: meeting point', raised_at: at(3), source: { ...src(w, 'CORSIA Conductor'), parent_reviewed: true }, open: { artifact_id: w.draft }, rank, detail: { artifact_id: w.draft } };
		// The step needs a description to be raised at all; the approved frame draws a task that says only what waits, so the read drops it.
		await page.route(/\/api\/attention\/[^/]+\/review(\?.*)?$/, async (route) => {
			const response = await route.fetch();
			const body = await response.json();
			if (body.person_step) body.person_step.node_description = '';
			await route.fulfill({ response, json: body });
		});
		await markEmailDraft(page, w.draftPath);
		await mockEmailDraftContext(page, () => reply());
		await serve(page, w, [taskDoc, draft]);
		await arrive(page, width, height);
		await expect(rows(page)).toHaveCount(2);
		await openItem(page, tag, 0);
		await expect(page.locator('.pane-col').getByRole('button', { name: 'Done', exact: true })).toBeVisible({ timeout: 20_000 });
		await page.mouse.move(width / 2, height - 4);
		await shot(page, `t-doc-${tag}.png`);
		await openItem(page, tag, 1);
		await expect(page.locator('.pane-col').getByRole('button', { name: /Send/ }).first()).toBeVisible({ timeout: 20_000 });
		await page.mouse.move(width / 2, height - 4);
		await shot(page, `e-new-${tag}.png`);
	});
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
		await phoneScroll(page, tag, 'options');
		await shot(page, `d-new-${tag}.png`);
		await pane.getByRole('radio', { name: /Something else/ }).check({ force: true });
		await pane.getByLabel('Your answer').fill('Wake it, but only after the first-chat test passes on the release build.');
		await phoneScroll(page, tag, 'end');
		await shot(page, `d-new-other-${tag}.png`);
		// Answered in another chat first: the grey line, the item stays open.
		await page.route('**/api/plans/decisions/decision-notice/answer', (route) => route.fulfill({
			status: 410, contentType: 'application/json', body: JSON.stringify({ detail: 'This decision was answered in another chat.' }),
		}));
		await pane.getByRole('button', { name: 'Answer', exact: true }).click();
		await expect(pane.getByText(/It stays open here until you pick another item/)).toBeVisible();
		await phoneScroll(page, tag, 'end');
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
