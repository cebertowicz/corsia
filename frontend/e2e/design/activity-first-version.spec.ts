/**
 * Build frames for Activity, first version: the nine approved frames' states in the real app.
 *
 * The real app on the disposable mock E2E instance. Real threads, documents, a real review ask and a real comment
 * reply come from the API; Activity's own read (`/api/attention/queue?view=activity`) is supplied with the frames'
 * items in the Contract's wire shape, since the server half lands separately. Run through `scripts/e2e-local` in
 * mock mode. Frames go to ACTIVITY_OUT; without it the spec only asserts and writes nothing into the checkout.
 */
import path from 'node:path';
import type { APIRequestContext, Page } from '@playwright/test';
import { test, expect } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { raiseAsk } from '../support/reviewAsk';
import { isolatedDb } from '../support/imageRegionComments';

process.env.TZ = 'America/Los_Angeles';

const OUT = process.env.ACTIVITY_OUT;
const tok = () => encodeURIComponent(TEST_TOKEN);
const HOUR = 3_600_000;

test.use({ timezoneId: 'America/Los_Angeles', locale: 'en-US', colorScheme: 'light', seedPerTest: true });
test.beforeEach(({}, info) => test.skip(info.project.name === 'chromium-mobile', 'each frame sets its own viewport'));

type Kind = 'review' | 'decision' | 'task' | 'reply' | 'message';
interface Source { thread_id: string; thread_label: string; project_id: null; project_label: null; parent_reviewed: true }
interface World { now: number; rankedAt: string; threads: Record<string, string>; docs: Record<string, string>; ranker: string; comment: { rootId: string; replyId: string } }

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

const LABELS = [
	'Governance & Scopes', 'CORSIA Conductor', 'Agent Substrate', 'Ecosystem', 'Packaging & Onboarding',
	'Structured Plans', 'Quality Gate', 'Browser',
];

/** The frames' threads and documents, made for real: a review ask on each review document and an answered comment. */
async function seedWorld(request: APIRequestContext): Promise<World> {
	const threads: Record<string, string> = {};
	for (const label of LABELS) threads[label] = await createSession(request, label);
	const root = await workspaceRoot(request);
	const stamp = Date.now().toString(36);
	const docs: Record<string, string> = {
		credentials: await writeDoc(request, root, threads['Governance & Scopes'], `credentials-design-${stamp}.md`,
			'# Agents sign in with your saved passwords\n\n**Approve the design?** An agent asks before it uses a saved password. You approve once per site for a set time, unless you gave that agent standing access.\n\n## How it works\n\nThe agent names the site, the account and how long your approval lasts. You can take the approval back at any time from Settings.\n'),
		instructions: await writeDoc(request, root, threads['Agent Substrate'], `instruction-files-${stamp}.md`,
			'# Instruction files\n\nThe workspace, project and agent files every agent loads.\n'),
		readme: await writeDoc(request, root, threads['Ecosystem'], `README-${stamp}.md`, '# CORSIA\n\nDownload first.\n'),
		later: await writeDoc(request, root, threads['Structured Plans'], `later-look-${stamp}.md`,
			'# Plan pane: the Later row\n\nItems you set aside sit under a folded **Later** row at the bottom of the plan, with their count.\n'),
		whenToAsk: await writeDoc(request, root, threads['Agent Substrate'], `when-to-ask-${stamp}.md`,
			'# When agents ask you, and when they decide\n\n**Approve the wording?** Every agent loads it on each turn.\n'),
	};
	raiseAsk(threads['Governance & Scopes'], docs.credentials, 'Ready for review');
	raiseAsk(threads['Agent Substrate'], docs.whenToAsk, 'Ready for review');
	const comment = await answeredComment(request, docs.later, threads['Structured Plans']);
	const now = Date.now();
	const ranked = new Date(now);
	ranked.setHours(20, 10, 0, 0);
	if (ranked.getTime() > now) ranked.setDate(ranked.getDate() - 1);
	return { now, rankedAt: ranked.toISOString(), threads, docs, ranker: threads['CORSIA Conductor'], comment };
}

