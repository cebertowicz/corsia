/**
 * Build frames for "External waits look alive" (shared/planning/plans-external-waits-alive-2026-10-20.md).
 *
 * The real app on the disposable mock E2E instance. Real threads, agents and one real document come from
 * the API; the plan-state reads (chat list thread states, the open thread's plan) are supplied by intercepting
 * them with the server's wire shape at the branch head. Run through `scripts/e2e-local` in mock mode.
 */
import { test, expect } from '../support/test';
import { createDocument } from '../support/documentOpen';
import type { APIRequestContext, Page } from '@playwright/test';
import {
	chatComposer, createSession, openAppPastLanding, sendChatMessage, TEST_TOKEN,
	waitForWsConnected,
} from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';
import { waitForMainSlideToSettle } from '../support/sidebar';
import { setFocusMode } from '../support/focusMode';
import path from 'node:path';

process.env.TZ = 'America/Los_Angeles';

const OUT = process.env.WAITS_OUT
	?? path.resolve(process.cwd(), '../shared/planning/renders/plans-external-waits-build');
const AFTER = true;

test.use({ timezoneId: 'America/Los_Angeles', locale: 'en-US', seedPerTest: true });
// Each frame sets its own viewport; under the touch profile a 1440 viewport sends with Enter, which a touch composer ignores.
test.beforeEach(({}, info) => test.skip(info.project.name === 'chromium-mobile', 'the frames set their own viewports on the desktop project'));

const MIN = 60_000;
const LONG_LABEL = 'CI on the release candidates for the quarterly billing migration, the platform security review, '
	+ 'and legal sign-off on the revised data processing addendum for our two largest enterprise customers '
	+ 'in the European Union and the United Kingdom';
// A URL-only wait reason reaches the person as its host and last path segment, linked to the full address.
const URL_LABEL = 'github.com/…/checks';
const URL_HREF = 'https://github.com/cebertowicz/corsia/pull/1187/checks?check_run_id=2938475611&pr=1187';

type Kind = 'waiting' | 'aside' | 'moving' | 'needs' | 'paused' | 'none';
interface Spec {
	title: string;
	agent: boolean;
	kind: Kind;
	label?: string;
	/** The watched artifact's link; the label becomes a link to it. */
	/** An unread update since the person's last visit. */
	update?: boolean;
	more?: number;
	/** Minutes from now the watch's time falls; negative once it has passed. Unused for a set-aside agent. */
	min?: number;
	/** An exact fixture deadline, for copy-accurate evidence frames. */
	until?: number;
	/** How many minutes the current run has been waiting. */
	waitSince?: number;
	/** Proven active run time to restore when another turn begins. */
	activeTime?: string;
}
interface Made extends Spec { id: string; until: number; since?: number; artifact?: string }

const RW: Spec = { title: 'Release watcher', agent: true, kind: 'waiting', label: 'CI on PR #1187', min: 12 };
const DR: Spec = { title: 'Docs refresh', agent: true, kind: 'waiting', label: 'an agent working on Draft release notes', min: 26 };

function asideAt(now: number): number {
	const d = new Date(now);
	d.setDate(d.getDate() + 1);
	d.setHours(9, 0, 0, 0);
	return d.getTime();
}

const iso = (ms: number) => new Date(ms).toISOString();

function waitOf(m: Made) {
	return { label: m.label ?? 'a scheduled check', until: iso(m.until), more: m.more ?? 0, ...(m.artifact ? { artifact_id: m.artifact } : {}) };
}

/** What the chat list reads for a thread: the server's waiting state, or the set-aside time of a plan that waits on nothing outside. */
function stateEntry(m: Made) {
	const base = { label_key: null, paused: false, attention_items: [] as unknown[], has_update: false, opened: true, settled: false };
	switch (m.kind) {
		case 'waiting': return { ...base, state: 'waiting', wait: waitOf(m) };
		case 'aside': return { ...base, state: 'idle', set_aside_until: iso(m.until) };
		case 'moving': return { ...base, state: 'moving' };
		default: return undefined;
	}
}

