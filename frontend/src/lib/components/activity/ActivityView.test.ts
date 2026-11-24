import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ActivityQueueItem } from '$lib/api/activityQueue';
import { activityFrameItems, activityItem, activityQueueBody, activitySource } from '$lib/__tests__/fixtures/attentionQueue';

const { mobile, mutate, emit, closeTab } = vi.hoisted(() => ({
	mobile: { store: null as null | import('svelte/store').Writable<boolean> },
	mutate: vi.fn(),
	emit: vi.fn(),
	closeTab: vi.fn(),
}));

vi.mock('$lib/ws/events', () => ({ on: () => () => {} }));
vi.mock('$lib/ws', () => ({ on: () => () => {} }));
vi.mock('$lib/stores/viewport', async (original) => {
	const { writable: w } = await import('svelte/store');
	mobile.store = w(false);
	return { ...(await original<typeof import('$lib/stores/viewport')>()), isMobile: mobile.store };
});
vi.mock('$lib/api/plans', async (original) => ({
	...(await original<typeof import('$lib/api/plans')>()),
	mutateUserTask: mutate,
}));
vi.mock('$lib/product_events', () => ({ emitProductEvent: emit }));
// The ordinary close, watched: closing a document tab sends its unsent comments.
vi.mock('$lib/artifactTabClose', async (original) => {
	const real = await original<typeof import('$lib/artifactTabClose')>();
	closeTab.mockImplementation(real.requestArtifactTabClose);
	return { ...real, requestArtifactTabClose: closeTab };
});

import ActivityView from './ActivityView.svelte';
import shellSource from '$lib/components/ReviewBarShell.svelte?raw';
import paneSource from './ActivityItemPane.svelte?raw';
import listSource from './ActivityList.svelte?raw';
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
import { activityWorkspaceKey } from '$lib/stores/activityWorkspace';
import { consumeTabLanding, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeId, expandedId, goneReply } from '$lib/stores/attentionDock.svelte';
import { answerNotice, answers } from '$lib/stores/attentionAnswerState';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
/** What the next queue read returns; tests change it to model the server settling an item. */
let served: ActivityQueueItem[] = [];
let queueReads = 0;

function respond(status: number, body: unknown): Response {
	return { ok: status < 400, status, json: async () => body } as Response;
}

async function settle() {
	for (let i = 0; i < 6; i += 1) {
		await Promise.resolve();
		await tick();
	}
	flushSync();
}

function render(onSelectThread = vi.fn()) {
	component = mount(ActivityView, { target: host, props: { projectId: 'project-1', onSelectThread } });
	return onSelectThread;
}

const rows = () => [...host.querySelectorAll<HTMLButtonElement>('[data-activity-row]')];
const openRow = () => host.querySelector<HTMLButtonElement>('[data-activity-row][aria-current="true"]');
const nextButton = () => host.querySelector<HTMLButtonElement>('button.next')!;
const pane = () => host.querySelector<HTMLElement>('.pane-col')!;
const buttonNamed = (name: string) =>
	[...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === name);
const alert = () => pane().querySelector('[role="alert"]')?.textContent ?? null;

/** Answers `match`ing requests from `handler`; everything else gets the queue stub's answer. */
function alsoServe(match: string, handler: (init?: RequestInit) => Response | Promise<Response>) {
	const stub = vi.mocked(fetch);
	const base = stub.getMockImplementation()!;
	stub.mockImplementation(async (input, init) => (String(input).includes(match) ? handler(init) : base(input, init)));
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	served = activityFrameItems();
	queueReads = 0;
	mobile.store?.set(false);
	activityPhoneItem.set(null);
	mutate.mockReset();
	emit.mockReset();
	closeTab.mockClear();
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/api/attention/queue')) {
			queueReads += 1;
			expect(url).toContain('view=activity');
			expect(url).toContain('project_id=project-1');
			return respond(200, activityQueueBody(served));
		}
		// Document lookups fail here, so a document item shows its can't-open panel instead of the editor.
		return respond(404, {});
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
});

