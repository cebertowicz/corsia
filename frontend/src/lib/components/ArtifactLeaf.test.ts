import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toasts } from '$lib/stores/toast';

const mocks = vi.hoisted(() => {
	let comments: Record<string, unknown[]> = {};
	const subscribers = new Set<(value: Record<string, unknown[]>) => void>();
	return {
		fetchContent: vi.fn(),
		fetchRevision: vi.fn(),
		loadComments: vi.fn(),
		prepareCommentArrival: vi.fn(),
		revealCommentArrival: vi.fn(),
		addReply: vi.fn(),
		resolveComment: vi.fn(),
		ackComment: vi.fn(),
		rangeFor: vi.fn(),
		docComments: {
			subscribe(run: (value: Record<string, unknown[]>) => void) {
				subscribers.add(run);
				run(comments);
				return () => subscribers.delete(run);
			},
		},
		sendDocFeedback: vi.fn(),
		leaveDocFeedback: vi.fn(),
		returnDocFeedback: vi.fn(),
		retryDocFeedback: vi.fn(),
		restoreDocumentPlace: vi.fn(),
		setComments(value: Record<string, unknown[]>) {
			comments = value;
			for (const run of subscribers) run(comments);
		},
	};
});

vi.mock('$lib/api/docs', () => ({
	fetchContent: mocks.fetchContent,
	fetchRevision: mocks.fetchRevision,
	writeContent: vi.fn(async () => ({ saved: true, revision: 'r-written' })),
	registerArtifact: vi.fn(),
}));
vi.mock('$lib/api/docFeedback', () => ({
		DocFeedbackError: class extends Error {
			constructor(readonly status: number) { super(`HTTP ${status}`); }
		},
		sendDocFeedback: mocks.sendDocFeedback,
		leaveDocFeedback: mocks.leaveDocFeedback,
		returnDocFeedback: mocks.returnDocFeedback,
		retryDocFeedback: mocks.retryDocFeedback,
	}));

vi.mock('$lib/stores/docCommentArrivals', () => ({
	prepareCommentArrival: mocks.prepareCommentArrival,
	revealCommentArrival: mocks.revealCommentArrival,
	holdCommentArrival: () => {},
	releaseCommentArrival: () => {},
	releaseCommentArrivals: () => {},
}));
vi.mock('$lib/stores/docComments', async () => ({
	docComments: mocks.docComments,
	commentLoads: { subscribe(run: (value: Record<string, unknown>) => void) { run({}); return () => {}; } },
	hasNewReplies: (await import('$lib/stores/docCommentProjection')).hasNewReplies,
	rangeFor: mocks.rangeFor,
	remapComments: () => {},
	loadComments: mocks.loadComments,
	addReply: mocks.addReply,
	resolveComment: mocks.resolveComment,
	ackComment: mocks.ackComment,
	flushPositions: vi.fn(),
}));

vi.mock('$lib/documentImagePlace', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/documentImagePlace')>(),
	restoreDocumentPlace: mocks.restoreDocumentPlace,
}));

import ArtifactLeaf from './ArtifactLeaf.svelte';
import { documentOpenCapability } from '$lib/artifactDocumentOpenCapability';
import { updateDocumentOpenCapability } from '$lib/ws/presence';
import { threadWorkspaces, openArtifactTab, openLegacyDiscussionArchiveTab, openSubagentConversationTab, setActiveTab, splitWidth } from '$lib/stores/artifactWorkspace';
import { clearDraft, docSaveDrafts, seedDraft, setDraftText } from '$lib/stores/docSaveQueue';
import { token } from '$lib/stores/auth';
import { paneOpen } from '$lib/stores/paneOpen';
import { recordDocActivity, resetDocPresenceForTests } from '$lib/docPresence';
import { startDocPresence } from '$lib/docPresenceRuntime';
import { emit } from '$lib/ws/events';
import { setWs } from '$lib/ws/state';
import { connectionState } from '$lib/stores/connection';
import { LIVE_OPEN_WAIT_MS, startLiveDocuments } from '$lib/stores/liveDocuments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { recordTabDocumentPlace, tabDocumentImage, tabViewState } from '$lib/stores/tabViewState';
import { EditorView } from '@codemirror/view';

let host: HTMLElement;
let realRO: typeof ResizeObserver;
let component: Record<string, unknown> | null = null;
let stopLive: (() => void) | null = null;
const liveOpens: string[] = [];
// The server answers doc_open for a document that is not live with live:false;
// these legacy-path tests run against that answer over a connected socket.
const socket = {
	readyState: 1,
	send(text: string) {
		const frame = JSON.parse(text);
		if (frame.type !== 'doc_open') return;
		liveOpens.push(frame.path);
		void Promise.resolve().then(() => emit('doc_opened', { type: 'doc_opened', live: false, request_id: frame.request_id }));
	},
} as unknown as WebSocket;
const THREAD = 'thread-leaf';

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function tabsOf() {
	return get(threadWorkspaces)[THREAD]?.tabs ?? [];
}

function requestVisibleReload(revision = 'r-user') {
	threadWorkspaces.update((all) => ({
		...all,
		[THREAD]: {
			...all[THREAD],
			tabs: all[THREAD].tabs.map((tab) => ({ ...tab, latestRevision: revision })),
		},
	}));
	flushSync();
	host.querySelector<HTMLButtonElement>('.stale-control')!.click();
	flushSync();
}