function activityRow(m: Made, index: number, now: number) {
	return {
		plan: { id: `plan-${m.id}`, owner_kind: 'thread', owner_id: m.id, state: 'active', revision: 3, updated_at: iso(now - index * MIN) },
		owner: { label: m.title, project_id: null, thread_id: m.id },
		progress: { open: 1, liveness: 'waiting' },
		has_nodes: false,
		display: { label_key: 'waiting', tone_key: 'waiting' },
		priority_at: iso(now - index * MIN),
	};
}

// A real operator node, from the first-run plan, re-titled: every field the pane reads stays valid.
function planNode(tpl: any, thread: Made, id: string, title: string, over: Record<string, unknown>) {
	const node = structuredClone(tpl);
	return Object.assign(node, {
		id, parent_id: null, depth: 0, title, commitment_id: id, commitment_cost: null,
		executor: { thread_id: thread.id, label: thread.title }, ...over,
	});
}

const WAITING_NODE = { state: 'active', display: { label_key: 'waiting', tone_key: 'waiting' } };

function waitingExecution(tpl: any, cause: Record<string, unknown>) {
	return { ...structuredClone(tpl.execution), condition: 'waiting', causes: [cause] };
}

/** A task behind an unfinished prerequisite: it projects as blocked and reads "Waiting · After …". */
function afterNode(tpl: any, m: Made, id: string, title: string, afterId: string, afterTitle: string) {
	return planNode(tpl, m, id, title, {
		state: 'pending', display: { label_key: 'waiting', tone_key: 'waiting' }, unmet_dependencies: [afterId],
		status_reason: `After ${afterTitle}`,
		execution: {
			...structuredClone(tpl.execution), condition: 'blocked', selection: null,
			causes: [{ kind: 'blocked', reason_key: 'dependency', reason: `After ${afterTitle}`, owner_kind: 'system', owner_id: null, dependency_node_id: afterId }],
		},
	});
}

function planNodesFor(tpl: any, m: Made, childWaitAt: number): any[] | null {
	const wait = (label: string, until: number) => ({ label, until: iso(until), more: 0 });
	const timed = (reason: string, owner: string, until: number) => waitingExecution(tpl, {
		kind: 'waiting', reason_key: 'deadline', reason, owner_kind: owner, owner_id: null, watch_id: `w-${m.id}`, ends_at: iso(until),
	});
	if (m.title === 'Plan pane states') {
		const outside = planNode(tpl, m, `outside-${m.id}`, 'Wait for CI checks', {
			...WAITING_NODE, execution: timed('Waiting for CI on PR #1193', 'external', atToday(23, 46)),
			status_reason: 'Waiting for CI on PR #1193', wait: wait('CI on PR #1193', atToday(23, 46)),
		});
		const late = planNode(tpl, m, `late-${m.id}`, 'Confirm release checks', {
			...WAITING_NODE, execution: timed('Waiting for CI on PR #1181', 'external', Date.now() - 18 * MIN),
			status_reason: 'Waiting for CI on PR #1181', wait: wait('CI on PR #1181', Date.now() - 18 * MIN),
		});
		return [outside, late];
	}
	if (m.title === 'Docs refresh') {
		return [planNode(tpl, m, `n1-${m.id}`, 'Draft release notes', {
			...WAITING_NODE, assignee: 'subagent', status_reason: 'Waiting for the delegated child to return',
			execution: timed('Waiting for the delegated child to return', 'thread', childWaitAt),
			child: {
				available: true, plan_id: 'plan-child-release-notes', thread_id: 'child-release-notes',
				progress: { done: 1, total: 3, open: 2, liveness: 'waiting' }, wait: wait('CI on PR #1193', childWaitAt),
			},
		})];
	}
	return null;
}