describe('Activity list', () => {
	it('shows every kind in the ranker’s order and opens the top item with why it is there', async () => {
		render();
		await settle();
		expect(rows().map((row) => row.querySelector('.rank')?.textContent)).toEqual(['1', '2', '3', '4', '5', '6', '7']);
		expect(rows().map((row) => row.querySelector('strong')?.textContent)).toEqual(served.map((item) => item.title));
		expect(host.querySelector('.rtop h2')?.textContent).toBe('Waiting on you');
		expect(host.querySelector('.count')?.textContent).toBe('7');
		expect(host.querySelector('.by')?.textContent).toContain('Order set by CORSIA Conductor');
		expect(openRow()).toBe(rows()[0]);
		expect(openRow()?.querySelector('.why')?.textContent).toContain('0.5.0 publishes Nov 24');
		expect(rows()[1].querySelector('.why')).toBeNull();
		expect(rows()[4].querySelector('.sub')?.textContent).toContain('Packaging & Onboarding · Task');
	});

	it('Next opens the following item without acting', async () => {
		render();
		await settle();
		nextButton().click();
		await settle();
		expect(openRow()).toBe(rows()[1]);
		expect(pane().querySelector('h1')?.textContent).toBe(served[1].title);
		expect(mutate).not.toHaveBeenCalled();
	});

	it('greys Next on the last item', async () => {
		render();
		await settle();
		expect(nextButton().disabled).toBe(false);
		rows()[6].click();
		await settle();
		expect(openRow()).toBe(rows()[6]);
		expect(nextButton().disabled).toBe(true);
	});

	it('opens what followed after acting, once the server stops returning the item', async () => {
		mutate.mockImplementation(async () => {
			served = served.filter((item) => item.key !== 'task:sign-build');
			return { status: 'success' };
		});
		render();
		await settle();
		rows()[4].click();
		await settle();
		const reads = queueReads;
		buttonNamed('Done')!.click();
		await settle();
		expect(mutate).toHaveBeenCalledWith('plan-packaging', 'mark_done', 'node-sign', 4, 'mark_done:node-sign:4');
		expect(queueReads).toBeGreaterThan(reads);
		expect(rows()).toHaveLength(6);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[4].title);
		expect(served[4].key).toBe('message:intel-mac');
		expect(host.querySelector('.count')?.textContent).toBe('6');
	});

	it('keeps a failed item open with the reason and Try again', async () => {
		mutate.mockResolvedValue({ status: 'error', message: 'Plans are read-only right now.' });
		render();
		await settle();
		rows()[4].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		expect(pane().querySelector('[role="alert"]')?.textContent).toBe('Plans are read-only right now.');
		expect(buttonNamed('Try again')).toBeTruthy();
		expect(openRow()).toBe(rows()[4]);
		expect(rows()).toHaveLength(7);
	});

	it('says nothing is waiting when the queue is empty', async () => {
		served = [];
		render();
		await settle();
		expect(host.textContent).toContain('Nothing is waiting on you.');
		expect(rows()).toHaveLength(0);
		expect(pane().textContent).toContain('Nothing to open.');
	});

	it('says the read failed and offers Try again, naming the connection only when the request never arrived', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		render();
		await settle();
		const offline = host.querySelector('[role="alert"]')?.textContent ?? '';
		expect(offline).toContain('Activity couldn’t load.');
		expect(offline).toContain('Check your connection, then try again.');
		expect(buttonNamed('Try again')).toBeTruthy();
	});

	it.each([500, 403])('says only to try again when the server answered %i', async (status) => {
		vi.stubGlobal('fetch', vi.fn(async () => respond(status, {})));
		render();
		await settle();
		const text = host.querySelector('[role="alert"]')?.textContent ?? '';
		expect(text).toContain('Activity couldn’t load.');
		expect(text).toContain('Try again.');
		expect(text).not.toContain('connection');
		expect(buttonNamed('Try again')).toBeTruthy();
	});

	it.each([
		// Today's default queue, as a server without Activity's view answers it.
		['the default view', { current: null, count: 1, items: [{ external_id: 'd1', renderer: 'decision', detail: {} }] }],
		['a body without items', { count: 0 }],
		['a row without a source', { count: 1, items: [{ key: 'k', kind: 'task', title: 'T' }] }],
		['a body that is not JSON', undefined],
	])('shows %s as a failed read, never as an empty list', async (_name, body) => {
		vi.stubGlobal('fetch', vi.fn(async () => body === undefined
			? ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token'); } } as unknown as Response)
			: respond(200, body)));
		render();
		await settle();
		expect(host.textContent).not.toContain('Nothing is waiting on you.');
		const text = host.querySelector('[role="alert"]')?.textContent ?? '';
		expect(text).toContain('Activity couldn’t load.');
		expect(text).not.toContain('connection');
		expect(rows()).toHaveLength(0);
	});

	it('no longer shows the old sections', async () => {
		render();
		await settle();
		const text = host.textContent ?? '';
		for (const gone of ['Continue', 'Needs you', 'In progress', 'Complete (', 'Measures']) expect(text).not.toContain(gone);
		expect(host.querySelector('.row-summary, .section-toggle, .activity-measures, .needs-you, #activity-work-title')).toBeNull();
	});
});