/** ArtifactLeaf renders without ChatArtifactSplit, which publishes what the canvas shows; do the same for a split layout. */
let stopShowing: (() => void) | null = null;
function publishShowing() {
	stopShowing?.();
	stopShowing = threadWorkspaces.subscribe((all) => {
		const workspace = all[THREAD];
		if (workspace) updateDocumentOpenCapability(THREAD, documentOpenCapability(workspace, { mode: 'split', artifactWidth: 500 }, false));
	});
}

function render(mode: 'expanded' | 'compact' = 'expanded') {
	publishShowing();
	component = mount(ArtifactLeaf, {
		target: host,
		props: { threadId: THREAD, mode },
	});
	flushSync();
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	} as unknown as typeof ResizeObserver;
	localStorage.clear();
	threadWorkspaces.set({});
	splitWidth.set(null);
	token.set('test-bearer');
	mocks.fetchContent.mockReset();
	mocks.fetchRevision.mockReset().mockResolvedValue({
		exists: true,
		path: null,
		revision: 'r-current',
		writable: true,
		artifact_id: null,
	});
	mocks.loadComments.mockReset().mockResolvedValue({
		open_count: 0,
		owner_label: null,
		grace_closes_at: null,
		undelivered_batches: [],
	});
	mocks.prepareCommentArrival.mockReset();
	mocks.revealCommentArrival.mockReset();
	mocks.addReply.mockReset();
	mocks.resolveComment.mockReset();
	mocks.ackComment.mockReset();
	mocks.rangeFor.mockReset().mockReturnValue(null);
	mocks.sendDocFeedback.mockReset().mockResolvedValue({ woken: [], undelivered_batches: [] });
	mocks.leaveDocFeedback.mockReset().mockResolvedValue({ woken: [], undelivered_batches: [] });
	mocks.returnDocFeedback.mockReset().mockResolvedValue({ woken: [], undelivered_batches: [] });
	mocks.retryDocFeedback.mockReset().mockResolvedValue({ woken: [], undelivered_batches: [] });
	mocks.restoreDocumentPlace.mockReset().mockResolvedValue(undefined);
	mocks.setComments({});
	resetDocPresenceForTests();
	liveOpens.length = 0;
	setWs(socket);
	connectionState.set('connected');
	stopLive = startLiveDocuments();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	stopShowing?.(); stopShowing = null;
	if (component) unmount(component);
	component = null;
	stopLive?.(); stopLive = null;
	setWs(null);
	connectionState.set('disconnected');
	globalThis.ResizeObserver = realRO;
	host.remove();
	vi.useRealTimers();
	vi.restoreAllMocks();
	resetDocPresenceForTests();
});

describe('ArtifactLeaf subagent conversation presentation', () => {
	it('renders the child transcript in the existing leaf without changing the parent thread workspace', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => ({
				messages: [
					{
						id: 'child-message',
						thread_id: 'child-thread',
						role: 'assistant',
						content: 'Working inside the child',
						created_at: '2026-08-13T00:00:00Z',
						metadata: {},
					},
				],
			}),
		});
		vi.stubGlobal('fetch', fetchMock);
		openSubagentConversationTab(THREAD, {
			threadId: 'child-thread',
			title: 'Delegated work',
			sourceNodeId: 'node-1',
		});

		render();

		await vi.waitFor(() => expect(host.textContent).toContain('Working inside the child'));
		expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/threads/child-thread/messages?'));
		expect(get(threadWorkspaces)[THREAD].activeSurface).toBe('artifact');
		expect(get(threadWorkspaces)[THREAD].tabs[0]).toMatchObject({
			kind: 'subagent',
			threadId: 'child-thread',
		});
	});
});

describe('ArtifactLeaf legacy discussion compatibility', () => {
	it('renders an existing discussion read-only in the artifact leaf', async () => {
		mocks.setComments({
			'discussion-1': [
				{
					id: 'comment-1',
					anchor: '',
					author: 'agent-1',
					authorKind: 'agent',
					text: 'Preserved plan discussion',
					at: 'just now',
					resolved: false,
					acked: false,
					replies: [],
				},
			],
		});
		openLegacyDiscussionArchiveTab(THREAD, {
			artifactId: 'discussion-1',
			title: 'Plan node discussion',
		});

		render();

		await vi.waitFor(() => {
			expect(host.textContent).toContain('Legacy discussion archive');
			expect(host.textContent).toContain('Preserved plan discussion');
		});
		expect(mocks.loadComments).toHaveBeenCalledWith('discussion-1');
		expect(host.querySelector('.ct-compose')).toBeNull();
		expect(Array.from(host.querySelectorAll<HTMLButtonElement>('.ct-act')).every((button) => button.disabled)).toBe(true);
		expect(get(threadWorkspaces)[THREAD].tabs[0]).toMatchObject({
			kind: 'legacy-discussion',
			artifactId: 'discussion-1',
		});
	});
});