/** The person's comment on "Later" and the agent's answer to it, written as that agent. */
async function answeredComment(request: APIRequestContext, artifactId: string, agentThread: string) {
	const text = '# Plan pane: the Later row\n\nItems you set aside sit under a folded **Later** row';
	const from = text.indexOf('Later row');
	const root = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: 'Why is Later folded by default? I lose track of what’s in it.', anchor_text: 'Later', from_off: from, to_off: from + 5, delivery_policy: 'immediate' },
	});
	expect(root.ok(), await root.text()).toBeTruthy();
	const rootId = (await root.json()).comment.id as string;
	const reply = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: 'It stays folded so the plan shows what’s moving. The row now shows the count and the oldest item’s age, so you can tell when to open it.', parent_id: rootId, delivery_policy: 'immediate' },
	});
	expect(reply.ok(), await reply.text()).toBeTruthy();
	const replyId = (await reply.json()).comment.id as string;
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Structured Plans", sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], agentThread, replyId);
	return { rootId, replyId };
}

function source(w: World, label: string): Source {
	return { thread_id: w.threads[label], thread_label: label, project_id: null, project_label: null, parent_reviewed: true };
}

const RENDERER: Record<Kind, string> = { review: 'review', decision: 'decision', task: 'task', reply: 'reply', message: 'held-reply' };

function item(w: World, key: string, kind: Kind, title: string, label: string, hours: number, position: number, reason: string, extra: Record<string, unknown>) {
	return {
		key, external_id: key, renderer: RENDERER[kind], kind, title,
		raised_at: new Date(w.now - hours * HOUR).toISOString(), source: source(w, label),
		open: null as unknown, rank: { position, reason, ranked_by: w.ranker, ranked_at: w.rankedAt },
		...extra,
	};
}

/** The seven items the approved frames draw, in their order. */
function frameItems(w: World) {
	const review = (doc: string) => ({ open: { artifact_id: w.docs[doc] }, detail: { artifact_id: w.docs[doc] } });
	return [
		item(w, 'review:credentials', 'review', 'Approve or change how agents sign in to sites with your saved passwords.', 'Governance & Scopes', 24 * 5, 1,
			'0.5.0 publishes Nov 24, and agents can’t sign in to a site without you there.', review('credentials')),
		item(w, 'decision:docs-review', 'decision', 'Ship 0.5.0 without each paused agent reviewing its public docs, or wake them to do it?', 'CORSIA Conductor', 1, 2,
			'It decides whether 0.5.0 can publish on your date.', { detail: {
				id: 'decision-docs', plan_id: 'plan-frames', node_id: 'node-decision', source_node_id: 'node-source', thread_id: w.ranker,
				title: 'Ship 0.5.0 without each paused agent reviewing its public docs, or wake them to do it?',
				context: 'The release steps say each agent reviews its public docs before a release. Seven of the owners are paused.',
				tradeoff: '', recommendation: 'ship', rationale: '', other_enabled: false, order_key: 0, revision: 1,
				raised_at: new Date(w.now - HOUR).toISOString(), source: source(w, 'CORSIA Conductor'),
				options: [
					{ id: 'ship', label: 'Ship without their review', detail: 'The conductor checks the docs against the build itself. Faster; the owners’ detail is missed.' },
					{ id: 'wake', label: 'Wake the seven to review', detail: 'Each reviews its own docs tonight. Slower; costs their runs.' },
				],
			} }),
		item(w, 'review:instructions', 'review', 'Read the final workspace, project and agent instruction files before they ship.', 'Agent Substrate', 2, 3,
			'Every agent in 0.5.0 loads these.', review('instructions')),
		item(w, 'review:readme', 'review', 'Approve the README: download first, the licence, and what it costs to run.', 'Ecosystem', 3, 4,
			'It’s the first page a stranger reads.', review('readme')),
		item(w, 'task:sign-build', 'task', 'Sign the 0.5.0 build: enter the code Apple sends you when asked.', 'Packaging & Onboarding', 3, 5,
			'An unsigned Mac app won’t open for a stranger.', { detail: {
				plan_id: 'plan-packaging', node_id: 'node-sign', revision: 4,
				description: '1. Packaging & Onboarding starts the signing. Your Mac shows an Apple sign-in.\n2. Sign in with your Apple ID and enter the six-digit code Apple sends to your phone.\n3. Press Done. The agent checks the build is signed before it counts.',
			} }),
		item(w, 'message:intel-mac', 'message', 'Send Ecosystem’s reply on the public issue asking whether CORSIA runs on an Intel Mac?', 'Ecosystem', 4, 6,
			'It goes to someone outside, so it waits for your yes.', { external_id: 'held-intel', detail: {
				id: 'held-intel', thread_id: w.threads['Ecosystem'],
				title: 'Send Ecosystem’s reply on the public issue asking whether CORSIA runs on an Intel Mac?',
				text: 'Thanks for asking. We haven’t tried 0.5.0 on an Intel Mac yet; we’ll post here once we have.',
				recipient: 'the public issue “Does CORSIA run on an Intel Mac?”', provider: 'GitHub', error: null,
				raised_at: new Date(w.now - 4 * HOUR).toISOString(), source: source(w, 'Ecosystem'),
			} }),
		item(w, 'reply:later-row', 'reply', 'Structured Plans replied to your comment on the plan pane’s Later row.', 'Structured Plans', 26, 7,
			'Nothing waits on it; it’s here so you see the reply.', { open: { artifact_id: w.docs.later }, detail: { artifact_id: w.docs.later, root_id: w.comment.rootId, reply_id: w.comment.replyId } }),
	];
}