describe('what a press comes to, reported in Activity', () => {
	const dockState = () => ({
		expanded: get(expandedId), active: get(activeId), notice: get(answerNotice), gone: get(goneReply), answers: get(answers),
	});
	const dockAtRest = { expanded: null, active: null, notice: null, gone: null, answers: {} };
	const dropServed = (key: string) => { served = served.filter((item) => item.key !== key); };

	beforeEach(() => {
		expandedId.set(null);
		activeId.set(null);
		answerNotice.set(null);
		goneReply.set(null);
		answers.set({});
	});

	it('keeps a message that comes back not sent, with the server’s reason and no Try again, until the person moves on', async () => {
		const line = 'Approved, but the public issue is closed, so it wasn’t sent.';
		alsoServe('/api/held-replies/held-intel/approve', () => {
			dropServed('message:intel-mac');
			return respond(200, { state: 'not_sent', line, reason: 'recipient_gone' });
		});
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Send')!.click();
		await settle();

		expect(alert()).toBe(line);
		expect(buttonNamed('Try again')).toBeUndefined();
		expect(buttonNamed('Send')!.disabled).toBe(true);
		expect(buttonNamed('Don’t send')!.disabled).toBe(true);
		// The server no longer returns it, and it stays open, in its place, until the person moves on.
		expect(served).toHaveLength(6);
		expect(pane().querySelector('h1')?.textContent).toBe(activityFrameItems()[5].title);
		expect(openRow()).toBe(rows()[5]);
		expect(rows()).toHaveLength(7);
		expect(host.querySelector('.count')?.textContent).toBe('6');
		expect(dockState()).toEqual(dockAtRest);

		nextButton().click();
		await settle();
		expect(rows()).toHaveLength(6);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[5].title);
		expect(alert()).toBeNull();
	});

	it('lets go of a kept item when another row is opened', async () => {
		alsoServe('/api/held-replies/held-intel/approve', () => {
			dropServed('message:intel-mac');
			return respond(200, { state: 'not_sent', line: 'It wasn’t sent.', reason: 'scope' });
		});
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Send')!.click();
		await settle();
		expect(rows()).toHaveLength(7);
		rows()[0].click();
		await settle();
		expect(rows()).toHaveLength(6);
		expect(rows().map((row) => row.querySelector('strong')?.textContent)).not.toContain(activityFrameItems()[5].title);
	});

	it('keeps a decision answered elsewhere first, with the server’s sentence and no Try again', async () => {
		const closed = 'This decision was answered in another chat.';
		let body: unknown = null;
		alsoServe('/api/plans/decisions/decision-docs/answer', (init) => {
			body = JSON.parse(String(init?.body));
			dropServed('decision:docs-review');
			return respond(410, { detail: closed });
		});
		render();
		await settle();
		rows()[1].click();
		await settle();
		pane().querySelector<HTMLInputElement>('input[value="ship"]')!.click();
		await settle();
		buttonNamed('Answer')!.click();
		await settle();

		expect(body).toMatchObject({ option_id: 'ship', expected_revision: expect.any(Number), mutation_id: expect.any(String) });
		expect(alert()).toBe(closed);
		expect(buttonNamed('Try again')).toBeUndefined();
		expect(buttonNamed('Answer')!.disabled).toBe(true);
		expect(openRow()).toBe(rows()[1]);
		expect(pane().querySelector('h1')?.textContent).toBe(activityFrameItems()[1].title);
		expect(dockState()).toEqual(dockAtRest);
	});

	it('keeps a failed answer with its reason and Try again, retries with the same key, and leaves the dock alone', async () => {
		const keys: string[] = [];
		let fail = true;
		alsoServe('/api/plans/decisions/decision-docs/answer', (init) => {
			keys.push((JSON.parse(String(init?.body)) as { mutation_id: string }).mutation_id);
			if (fail) return respond(500, {});
			dropServed('decision:docs-review');
			return respond(200, {});
		});
		render();
		await settle();
		rows()[1].click();
		await settle();
		pane().querySelector<HTMLInputElement>('input[value="wake"]')!.click();
		await settle();
		buttonNamed('Answer')!.click();
		await settle();

		expect(alert()).toBe('Couldn’t save your answer. Try again.');
		expect(buttonNamed('Try again')!.disabled).toBe(false);
		expect(openRow()).toBe(rows()[1]);
		// The dock's drawer must not open on this decision the next time a chat shows.
		expect(dockState()).toEqual(dockAtRest);

		fail = false;
		buttonNamed('Try again')!.click();
		await settle();
		expect(keys).toHaveLength(2);
		expect(keys[1]).toBe(keys[0]);
		expect(rows()).toHaveLength(6);
		expect(alert()).toBeNull();
		expect(dockState()).toEqual(dockAtRest);
	});

	it('puts Try again on the button that failed', async () => {
		alsoServe('/api/held-replies/held-intel/decline', () => respond(500, {}));
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Don’t send')!.click();
		await settle();
		expect(alert()).toBe('Couldn’t decline this message. Try again.');
		// Send keeps its name: Try again there would send what the person chose not to.
		expect(buttonNamed('Send')).toBeTruthy();
		expect(buttonNamed('Don’t send')).toBeUndefined();
		expect(buttonNamed('Try again')).toBeTruthy();
	});

	it('names the action when a press throws', async () => {
		mutate.mockRejectedValue(new TypeError('Failed to fetch'));
		render();
		await settle();
		rows()[4].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		expect(alert()).toBe('Couldn’t mark this task done. Try again.');
		expect(buttonNamed('Try again')).toBeTruthy();
	});
});

