import { afterEach, beforeEach, expect, it, vi } from 'vitest';
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
import type { CommentReply } from '$lib/stores/docCommentProjection';
import { relLabel } from '$lib/time';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn(), markCommentThreadViewed: vi.fn() }));
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
let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
let innerWidth: number;
const shown: DocViewerTarget[] = [];

function thread(id: string, artifact: string, newReplies: string[]): ServerComment {
	return {
		id, artifact_id: artifact, parent_id: null, author: 'you', author_kind: 'user', body: `ask ${id}`, anchor_text: 'Alpha',
		from_off: 0, to_off: 5, created_at: '2026-11-16T01:24:00+00:00', review: false, resolved: false, new_reply_ids: newReplies,
		replies: [{ id: `${id}-a1`, author: 'Browser', author_kind: 'agent', body: 'first answer', created_at: '2026-11-16T01:34:00+00:00' }],
	} as unknown as ServerComment;
}
function documentTarget(artifact: string, comments: ServerComment[]): DocViewerTarget {
	api.fetchComments.mockImplementation(async () => ({ comments, feedback: { open_count: comments.length, owner_label: null, undelivered_batches: [] } }));
	api.fetchContent.mockResolvedValue({ text: TEXT, writable: true, artifact: { id: artifact }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: artifact, freshness: null });
	const target = { instanceId: `stamp-home-${artifact}`, path: `/documents/${artifact}.md`, name: `${artifact}.md`, projectId: 'project', threadId: 'thread', artifactId: artifact };
	shown.push(target);
	return target;
}
/** Another party answers in the open thread: the server lists the new reply as unread. */
const replyArrives = (artifact: string, id: string, replyId: string) => {
	const createdAt = '2026-11-16T02:10:00+00:00';
	const reply: CommentReply = { id: replyId, author: 'Browser', authorKind: 'agent', text: 'a later answer', at: relLabel(createdAt), createdAt };
	docComments.update((all) => ({
		...all, [artifact]: all[artifact].map((comment) => comment.id === id ? { ...comment, newReplyIds: [replyId], replies: [...(comment.replies ?? []), reply] } : comment),
	}));
};

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
	api.markCommentThreadViewed.mockImplementation(async (_artifact: string, rootId: string) => ({ root_id: rootId, new_reply_ids: [] }));
	connectionState.set('connected');
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	for (const target of shown.splice(0)) clearDraft(target);
	docComments.set({});
	focusedDocComment.set(null);
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: innerWidth });
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

it('reads a reply that arrived while a card was open in the document it lives in, when the viewer switches to another document', async () => {
	const props = $state({ target: documentTarget('doc-one', [thread('alpha', 'doc-one', ['alpha-a1'])]) });
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props });
	flushSync();
	await vi.waitFor(() => expect(get(docComments)['doc-one']?.length).toBe(1));
	await vi.waitFor(() => expect(host.querySelector('[data-margin-card="alpha"] [data-comment-card]')).not.toBeNull());
	// The person presses the card: it opens and reads the unread answer.
	host.querySelector<HTMLElement>('[data-margin-card="alpha"] [data-comment-card]')!.click();
	flushSync();
	await vi.waitFor(() => expect(api.markCommentThreadViewed.mock.calls).toEqual([['doc-one', 'alpha', 'alpha-a1']]));
	// An answer arrives while it is open: arriving reads nothing.
	replyArrives('doc-one', 'alpha', 'alpha-a2');
	flushSync();
	expect(api.markCommentThreadViewed).toHaveBeenCalledTimes(1);
	// The viewer, not rebuilt per document, switches to another one: the open card closes and reads in its own document.
	props.target = documentTarget('doc-two', [thread('beta', 'doc-two', [])]);
	flushSync();
	await vi.waitFor(() => expect(api.markCommentThreadViewed).toHaveBeenCalledTimes(2));
	expect(api.markCommentThreadViewed.mock.calls[1]).toEqual(['doc-one', 'alpha', 'alpha-a2']);
});
