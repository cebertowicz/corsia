import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import DocViewer from './DocViewer.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { DocumentImageRequest } from '$lib/docViewerImages';
import type { ServerComment } from '$lib/api/docs';
import { connectionState } from '$lib/stores/connection';
import { docComments } from '$lib/stores/docComments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { clearDraft } from '$lib/stores/docSaveQueue';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
// The markdown image widget needs a real browser to draw; the viewer's own choice (open the image or not) is what is under test.
vi.mock('$lib/cm/markdown-image', async importOriginal => ({ ...await importOriginal<typeof import('$lib/cm/markdown-image')>(),
	imageRequestAt: vi.fn(async (_root: HTMLElement, at: { from: number; to: number }) => ({ src: 'shot.png', alt: 'shot', from: at.from, to: at.to })) }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
// The legacy (non-live) open: the document is the saved file, which is all these tests need.
vi.mock('$lib/stores/liveDocuments', async importOriginal => ({ ...await importOriginal<typeof import('$lib/stores/liveDocuments')>(), openLiveDocument: vi.fn(async () => null) }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

const TEXT = 'Alpha opens the plan.\n\n![shot](shot.png)\n\nBeta closes the plan.\n';
const IMAGE = { from: TEXT.indexOf('![shot]'), to: TEXT.indexOf('![shot]') + '![shot](shot.png)'.length };
const BETA_AT = TEXT.indexOf('Beta');
const target: DocViewerTarget = { instanceId: 'reply-chip-tab', path: '/documents/plan.md', name: 'plan.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;

function root(id: string, offsets: { from: number; to: number }, anchor: string, newReplies: string[], extra: Partial<ServerComment> = {}): ServerComment {
	return {
		id, artifact_id: 'artifact', parent_id: null, author: 'you', author_kind: 'user', body: `ask ${id}`, anchor_text: anchor,
		from_off: offsets.from, to_off: offsets.to, created_at: '2026-10-27T10:00:00Z', review: false, resolved: false, acked: false,
		new_reply_ids: newReplies,
		replies: newReplies.map((reply) => ({ id: reply, author: 'Maya Chen', author_kind: 'agent', body: `answer ${reply}`, created_at: '2026-10-27T11:00:00Z' })),
		...extra,
	} as unknown as ServerComment;
}
const pin = (number: number) => ({ region_number: number, region_x: 0.2 * number, region_y: 0.2 * number, region_w: 0.1, region_h: 0.1, image_src: 'shot.png' });
const ALPHA = root('alpha', { from: 0, to: 5 }, 'Alpha', ['alpha-r1']);
const PIN_ONE = root('pin-one', IMAGE, '![shot](shot.png)', ['pin-one-r1'], pin(1));
const PIN_TWO = root('pin-two', IMAGE, '![shot](shot.png)', ['pin-two-r1', 'pin-two-r2'], pin(2));
const BETA = root('beta', { from: BETA_AT, to: BETA_AT + 4 }, 'Beta', []);

async function open(comments: ServerComment[], props: { onOpenImage?: (request: DocumentImageRequest) => void; covered?: boolean } = {}) {
	api.fetchComments.mockResolvedValue({ comments, feedback: { open_count: comments.length, owner_label: null, undelivered_batches: [] } });
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target, ...props } });
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toContain('Alpha opens the plan.'));
	await vi.waitFor(() => expect(get(docComments).artifact?.length).toBe(comments.length));
}
const chip = () => host.querySelector<HTMLButtonElement>('[data-doc-reply-chip] button.tile.chip');

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	HTMLElement.prototype.scrollIntoView = vi.fn();
	Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
	Range.prototype.getBoundingClientRect = vi.fn(() => new DOMRect());
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
	api.fetchContent.mockResolvedValue({ text: TEXT, writable: true, artifact: { id: 'artifact' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	clearDraft(target);
	docComments.set({});
	focusedDocComment.set(null);
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

describe('the document reply chip', () => {
	it('shows the chat chip with the count of new replies in this document', async () => {
		await open([ALPHA, PIN_ONE, PIN_TWO, BETA]);
		await vi.waitFor(() => expect(chip()).not.toBeNull());
		expect(chip()!.textContent).toContain('4 new replies');
		expect(chip()!.querySelector('.ct-new')?.textContent).toBe('New');
	});

	it('is absent when no reply is waiting', async () => {
		await open([BETA]);
		expect(host.querySelector('[data-doc-reply-chip]')).toBeNull();
	});

	it('is absent while an image covers the document', async () => {
		await open([ALPHA], { covered: true });
		expect(host.querySelector('[data-doc-reply-chip]')).toBeNull();
	});

	it('lands a text reply as arrival does: its thread focused', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(chip()).not.toBeNull());
		chip()!.click();
		flushSync();
		await vi.waitFor(() => expect(get(focusedDocComment)).toBe('alpha'));
	});

	it('opens the image at its pin for a reply on an image comment, then steps on, and round again', async () => {
		const onOpenImage = vi.fn();
		await open([PIN_ONE, PIN_TWO], { onOpenImage });
		await vi.waitFor(() => expect(chip()).not.toBeNull());
		chip()!.click();
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledTimes(1));
		expect(onOpenImage.mock.calls[0][0]).toMatchObject({ src: 'shot.png', focus: { id: 'pin-one' } });
		chip()!.click();
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledTimes(2));
		expect(onOpenImage.mock.calls[1][0]).toMatchObject({ focus: { id: 'pin-two' } });
		chip()!.click();
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledTimes(3));
		expect(onOpenImage.mock.calls[2][0]).toMatchObject({ focus: { id: 'pin-one' } });
	});

	it('steps aside while the comments sheet it opened is up, and is back, still counting, once it closes', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(chip()).not.toBeNull());
		chip()!.click();
		await vi.waitFor(() => expect(get(focusedDocComment)).toBe('alpha'));
		await vi.waitFor(() => expect(host.querySelector('[data-doc-reply-chip]')).toBeNull());
		document.querySelector<HTMLButtonElement>('button[aria-label="Close comments"]')!.click();
		await vi.waitFor(() => expect(chip()?.textContent).toContain('1 new reply'), { timeout: 2000 });
	});

	it('counts down as replies are read and leaves with the last', async () => {
		await open([ALPHA, PIN_ONE]);
		await vi.waitFor(() => expect(chip()?.textContent).toContain('2 new replies'));
		docComments.update((all) => ({ ...all, artifact: all.artifact.map((comment) => comment.id === 'alpha' ? { ...comment, newReplyIds: [] } : comment) }));
		await vi.waitFor(() => expect(chip()?.textContent).toContain('1 new reply'));
		docComments.update((all) => ({ ...all, artifact: all.artifact.map((comment) => ({ ...comment, newReplyIds: [] })) }));
		await vi.waitFor(() => expect(host.querySelector('[data-doc-reply-chip]')).toBeNull(), { timeout: 2000 });
	});
});