describe('product events', () => {
	const events = () => emit.mock.calls.map(([category, action, properties]) => [`${category}.${action}`, properties ?? {}]);

	it('records Activity opening, a row opened, Next, and each press with its outcome', async () => {
		mutate.mockResolvedValueOnce({ status: 'error', message: 'Plans are read-only right now.' });
		mutate.mockResolvedValueOnce({ status: 'success' });
		render();
		await settle();
		rows()[4].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		buttonNamed('Try again')!.click();
		await settle();
		nextButton().click();
		await settle();
		expect(events()).toEqual([
			['activity.opened', { scope: 'project' }],
			['activity.row_opened', { kind: 'task' }],
			['activity.acted', { kind: 'task', action: 'done', outcome: 'failed' }],
			['activity.acted', { kind: 'task', action: 'done', outcome: 'succeeded' }],
			['activity.next', {}],
		]);
	});

	it('records a refused press with its reason', async () => {
		alsoServe('/api/held-replies/held-intel/approve', () => respond(200, { state: 'not_sent', line: 'Not sent.', reason: 'recipient_gone' }));
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Send')!.click();
		await settle();
		expect(events()).toContainEqual(['activity.acted', { kind: 'message', action: 'send', outcome: 'refused', reason: 'recipient_gone' }]);
	});
});