describe('ArtifactLeaf in-canvas comments', () => {
	it('shows an orphan marker and opens its list inside the canvas without changing the right pane', async () => {
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 });
		// jsdom lays nothing out, so give the document column the width a wide canvas has: wide enough for the inline drawer.
		const measure = HTMLElement.prototype.getBoundingClientRect;
		const wide = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
			const rect = measure.call(this);
			return this.classList.contains('doc') ? { ...rect, width: 1000, right: rect.left + 1000 } as DOMRect : rect;
		});
		try {
			mocks.fetchContent.mockResolvedValue({ text: 'Changed document', revision: 'r-1', writable: true, artifact: { id: 'art-comments' } });
			mocks.setComments({ 'art-comments': [{ id: 'orphan', from: 0, to: 5, anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] }] });
			openArtifactTab(THREAD, { path: '/comments.md', title: 'comments.md' });
			render();
			await vi.waitFor(() => expect(host.querySelector('.comment-status')?.getAttribute('title')).toBe("1 comment couldn't be placed"));
			paneOpen.set(true);
			host.querySelector<HTMLButtonElement>('.comment-status')!.click();
			flushSync();
			expect(host.querySelector('.doc [data-doc-comments-drawer]')).not.toBeNull();
			expect(document.body.querySelector('.popover')).toBeNull();
			expect(host.querySelector('[data-adaptive-drawer]')).toBeNull();
			expect(get(paneOpen)).toBe(true);
		} finally {
			wide.mockRestore();
		}
	});

	it('omits the amber marker when every thread remains anchored', async () => {
		mocks.rangeFor.mockReturnValue({ from: 0, to: 8 });
		mocks.fetchContent.mockResolvedValue({ text: 'Document', revision: 'r-1', writable: true, artifact: { id: 'art-anchored' } });
		mocks.setComments({ 'art-anchored': [{ id: 'comment', from: 0, to: 8, anchor: 'Document', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] }] });
		openArtifactTab(THREAD, { path: '/anchored.md', title: 'anchored.md' });
		render();
		await vi.waitFor(() => expect(host.querySelector('.comment-status')).not.toBeNull());
		expect(host.querySelector('.orphan-status')).toBeNull();
	});

	it('opens the comment list as a sheet contained by the document column on a narrow viewport without changing the right pane', async () => {
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 });
		mocks.fetchContent.mockResolvedValue({ text: 'Document', revision: 'r-1', writable: true, artifact: { id: 'art-narrow' } });
		mocks.setComments({ 'art-narrow': [{ id: 'comment', from: 0, to: 1, anchor: 'D', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] }] });
		openArtifactTab(THREAD, { path: '/narrow.md', title: 'narrow.md' });
		render('compact');
		// The pill sits in the header from the first frame and stays disabled until the comments load.
		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('.comment-status')?.disabled).toBe(false));
		paneOpen.set(false);
		host.querySelector<HTMLButtonElement>('.comment-status')!.click();
		flushSync();
		expect(document.querySelector('[data-adaptive-drawer].within [data-doc-comments-drawer]')).not.toBeNull();
		expect(document.body.querySelector('.popover')).toBeNull();
		expect(get(paneOpen)).toBe(false);
	});

	it('reloads text and comments together before revealing an agent edit', async () => {
		mocks.fetchContent
			.mockResolvedValueOnce({ text: '# saved', revision: 'r-1', writable: true, artifact: { id: 'art-clean' } })
			.mockResolvedValueOnce({ text: '# remote edit', revision: 'r-2', writable: true, artifact: { id: 'art-clean' } });
		openArtifactTab(THREAD, { artifactId: 'art-clean', path: '/clean.md', title: 'clean.md' });
		const stopPresence = startDocPresence();
		try {
			render();
			const tab = tabsOf()[0];
			await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# saved'));
			await vi.waitFor(() => expect(mocks.loadComments).toHaveBeenCalledWith('art-clean'));
			mocks.loadComments.mockClear();
			emit('doc_activity', { type: 'doc_activity', artifact_id: 'art-clean', thread_id: 'agent-a', agent: 'Reviewer', kind: 'edit', span: { from: 2, to: 3 }, comment_id: null, revision: 'r-2', at: new Date().toISOString() });
			await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# remote edit'), { timeout: 1_500 });
			expect(mocks.fetchContent).toHaveBeenCalledTimes(2);
			expect(mocks.loadComments).toHaveBeenCalledWith('art-clean');
		} finally {
			stopPresence();
		}
	});

	it.each(['presence-first', 'user-first'] as const)(
		'keeps the newer user reload when concurrent presence content resolves %s',
		async (first) => {
			const presence = deferred<Record<string, unknown>>();
			const user = deferred<Record<string, unknown>>();
			mocks.fetchContent
				.mockResolvedValueOnce({ text: '# saved', revision: 'r-1', writable: true, artifact: { id: 'art-race' } })
				.mockImplementationOnce(() => presence.promise)
				.mockImplementationOnce(() => user.promise);
			openArtifactTab(THREAD, { artifactId: 'art-race', path: '/race.md', title: 'race.md' });
			const stopPresence = startDocPresence();
			try {
				render();
				const tab = tabsOf()[0];
				await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# saved'));
				emit('doc_activity', { type: 'doc_activity', artifact_id: 'art-race', thread_id: 'agent-a', agent: 'Reviewer', kind: 'edit', span: { from: 0, to: 0 }, comment_id: null, revision: 'r-presence', at: new Date().toISOString() });
				await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledTimes(2));
				requestVisibleReload();
				await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledTimes(3));

				const presenceResponse = { text: '# presence', revision: 'r-presence', writable: true, artifact: { id: 'art-race' } };
				const userResponse = { text: '# user', revision: 'r-user', writable: true, artifact: { id: 'art-race' } };
				if (first === 'presence-first') {
					presence.resolve(presenceResponse);
					await Promise.resolve();
					user.resolve(userResponse);
				} else {
					user.resolve(userResponse);
					await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# user'));
					presence.resolve(presenceResponse);
				}

				await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# user'));
				await new Promise((resolve) => setTimeout(resolve, 450));
				expect(get(docSaveDrafts)[tab.instanceId]).toMatchObject({ editText: '# user', savedText: '# user' });
			} finally {
				stopPresence();
			}
		},
	);

	it('keeps an in-flight user reload when a presence reload fails before its reveal', async () => {
		const user = deferred<Record<string, unknown>>();
		mocks.fetchContent
			.mockResolvedValueOnce({ text: '# saved', revision: 'r-1', writable: true, artifact: { id: 'art-failed' } })
			.mockImplementationOnce(() => user.promise)
			.mockRejectedValueOnce(new Error('offline'));
		openArtifactTab(THREAD, { artifactId: 'art-failed', path: '/failed.md', title: 'failed.md' });
		const stopPresence = startDocPresence();
		try {
			render();
			const tab = tabsOf()[0];
			await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# saved'));
			requestVisibleReload();
			await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledTimes(2));
			emit('doc_activity', { type: 'doc_activity', artifact_id: 'art-failed', thread_id: 'agent-a', agent: 'Reviewer', kind: 'edit', span: { from: 0, to: 0 }, comment_id: null, revision: 'r-presence', at: new Date().toISOString() });
			await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledTimes(3));
			user.resolve({ text: '# user', revision: 'r-user', writable: true, artifact: { id: 'art-failed' } });
			await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]).toMatchObject({ editText: '# user', savedText: '# user' }));
		} finally {
			stopPresence();
		}
	});

	it('does not reveal prepared presence content after the local draft becomes dirty', async () => {
		mocks.fetchContent
			.mockResolvedValueOnce({ text: '# saved', revision: 'r-1', writable: true, artifact: { id: 'art-dirty-race' } })
			.mockResolvedValueOnce({ text: '# remote edit', revision: 'r-2', writable: true, artifact: { id: 'art-dirty-race' } });
		openArtifactTab(THREAD, { artifactId: 'art-dirty-race', path: '/dirty-race.md', title: 'dirty-race.md' });
		const stopPresence = startDocPresence();
		try {
			render();
			const tab = tabsOf()[0];
			const target = { instanceId: tab.instanceId, artifactId: 'art-dirty-race', path: tab.path, name: tab.title, projectId: null, threadId: THREAD };
			await vi.waitFor(() => expect(get(docSaveDrafts)[tab.instanceId]?.editText).toBe('# saved'));
			emit('doc_activity', { type: 'doc_activity', artifact_id: 'art-dirty-race', thread_id: 'agent-a', agent: 'Reviewer', kind: 'edit', span: { from: 0, to: 1 }, comment_id: null, revision: 'r-2', at: new Date().toISOString() });
			await vi.waitFor(() => expect(host.querySelector('.participant-trigger.arrival')).not.toBeNull());
			setDraftText(target, '# local draft');
			await vi.waitFor(() => expect(host.querySelector('.participant-trigger.arrival')).toBeNull(), { timeout: 1_500 });
			expect(get(docSaveDrafts)[tab.instanceId]).toMatchObject({ editText: '# local draft', savedText: '# saved' });
		} finally {
			stopPresence();
		}
	});

	it('keeps a dirty local draft when an agent edit arrives', async () => {
		mocks.fetchRevision.mockResolvedValue({ exists: true, path: null, revision: 'r-2', writable: false, artifact_id: 'art-dirty' });
		openArtifactTab(THREAD, { artifactId: 'art-dirty', path: '/dirty.md', title: 'dirty.md' });
		const tab = tabsOf()[0];
		const target = { instanceId: tab.instanceId, artifactId: 'art-dirty', path: tab.path, name: tab.title, projectId: null, threadId: THREAD };
		seedDraft(target, '# saved', 'r-1');
		setDraftText(target, '# local draft');
		const stopPresence = startDocPresence();
		try {
			render();
			await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toContain('local draft'));
			mocks.fetchContent.mockClear();
			emit('doc_activity', { type: 'doc_activity', artifact_id: 'art-dirty', thread_id: 'agent-a', agent: 'Reviewer', kind: 'edit', span: { from: 0, to: 8 }, comment_id: null, revision: 'r-2', at: new Date().toISOString() });
			await vi.waitFor(() => expect(host.querySelector('.participant-trigger.arrival')).not.toBeNull());
			expect(mocks.fetchContent).not.toHaveBeenCalled();
			expect(get(docSaveDrafts)[tab.instanceId].editText).toBe('# local draft');
		} finally {
			stopPresence();
		}
	});

	it('shows only the other participants and keeps same-title agent avatars and cursors separate', async () => {
		mocks.fetchContent.mockResolvedValue({ text: 'alpha beta gamma', revision: 'r-1', writable: true, artifact: { id: 'art-presence' } });
		const at = new Date().toISOString();
		recordDocActivity({ type: 'doc_activity', artifact_id: 'art-presence', thread_id: 'agent-a', agent: 'Reviewer', kind: 'read', span: { from: 0, to: 5 }, comment_id: null, revision: 'r-1', at });
		recordDocActivity({ type: 'doc_activity', artifact_id: 'art-presence', thread_id: 'agent-b', agent: 'Reviewer', kind: 'edit', span: { from: 6, to: 10 }, comment_id: null, revision: 'r-1', at });
		openArtifactTab(THREAD, { path: '/presence.md', title: 'presence.md' });
		render();
		await vi.waitFor(() => expect(host.querySelector('.presence-row')).not.toBeNull());
		// The viewer alone says nothing the viewer does not know, so no "You" avatar.
		expect([...host.querySelectorAll('.presence-row .avatar')].map((avatar) => avatar.getAttribute('aria-label'))).not.toContain('You');
		expect([...host.querySelectorAll<HTMLElement>('.participant-trigger')].map((row) => row.dataset.participantId)).toEqual(['agent-a', 'agent-b']);
		expect([...host.querySelectorAll<HTMLElement>('.cm-presence-cursor')].map((row) => row.dataset.threadId)).toEqual(['agent-a', 'agent-b']);
		expect(host.querySelector('.presence-row')?.textContent?.toLowerCase()).not.toContain('now');
	});
});

