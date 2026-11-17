import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import DocViewer from './DocViewer.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { ServerComment } from '$lib/api/docs';
import { connectionState } from '$lib/stores/connection';
import { docComments } from '$lib/stores/docComments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { clearDraft } from '$lib/stores/docSaveQueue';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/liveDocuments', async importOriginal => ({ ...await importOriginal<typeof import('$lib/stores/liveDocuments')>(), openLiveDocument: vi.fn(async () => null) }));
vi.mock('$lib/stores/viewport', async importOriginal => ({ ...await importOriginal<typeof import('$lib/stores/viewport')>(), isMobile: (await import('svelte/store')).writable(false) }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
// jsdom has no layout: a desktop canvas with room for the margin column, so it shows on load as it does at 1440.
vi.mock('$lib/docCommentSurfaceState.svelte', async importOriginal => ({ ...await importOriginal<typeof import('$lib/docCommentSurfaceState.svelte')>(), hasRoomForMargin: () => true }));

const TEXT = 'Alpha opens the plan.\n\nBeta closes the plan.\n';
const BETA_AT = TEXT.indexOf('Beta');
let tabs = 0;
let target: DocViewerTarget;
let artifact = 'artifact-0';
let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
let innerWidth: number;

/** A thread with more than one other party: the person's root, an agent's answer, the person's reply, a second agent's answer. */
function thread(id: string, offsets: { from: number; to: number }, anchor: string, newReplies: string[]): ServerComment {
	return {
		id, artifact_id: artifact, parent_id: null, author: 'you', author_kind: 'user', body: `ask ${id}`, anchor_text: anchor,
		from_off: offsets.from, to_off: offsets.to, created_at: '2026-11-16T01:24:00+00:00', review: false, resolved: false,
		new_reply_ids: newReplies,
		replies: [
			{ id: `${id}-a1`, author: 'Governance & Scopes', author_kind: 'agent', body: 'first answer', created_at: '2026-11-16T01:34:00+00:00' },
			{ id: `${id}-p1`, author: 'you', author_kind: 'user', body: 'a follow-up', created_at: '2026-11-16T01:35:00+00:00' },
			{ id: `${id}-a2`, author: 'Browser', author_kind: 'agent', body: 'second answer', created_at: '2026-11-16T01:53:00+00:00' },
		],
	} as unknown as ServerComment;
}
const ALPHA = () => thread('alpha', { from: 0, to: 5 }, 'Alpha', ['alpha-a2']);
const BETA = () => thread('beta', { from: BETA_AT, to: BETA_AT + 4 }, 'Beta', []);

/** The surface's own result of a stamp (`markThreadViewed`): the server names no unread item in the thread any more. */
const serverSaysRead = (id: string) => docComments.update((all) => ({
	...all, [artifact]: all[artifact].map((comment) => comment.id === id ? { ...comment, newComment: false, newReplyIds: [] } : comment),
}));
const unreadMark = (id: string) => host.querySelector(`.cm-comment-anchor[data-unread][data-comment-new="${id}"]`);

async function open(threads: Array<() => ServerComment>) {
	// A fresh tab and document each time: a tab remembers whether its column was shown, and the load check runs once per document.
	artifact = `artifact-${++tabs}`;
	const comments = threads.map((make) => make());
	api.fetchComments.mockResolvedValue({ comments, feedback: { open_count: comments.length, owner_label: null, undelivered_batches: [] } });
	api.fetchContent.mockResolvedValue({ text: TEXT, writable: true, artifact: { id: artifact }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: artifact, freshness: null });
	target = { instanceId: `unread-tint-tab-${tabs}`, path: '/documents/plan.md', name: 'plan.md', projectId: 'project', threadId: 'thread', artifactId: artifact };
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } });
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toContain('Alpha opens the plan.'));
	await vi.waitFor(() => expect(get(docComments)[artifact]?.length).toBe(comments.length));
	await vi.waitFor(() => expect(host.querySelector('[data-margin-card="alpha"]'), 'the margin column is up on load').not.toBeNull());
}

beforeEach(() => {
	innerWidth = window.innerWidth;
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	HTMLElement.prototype.scrollIntoView = vi.fn();
	Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
	Range.prototype.getBoundingClientRect = vi.fn(() => new DOMRect());
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	if (target) clearDraft(target);
	docComments.set({});
	focusedDocComment.set(null);
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: innerWidth });
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

describe('the unread tint on a document highlight, margin column up', () => {
	it('is drawn while the thread is unread', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(unreadMark('alpha')).not.toBeNull());
		expect(unreadMark('beta')).toBeNull();
	});

	it('goes once the thread is read and its card is let go, with the column still up', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(unreadMark('alpha')).not.toBeNull());
		focusedDocComment.set('alpha'); // the reader picks the card; opening it stamps the newest unread answer
		flushSync();
		serverSaysRead('alpha');
		flushSync();
		focusedDocComment.set(null); // the card goes back to its line; the column stays
		flushSync();
		await vi.waitFor(() => expect(unreadMark('alpha'), 'a read thread whose card is closed still has the unread tint').toBeNull(), { timeout: 1000 });
	});

	it('goes when the thread was read elsewhere and its card was never opened here', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(unreadMark('alpha')).not.toBeNull());
		serverSaysRead('alpha');
		flushSync();
		await vi.waitFor(() => expect(unreadMark('alpha'), 'a thread nobody opened here keeps the unread tint after it is read').toBeNull(), { timeout: 1000 });
	});
});