describe('Activity’s bottom band at rest', () => {
	it.each([
		[1, 'CORSIA Conductor', 'Waiting for your answer'],
		[4, 'Packaging & Onboarding', 'A step only you can do'],
		[5, 'Ecosystem', 'Waiting for your yes before it’s sent'],
	])('row %i names %s before its status, without being opened', async (row, agent, status) => {
		render();
		await settle();
		rows()[row].click();
		await settle();
		const band = pane().querySelector<HTMLElement>('.rb-dock')!;
		expect(band.classList.contains('open')).toBe(false);
		expect(band.classList.contains('asker')).toBe(true);
		expect(band.querySelector('.rb-dock-who')?.textContent).toBe(agent);
		expect(band.querySelector('.rb-dock-q')?.textContent).toBe(status);
	});

	it('keeps the name and its separator shown on the resting line', () => {
		// The docked card hides the asker at rest everywhere else; this rule is what shows it in Activity.
		const selector = '.rb-dock.asker:not(.open) .rb-dock-by, .rb-dock.asker:not(.open) .rb-dock-sep {';
		const start = shellSource.indexOf(selector);
		expect(start).toBeGreaterThan(-1);
		expect(shellSource.slice(start, shellSource.indexOf('}', start))).toContain('display: inline;');
		// It must come after the rule that hides them, or that rule wins.
		expect(start).toBeGreaterThan(shellSource.indexOf('.rb-dock:not(.named) .rb-dock-sep, .rb-dock:not(.named):not(.open) .rb-dock-by {'));
	});
});

describe('item bodies', () => {
	it('shows a task’s numbered steps as a numbered list', async () => {
		render();
		await settle();
		rows()[4].click();
		await settle();
		const steps = [...pane().querySelectorAll('ol.steps li')].map((li) => li.textContent);
		expect(steps).toEqual([
			'Packaging & Onboarding starts the signing.',
			'Sign in with your Apple ID and enter the code Apple sends.',
			'Press Done.',
		]);
		expect(pane().querySelector('.pre')).toBeNull();
	});

	it('reads every kind’s body at the document’s body size and line spacing', () => {
		// jsdom applies no component styles, so the rule is read from the source, as the band test above does.
		const rule = paneSource.match(/\n\t\.dctx, \.body, blockquote, \.opt \{([^}]*)\}/);
		expect(rule?.[1]).toContain('font-size: var(--text-body)');
		expect(rule?.[1]).toContain('line-height: var(--lh-relaxed)');
		// On a phone the document's text is --text-input (cm/theme.ts), and so is every body here.
		expect(paneSource).toContain(':global(.activity.phone) :is(.dctx, .body, blockquote, .opt) { font-size: var(--text-input); }');
		// No other rule in the pane sets a size on those bodies.
		for (const selector of ['.dctx {', '.body {', 'blockquote {', '.opt {', '.opt-detail {']) {
			const start = paneSource.indexOf(`\t${selector}`);
			expect(start, selector).toBeGreaterThan(-1);
			const block = paneSource.slice(start, paneSource.indexOf('}', start));
			expect(block, selector).not.toContain('font-size');
			expect(block, selector).not.toContain('line-height');
		}
	});
});