describe('ArtifactLeaf load-status ownership', () => {
	it('cancels an interrupted tab-return restore before it can change the next tab', async () => {
		let continueRestore: (() => void) | undefined;
		const firstLoad = deferred<{ text: string; revision: string; writable: boolean; artifact: { id: string } }>();
		const returningFirstLoad = deferred<{ text: string; revision: string; writable: boolean; artifact: { id: string } }>();
		mocks.fetchContent.mockImplementation((path: string) => {
			if (path === '/first.md' && mocks.fetchContent.mock.calls.filter(([requested]) => requested === path).length > 1) return returningFirstLoad.promise;
			if (path === '/first.md') return firstLoad.promise;
			return Promise.resolve({ text: '# Second', revision: 'r-2', writable: true, artifact: { id: 'second' } });
		});
		mocks.restoreDocumentPlace.mockImplementation((root: HTMLElement | undefined, place: { scrollTop: number }, options: { signal?: AbortSignal } = {}) => new Promise<void>((resolve) => {
			continueRestore = () => {
				if (!options.signal?.aborted) {
					const scroller = root?.querySelector<HTMLElement>('.cm-scroller');
					if (scroller) {
						scroller.scrollTop = place.scrollTop;
						scroller.dispatchEvent(new Event('scroll'));
					}
				}
				resolve();
			};
		}));

		openArtifactTab(THREAD, { path: '/first.md', title: 'first.md' });
		const first = get(threadWorkspaces)[THREAD].activeTabId!;
		openArtifactTab(THREAD, { path: '/second.md', title: 'second.md' });
		const second = get(threadWorkspaces)[THREAD].activeTabId!;
		setActiveTab(THREAD, first);
		recordTabDocumentPlace(THREAD, first, { from: 0, top: 0, scrollTop: 420 });
		render();
		firstLoad.resolve({ text: '# First', revision: 'r-1', writable: true, artifact: { id: 'first' } });
		await vi.waitFor(() => expect(tabsOf().find((tab) => tab.instanceId === first)?.status).toBe('ready'));
		await vi.waitFor(() => expect(mocks.restoreDocumentPlace).toHaveBeenCalledOnce());
		// Its first reveal was not on screen yet, so it stays covered until it is in place.
		expect(host.querySelector('.doc-host')?.classList.contains('covered')).toBe(true);

		setActiveTab(THREAD, second);
		flushSync();
		await vi.waitFor(() => expect(tabsOf().find((tab) => tab.instanceId === second)?.status).toBe('ready'));
		const secondScroller = host.querySelector<HTMLElement>('.cm-scroller')!;
		secondScroller.scrollTop = 31;
		recordTabDocumentPlace(THREAD, second, { from: 0, top: 0, scrollTop: 31 });
		continueRestore!();
		await Promise.resolve();

		expect(secondScroller.scrollTop).toBe(31);
		expect(tabViewState(THREAD, second)?.documentPlace).toEqual({ from: 0, top: 0, scrollTop: 31 });
		expect(tabViewState(THREAD, first)?.documentPlace).toEqual({ from: 0, top: 0, scrollTop: 420 });

		setActiveTab(THREAD, first);
		flushSync();
		expect(host.querySelector('.doc-host')?.classList.contains('covered')).toBe(false);
		await vi.waitFor(() => expect(mocks.restoreDocumentPlace).toHaveBeenCalledTimes(2));
		// The return paints its cached text at once, so neither its place nor the confirmed load's covers it.
		expect(host.querySelector('.doc-host')?.classList.contains('covered')).toBe(false);
		returningFirstLoad.resolve({ text: '# First', revision: 'r-1', writable: true, artifact: { id: 'first' } });
		await vi.waitFor(() => expect(mocks.restoreDocumentPlace).toHaveBeenCalledTimes(3));
		expect(host.querySelector('.doc-host')?.classList.contains('covered')).toBe(false);
	});

	it('keeps a tab restored before the socket connects loading, then opens it once connected', async () => {
		connectionState.set('connecting');
		mocks.fetchContent.mockResolvedValue({ text: '# restored', revision: 'r-1', writable: true, artifact: { id: 'art-restored' } });
		openArtifactTab(THREAD, { path: '/restored.md', title: 'restored.md' });
		render();
		await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledOnce());
		await new Promise(resolve => setTimeout(resolve, 20));
		expect(tabsOf()[0].status).toBe('loading');
		expect(liveOpens).toEqual([]);
		connectionState.set('connected');
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('ready'));
		expect(liveOpens).toEqual(['/restored.md']);
		expect(host.textContent).not.toContain('Live editing is not connected.');
	});

	it('shows the typed not-connected state with Retry once the open wait passes, and Retry reloads over HTTP', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		connectionState.set('connecting');
		mocks.fetchContent.mockResolvedValue({ text: '# kept', revision: 'r-1', writable: true, artifact: { id: 'art-wait' } });
		openArtifactTab(THREAD, { path: '/wait.md', title: 'wait.md' });
		render();
		await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledOnce());
		vi.advanceTimersByTime(LIVE_OPEN_WAIT_MS);
		await vi.waitFor(() => expect(host.textContent).toContain('Live editing is not connected.'));
		expect(host.querySelector('.cm-content')?.textContent).toContain('kept');
		const retry = [...host.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Retry')!;
		connectionState.set('connected');
		retry.click(); flushSync();
		await vi.waitFor(() => expect(mocks.fetchContent).toHaveBeenCalledTimes(2));
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('ready'));
		expect(host.textContent).not.toContain('Live editing is not connected.');
	});

	it('never lets a slow load stamp the tab that became active while it was in flight', async () => {
		vi.useFakeTimers();
		const slow = deferred<Record<string, unknown>>();
		const fast = deferred<Record<string, unknown>>();
		mocks.fetchContent.mockImplementationOnce(() => slow.promise).mockImplementationOnce(() => fast.promise);

		openArtifactTab(THREAD, { path: '/slow.md', title: 'slow.md' });
		render();
		const slowId = tabsOf()[0].instanceId;

		// The user opens and switches to a second document mid-flight.
		openArtifactTab(THREAD, { path: '/fast.md', title: 'fast.md' });
		flushSync();
		const fastId = tabsOf().find((t) => t.path === '/fast.md')!.instanceId;
		expect(get(threadWorkspaces)[THREAD].activeTabId).toBe(fastId);

		fast.resolve({
			text: '# fast',
			revision: 'r-fast',
			writable: true,
			artifact: { id: 'art-fast' },
		});
		await vi.waitFor(() => expect(tabsOf().find((t) => t.instanceId === fastId)!.status).toBe('ready'));

		// The stale load finally settles. Only one viewer is mounted, so its own
		// sequence guard discards the result outright — but the guarantee under
		// test is that none of it can reach the tab that is now active.
		slow.resolve({
			text: '# slow',
			revision: 'r-slow',
			writable: true,
			artifact: { id: 'art-slow' },
		});
		await Promise.resolve();

		const fastTab = tabsOf().find((t) => t.instanceId === fastId)!;
		expect(fastTab.loadedRevision).toBe('r-fast');
		expect(fastTab.artifactId).toBe('art-fast');
		expect(fastTab.status).toBe('ready');

		const slowTab = tabsOf().find((t) => t.instanceId === slowId)!;
		expect(slowTab.loadedRevision).not.toBe('r-fast');
		expect(slowTab.artifactId).not.toBe('art-fast');
	});

	it('marks a tab readonly when the server refuses writes to its path', async () => {
		mocks.fetchContent.mockResolvedValue({
			text: '# system',
			revision: 'r1',
			writable: false,
			artifact: null,
		});
		openArtifactTab(THREAD, { path: '/context/SYSTEM.md', title: 'SYSTEM.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('readonly'));
		expect(host.textContent).toContain('Read-only');
	});

	it('resets readonly state when switching to an existing dirty writable draft', async () => {
		vi.useFakeTimers();
		const savedAtDraftLoad: string[] = [];
		mocks.fetchContent.mockImplementation(async (path: string) => {
			if (path !== '/draft.md') return { text: '# system', revision: 'r1', writable: false, artifact: null };
			savedAtDraftLoad.push(Object.values(get(docSaveDrafts)).find(draft => draft.target.path === '/draft.md')!.savedText);
			return { text: '# local draft', revision: 'r2', writable: true, artifact: null };
		});
		openArtifactTab(THREAD, { path: '/context/SYSTEM.md', title: 'SYSTEM.md' });
		openArtifactTab(THREAD, { path: '/draft.md', title: 'draft.md' });
		const [readonlyTab, draftTab] = tabsOf();
		setActiveTab(THREAD, readonlyTab.instanceId);
		const draftTarget = {
			instanceId: draftTab.instanceId,
			path: draftTab.path,
			name: draftTab.title,
			projectId: null,
			threadId: THREAD,
			artifactId: draftTab.artifactId,
		};
		seedDraft(draftTarget, '# saved draft');
		setDraftText(draftTarget, '# local draft');
		render();
		await vi.waitFor(() => expect(host.querySelector('.readonly-note')).not.toBeNull());

		setActiveTab(THREAD, draftTab.instanceId);
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.readonly-note')).toBeNull());
		// The legacy draft saves over HTTP before the live-admission load (approved legacy-draft-first order).
		expect(savedAtDraftLoad).toEqual(['# local draft']);
		expect(mocks.fetchContent).toHaveBeenLastCalledWith('/draft.md', expect.objectContaining({ liveOpen: true }));
		expect(get(docSaveDrafts)[draftTab.instanceId]).toMatchObject({
			editText: '# local draft',
			savedText: '# local draft',
		});
		expect(mocks.fetchRevision).toHaveBeenCalledWith('/draft.md', null, null);
		clearDraft(draftTarget);
	});

	it('keeps a restored dirty draft read-only when current write permission was revoked', async () => {
		mocks.fetchRevision.mockResolvedValue({
			exists: true,
			path: '/draft.md',
			revision: 'r-current',
			writable: false,
			artifact_id: 'art-draft',
		});
		openArtifactTab(THREAD, { path: '/draft.md', title: 'draft.md' });
		const tab = tabsOf()[0];
		const draftTarget = {
			instanceId: tab.instanceId,
			path: tab.path,
			name: tab.title,
			projectId: null,
			threadId: THREAD,
			artifactId: tab.artifactId,
		};
		seedDraft(draftTarget, '# saved draft', 'r-saved');
		setDraftText(draftTarget, '# local draft');

		render();

		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('readonly'));
		expect(host.textContent).toContain('Read-only');
		expect(mocks.fetchContent).not.toHaveBeenCalled();
		expect(get(docSaveDrafts)[tab.instanceId]).toMatchObject({
			editText: '# local draft',
			savedText: '# saved draft',
			status: 'dirty',
			target: { artifactId: 'art-draft' },
		});
		clearDraft(draftTarget);
	});

	it('keeps a dirty draft locally editable when the permission probe fails', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		mocks.fetchRevision.mockRejectedValue(new Error('503 revision service unavailable'));
		openArtifactTab(THREAD, { path: '/draft.md', title: 'draft.md' });
		const tab = tabsOf()[0];
		const draftTarget = {
			instanceId: tab.instanceId,
			path: tab.path,
			name: tab.title,
			projectId: null,
			threadId: THREAD,
			artifactId: tab.artifactId,
		};
		seedDraft(draftTarget, '# saved draft', 'r-saved');
		setDraftText(draftTarget, '# local draft');

		render();

		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('error'));
		expect(host.querySelector('.readonly-note')).toBeNull();
		expect(host.querySelector('[contenteditable="true"]')).not.toBeNull();
		expect(get(docSaveDrafts)[tab.instanceId]).toMatchObject({
			editText: '# local draft',
			savedText: '# saved draft',
			status: 'dirty',
		});
		clearDraft(draftTarget);
	});

	it('shows a 404 as the explicit unavailable state, not a raw load error', async () => {
		mocks.fetchContent.mockRejectedValue(new Error('404 No such file'));
		openArtifactTab(THREAD, { path: '/gone.md', title: 'gone.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('unavailable'));
		flushSync();
		expect(host.textContent).toContain('no longer available');
		expect(host.querySelector('.gone button')?.textContent).toContain('Close tab');
	});

	it('keeps a genuine load failure as an error rather than claiming the doc is gone', async () => {
		mocks.fetchContent.mockRejectedValue(new Error('500 upstream exploded'));
		openArtifactTab(THREAD, { path: '/broken.md', title: 'broken.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('error'));
	});

	it('shows the stale banner only once a newer revision is known', async () => {
		mocks.fetchContent.mockResolvedValue({
			text: '# doc',
			revision: 'r1',
			writable: true,
			artifact: { id: 'art-1' },
		});
		openArtifactTab(THREAD, { path: '/doc.md', title: 'doc.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('ready'));
		flushSync();
		expect(host.textContent).not.toContain('File changed');

		threadWorkspaces.update((all) => ({
			...all,
			[THREAD]: {
				...all[THREAD],
				tabs: all[THREAD].tabs.map((t) => ({ ...t, latestRevision: 'r2' })),
			},
		}));
		flushSync();
		expect(host.textContent).toContain('File changed');
	});

	it('sends the document batch exactly once before an explicit tab close', async () => {
		mocks.fetchContent.mockResolvedValue({
			text: '# doc', revision: 'r1', writable: true, artifact: { id: 'art-close' },
		});
		openArtifactTab(THREAD, { artifactId: 'art-close', path: '/close.md', title: 'close.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('ready'));

		host.getElementsByClassName('tab-close')[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
		await vi.waitFor(() => expect(mocks.sendDocFeedback).toHaveBeenCalledTimes(1));
		expect(mocks.sendDocFeedback).toHaveBeenCalledWith('art-close');
		await vi.waitFor(() => expect(tabsOf()).toHaveLength(0));
	});

	it('still closes the tab when the close-time send fails, and says how to recover', async () => {
		const show = vi.spyOn(toasts, 'show');
		mocks.fetchContent.mockResolvedValue({
			text: '# doc', revision: 'r1', writable: true, artifact: { id: 'art-offline' },
		});
		mocks.sendDocFeedback.mockRejectedValue(new TypeError('Failed to fetch'));
		openArtifactTab(THREAD, { artifactId: 'art-offline', path: '/offline.md', title: 'offline.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('ready'));

		host.getElementsByClassName('tab-close')[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
		await vi.waitFor(() => expect(tabsOf()).toHaveLength(0));
		expect(show).toHaveBeenCalledWith("Couldn't send your comments. Reopen the document to send them.", 'error');
		show.mockRestore();
	});

	it('closes a tab whose artifact is gone without a send error', async () => {
		const show = vi.spyOn(toasts, 'show');
		const { DocFeedbackError } = await import('$lib/api/docFeedback');
		mocks.fetchContent.mockResolvedValue({
			text: '# doc', revision: 'r1', writable: true, artifact: { id: 'art-gone' },
		});
		mocks.sendDocFeedback.mockRejectedValue(new DocFeedbackError(404));
		openArtifactTab(THREAD, { artifactId: 'art-gone', path: '/gone-row.md', title: 'gone-row.md' });
		render();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('ready'));

		host.getElementsByClassName('tab-close')[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
		await vi.waitFor(() => expect(tabsOf()).toHaveLength(0));
		expect(show).not.toHaveBeenCalledWith(expect.stringContaining("Couldn't send"), 'error');
		show.mockRestore();
	});

	it('uses leave, not send, when switching away from or navigating off a document', async () => {
		mocks.fetchContent.mockResolvedValue({
			text: '# doc', revision: 'r1', writable: true, artifact: { id: 'art-leave' },
		});
		mocks.loadComments.mockResolvedValue({
			open_count: 1, owner_label: 'Documents & Artifacts', grace_closes_at: null, undelivered_batches: [],
		});
		openArtifactTab(THREAD, { artifactId: 'art-leave', path: '/leave.md', title: 'leave.md' });
		openArtifactTab(THREAD, { artifactId: 'art-other', path: '/other.md', title: 'other.md' });
		const leaveTab = tabsOf()[0];
		setActiveTab(THREAD, leaveTab.instanceId);
		render();
		await vi.waitFor(() => expect(tabsOf().find((tab) => tab.instanceId === leaveTab.instanceId)?.status).toBe('ready'));
		mocks.leaveDocFeedback.mockClear();
		setActiveTab(THREAD, tabsOf()[1].instanceId);
		await vi.waitFor(() => expect(mocks.leaveDocFeedback).toHaveBeenCalledWith('art-leave'));
		expect(mocks.sendDocFeedback).not.toHaveBeenCalled();

		mocks.leaveDocFeedback.mockClear();
		unmount(component as never);
		component = null;
		await vi.waitFor(() => expect(mocks.leaveDocFeedback).toHaveBeenCalled());
		expect(mocks.sendDocFeedback).not.toHaveBeenCalled();
	});

	it('closes an unavailable tab without needing it to load first', async () => {
		mocks.fetchContent.mockRejectedValue(new Error('404 No such file'));
		openArtifactTab(THREAD, { path: '/gone.md', title: 'gone.md' });
		openArtifactTab(THREAD, { path: '/kept.md', title: 'kept.md' });
		render();
		const goneId = tabsOf()[0].instanceId;
		setActiveTab(THREAD, goneId);
		flushSync();
		await vi.waitFor(() => expect(tabsOf()[0].status).toBe('unavailable'));
		flushSync();

		host.querySelector<HTMLButtonElement>('.gone button')!.click();
		await vi.waitFor(() => expect(tabsOf()).toHaveLength(1));
		expect(tabsOf()[0].path).toBe('/kept.md');
	});
});

// Opening a thread from inside the text must leave the user's caret and focus where they are.
describe('ArtifactLeaf comment focus from the text', () => {
	const QUOTE = { from: 4, to: 9 };
	// jsdom has no text layout; give the editor one line box so it can measure the caret.
	const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	beforeEach(() => {
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 });
		rangeLayout.getClientRects = () => [new DOMRect(10, 20, 8, 16)] as unknown as DOMRectList;
		rangeLayout.getBoundingClientRect = () => new DOMRect(10, 20, 8, 16);
		Element.prototype.scrollIntoView = () => {};
	});
	afterEach(() => {
		delete rangeLayout.getClientRects;
		delete rangeLayout.getBoundingClientRect;
		delete (Element.prototype as Partial<Element>).scrollIntoView;
	});

	async function openCommented(artifactId: string) {
		focusedDocComment.set(null);
		mocks.rangeFor.mockReturnValue(QUOTE);
		mocks.fetchContent.mockResolvedValue({ text: 'The quick brown fox.', revision: 'r-1', writable: true, artifact: { id: artifactId } });
		mocks.setComments({ [artifactId]: [{ id: 'comment', ...QUOTE, anchor: 'quick', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] }] });
		openArtifactTab(THREAD, { path: `/${artifactId}.md`, title: `${artifactId}.md` });
		render();
		await vi.waitFor(() => expect(host.querySelector('.cm-editor')).not.toBeNull());
		return EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	}

	it('keeps an empty caret inside commented text where the user put it', async () => {
		const view = await openCommented('art-caret');
		view.dispatch({ selection: { anchor: QUOTE.from + 2 } });
		flushSync();
		// The editor's anchor-focus callback reached the viewer and opened this thread.
		expect(get(focusedDocComment)).toBe('comment');
		expect(document.body.querySelector('.popover')).not.toBeNull();
		expect(view.state.selection.main).toMatchObject({ anchor: QUOTE.from + 2, head: QUOTE.from + 2 });
	});

	it('opens a single-comment marker without moving the caret or taking focus', async () => {
		const view = await openCommented('art-marker');
		const marker = await vi.waitFor(() => {
			const found = host.querySelector<HTMLButtonElement>('.cm-comment-marker');
			expect(found).not.toBeNull();
			return found!;
		});
		marker.focus();
		marker.click();
		flushSync();
		expect(get(focusedDocComment)).toBe('comment');
		expect(document.body.querySelector('.popover')).not.toBeNull();
		expect(view.state.selection.main).toMatchObject({ anchor: 0, head: 0 });
		expect(view.contentDOM.contains(document.activeElement)).toBe(false);
	});

	it('opens the image from a marker over a comment on part of it, at that comment, instead of its card', async () => {
		focusedDocComment.set(null);
		const IMG = { from: 7, to: 21 };
		mocks.rangeFor.mockReturnValue(IMG);
		mocks.fetchContent.mockResolvedValue({ text: 'Intro\n\n![Mock](a.png)\n', revision: 'r-1', writable: true, artifact: { id: 'art-pin' } });
		mocks.setComments({ 'art-pin': [{
			id: 'pin', ...IMG, anchor: '![Mock](a.png)', author: 'you', text: 'note', at: 'now', resolved: false, replies: [],
			region: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, regionNumber: 1, imageSrc: '/a.png',
		}] });
		openArtifactTab(THREAD, { path: '/art-pin.md', title: 'art-pin.md' });
		render();
		const img = await vi.waitFor(() => {
			const found = host.querySelector<HTMLImageElement>('.cm-md-image img');
			expect(found).not.toBeNull();
			return found!;
		});
		Object.defineProperty(img, 'complete', { value: true });
		Object.defineProperty(img, 'naturalWidth', { value: 640 });
		const marker = await vi.waitFor(() => {
			const found = host.querySelector<HTMLButtonElement>('.cm-comment-marker');
			expect(found).not.toBeNull();
			return found!;
		});
		marker.click();
		const tab = get(threadWorkspaces)[THREAD].activeTabId!;
		await vi.waitFor(() => expect(tabDocumentImage(THREAD, tab)).toMatchObject({ ...IMG, focus: { id: 'pin' } }));
		await vi.waitFor(() => expect(host.querySelector('.image-overlay [data-image-comment]')).not.toBeNull());
		expect(document.body.querySelector('.popover')).toBeNull();
	});
});
