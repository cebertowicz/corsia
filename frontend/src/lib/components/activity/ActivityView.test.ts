import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ActivityQueueItem } from '$lib/api/activityQueue';
import type { DecisionDetail } from '$lib/api/attentionQueue';
import { decisionOf } from './activityItems';
import { activityDocument, activityFrameItems, activityItem, activityQueueBody, activitySource } from '$lib/__tests__/fixtures/attentionQueue';

const { mobile, mutate, emit, closeTab, listeners } = vi.hoisted(() => ({
	mobile: { store: null as null | import('svelte/store').Writable<boolean> },
	mutate: vi.fn(),
	emit: vi.fn(),
	closeTab: vi.fn(),
	listeners: new Map<string, () => void>(),
}));

vi.mock('$lib/ws/events', () => ({ on: (event: string, handler: () => void) => { listeners.set(event, handler); return () => listeners.delete(event); } }));
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
import documentBarSource from './ActivityDocumentBar.svelte?raw';
import listSource from './ActivityList.svelte?raw';
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
import { emailDraftHeaders } from '$lib/stores/emailDraftHeaders';
import { activityOpenRequest, openActivity } from '$lib/stores/activityOpen';
import { activityWorkspaceKey } from '$lib/stores/activityWorkspace';
import { closeArtifactTab, consumeTabLanding, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { connectionState } from '$lib/stores/connection';
import { token, user } from '$lib/stores/auth';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
/** What the next queue read returns; tests change it to model the server settling an item. */
let served: ActivityQueueItem[] = [];
/** What the next read returns under Notifications. */
let notes: ActivityQueueItem[] = [];
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
const tabNamed = (id: 'tasks' | 'notifications') => host.querySelector<HTMLButtonElement>(`[role="tab"][data-tab="${id}"]`)!;
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
	notes = [];
	queueReads = 0;
	mobile.store?.set(false);
	connectionState.set('connected');
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
			return respond(200, activityQueueBody(served, notes));
		}
		// Document lookups fail here, so a document item shows its can't-open panel instead of the editor.
		return respond(404, {});
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	emailDraftHeaders.set({});
	vi.unstubAllGlobals();
});

describe('Activity browser steps', () => {
	it('opens the browser for its thread, while an ordinary task stays a task', async () => {
		const ordinary = served.find((item) => item.kind === 'task')!;
		const browser: ActivityQueueItem = {
			...ordinary,
			key: 'task:browser', external_id: 'browser', title: 'Continue in browser',
			detail: { plan_id: 'p', node_id: 'browser', revision: 1, description: 'Enter your password.', browser_thread_id: 'browser-owner' },
		};
		served = [ordinary, browser];
		render();
		await settle();
		const key = activityWorkspaceKey('project-1');
		expect(get(threadWorkspaces)[key]?.tabs.some((tab) => tab.kind === 'browser')).not.toBe(true);
		rows()[1].click();
		await settle();
		expect(get(threadWorkspaces)[key]?.tabs.some((tab) => tab.kind === 'browser' && tab.threadId === 'browser-owner')).toBe(true);
	});

	it('keeps the step’s instructions and Done beside its browser, and returns to the task when the tab closes', async () => {
		const ordinary = served.find((item) => item.kind === 'task')!;
		served = [{
			...ordinary,
			key: 'task:browser', external_id: 'browser', title: 'Continue in browser',
			detail: { plan_id: 'p', node_id: 'browser', revision: 1, description: 'Enter your password.', browser_thread_id: 'browser-owner' },
		}];
		render();
		await settle();
		rows()[0].click();
		await settle();
		const key = activityWorkspaceKey('project-1');
		const tab = get(threadWorkspaces)[key]!.tabs.find((entry) => entry.kind === 'browser')!;
		expect(pane().textContent).toContain('Enter your password.');
		expect(buttonNamed('Done')).toBeDefined();
		closeArtifactTab(key, tab.instanceId);
		await settle();
		expect(get(threadWorkspaces)[key]?.tabs.some((entry) => entry.kind === 'browser')).not.toBe(true);
		expect(pane().textContent).toContain('Enter your password.');
		expect(buttonNamed('Done')).toBeDefined();
	});

	it('closes the phone’s open-items sheet when its last tab closes, as a thread does', async () => {
		mobile.store!.set(true);
		const ordinary = served.find((item) => item.kind === 'task')!;
		served = [{
			...ordinary,
			key: 'task:browser', external_id: 'browser', title: 'Continue in browser',
			detail: { plan_id: 'p', node_id: 'browser', revision: 1, description: 'Enter your password.', browser_thread_id: 'browser-owner' },
		}];
		render();
		await settle();
		rows()[0].click();
		await settle();
		const key = activityWorkspaceKey('project-1');
		const tab = get(threadWorkspaces)[key]!.tabs.find((entry) => entry.kind === 'browser')!;
		get(activityPhoneItem)!.artifact!.onTabs();
		await settle();
		expect(get(activityPhoneItem)!.tabsOpen).toBe(true);
		closeArtifactTab(key, tab.instanceId);
		await settle();
		expect(get(activityPhoneItem)!.tabsOpen).toBe(false);
		expect(pane().textContent).toContain('Enter your password.');
	});
});