describe('a reply row in a document with two replied threads', () => {
	const replyRow = (key: string, root: string, reply: string, position: number) => activityItem({
		key, kind: 'reply', title: `Structured Plans replied on ${root}.`,
		source: activitySource('Structured Plans', 'thread-plans'),
		open: { artifact_id: 'art-later', comment_id: root },
		detail: { artifact_id: 'art-later', root_id: root, reply_id: reply },
		rank: { position, reason: 'So you see the reply.', ranked_by: 'thread-conductor', ranked_at: '2026-11-24T04:10:00Z' },
	});
	const activityTab = () => {
		const ws = get(threadWorkspaces)[activityWorkspaceKey('project-1')];
		return ws?.tabs.find((tab) => tab.instanceId === ws.activeTabId);
	};

	it('opens each row, and its Reply, on that row’s own thread', async () => {
		served = [replyRow('reply:first', 'c-first', 'r-first', 1), replyRow('reply:second', 'c-second', 'r-second', 2)];
		const fetchStub = vi.mocked(fetch);
		const queue = fetchStub.getMockImplementation()!;
		fetchStub.mockImplementation(async (input) => String(input).includes('/api/threads/thread-plans/artifacts')
			? respond(200, { nodes: [{ kind: 'artifact', id: 'art-later', name: 'later.md', title: 'Later row', path: '/docs/later.md' }], needs_attention: [] })
			: queue(input));
		render();
		await settle();
		expect(activityTab()).toMatchObject({ landOn: 'new-replies', landOnCommentId: 'c-first' });

		rows()[1].click();
		await settle();
		const tab = activityTab()!;
		expect(tab.landOnCommentId).toBe('c-second');

		// Reply lands the open document on the same thread again, not the document's first unread one, without closing
		// the tab: a close would send the person's unsent comments on it.
		consumeTabLanding(activityWorkspaceKey('project-1'), tab.instanceId);
		const closes = closeTab.mock.calls.length;
		buttonNamed('Reply')!.click();
		await settle();
		expect(activityTab()).toMatchObject({ instanceId: tab.instanceId, landOn: 'new-replies', landOnCommentId: 'c-second' });
		expect(closeTab.mock.calls.length).toBe(closes);
		expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/api/doc-feedback'))).toBe(false);
	});
});

describe('Activity on a phone', () => {
	it('shows the list, then the item alone with position and Next in the header', async () => {
		mobile.store!.set(true);
		render();
		await settle();
		const view = host.querySelector('.activity')!;
		expect(view.classList.contains('phone')).toBe(true);
		expect(view.classList.contains('phone-item')).toBe(false);
		expect(get(activityPhoneItem)).toBeNull();

		rows()[1].click();
		await settle();
		expect(view.classList.contains('phone-item')).toBe(true);
		const header = get(activityPhoneItem)!;
		expect(header).toMatchObject({ position: 2, count: 7, nextDisabled: false });

		header.onNext();
		await settle();
		expect(get(activityPhoneItem)).toMatchObject({ position: 3, count: 7 });

		get(activityPhoneItem)!.onBack();
		await settle();
		expect(view.classList.contains('phone-item')).toBe(false);
		expect(get(activityPhoneItem)).toBeNull();
		expect(openRow()).toBe(rows()[2]);
	});

	it('keeps the list’s type and header spacing as on a wide screen', () => {
		// Read from the source: jsdom applies no component styles.
		const phoneRules = listSource.split('\n').filter((line) => line.includes(':global(.activity.phone)'));
		// Titles keep the sidebar rows' token; the phone type scale alone raises it.
		expect(phoneRules.some((line) => line.includes('strong'))).toBe(false);
		// Links get their touch area from a pseudo-element, so Next and the ranker link don't push the header lines apart.
		expect(listSource).not.toMatch(/min-height: var\(--touch-target\)/);
		expect(listSource).toMatch(/\.link::after \{\n[^}]*height: var\(--touch-target\)/);
	});
});