const BULK: Array<[Kind, string]> = [
	['review', 'Approve the copy for the first-run screen that explains who owns which file.'],
	['decision', 'Decide whether a failed sign-in retries once or asks you.'],
	['task', 'Check the Linux install line in the README on a clean machine.'],
	['review', 'Approve the change to how long kept files last.'],
	['reply', 'Browser replied to your comment on the pane typing fix.'],
	['decision', 'Decide which of two update messages a stranger sees.'],
	['review', 'Approve the wording of the update notice in Settings.'],
	['decision', 'Decide whether paused agents show in the left menu.'],
	['task', 'Connect the CORSIA mailbox so agents can email you.'],
	['reply', 'Quality Gate replied to your comment on review timing.'],
	['review', 'Approve the empty state for a project with no agents.'],
	['decision', 'Decide how long a stopped worker’s files are kept.'],
	['review', 'Approve the phone layout for the document pane.'],
	['task', 'Choose the Mac that runs the local model.'],
	['decision', 'Decide whether the changelog lists every agent’s work.'],
	['reply', 'Ecosystem replied to your comment on the docs index.'],
	['review', 'Approve the text that explains what agents can reach.'],
	['decision', 'Decide whether Measurement shows cost per piece of work.'],
];

/** Frame i: forty items, the fourth with a very long title, open. */
function fortyItems(w: World) {
	const first = frameItems(w).slice(0, 3);
	const long = item(w, 'review:when-to-ask', 'review',
		'Approve or change the wording every agent loads on each turn about when to ask you, when to decide and tell you afterwards, and when to leave it to the project’s conductor, including the three cases you corrected last week and the new line on dates.',
		'Agent Substrate', 6, 4, 'Every agent loads this text.', { open: { artifact_id: w.docs.whenToAsk }, detail: { artifact_id: w.docs.whenToAsk } });
	const rest = Array.from({ length: 36 }, (_, k) => {
		const [kind, title] = BULK[k % BULK.length];
		const label = kind === 'reply' ? title.split(' replied')[0] : LABELS[k % LABELS.length];
		return item(w, `bulk-${k}`, kind, title, label, 24 * (k + 1) + 1, k + 5, 'Ranked after the release items.',
			{ open: { artifact_id: w.docs.readme }, detail: { artifact_id: w.docs.readme } });
	});
	return [...first, long, ...rest];
}

/** Serves Activity's read; the dock and sidebar keep reading the real default view. */
async function serveActivity(page: Page, w: World, items: unknown[]) {
	await page.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
		status: 200, contentType: 'application/json',
		body: JSON.stringify({ count: items.length, items, ranker: { thread_id: w.ranker, label: 'CORSIA Conductor' } }),
	}));
}