function planEnvelope(tpl: any, m: Made, nodes: any[]) {
	const env = structuredClone(tpl);
	const entry = env.plans[0];
	entry.plan = { ...entry.plan, id: `plan-${m.id}`, owner_id: m.id };
	entry.owner = { label: m.title, project_id: null, thread_id: m.id };
	entry.nodes = nodes;
	entry.current_node_ids = [];
	entry.next_node_ids = [];
	entry.root_count = nodes.length;
	entry.display = { label_key: 'waiting', tone_key: 'waiting' };
	entry.progress = {
		done: 0, total: nodes.length, open: nodes.length, liveness: 'waiting',
		distribution: { ...entry.progress.distribution, done: 0, active: 1, pending_ready: nodes.length - 1, waiting: 1 },
	};
	env.target = { kind: 'thread', id: m.id, label: m.title };
	return env;
}

async function makeRoster(request: APIRequestContext, specs: Spec[], now: number): Promise<Made[]> {
	const made: Made[] = [];
	for (const spec of specs) {
		const id = await createSession(request, spec.title);
		if (spec.agent) {
			const res = await request.patch(`/api/agents/${id}?token=${TEST_TOKEN}`, { data: { responsibility: `${spec.title}.` } });
			expect(res.ok()).toBeTruthy();
		}
		const until = spec.kind === 'aside' ? asideAt(now) : spec.until ?? now + (spec.min ?? 0) * MIN;
		made.push({ ...spec, id, until, ...(spec.waitSince === undefined ? {} : { since: now - spec.waitSince * MIN }) });
	}
	return made;
}