describe('Activity list', () => {
	it('shows a missing draft and lets the person discard a registered one', async () => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true } })];
		alsoServe('/api/email-drafts/art-draft/discard', () => {
			served = [];
			return respond(200, { state: 'discarded' });
		});
		render(); await settle();
		expect(rows()[0].textContent).toContain('File missing');
		expect(pane().textContent).toContain('This draft’s file is missing or can’t be opened, so it can’t be sent.');
		expect(buttonNamed('Send now')).toBeUndefined();
		buttonNamed('Don’t send')!.click();
		await vi.waitFor(() => expect(rows()).toHaveLength(0));
	});

	it('acknowledges an unconfirmed missing draft and clears its row', async () => {
		served = [activityItem({ key: 'draft:missing', external_id: 'draft-1', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true, unconfirmed: true } })];
		alsoServe('/api/attention/drafts/draft-1/acknowledge', () => { served = []; return respond(204, null); });
		render(); await settle();
		expect(pane().textContent).toContain('Couldn’t confirm the email left. Check your Sent mail.');
		expect(buttonNamed('Don’t send')).toBeUndefined();
		buttonNamed('Done')!.click();
		await vi.waitFor(() => expect(rows()).toHaveLength(0));
	});

	it('keeps an unconfirmed missing draft when acknowledgement fails', async () => {
		served = [activityItem({ key: 'draft:missing', external_id: 'draft-1', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true, unconfirmed: true } })];
		alsoServe('/api/attention/drafts/draft-1/acknowledge', () => respond(500, {}));
		render(); await settle();
		buttonNamed('Done')!.click();
		await vi.waitFor(() => expect(alert()).toContain('Couldn’t mark this done. Try again.'));
		expect(pane().querySelector('[role="alert"]')?.classList.contains('failure')).toBe(true);
		expect(rows()).toHaveLength(1);
	});

	it('pauses Done for an unconfirmed missing draft while offline', async () => {
		served = [activityItem({ key: 'draft:missing', external_id: 'draft-1', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true, unconfirmed: true } })];
		connectionState.set('disconnected');
		render(); await settle();
		expect(buttonNamed('Done')?.disabled).toBe(true);
		expect(pane().textContent).toContain('Done is paused until this reconnects.');
	});

	it('explains why removing an open missing draft is paused offline', async () => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true } })];
		connectionState.set('disconnected');
		render(); await settle();
		expect(buttonNamed('Don’t send')?.disabled).toBe(true);
		expect(pane().textContent).toContain('Don’t send is paused until this reconnects.');
	});

	it('shows a failure when a missing draft cannot be removed', async () => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			source: activitySource('Untitled thread', 'thread-1'),
			open: null, detail: { artifact_id: 'art-draft', missing: true } })];
		alsoServe('/api/email-drafts/art-draft/discard', () => respond(500, {}));
		render(); await settle();
		buttonNamed('Don’t send')!.click();
		await vi.waitFor(() => expect(alert()).toContain('Don’t send didn’t go through. Try again.'));
		expect(pane().querySelector('[role="alert"]')?.classList.contains('failure')).toBe(true);
		const actions = buttonNamed('Don’t send')!.parentElement!;
		expect(actions).toBe(buttonNamed('Open the chat')!.parentElement!.parentElement);
		expect(actions.nextElementSibling?.getAttribute('role')).toBe('alert');
		expect(rows()).toHaveLength(1);
	});

	it.each([
		['123456789012345678901234', 'Open 123456789012345678901234’s chat'],
		['1234567890123456789012345', 'Open the chat'],
	])('labels the missing draft chat for a %s name', async (name, label) => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			source: activitySource(name, 'thread-1'),
			open: null, detail: { artifact_id: 'art-draft', missing: true } })];
		const onSelectThread = render(); await settle();
		buttonNamed(label)!.click();
		expect(onSelectThread).toHaveBeenCalledWith('thread-1');
	});

	it.each(['Untitled thread', '1234567890123456789012345'])('keeps the named chat label for other fallback rows: %s', async (name) => {
		served = [activityItem({ key: 'review:unavailable', kind: 'review', title: 'Review',
			source: activitySource(name, 'thread-1'), open: null, detail: { artifact_id: null } })];
		const onSelectThread = render(); await settle();
		buttonNamed(`Open ${name}’s chat`)!.click();
		expect(onSelectThread).toHaveBeenCalledWith('thread-1');
	});

	it('opens an untitled missing draft chat with a natural label', async () => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			source: activitySource('Untitled thread', 'thread-1'),
			open: null, detail: { artifact_id: 'art-draft', missing: true } })];
		const onSelectThread = render(); await settle();
		buttonNamed('Open the chat')!.click();
		expect(onSelectThread).toHaveBeenCalledWith('thread-1');
	});

	it('keeps a draft’s unread document open when its review settles', async () => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true, unread_replies: 1 } })];
		alsoServe('/api/email-drafts/art-draft/discard', () => {
			served = [];
			notes = [activityDocument('draft', { artifact_id: 'art-draft' })];
			return respond(200, { state: 'discarded' });
		});
		render(); await settle();
		buttonNamed('Don’t send')!.click();
		await vi.waitFor(() => expect(pane().textContent).toContain('Couldn’t open this document.'));
		expect(buttonNamed('Don’t send')).toBeUndefined();
	});

	it('advances after a draft without a review even when its document has unread replies', async () => {
		const next = activityItem({ key: 'task:next', kind: 'task', title: 'Next task' });
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true } }), next];
		alsoServe('/api/email-drafts/art-draft/discard', () => {
			served = [next];
			notes = [activityDocument('draft', { artifact_id: 'art-draft' })];
			return respond(200, { state: 'discarded' });
		});
		render(); await settle();
		buttonNamed('Don’t send')!.click();
		await vi.waitFor(() => expect(pane().textContent).toContain('Next task'));
	});

	it('keeps the settled draft open when its queue reread fails', async () => {
		let failRead = false;
		let failedReads = 0;
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: 'art-draft', missing: true } })];
		alsoServe('/api/email-drafts/art-draft/discard', () => {
			failRead = true;
			return respond(200, { state: 'discarded' });
		});
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation((input, init) => {
			if (failRead && String(input).includes('/api/attention/queue')) {
				failedReads += 1;
				return Promise.resolve(respond(500, {}));
			}
			return base(input, init);
		});
		render(); await settle();
		buttonNamed('Don’t send')!.click();
		await vi.waitFor(() => expect(failedReads).toBeGreaterThan(0));
		await settle();
		expect(pane().textContent).toContain('This draft’s file is missing or can’t be opened, so it can’t be sent.');
		expect(rows()).toHaveLength(1);
	});

	it('offers no discard action for a missing draft without an identity', async () => {
		served = [activityItem({ key: 'draft:missing', kind: 'draft', title: 'Email draft',
			open: null, detail: { artifact_id: null, missing: true } })];
		render(); await settle();
		expect(buttonNamed('Don’t send')).toBeUndefined();
	});
	it('shows every kind in the ranker’s order and opens the top item with why it is there', async () => {
		render();
		await settle();
		expect(rows().map((row) => row.querySelector('.rank')?.textContent)).toEqual(['1', '2', '3', '4', '5', '6', '7']);
		expect(rows().map((row) => row.querySelector('strong')?.textContent)).toEqual(served.map((item) => item.title));
		expect(tabNamed('tasks').getAttribute('aria-selected')).toBe('true');
		expect(tabNamed('tasks').getAttribute('aria-label')).toBe('Your tasks, 7');
		expect(host.querySelector('.count')?.textContent).toBe('7');
		expect(host.querySelector('.by')?.textContent).toContain('Order set by CORSIA Conductor');
		expect(openRow()).toBe(rows()[0]);
		expect(openRow()?.querySelector('.why')?.textContent).toContain('0.5.0 publishes Nov 24');
		expect(rows()[1].querySelector('.why')).toBeNull();
		expect(rows()[4].querySelector('.sub')?.textContent).toContain('Packaging & Onboarding · Task');
	});

	it('keeps a review with unread replies in Your tasks, carrying its reply count', async () => {
		render();
		await settle();
		expect(rows()[6].querySelector('.sub')?.textContent).toMatch(/ · 3 unread replies$/);
		expect(rows()[0].querySelector('.sub')?.textContent).not.toContain('unread');
	});

	it('opening another row moves on without acting', async () => {
		render();
		await settle();
		rows()[1].click();
		await settle();
		expect(openRow()).toBe(rows()[1]);
		expect(pane().querySelector('h1')?.textContent).toBe(served[1].title);
		expect(mutate).not.toHaveBeenCalled();
	});

	it('opens the row above after acting on the last row', async () => {
		const sign = served.find((item) => item.key === 'task:sign-build')!;
		served = [...served.filter((item) => item !== sign), sign];
		mutate.mockImplementation(async () => {
			served = served.filter((item) => item.key !== 'task:sign-build');
			return { status: 'success' };
		});
		render();
		await settle();
		rows()[6].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		expect(rows()).toHaveLength(6);
		expect(openRow()).toBe(rows()[5]);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[5].title);
	});

	it('keeps an item that leaves on a re-read open until another row is opened', async () => {
		render();
		await settle();
		rows()[4].click();
		await settle();
		const original = pane().querySelector('h1');
		const title = served[4].title;
		served = served.filter((item) => item.key !== 'task:sign-build');
		listeners.get('doc_comment_changed')?.();
		await settle();
		expect(pane().querySelector('h1')).toBe(original);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(title);
		expect(pane().textContent).toContain('No longer waiting on you.');
		rows()[5].click();
		await settle();
		expect(rows()).toHaveLength(6);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[4].title);
		expect(pane().textContent).not.toContain('No longer waiting on you.');
	});

	it('follows the same document under a new key without reopening its pane', async () => {
		render();
		await settle();
		const original = pane().querySelector('h1');
		const reads = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/artifacts')).length;
		served = [{ ...served[0], key: 'review:new-key' }, ...served.slice(1)];
		listeners.get('doc_comment_changed')?.();
		await settle();
		expect(pane().querySelector('h1')).toBe(original);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[0].title);
		expect(pane().textContent).not.toContain('No longer waiting on you.');
		expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/artifacts'))).toHaveLength(reads);
	});

	it('keeps the original item when the same document returns from another thread', async () => {
		render();
		await settle();
		const original = pane().querySelector('h1');
		const originalRow = openRow();
		served = [{ ...served[0], key: 'review:new-key', source: activitySource('Another agent', 'thread-another') }, ...served.slice(1)];
		listeners.get('doc_comment_changed')?.();
		await settle();
		expect(pane().querySelector('h1')).toBe(original);
		expect(openRow()).toBe(originalRow);
		expect(pane().textContent).toContain('No longer waiting on you.');
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
		// The plans API's own generic line is not shown; Activity names the action, as every press does.
		mutate.mockResolvedValue({ status: 'error', message: 'The server had a problem handling this request.' });
		render();
		await settle();
		rows()[4].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		expect(pane().querySelector('[role="alert"]')?.textContent).toBe('Couldn’t mark this task done. Try again.');
		expect(buttonNamed('Try again')).toBeTruthy();
		expect(openRow()).toBe(rows()[4]);
		expect(rows()).toHaveLength(7);
	});

	it('says nothing needs you when the queue is empty, and keeps both tabs without a count', async () => {
		served = [];
		render();
		await settle();
		expect(host.querySelector('.empty-line')?.textContent).toBe('Nothing needs you.');
		expect(rows()).toHaveLength(0);
		expect(pane().textContent).toContain('Nothing needs you.');
		expect(tabNamed('tasks').getAttribute('aria-label')).toBe('Your tasks');
		expect(tabNamed('notifications').getAttribute('aria-label')).toBe('Notifications');
	});

	it('shows the notification empty line in both list and pane', async () => {
		served = [];
		render();
		await settle();
		tabNamed('notifications').click();
		await settle();
		expect(host.querySelector('.empty-line')?.textContent).toBe('No unread replies.');
		expect(pane().textContent).toContain('No unread replies.');
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
		// The button already says it; no sentence repeats it.
		expect(text).not.toContain('Try again.');
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

	it('keeps the last list and identifies it when a re-read is malformed, then clears the notice on recovery', async () => {
		let malformed = false;
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (String(input).includes('/api/attention/queue') && malformed) return respond(200, { count: 0 });
			return base(input, init);
		});
		render();
		await settle();
		const count = rows().length;
		malformed = true;
		listeners.get('plans_changed')!();
		await settle();
		expect(rows()).toHaveLength(count);
		expect(host.textContent).toContain('Showing last known items.');
		malformed = false;
		listeners.get('plans_changed')!();
		await settle();
		expect(host.textContent).not.toContain('Showing last known items.');
	});

	it('does not mark fresh Activity rows stale when the separate attention queue read fails', async () => {
		const attention = await import('$lib/stores/attentionQueueState.svelte');
		attention.__reset();
		token.set('test-token');
		render();
		await settle();
		const count = rows().length;
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation(async (input, init) => String(input).includes('view=activity')
			? base(input, init) : respond(503, {}));
		await attention.reload();
		await settle();
		expect(rows()).toHaveLength(count);
		expect(host.textContent).not.toContain('Showing last known items.');
		vi.mocked(fetch).mockImplementation(async (input, init) => String(input).includes('view=activity')
			? base(input, init) : respond(200, { current: null, count: 0, items: [] }));
		await attention.reload();
		await settle();
		expect(host.textContent).not.toContain('Showing last known items.');
		attention.__reset();
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
	const dropServed = (key: string) => { served = served.filter((item) => item.key !== key); };

	it('sends a held message once and advances after the server confirms delivery', async () => {
		let finish!: (response: Response) => void;
		let calls = 0;
		alsoServe('/api/held-replies/held-intel/approve', () => {
			calls += 1;
			return new Promise<Response>((resolve) => { finish = resolve; });
		});
		render();
		await settle();
		rows()[5].click();
		await settle();
		expect(pane().textContent).toContain('To: the public issue on GitHub');
		expect(pane().querySelector('blockquote')?.textContent).toBe("Thanks for asking. We haven't tried 0.5.0 on an Intel Mac yet; we'll post here once we have.");
		buttonNamed('Send')!.click();
		await settle();
		expect(calls).toBe(1);
		expect(pane().querySelector('h1')?.textContent).toBe(activityFrameItems()[5].title);
		dropServed('message:intel-mac');
		finish(respond(200, { state: 'sent' }));
		await settle();
		expect(calls).toBe(1);
		expect(rows()).toHaveLength(6);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[5].title);
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

		rows()[6].click();
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
		expect(alert()).toBe('It wasn’t sent.');
		expect(buttonNamed('Send')!.disabled).toBe(true);
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
	});

	it('shows a closed decision fallback when the refusal has no sentence', async () => {
		alsoServe('/api/plans/decisions/decision-docs/answer', () => respond(410, {}));
		render();
		await settle();
		rows()[1].click();
		await settle();
		pane().querySelector<HTMLInputElement>('input[value="ship"]')!.click();
		await settle();
		buttonNamed('Answer')!.click();
		await settle();
		expect(alert()).toBe('That decision is closed.');
		expect(buttonNamed('Answer')!.disabled).toBe(true);
	});

	it('answers the selected decision from its own thread, never another decision', async () => {
		const first = served[1];
		const firstDetail = decisionOf(first)!;
		const source = activitySource('Another agent', 'thread-other');
		served = [first, { ...first, key: 'decision:other', external_id: 'decision-other', title: 'Another decision', source,
			detail: { ...firstDetail, id: 'decision-other', thread_id: 'thread-other', source } }];
		const requests: string[] = [];
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			const url = String(input);
			if (url.includes('/api/plans/decisions/') && url.endsWith('/answer')) {
				requests.push(url);
				return respond(410, { detail: 'This decision was answered in another chat.' });
			}
			return base(input, init);
		});
		const selectThread = render();
		await settle();
		rows()[1].click();
		await settle();
		pane().querySelector<HTMLInputElement>('input[value="ship"]')!.click();
		await settle();
		buttonNamed('Answer')!.click();
		await settle();
		expect(requests).toEqual(['/api/plans/decisions/decision-other/answer']);
		expect(selectThread).not.toHaveBeenCalled();
	});

	it('keeps the stale decision in place when the server refuses an answer after a failed re-read', async () => {
		let failRead = false;
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (String(input).includes('/api/attention/queue') && failRead) return respond(500, {});
			return base(input, init);
		});
		const writes: string[] = [];
		alsoServe('/api/plans/decisions/decision-docs/answer', (init) => {
			writes.push(String(init?.body));
			dropServed('decision:docs-review');
			return respond(410, { detail: 'This decision was answered in another chat.' });
		});
		render();
		await settle();
		rows()[1].click();
		await settle();
		const title = pane().querySelector('h1')?.textContent;
		failRead = true;
		listeners.get('plans_changed')!();
		await settle();
		expect(host.textContent).toContain('Showing last known items.');
		expect(pane().querySelector('h1')?.textContent).toBe(title);
		pane().querySelector<HTMLInputElement>('input[value="ship"]')!.click();
		await settle();
		failRead = false;
		buttonNamed('Answer')!.click();
		await settle();
		expect(writes).toHaveLength(1);
		expect(JSON.parse(writes[0])).toMatchObject({ option_id: 'ship' });
		expect(mutate).not.toHaveBeenCalled();
		expect(alert()).toBe('This decision was answered in another chat.');
		expect(openRow()).toBe(rows()[1]);
		expect(pane().querySelector('h1')?.textContent).toBe(title);
	});

	it('keeps a failed answer with its reason and Try again, retries with the same key, and leaves the dock alone', async () => {
		const keys: string[] = [];
		let fail = true;
		alsoServe('/api/plans/decisions/decision-docs/answer', (init) => {
			keys.push((JSON.parse(String(init?.body)) as { mutation_id: string }).mutation_id);
			if (fail) return respond(500, { detail: 'internal identifier should stay private' });
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

		fail = false;
		buttonNamed('Try again')!.click();
		await settle();
		expect(keys).toHaveLength(2);
		expect(keys[1]).toBe(keys[0]);
		expect(rows()).toHaveLength(6);
		expect(alert()).toBeNull();
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

	it('shows the server’s refusal of Done without Try again', async () => {
		mutate.mockResolvedValue({ status: 'error', message: 'This task is already finished.', refused: true });
		render();
		await settle();
		rows()[4].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		expect(alert()).toBe('This task is already finished.');
		expect(buttonNamed('Try again')).toBeUndefined();
		expect(emit).toHaveBeenCalledWith('activity', 'acted', { kind: 'task', action: 'done', outcome: 'refused', reason: 'rejected' });
	});

	it('shows a failed Send’s reference and retries the same message', async () => {
		let calls = 0;
		alsoServe('/api/held-replies/held-intel/approve', () => {
			calls += 1;
			if (calls === 1) return respond(502, { detail: 'Delivery failed.', reference: 'err-7f3a' });
			dropServed('message:intel-mac');
			return respond(200, { state: 'sent' });
		});
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Send')!.click();
		await settle();
		expect(alert()).toBe('Delivery failed.');
		expect(pane().querySelector('.reference')?.textContent).toBe('Reference: err-7f3a');
		// The band is a wrapping flex row: the reference must share its line's flex item, or it lands beside the buttons.
		expect(pane().querySelector('.act-failure-row > .act-failure + .reference')).toBeTruthy();
		buttonNamed('Try again')!.click();
		await settle();
		expect(calls).toBe(2);
		expect(rows()).toHaveLength(6);
	});

	it('shows why an earlier Send didn’t go, with its reference, before any press', async () => {
		const detail = served[5].detail as { error: string | null; error_ref?: string | null };
		detail.error = 'Couldn’t reach GitHub.';
		detail.error_ref = 'err-91c2';
		render();
		await settle();
		rows()[5].click();
		await settle();
		expect(pane().querySelector('.act-failure')?.textContent).toBe('Couldn’t reach GitHub.');
		expect(pane().querySelector('.reference')?.textContent).toBe('Reference: err-91c2');
		expect(pane().querySelector('.act-failure-row > .act-failure + .reference')).toBeTruthy();
		expect(buttonNamed('Send')).toBeTruthy();
	});
});

// What the dock's own tests checked about answering, on Activity's controls, now that Activity is the one place a
// decision or a held message is answered.
describe('opened from another surface', () => {
	afterEach(() => activityOpenRequest.set(null));

	it('opens the named to-do in Your tasks, from Notifications too, and clears the request', async () => {
		notes = [activityDocument('plan-pane', {}, 1)];
		render();
		await settle();
		tabNamed('notifications').click();
		await settle();
		openActivity('decision-docs');
		await settle();
		expect(tabNamed('tasks').getAttribute('aria-selected')).toBe('true');
		expect(openRow()).toBe(rows()[1]);
		expect(get(activityOpenRequest)).toBeNull();
	});

	it('opens the named to-do once a read has it, when asked before Activity mounted', async () => {
		openActivity('decision-docs');
		render();
		await settle();
		expect(openRow()).toBe(rows()[1]);
		expect(get(activityOpenRequest)).toBeNull();
	});

	it('with no item named, opens on the top to-do', async () => {
		openActivity();
		render();
		await settle();
		expect(openRow()).toBe(rows()[0]);
		expect(get(activityOpenRequest)).toBeNull();
	});
});

describe('answering a decision or a held message in Activity', () => {
	const answers: Record<string, unknown>[] = [];
	const openDecision = async (over: Partial<DecisionDetail> = {}) => {
		served = served.map((item) => {
			const detail = decisionOf(item);
			return item.key === 'decision:docs-review' && detail
				? { ...item, detail: { ...detail, ...over } }
				: item;
		});
		render();
		await settle();
		rows()[1].click();
		await settle();
	};
	const answerWith = (status: number, body: unknown = {}) => alsoServe('/api/plans/decisions/decision-docs/answer', (init) => {
		answers.push(JSON.parse(String(init?.body)));
		return respond(status, body);
	});
	const choose = async (value: string) => {
		pane().querySelector<HTMLInputElement>(`input[value="${value}"]`)!.click();
		await settle();
	};
	const typeOther = async (text: string) => {
		const field = pane().querySelector<HTMLTextAreaElement>('textarea.other')!;
		field.value = text;
		field.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
	};

	beforeEach(() => { answers.length = 0; });

	it('identifies a decision raised without parent review', async () => {
		const source = decisionOf(served[1])!.source;
		await openDecision({ source: { ...source, parent_reviewed: false } });
		expect(pane().textContent).toContain('Raised directly — no parent review.');
	});

	it('shows decision context and tradeoff before the choices', async () => {
		await openDecision({ context: 'The context.', tradeoff: 'The tradeoff.' });
		const text = pane().textContent!;
		expect(text).toContain('The context.');
		expect(text).toContain('The tradeoff.');
		expect(text.indexOf('The context.')).toBeLessThan(text.indexOf('The tradeoff.'));
		expect(text.indexOf('The tradeoff.')).toBeLessThan(text.indexOf('Ship without their review'));
	});

	it('marks only the recommended option', async () => {
		await openDecision();
		expect(pane().textContent).not.toContain('Raised directly');
		const marked = [...pane().querySelectorAll('label.opt')].filter((label) => label.querySelector('em')?.textContent === 'Recommended');
		expect(marked.map((label) => label.querySelector('b')?.textContent)).toEqual(['Ship without their review']);
	});

	it('shows the recommendation reason under the recommended option and describes it', async () => {
		await openDecision({ recommendation: 'ship', rationale: 'This is the reason.' });
		const option = pane().querySelector<HTMLInputElement>('input[value="ship"]')!;
		const reason = document.getElementById(option.getAttribute('aria-describedby')!)!;
		expect(reason.textContent).toContain('Why');
		expect(reason.textContent).toContain('This is the reason.');
		expect(option.closest('label')!.contains(reason)).toBe(true);
		expect(pane().querySelector('dl')?.textContent ?? '').not.toContain('Why');
	});

	it('keeps four long pick-any labels in separate choices', async () => {
		const long = 'A deliberately long option label that wraps in its row '.repeat(2).trim();
		await openDecision({ pick: 'any', recommendation: ['A'], options: ['A', 'B', 'C', 'D'].map((id) => ({ id, label: `${id} ${long}`, detail: long })) });
		const choices = [...pane().querySelectorAll('label.opt')];
		expect(choices).toHaveLength(4);
		expect(choices.map((choice) => choice.querySelector('b')?.textContent)).toEqual(['A', 'B', 'C', 'D'].map((id) => `${id} ${long}`));
	});

	it('shows a pick-any recommendation reason once, on the first recommended option', async () => {
		await openDecision({ pick: 'any', recommendation: ['wake', 'ship'], rationale: 'Choose both.' });
		const options = [...pane().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
		const reason = document.getElementById(options[0].getAttribute('aria-describedby')!)!;
		expect(reason.textContent).toContain('Choose both.');
		expect(options[0].closest('label')!.contains(reason)).toBe(true);
		expect(options[1].getAttribute('aria-describedby')).toBeNull();
		expect(pane().querySelectorAll('.option-rationale')).toHaveLength(1);
		expect(pane().textContent!.match(/Choose both\./g)).toHaveLength(1);
	});

	it('shows a Why definition when the recommendation matches no option', async () => {
		await openDecision({ recommendation: 'missing', rationale: 'No match.' });
		const term = [...pane().querySelectorAll('dt')].find((el) => el.textContent === 'Why');
		expect(term?.nextElementSibling?.textContent).toBe('No match.');
		expect(pane().textContent).not.toContain('Recommended');
		expect(pane().querySelector('.option-rationale')).toBeNull();
	});

	it('keeps a decision choice and typed answer across row switches', async () => {
		await openDecision({ other_enabled: true });
		await choose('other');
		await typeOther('A draft to keep.');
		rows()[0].click();
		await settle();
		rows()[1].click();
		await settle();
		expect(pane().querySelector<HTMLInputElement>('input[value="other"]')!.checked).toBe(true);
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('A draft to keep.');
	});

	it('discards a draft and an outstanding read when Activity unmounts', async () => {
		await openDecision({ other_enabled: true });
		await choose('other');
		await typeOther('Previous session draft');
		let resolveOld!: (response: Response) => void;
		const oldRead = new Promise<Response>((resolve) => { resolveOld = resolve; });
		alsoServe('/api/attention/queue', () => oldRead);
		listeners.get('plans_changed')!();
		await settle();
		await unmount(component!);
		component = null;
		const replacement = served.map((item) => ({ ...item, title: `New session: ${item.title}` }));
		vi.mocked(fetch).mockImplementation(async (input) => String(input).includes('/api/attention/queue')
			? respond(200, activityQueueBody(replacement)) : respond(404, {}));
		render();
		await settle();
		resolveOld(respond(200, activityQueueBody(served)));
		await settle();
		expect(rows().every((row) => row.textContent?.includes('New session:'))).toBe(true);
		rows()[1].click();
		await settle();
		expect(pane().querySelector<HTMLInputElement>('input[value="other"]')?.checked).toBe(false);
		expect(pane().textContent).not.toContain('Previous session draft');
	});

	it('keeps separate drafts for different decisions', async () => {
		const first = served[1];
		const detail = decisionOf(first)!;
		served[1] = { ...first, detail: { ...detail, other_enabled: true } };
		served.splice(2, 0, { ...first, key: 'decision:second', title: 'Another decision', detail: { ...detail, id: 'decision-second', other_enabled: true } });
		render();
		await settle();
		rows()[1].click();
		await settle();
		await choose('other');
		await typeOther('First answer');
		rows()[2].click();
		await settle();
		expect(pane().querySelector<HTMLInputElement>('input[value="other"]')!.checked).toBe(false);
		await choose('other');
		await typeOther('Second answer');
		rows()[1].click();
		await settle();
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('First answer');
		rows()[2].click();
		await settle();
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('Second answer');
	});

	it('keeps Answer off until something is chosen, and sends Something else’s text under the reserved option only', async () => {
		answerWith(200);
		await openDecision({ other_enabled: true });
		expect(buttonNamed('Answer')!.disabled).toBe(true);
		await choose('other');
		expect(buttonNamed('Answer')!.disabled).toBe(true);
		await typeOther('   ');
		expect(buttonNamed('Answer')!.disabled).toBe(true);
		await typeOther('Wake only Ecosystem.');
		expect(buttonNamed('Answer')!.disabled).toBe(false);
		buttonNamed('Answer')!.click();
		await settle();
		expect(answers).toEqual([expect.objectContaining({ option_id: 'other', other_text: 'Wake only Ecosystem.' })]);
	});

	it('sends a listed option without a leftover Something else draft', async () => {
		answerWith(200);
		await openDecision({ other_enabled: true });
		await choose('other');
		await typeOther('Changed my mind.');
		await choose('wake');
		buttonNamed('Answer')!.click();
		await settle();
		expect(answers).toEqual([expect.objectContaining({ option_id: 'wake', other_text: null })]);
	});

	it.each([
		[409, {}, 'This decision changed. Showing the latest.'],
		[422, { detail: 'Pick one of the listed options.' }, 'Pick one of the listed options.'],
		[403, {}, 'This decision isn’t available.'],
		[404, {}, 'This decision isn’t available.'],
	])('shows a %i refusal with its sentence and no Try again, and re-reads', async (status, body, line) => {
		answerWith(status, body);
		await openDecision();
		await choose('ship');
		const reads = queueReads;
		buttonNamed('Answer')!.click();
		await settle();
		expect(alert()).toBe(line);
		expect(buttonNamed('Try again')).toBeUndefined();
		expect(queueReads).toBeGreaterThan(reads);
		expect(openRow()).toBe(rows()[1]);
	});

	it('stops offering an answer when the decision is unavailable', async () => {
		answerWith(403, { detail: 'private server detail' });
		await openDecision();
		await choose('ship');
		buttonNamed('Answer')!.click();
		await settle();
		expect(alert()).toBe('This decision isn’t available.');
		expect(pane().textContent).not.toContain('private server detail');
		await choose('ship');
		expect(buttonNamed('Answer')!.disabled).toBe(true);
	});

	it('keeps typed text and selection after a validation refusal and a row switch', async () => {
		answerWith(422, { detail: 'Please revise your answer.' });
		await openDecision({ other_enabled: true });
		await choose('other');
		await typeOther('Keep this answer.');
		buttonNamed('Answer')!.click();
		await settle();
		expect(alert()).toBe('Please revise your answer.');
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('Keep this answer.');
		rows()[0].click();
		await settle();
		rows()[1].click();
		await settle();
		expect(pane().querySelector<HTMLInputElement>('input[value="other"]')!.checked).toBe(true);
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('Keep this answer.');
	});

	it('answers a pick-any decision with the ticked set and the note, and an explicit none as an empty list', async () => {
		answerWith(500);
		await openDecision({ pick: 'any', recommendation: ['ship'], other_enabled: true });
		expect([...pane().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].map((box) => box.checked)).toEqual([false, false]);
		buttonNamed('Answer')!.click();
		await settle();
		// Pick any's boxes carry no value; they're ticked in option order.
		for (const box of pane().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')) box.click();
		await settle();
		await typeOther('Both, please.');
		buttonNamed('Try again')!.click();
		await settle();
		expect(answers[0]).toMatchObject({ option_ids: [], other_text: null });
		expect(answers[1]).toMatchObject({ option_ids: ['ship', 'wake'], other_text: 'Both, please.' });
		expect(answers[1].mutation_id).toBe(answers[0].mutation_id);
	});

	it('retains pick-any selections and note after a failed answer and a row switch', async () => {
		answerWith(500);
		await openDecision({ pick: 'any', other_enabled: true });
		pane().querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
		await settle();
		await typeOther('Keep this note.');
		buttonNamed('Answer')!.click();
		await settle();
		expect(buttonNamed('Try again')).toBeDefined();
		rows()[0].click();
		await settle();
		rows()[1].click();
		await settle();
		expect([...pane().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].map((box) => box.checked)).toEqual([true, false]);
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('Keep this note.');
	});

	it('pauses decision controls offline and restores the draft on reconnect', async () => {
		await openDecision({ pick: 'any', other_enabled: true });
		pane().querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
		await settle();
		await typeOther('Keep this note.');
		connectionState.set('disconnected');
		await settle();
		expect([...pane().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].every((box) => box.disabled)).toBe(true);
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.disabled).toBe(true);
		expect(buttonNamed('Answer')!.disabled).toBe(true);
		expect(pane().textContent).toContain('Answering is paused until this reconnects.');
		connectionState.set('connected');
		await settle();
		expect(pane().querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(true);
		expect(pane().querySelector<HTMLTextAreaElement>('textarea.other')!.value).toBe('Keep this note.');
		expect(buttonNamed('Answer')!.disabled).toBe(false);
	});

	it('pauses held-message actions offline and restores them on reconnect', async () => {
		render();
		await settle();
		rows()[5].click();
		await settle();
		connectionState.set('disconnected');
		await settle();
		expect(buttonNamed('Don’t send')!.disabled).toBe(true);
		expect(buttonNamed('Send')!.disabled).toBe(true);
		expect(pane().textContent).toContain('Sending and declining are paused until this reconnects.');
		connectionState.set('connected');
		await settle();
		expect(buttonNamed('Send')!.disabled).toBe(false);
	});

	it('declines a held message through the decline call, and it leaves', async () => {
		let declined = false;
		alsoServe('/api/held-replies/held-intel/decline', () => {
			declined = true;
			served = served.filter((item) => item.key !== 'message:intel-mac');
			return respond(200, { state: 'declined' });
		});
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Don’t send')!.click();
		await settle();
		expect(declined).toBe(true);
		expect(rows()).toHaveLength(6);
		expect(alert()).toBeNull();
	});

	it('lets an already-decided held message leave without an error', async () => {
		alsoServe('/api/held-replies/held-intel/approve', () => {
			served = served.filter((item) => item.key !== 'message:intel-mac');
			return respond(409, { detail: 'This reply was already decided' });
		});
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed('Send')!.click();
		await settle();
		expect(rows()).toHaveLength(6);
		expect(alert()).toBeNull();
	});

	it.each(['Send', 'Don’t send'])('clears the expired session when %s gets a 401', async (action) => {
		token.set('test-session');
		user.set({ id: 'test-user', name: 'Test' });
		alsoServe('/api/held-replies/held-intel/', () => respond(401, {}));
		render();
		await settle();
		rows()[5].click();
		await settle();
		buttonNamed(action)!.click();
		await settle();
		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
	});

	it('re-reads when a held message changes elsewhere', async () => {
		render();
		await settle();
		const reads = queueReads;
		listeners.get('held_reply_changed')?.();
		await settle();
		expect(queueReads).toBeGreaterThan(reads);
	});
});

describe('product events', () => {
	const events = () => emit.mock.calls.map(([category, action, properties]) => [`${category}.${action}`, properties ?? {}]);

	it('records Activity opening, a row opened, and each press with its outcome', async () => {
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
		expect(events()).toEqual([
			['activity.opened', { scope: 'project' }],
			['activity.row_opened', { kind: 'task' }],
			['activity.acted', { kind: 'task', action: 'done', outcome: 'failed' }],
			['activity.acted', { kind: 'task', action: 'done', outcome: 'succeeded' }],
		]);
	});

	it('records a tab opened, a row dismissed, and Mark all read with how many documents it cleared', async () => {
		notes = [activityDocument('first', {}, 1), activityDocument('second', {}, 2), activityDocument('third', {}, 3)];
		alsoServe('/api/attention/notifications/read', (init) => {
			const read = (JSON.parse(String(init?.body)).documents as { artifact_id: string }[]).map((doc) => doc.artifact_id);
			notes = notes.filter((item) => !read.includes(item.open!.artifact_id ?? ''));
			return respond(200, {});
		});
		render();
		await settle();
		tabNamed('notifications').click();
		await settle();
		host.querySelectorAll<HTMLButtonElement>('button.dismiss')[2].click();
		await settle();
		buttonNamed('Mark all read')!.click();
		await settle();
		const seen = events().map(([name]) => name);
		expect(seen).not.toContain('activity.next');
		expect(events()).toContainEqual(['activity.tab_opened', { tab: 'notifications' }]);
		expect(events()).toContainEqual(['activity.dismissed', {}]);
		expect(events()).toContainEqual(['activity.marked_all_read', { count: 2 }]);
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
		expect(start).toBeGreaterThan(shellSource.indexOf('.rb-dock .rb-dock-sep, .rb-dock:not(.open) .rb-dock-by {'));
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

describe('a document row in Notifications', () => {
	const activityTab = () => {
		const ws = get(threadWorkspaces)[activityWorkspaceKey('project-1')];
		return ws?.tabs.find((tab) => tab.instanceId === ws.activeTabId);
	};
	let reads: unknown[] = [];

	beforeEach(() => {
		reads = [];
		notes = [
			activityDocument('plan-pane', {
				unread: 3, latest_reply_id: 'plan-pane-reply-3',
				agents: ['Governance & Scopes', 'Structured Plans', 'Ecosystem'],
			}, 1),
			activityDocument('tabs', { unread: 1, latest_reply_id: 'tabs-reply-1' }, 30),
		];
		const fetchStub = vi.mocked(fetch);
		const queue = fetchStub.getMockImplementation()!;
		fetchStub.mockImplementation(async (input, init) => {
			const url = String(input);
			const artifact = url.match(/\/api\/artifacts\/(art-[^/?]+)(?:\?|$)/);
			if (artifact) return respond(200, { artifact: { id: artifact[1], title: `${artifact[1]}.md`, path: `/docs/${artifact[1]}.md` } });
			if (url.includes('/api/attention/notifications/read')) {
				const body = JSON.parse(String(init?.body));
				reads.push(body);
				const read = (body.documents as { artifact_id: string }[]).map((doc) => doc.artifact_id);
				notes = notes.filter((item) => !read.includes(item.open!.artifact_id ?? ''));
				return respond(200, {});
			}
			return queue(input, init);
		});
	});

	/** Switching tabs keeps what is open; the person then opens a document's row. */
	async function openNotifications(openFirst = true) {
		render();
		await settle();
		tabNamed('notifications').click();
		await settle();
		if (!openFirst) return;
		rows()[0].click();
		await settle();
	}

	it('shows the current email subject in the phone header, falling back to the document title', async () => {
		served = [];
		mobile.store!.set(true);
		await openNotifications();
		const fallback = get(activityPhoneItem)!.artifact!.title;
		flushSync(() => emailDraftHeaders.set({ 'art-plan-pane': { id: 'draft', header: { text: 'Subject: First subject', recipients: [] } } }));
		expect(get(activityPhoneItem)!.artifact!.title).toBe('First subject');
		flushSync(() => emailDraftHeaders.set({ 'art-plan-pane': { id: 'draft', header: { text: 'Subject: Updated subject', recipients: [] } } }));
		expect(get(activityPhoneItem)!.artifact!.title).toBe('Updated subject');
		flushSync(() => emailDraftHeaders.set({}));
		expect(get(activityPhoneItem)!.artifact!.title).toBe(fallback);
	});

	it('opens the top row when switching to a populated tab with nothing open on desktop', async () => {
		served = [];
		render();
		await settle();
		expect(pane().textContent).toContain('Nothing needs you.');
		tabNamed('notifications').click();
		await settle();
		expect(openRow()).toBe(rows()[0]);
		expect(activityTab()?.artifactId).toBe('art-plan-pane');
	});

	it('selects the top row but stays on the list on a phone when nothing is open', async () => {
		served = [];
		mobile.store!.set(true);
		render();
		await settle();
		tabNamed('notifications').click();
		await settle();
		expect(openRow()).toBe(rows()[0]);
		expect(host.querySelector('.activity')?.classList.contains('phone-item')).toBe(false);
		expect(get(activityPhoneItem)).toBeNull();
	});

	it('keeps the open item when switching to a populated tab', async () => {
		render();
		await settle();
		const original = pane().getAttribute('aria-label');
		expect(original).not.toBe('Nothing open');
		tabNamed('notifications').click();
		await settle();
		expect(pane().getAttribute('aria-label')).toBe(original);
		expect(openRow()).toBeNull();
	});

	it('lists one row per document, newest reply first, counted by document', async () => {
		await openNotifications();
		expect(tabNamed('notifications').getAttribute('aria-label')).toBe('Notifications, 2');
		expect(rows().map((row) => row.querySelector('strong')?.textContent)).toEqual(['plan-pane.md', 'tabs.md']);
		expect(rows()[0].querySelector('.sub')?.textContent).toMatch(/^3 unread replies · latest /);
		expect(rows()[1].querySelector('.sub')?.textContent).toMatch(/^1 unread reply · latest /);
		expect(rows()[0].querySelector('.rank')).toBeNull();
	});

	it('opens the document at its newest unread reply, with who replied and Done on the bar’s line', async () => {
		await openNotifications();
		expect(openRow()).toBe(rows()[0]);
		expect(activityTab()).toMatchObject({ artifactId: 'art-plan-pane', landOn: 'new-replies', landOnCommentId: 'plan-pane-root' });
		expect(pane().querySelector('.doc-who')?.textContent).toBe('Governance & Scopes and 2 others');
		expect(pane().querySelector('.doc-at')?.textContent).toBe('1 of 3 replies to your comments');
		expect(pane().querySelector('.doc-line .doc-done button')?.textContent?.trim()).toBe('Done');
	});

	it('Done marks the document read up to its latest reply and opens the next document', async () => {
		await openNotifications();
		buttonNamed('Done')!.click();
		await settle();
		expect(reads).toEqual([{ documents: [{ artifact_id: 'art-plan-pane', through_reply_id: 'plan-pane-reply-3' }] }]);
		expect(rows()).toHaveLength(1);
		expect(openRow()?.querySelector('strong')?.textContent).toBe('tabs.md');
		expect(pane().querySelector('.doc-at')?.textContent).toBe('1 reply to your comment');
	});

	it('keeps a document read elsewhere open where it was, reading All replies read, until another row is opened', async () => {
		await openNotifications();
		notes = notes.slice(1);
		// The open document's own views take over the mock's doc_comment_changed slot; this event reaches only Activity.
		listeners.get('chat_comments_changed')?.();
		await settle();
		expect(openRow()?.querySelector('strong')?.textContent).toBe('plan-pane.md');
		expect(openRow()?.querySelector('.sub')?.textContent).toBe('All replies read');
		expect(pane().querySelector('.doc-at')?.textContent).toBe('All replies read.');
		expect(buttonNamed('Done')).toBeUndefined();
		rows()[1].click();
		await settle();
		expect(rows()).toHaveLength(1);
		expect(openRow()?.querySelector('strong')?.textContent).toBe('tabs.md');
	});

	it('keeps the document open with the reason when Done fails', async () => {
		alsoServe('/api/attention/notifications/read', () => respond(500, {}));
		await openNotifications();
		buttonNamed('Done')!.click();
		await settle();
		expect(pane().querySelector('.doc-bar [role="alert"]')?.textContent).toBe('Couldn’t mark these replies read. Try again.');
		expect(openRow()).toBe(rows()[0]);
		expect(rows()).toHaveLength(2);
	});

	it('switching tabs changes only the list; the open item stays', async () => {
		await openNotifications(false);
		expect(openRow()).toBeNull();
		expect(activityTab()?.artifactId).toBe(served[0].open?.artifact_id);
		tabNamed('tasks').click();
		await settle();
		expect(openRow()).toBe(rows()[0]);
	});

	it('dismisses a row without opening it, through the same call as Done', async () => {
		await openNotifications();
		host.querySelectorAll<HTMLButtonElement>('button.dismiss')[1].click();
		await settle();
		expect(reads).toEqual([{ documents: [{ artifact_id: 'art-tabs', through_reply_id: 'tabs-reply-1' }] }]);
		expect(rows().map((row) => row.querySelector('strong')?.textContent)).toEqual(['plan-pane.md']);
		expect(openRow()).toBe(rows()[0]);
	});

	it('Mark all read clears every listed document in one call, and says so when it fails', async () => {
		await openNotifications();
		alsoServe('/api/attention/notifications/read', () => respond(500, {}));
		buttonNamed('Mark all read')!.click();
		await settle();
		expect(host.querySelector('.panel [role="alert"]')?.textContent).toBe('Couldn’t mark these read. Try again.');
		expect(rows()).toHaveLength(2);
	});

	it('clears a Mark all read failure when opening or dismissing a row', async () => {
		await openNotifications();
		alsoServe('/api/attention/notifications/read', () => respond(500, {}));
		buttonNamed('Mark all read')!.click();
		await settle();
		expect(host.querySelector('.panel [role="alert"]')).not.toBeNull();
		rows()[1].click();
		await settle();
		expect(host.querySelector('.panel [role="alert"]')).toBeNull();
		buttonNamed('Mark all read')!.click();
		await settle();
		expect(host.querySelector('.panel [role="alert"]')).not.toBeNull();
		host.querySelector<HTMLButtonElement>('button.dismiss')!.click();
		await settle();
		expect(host.querySelector('.panel > [role="alert"]')).toBeNull();
	});

	it('moves between tabs with the arrow keys', async () => {
		render();
		await settle();
		tabNamed('tasks').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
		await settle();
		expect(tabNamed('notifications').getAttribute('aria-selected')).toBe('true');
		expect(document.activeElement).toBe(tabNamed('notifications'));
		tabNamed('notifications').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
		await settle();
		expect(tabNamed('tasks').getAttribute('aria-selected')).toBe('true');
	});
});

describe('Activity on a phone', () => {
	it('shows when the open item is based on the last successful read', async () => {
		mobile.store!.set(true);
		let failRead = false;
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (String(input).includes('/api/attention/queue') && failRead) return respond(500, {});
			return base(input, init);
		});
		render();
		await settle();
		rows()[1].click();
		await settle();
		failRead = true;
		listeners.get('plans_changed')!();
		await settle();
		expect(pane().textContent).toContain('Showing last known items.');
		failRead = false;
		listeners.get('plans_changed')!();
		await settle();
		expect(pane().textContent).not.toContain('Showing last known items.');
	});

	it('shows the list, then the item alone with only the way back in the header', async () => {
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
		expect(header.backLabel).toBe('Your tasks');

		header.onBack();
		await settle();
		expect(view.classList.contains('phone-item')).toBe(false);
		expect(get(activityPhoneItem)).toBeNull();
		expect(openRow()).toBe(rows()[1]);
	});

	it('keeps the list’s type and header spacing as on a wide screen', () => {
		// Read from the source: jsdom applies no component styles.
		const phoneRules = listSource.split('\n').filter((line) => line.includes(':global(.activity.phone)'));
		// Titles keep the sidebar rows' token; the phone type scale alone raises it.
		expect(phoneRules.some((line) => line.includes('strong'))).toBe(false);
		// The header line is 44 px on both tabs, so switching tabs doesn't move the rows.
		// Links get their touch area from a pseudo-element; tabs also need a 44 px target.
		const grown = listSource.split('\n').filter((line) => /min-height: var\(--touch-target\)/.test(line));
		expect(grown).toEqual([
			'\t:global(.activity.phone) .tab { min-height: var(--touch-target); align-items: center; }',
			'\t:global(.activity.phone) .by { min-height: var(--touch-target); padding-block: 0; align-items: center; }',
		]);
		expect(listSource).toMatch(/\.link::after \{\n[^}]*height: var\(--touch-target\)/);
	});

	it('counts a chat once and advances after Done', async () => {
		const chat: ActivityQueueItem = { ...activityDocument('chat-source'), key: 'chat-reply:t', external_id: 'chat-reply:t',
			renderer: 'chat', kind: 'chat', title: 'Planning', open: null,
			detail: { thread_id: 't', unread: 4, through: '2026-11-25T12:00:00Z' } };
		notes = [chat, activityDocument('next')];
		alsoServe('/notifications/chats/', () => respond(200, { messages: [] }));
		alsoServe('/notifications/read', () => { notes = notes.filter(item => item.key !== chat.key); return respond(200, {}); });
		render(); await settle();
		tabNamed('notifications').click(); await settle();
		expect(tabNamed('notifications').getAttribute('aria-label')).toBe('Notifications, 2');
		expect(rows()[0].textContent).toContain('Chat · 4 unread replies');
		rows()[0].click(); await settle();
		buttonNamed('Done')!.click(); await settle();
		expect(tabNamed('notifications').getAttribute('aria-label')).toBe('Notifications, 1');
		expect(openRow()?.textContent).toContain('next');
		expect(emit).toHaveBeenCalledWith('activity', 'acted', { kind: 'chat', action: 'done', outcome: 'succeeded' });
	});

	it('marks the latest displayed chat reply when another arrives while open', async () => {
		const chat: ActivityQueueItem = { ...activityDocument('chat-source'), key: 'chat-reply:t', external_id: 'chat-reply:t',
			renderer: 'chat', kind: 'chat', title: 'Planning', open: null,
			detail: { thread_id: 't', unread: 1, through: '2026-11-25T12:00:00Z' } };
		notes = [chat];
		let answer = 'First answer';
		alsoServe('/notifications/chats/', () => respond(200, { messages: [{ id: 'm', seq: 1, content: 'text', created_at: '2026-11-25T12:00:00Z',
			chat_comments: [{ id: 'c', cid: 'c1.1', user_message_id: 'u', from_off: 0, to_off: 0, quote: '', note: 'Question', created_at: '2026-11-25T12:00:00Z',
				answers: [{ author_kind: 'agent', text: answer, created_at: '2026-11-25T12:00:00Z', unread: true }] }] }] }));
		let read: unknown;
		alsoServe('/notifications/read', (init) => { read = JSON.parse(String(init?.body)); notes = []; return respond(200, {}); });
		render(); await settle();
		tabNamed('notifications').click(); await settle();
		rows()[0].click(); await settle();
		expect(pane().textContent).toContain('First answer');
		answer = 'Later answer';
		notes = [{ ...chat, detail: { thread_id: 't', unread: 2, through: '2026-11-25T12:01:00Z' } }];
		listeners.get('chat_comments_changed')?.(); await settle();
		await vi.waitFor(() => expect(pane().textContent).toContain('Later answer'));
		buttonNamed('Done')!.click(); await settle();
		expect(read).toEqual({ documents: [], chats: [{ thread_id: 't', through: '2026-11-25T12:01:00Z' }] });
		expect(rows()).toHaveLength(0);
	});

	it('closing a desktop chat opens the next row, or the row above at the end, without marking it read', async () => {
		const chat: ActivityQueueItem = { ...activityDocument('chat-source'), key: 'chat-reply:t', external_id: 'chat-reply:t',
			renderer: 'chat', kind: 'chat', title: 'Planning', open: null,
			detail: { thread_id: 't', unread: 1, through: '2026-11-25T12:00:00Z' } };
		notes = [chat, activityDocument('next')];
		alsoServe('/notifications/chats/', () => respond(200, { messages: [] }));
		render(); await settle();
		tabNamed('notifications').click(); await settle();
		rows()[0].click(); await settle();
		host.querySelector<HTMLButtonElement>('button[aria-label="Close Planning"]')!.click(); await settle();
		expect(openRow()?.textContent).toContain('next');
		expect(rows()).toHaveLength(2);
		notes = [activityDocument('next'), chat];
		listeners.get('chat_comments_changed')?.(); await settle();
		rows()[1].click(); await settle();
		host.querySelector<HTMLButtonElement>('button[aria-label="Close Planning"]')!.click(); await settle();
		expect(openRow()?.textContent).toContain('next');
		expect(rows()).toHaveLength(2);
	});

	it('closing the only chat leaves the pane empty without claiming the list is empty', async () => {
		const chat: ActivityQueueItem = { ...activityDocument('chat-source'), key: 'chat-reply:t', external_id: 'chat-reply:t',
			renderer: 'chat', kind: 'chat', title: 'Planning', open: null,
			detail: { thread_id: 't', unread: 1, through: '2026-11-25T12:00:00Z' } };
		notes = [chat];
		alsoServe('/notifications/chats/', () => respond(200, { messages: [] }));
		render(); await settle();
		tabNamed('notifications').click(); await settle();
		rows()[0].click(); await settle();
		host.querySelector<HTMLButtonElement>('button[aria-label="Close Planning"]')!.click(); await settle();
		listeners.get('chat_comments_changed')?.(); await settle();
		expect(rows()).toHaveLength(1);
		expect(openRow()).toBeFalsy();
		expect(pane().textContent).not.toContain('No unread replies.');
	});

	it('keeps an opened document’s Done at least 44 px high on a phone, on the bar’s own line', () => {
		expect(documentBarSource).toContain(':global(.activity.phone) .doc-done :global(.btn) { min-height: var(--touch-target); }');
		expect(documentBarSource).toMatch(/<div class="doc-line">\s*<span class="doc-at"[^>]*>\{label\}<\/span>\s*<span class="doc-done">/);
	});
});