async function shot(page: Page, name: string) {
	await page.evaluate(() => document.fonts.ready);
	await page.waitForTimeout(400);
	if (OUT) await page.screenshot({ path: path.join(OUT, name), animations: 'disabled' });
}

const rows = (page: Page) => page.locator('[data-activity-row]');
const openRow = (page: Page) => page.locator('[data-activity-row][aria-current="true"]');

/** A fresh install's first-run landing puts the person in the CORSIA chat; Activity is one click from the menu. */
async function toActivity(page: Page) {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
}

async function arrive(page: Page, width: number, height: number) {
	await page.setViewportSize({ width, height });
	await openAppPastLanding(page);
	await toActivity(page);
}

async function reload(page: Page) {
	await page.reload();
	await toActivity(page);
}

test('Activity first version: built frames', async ({ page, request }) => {
	test.setTimeout(240_000);
	const w = await seedWorld(request);
	const items = frameItems(w);
	// The reply item carries the seeded comment's ids.

	// Desktop: the top item opens on arrival.
	await serveActivity(page, w, items);
	await arrive(page, 1440, 900);
	await expect(rows(page)).toHaveCount(7);
	await expect(openRow(page)).toContainText('saved passwords');
	await expect(page.locator('.pane-col').getByRole('button', { name: 'Approve', exact: true })).toBeVisible({ timeout: 15_000 });
	await shot(page, 'a-list-doc-1440.png');

	for (const [index, name, ready] of [
		[1, 'b-decision-1440.png', 'Answer'],
		[4, 'f-task-1440.png', 'Done'],
		[5, 'g-message-1440.png', 'Send'],
		[6, 'h-reply-1440.png', 'Done'],
	] as const) {
		await rows(page).nth(index).click();
		await expect(rows(page).nth(index)).toHaveAttribute('aria-current', 'true');
		await expect(page.locator('.pane-col').getByRole('button', { name: ready, exact: true })).toBeVisible({ timeout: 15_000 });
		await shot(page, name);
	}

	// Forty items with the long fourth open.
	await page.unroute(/\/api\/attention\/queue\?(.*&)?view=activity/);
	await serveActivity(page, w, fortyItems(w));
	await reload(page);
	await expect(rows(page)).toHaveCount(40);
	await rows(page).nth(3).click();
	await expect(openRow(page)).toContainText('including the three cases');
	await expect(page.locator('.pane-col').getByRole('button', { name: 'Approve', exact: true })).toBeVisible({ timeout: 15_000 });
	await shot(page, 'i-forty-1440.png');

	// Empty.
	await page.unroute(/\/api\/attention\/queue\?(.*&)?view=activity/);
	await serveActivity(page, w, []);
	await reload(page);
	await expect(page.getByText('Nothing is waiting on you.')).toBeVisible();
	await shot(page, 'c-empty-1440.png');

	// Phone: the list first with the top item marked, then one tap opens it.
	await page.unroute(/\/api\/attention\/queue\?(.*&)?view=activity/);
	await serveActivity(page, w, items);
	await page.setViewportSize({ width: 393, height: 852 });
	await reload(page);
	await expect(rows(page)).toHaveCount(7);
	await expect(openRow(page)).toContainText('saved passwords');
	await shot(page, 'd-list-393.png');
	await rows(page).first().click();
	await expect(page.getByText(/1 of 7 · Next/)).toBeVisible();
	await expect(page.locator('.pane-col').getByRole('button', { name: 'Approve', exact: true })).toBeVisible({ timeout: 15_000 });
	await shot(page, 'e-item-393.png');
});

/** Today's Activity at both widths, for the before row; run on the base commit. */
test('Activity first version: before frames', async ({ page, request }) => {
	test.skip(process.env.ACTIVITY_BEFORE !== '1', 'run only on the base commit');
	await seedWorld(request);
	await arrive(page, 1440, 900);
	await shot(page, 'before-1440.png');
	await page.setViewportSize({ width: 393, height: 852 });
	await reload(page);
	await page.waitForTimeout(1000);
	await shot(page, 'before-393.png');
});