async function templatePlan(request: APIRequestContext): Promise<any> {
	const conductor = await (await request.get(`/api/corsia-agent?token=${TEST_TOKEN}`)).json();
	const res = await request.get(`/api/plans/operator/threads/${conductor.target.thread_id}`, {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` },
	});
	expect(res.ok()).toBeTruthy();
	const env = await res.json();
	env.plans[0].nodes = [env.plans[0].nodes[0]];
	return env;
}

function stripEtag(route: import('@playwright/test').Route) {
	const headers = { ...route.request().headers() };
	delete headers['if-none-match'];
	return headers;
}

async function installFixture(page: Page, request: APIRequestContext, made: Made[], now: number) {
	const tpl = await templatePlan(request);
	const tplNode = tpl.plans[0].nodes[0];
	const childWaitAt = made.find((m) => m.title === 'Docs refresh')?.until ?? now + 40 * MIN;
	await page.route('**/api/plans/operator/thread-states*', async (route) => {
		const res = await route.fetch({ headers: stripEtag(route) });
		const body = await res.json().catch(() => ({}));
		const states = { ...(body.states ?? {}) };
		for (const m of made) {
			const entry = stateEntry(m);
			if (entry) states[m.id] = entry;
		}
		await route.fulfill({ response: res, json: { ...body, states } });
	});
	await page.route('**/api/plans/activity/scopes/workspace*', async (route) => {
		const res = await route.fetch({ headers: stripEtag(route) });
		const body = await res.json();
		const rows = made.filter((m) => m.kind !== 'none').map((m, i) => activityRow(m, i + 1, now));
		await route.fulfill({ response: res, json: { ...body, plans: [...body.plans, ...rows] } });
	});
	for (const m of made) {
		const nodes = planNodesFor(tplNode, m, childWaitAt);
		if (!nodes) continue;
		const envelope = planEnvelope(tpl, m, nodes);
		await page.route(new RegExp(`/api/plans/operator/threads/${m.id}(\\?.*)?$`), (route) => route.fulfill({ json: envelope }));
	}
}

async function view(page: Page, width: number, height: number, scheme: 'light' | 'dark') {
	await page.setViewportSize({ width, height });
	await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' });
	await page.waitForTimeout(600);
}

async function shot(page: Page, name: string) {
	await page.waitForTimeout(400);
	await page.screenshot({ path: path.join(OUT, `${name}.png`), animations: 'disabled' });
}

async function openThread(page: Page, title: string) {
	await page.locator('aside.sidebar').getByText(title, { exact: true }).first().click({ timeout: 10_000 });
	await expect(chatComposer(page)).toBeVisible({ timeout: 20_000 });
	await waitForWsConnected(page);
}

/** One real mock turn in a thread, so its transcript is a real conversation. */
async function converse(page: Page, request: APIRequestContext, m: Made, user: string, reply: string) {
	await openThread(page, m.title);
	await seedMockResponses(request, [{ content: reply, finish_reason: 'stop', match_thread: m.id }]);
	await sendChatMessage(page, user);
	// Focus is on, as in Steve's app: the finished turn folds to "Worked", so the proof of a reply is the fold.
	await expect(page.getByText(/^Worked · /).last()).toBeVisible({ timeout: 20_000 });
	await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
}

/** A turn the mock holds open, so the thread's row really shows work in motion. */
async function holdTurn(page: Page, request: APIRequestContext, m: Made, user: string) {
	await openThread(page, m.title);
	const before = await readMockCallCount(request);
	await seedMockResponses(request, [{
		content: 'Done.', finish_reason: 'stop', match_thread: m.id, completion_gate: `hold-${m.id}`,
		reasoning_content: 'Sorting the new messages by sender.',
	}]);
	await sendChatMessage(page, user);
	await expect.poll(() => readMockCallCount(request)).toBeGreaterThan(before);
	await expect(page.getByRole('button', { name: 'Stop turn' })).toBeVisible({ timeout: 10_000 });
}

async function openActivity(page: Page) {
	await page.locator('aside.sidebar').getByText('Activity', { exact: true }).click();
	await expect(page.getByRole('heading', { name: /Waiting \(\d+\)/ })).toBeVisible({ timeout: 15_000 });
	await page.getByRole('button', { name: /Waiting \(\d+\)/ }).click();
}

async function drawer(page: Page, open: boolean) {
	// On a phone the rail sits behind the main surface, which slides right to open it.
	const box = await page.locator('main.main').boundingBox().catch(() => null);
	const isOpen = !!box && box.x > 40;
	if (isOpen !== open) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await waitForMainSlideToSettle(page);
	await page.waitForTimeout(500);
}

const RW_CHAT = ['Keep an eye on the release PR and tell me when it has merged.',
	'I opened the release PR and its checks are running. I\'ll look again when they report.'] as const;
const DR_CHAT = ['Refresh the docs for the new release.',
	'I handed the release notes to a worker and I\'m waiting for it to return.'] as const;
const SD_CHAT = ['Send the support digest every weekday morning.',
	'I started the digest, but it needs the CI run on that PR to finish first.'] as const;

async function boot(page: Page, request: APIRequestContext, specs: Spec[]) {
	const now = Date.now();
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
	await page.setViewportSize({ width: 1440, height: 900 });
	const made = await makeRoster(request, specs, now);
	await installFixture(page, request, made, now);
	await openAppPastLanding(page);
	return { made, now, by: (title: string) => made.find((m) => m.title === title)! };
}

// ---------------------------------------------------------------- before the change (base)
function atToday(hour: number, minute: number): number {
	const date = new Date();
	date.setHours(hour, minute, 0, 0);
	return date.getTime();
}

function r2Wait(kind: 'waiting', until: number, label = 'CI on PR #1193'): Spec {
	return { title: 'Release watcher', agent: true, kind, label, until, waitSince: 14, activeTime: '8 min' };
}

async function r2Chat(page: Page, request: APIRequestContext, thread: Made) {
	await openThread(page, thread.title);
	await seedMockResponses(request, [{ content: RW_CHAT[1], finish_reason: 'stop', match_thread: thread.id }]);
	await sendChatMessage(page, RW_CHAT[0]);
	const chip = page.locator('[data-work-block]').last();
	await expect(chip).toContainText(/Waiting|Stalled/);
	return chip;
}


// Focus is a persisted server setting: frame 4 turns it off, so every frame puts it back.
test.afterEach(async ({ request }) => {
	await setFocusMode(request, true);
});

const sidebarFor = (page: Page) => page.locator('aside.sidebar');

// ---------------------------------------------------------------- the seven frames

test('frame 1: sidebar row waiting, with its hover title', async ({ page, request }) => {
	test.setTimeout(150_000);
	await boot(page, request, [r2Wait('waiting', atToday(23, 46)), { title: 'Weekly digest', agent: true, kind: 'aside' }]);
	const mark = sidebarFor(page).locator('.waiting-mark');
	await expect(mark).toHaveCount(1);
	const title = (await mark.locator('title').textContent()) ?? '';
	expect(title).toMatch(/^Waiting for CI on PR #1193 · checks back today at 11:46 PM$/);
	await mark.hover();
	await shot(page, '1-sidebar-waiting-row');
	console.log(`FRAME1 TITLE: ${title}`);
});

test('frame 2: Focus chip at 1440, label opens the document', async ({ page, request }) => {
	test.setTimeout(150_000);
	await setFocusMode(request, true);
	const { by } = await boot(page, request, [r2Wait('waiting', atToday(23, 46))]);
	const thread = by('Release watcher');
	const doc = await createDocument(request, thread.id, 'release-checks.md');
	thread.artifact = doc.artifactId;
	await page.reload();
	await openAppPastLanding(page);
	const chip = await r2Chat(page, request, thread);
	await expect(chip).toContainText(/Waiting · under 1 min · CI on PR #1193 · back 11:46 PM/);
	await view(page, 1440, 900, 'light');
	await shot(page, '2-focus-chip-1440');
	await chip.locator('.wait-link').click();
	await expect(page.getByText('release-checks.md').first()).toBeVisible({ timeout: 10_000 });
	console.log('FRAME2 OPEN: document tab visible');
});

test('frame 3: Focus chip at 393 with a long label', async ({ page, request }) => {
	test.setTimeout(150_000);
	await setFocusMode(request, true);
	const far = new Date();
	far.setDate(far.getDate() + 2);
	far.setHours(9, 0, 0, 0);
	const weekday = far.toLocaleDateString('en-US', { weekday: 'long' });
	await page.clock.install({ time: Date.now() });
	const { by } = await boot(page, request, [{ ...r2Wait('waiting', far.getTime(), LONG_LABEL), waitSince: 134 }]);
	const chip = await r2Chat(page, request, by('Release watcher'));
	// The agent has now waited 2 h 14 min since its turn ended; the clock moves, nothing else does.
	await page.clock.fastForward(133 * 60_000);
	await page.clock.runFor(61_000);
	await view(page, 393, 852, 'light');
	const face = chip.locator('.wait-face');
	await expect(face).toHaveAttribute('title', /2\s?h 14\s?min? so far/);
	await expect(face.locator('.wait-fixed').last()).toContainText(`back ${weekday} 9:00 AM`);
	await expect(face.locator('.wait-fixed').last()).toBeInViewport({ ratio: 1 });
	console.log(`FRAME3 FACE: ${(await face.innerText()).replace(/\s+/g, ' ')}`);
	await shot(page, '3-focus-chip-393-long-label');
});

test('frame 4: the non-Focus face', async ({ page, request }) => {
	test.setTimeout(150_000);
	await setFocusMode(request, false);
	const { by } = await boot(page, request, [r2Wait('waiting', atToday(23, 46))]);
	const thread = by('Release watcher');
	await openThread(page, thread.title);
	await seedMockResponses(request, [{ content: RW_CHAT[1], finish_reason: 'stop', match_thread: thread.id }]);
	await sendChatMessage(page, RW_CHAT[0]);
	await expect(page.getByText(RW_CHAT[1])).toBeVisible({ timeout: 20_000 });
	await expect(page.locator('.wait-line')).toContainText(/Waiting · .*CI on PR #1193 · back 11:46 PM/);
	await shot(page, '4-non-focus-face');
});

test('frame 5: Activity Waiting, expanded', async ({ page, request }) => {
	test.setTimeout(150_000);
	await boot(page, request, [
		{ title: 'Quality review', agent: true, kind: 'waiting', label: 'CI on PR #1194', until: atToday(23, 59) },
		r2Wait('waiting', atToday(23, 46)),
		{ title: 'Weekly digest', agent: true, kind: 'aside' },
	]);
	await openActivity(page);
	await expect(page.locator('.owner-copy .wait')).toHaveCount(2);
	await expect(page.locator('.owner-copy').filter({ hasText: /Set aside until/ })).toHaveCount(1);
	await shot(page, '5-activity-waiting-expanded');
});

test('frame 8: Activity Waiting at 393, set-aside subline visible', async ({ page, request }) => {
	test.setTimeout(150_000);
	await boot(page, request, [
		{ title: 'Quality review', agent: true, kind: 'waiting', label: 'CI on PR #1194', until: atToday(23, 59) },
		r2Wait('waiting', atToday(23, 46)),
		{ title: 'Weekly digest', agent: true, kind: 'aside' },
	]);
	await openActivity(page);
	await view(page, 393, 852, 'light');
	await expect(page.locator('.owner-copy > .set-aside')).toBeVisible();
	await expect(page.locator('.owner-copy > .set-aside')).toContainText(/Set aside until/);
	await shot(page, '8-activity-waiting-393');
});

test('frame 9: the static chip when the thread has no run to open', async ({ page, request }) => {
	test.setTimeout(150_000);
	await setFocusMode(request, true);
	const { by } = await boot(page, request, [r2Wait('waiting', atToday(23, 46))]);
	await openThread(page, by('Release watcher').title);
	await view(page, 1440, 900, 'light');
	const chip = page.locator('[data-work-block]').last();
	await expect(chip).toContainText(/Waiting · .*CI on PR #1193 · back 11:46 PM/);
	await expect(chip.locator('button[aria-expanded], .fold-chevron')).toHaveCount(0);
	await shot(page, '9-standalone-chip');
});

test('frame 6: plan pane rows, one outside wait and one due now', async ({ page, request }) => {
	test.setTimeout(150_000);
	const { by } = await boot(page, request, [{ title: 'Plan pane states', agent: true, kind: 'waiting', label: 'CI on PR #1181', until: Date.now() - 18 * 60_000 }]);
	await r2Chat(page, request, by('Plan pane states'));
	// The chip names the earliest wait, which is the late one (the server picks the earliest).
	await expect(page.locator('[data-work-block]').last()).toContainText('CI on PR #1181 · due now');
	await expect(page.locator('.row-subline').filter({ hasText: 'CI on PR #1193 · back 11:46 PM' })).toHaveCount(1);
	await expect(page.locator('.row-subline').filter({ hasText: 'CI on PR #1181 · due now' })).toHaveCount(1);
	await expect(page.locator('.row-subline').filter({ hasText: /due back|stalled|overdue/i })).toHaveCount(0);
	await shot(page, '6-pane-rows-outside-and-back-now');
});

test('frame 7: a parent child chip', async ({ page, request }) => {
	test.setTimeout(150_000);
	const { by } = await boot(page, request, [{ ...DR, label: 'CI on PR #1193', until: atToday(23, 46) }]);
	await r2Chat(page, request, by('Docs refresh'));
	const childChip = page.locator('.spawn-chip');
	await expect(childChip).toBeVisible();
	await expect(childChip.locator('.child-wait')).toHaveText('CI on PR #1193 · back 11:46 PM');
	await shot(page, '7-child-chip');
});

// Frame names (`copy-<n>-<width>`) are the evidence index's names; each test title says what its frames show.
test('plan copy: open-agent control, missing-task and closed-decision notices', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Both viewport widths are captured with a pointer.');
	test.setTimeout(150_000);
	const { by } = await boot(page, request, [{ ...DR, label: 'CI on PR #1193', until: atToday(23, 46) }]);
	await r2Chat(page, request, by('Docs refresh'));
	const open = page.getByRole('button', { name: 'Open the agent working on Draft release notes beside this chat' });
	await expect(open).toHaveAttribute('title', 'Open the agent beside this chat');
	for (const width of [1920, 393]) {
		await view(page, width, width === 393 ? 852 : 1080, 'light');
		if (width === 393) await page.getByRole('button', { name: /^Open plan —/ }).click();
		await open.hover();
		await shot(page, `copy-20-${width}`);
		await page.evaluate(async (threadId) => {
			const path = '/src/lib/planNodeFocus.ts';
			const { focusPlanNode } = await import(/* @vite-ignore */ path);
			await focusPlanNode(threadId, 'missing-task');
		}, by('Docs refresh').id);
		await expect(page.getByText('That task isn’t in this plan.')).toBeVisible();
		await shot(page, `copy-2-${width}`);
		await page.evaluate(async () => {
			const path = '/src/lib/stores/attentionDock.svelte.ts';
			const { reportOpenDecision } = await import(/* @vite-ignore */ path);
			reportOpenDecision('empty');
		});
		await expect(page.getByText('That decision is closed.').last()).toBeVisible();
		await shot(page, `copy-3-${width}`);
	}
});

test('plan copy: oversize count, resolved decision, update failure', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Both viewport widths are captured here.');
	test.setTimeout(150_000);
	const { by } = await boot(page, request, [{ ...DR, label: 'CI on PR #1193', until: atToday(23, 46) }]);
	const thread = by('Docs refresh');
	const tpl = await templatePlan(request);
	const envelope = planEnvelope(tpl, thread, planNodesFor(tpl.plans[0].nodes[0], thread, thread.until)!);
	envelope.attention = { awaiting_count: 1 };
	envelope.plans[0].oversize = { open: 41, budget: 40 };
	await page.route(new RegExp(`/api/plans/operator/threads/${thread.id}(\\?.*)?$`), route => route.fulfill({ json: envelope }));
	await page.route('**/api/attention/queue*', route => route.fulfill({ json: { current: null, count: 0, items: [] } }));
	await page.route('**/api/plans/*/mutations', route => route.fulfill({ status: 500, json: { detail: 'internal' } }));
	await r2Chat(page, request, thread);
	for (const width of [1920, 393]) {
		await view(page, width, width === 393 ? 852 : 1080, 'light');
		if (width === 393) await page.getByRole('button', { name: /^Open plan —/ }).click();
		if (width === 1920) {
			const count = page.locator('.plan-head-controls .progress.oversize');
			await expect(count).toHaveAttribute('title', 'This plan has more than 40 open tasks. Use Mark done or Won’t do on any that don’t need doing.');
			await count.hover();
			await shot(page, 'copy-4-1920');
		}
		await page.getByRole('button', { name: /1 decision/ }).click();
		await expect(page.getByText('That decision is closed. Refreshing the plan.')).toBeVisible();
		await shot(page, `copy-5-${width}`);
	}
});


test('plan copy: task details, read-only discussion, task menu, empty pane, activity row', async ({ page }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Both viewport widths are captured in this project.');
	test.setTimeout(150_000);
	for (const width of [1920, 393]) {
		for (const row of [6, 7, 8, 10, 15, 17]) {
			if ((row === 15 || row === 17) && width === 393) continue; // These labels are hidden on phones.
			await view(page, width, width === 393 ? 852 : 1080, 'light');
			await page.goto('/_design?frame=attention-dock&scenario=empty');
			await page.evaluate(async (selected) => {
				const load = (path: string) => import(/* @vite-ignore */ path);
				const { mount, flushSync } = await load('/node_modules/.vite/deps/svelte.js');
				const { node } = await load('/src/lib/__tests__/fixtures/planTreeNode.ts');
				document.body.replaceChildren();
				const host = document.createElement('main');
				host.style.cssText = 'padding:24px; max-width:800px;';
				document.body.appendChild(host);
				if (selected === 7) {
					const { default: Component } = await load('/src/lib/components/LegacyPlanDiscussionLeaf.svelte');
					mount(Component, { target: host, props: { artifactId: 'example', title: 'Discussion' } });
				} else if (selected === 8) {
					const { default: Component } = await load('/src/lib/components/plans/PlanTaskMenu.svelte');
					const item = node({ user_actions: ['mark_done', 'close_node'], child: { available: true, plan_id: 'p', thread_id: 't', progress: { done: 0, total: 1 } } });
					mount(Component, { target: host, props: { node: item, x: 24, y: 24, onAction: () => {}, onClose: () => {} } });
				} else if (selected === 15) {
					const { default: Component } = await load('/src/lib/components/Pane.svelte');
					const { activeThreadId } = await load('/src/lib/stores/threads.ts');
					activeThreadId.set(null);
					mount(Component, { target: host, props: { sections: [{ title: 'Plan', kind: 'plan' }] } });
				} else if (selected === 17) {
					const { default: Component } = await load('/src/lib/components/activity/ActivityPlanRow.svelte');
					const row = { entry: { plan: { id: 'p', revision: 1 }, owner: { thread_id: 't', label: 'Example chat', type: 'thread' }, progress: { open: 1, liveness: 'waiting' }, has_nodes: false, display: { label_key: 'waiting', tone_key: 'waiting' }, priority_at: new Date().toISOString() }, thread: null, ownerType: 'thread', state: 'waiting' };
					mount(Component, { target: host, props: { row, onSelectThread: () => {} } });
				} else {
					const { default: Component } = await load('/src/lib/components/plans/PlanNodeDetails.svelte');
					const item = node({ title: 'Review task' });
					if (selected === 6) item.execution = { ...item.execution, attempt_count: 1, attempts: [{ id: 'a', ordinal: 1, executor: { thread_id: 'agent', label: 'Agent' }, requirements_revision: 1, selected_at: 'earlier', released_at: 'later', release_reason: 'executor_changed' }] };
					item.node_cost = { own: { ...item.node_cost.own, waiting: { seconds: 600 } }, rolled: { ...item.node_cost.rolled, waiting: { seconds: 18180 } } };
					mount(Component, { target: host, props: { node: item, id: 'details' } });
				}
				flushSync();
			}, row);
			const text = row === 6 ? 'reassigned to another agent' : row === 7 ? 'Read-only.' : row === 8 ? 'Marking this done or Won’t do also stops the agent working on it.' : row === 15 ? 'Open a chat to see its plan' : row === 17 ? 'Chat' : 'Waiting, this task alone:';
			await expect(page.getByText(text, { exact: row === 17 })).toBeVisible();
			await shot(page, `copy-${row}-${width}`);
		}
	}
});

test('plan copy: the plan pane when its plan is forbidden, fails to load, or can’t be read', async ({ page }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Both viewport widths are captured in this project.');
	test.setTimeout(150_000);
	const states = [
		{ frame: 21, status: 403, json: {}, text: 'You don’t have access to this chat. Choose another.', button: null },
	];
	await page.route('**/api/attention/queue*', route => route.fulfill({ json: { current: null, count: 0, items: [] } }));
	for (const width of [1920, 393]) {
		for (const state of states) {
			await page.unroute('**/api/plans/operator/threads/**');
			await page.route('**/api/plans/operator/threads/**', route => route.fulfill({ status: state.status, json: state.json }));
			await view(page, width, width === 393 ? 852 : 1080, 'light');
			await page.goto('/_design?frame=attention-dock&scenario=empty');
			// The pane alone, at the plan drawer's width on desktop and full width on a phone.
			await page.evaluate(async (paneWidth) => {
				const load = (path: string) => import(/* @vite-ignore */ path);
				const { mount, flushSync } = await load('/node_modules/.vite/deps/svelte.js');
				const auth = await load('/src/lib/stores/auth.ts');
				auth.token.set('design-frame');
				const { default: Component } = await load('/src/lib/components/plans/ThreadPlanPane.svelte');
				document.body.replaceChildren();
				const host = document.createElement('aside');
				host.style.cssText = `width:${paneWidth}; border-right:1px solid var(--border); min-height:240px;`;
				document.body.appendChild(host);
				mount(Component, { target: host, props: { threadId: 'frame-thread' } });
				flushSync();
			}, width === 393 ? '100%' : '312px');
			await expect(page.getByText(state.text)).toBeVisible();
			if (state.button) await expect(page.getByRole('button', { name: state.button, exact: true })).toBeVisible();
			await shot(page, `copy-${state.frame}-${width}`);
		}
	}
});
