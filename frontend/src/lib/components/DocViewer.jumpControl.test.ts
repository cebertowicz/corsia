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
const target: DocViewerTarget = { instanceId: 'jump-control-tab', path: '/documents/plan.md', name: 'plan.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;

function root(id: string, offsets: { from: number; to: number }, anchor: string, newReplies: string[], extra: Partial<ServerComment> = {}): ServerComment {
	return {
		id, artifact_id: 'artifact', parent_id: null, author: 'you', author_kind: 'user', body: `ask ${id}`, anchor_text: anchor,
		from_off: offsets.from, to_off: offsets.to, created_at: '2026-10-27T10:00:00Z', review: false, resolved: false,
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
const control = () => host.querySelector<HTMLButtonElement>('[data-doc-jump] button');
const band = () => host.querySelector('[data-doc-bottom-band]');

/** The document's unread answers, as the surface holds them: every thread cleared, or one thread's cleared. */
const readThread = (id: string) => docComments.update((all) => ({
	...all,
	artifact: all.artifact.map((comment) => (id === '*' || comment.id === id) ? { ...comment, newComment: false, newReplyIds: [] } : comment),
}));

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
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

describe('the document jump control', () => {
	// Nothing is drawn at measured boxes here (jsdom has no layout), so every unread thread is a target the reader has not reached.
	it('counts every unread answer in one control and names the one it points at', async () => {
		await open([ALPHA, PIN_ONE, PIN_TWO, BETA]);
		await vi.waitFor(() => expect(control()).not.toBeNull());
		expect(control()!.textContent).toContain('4 new');
		expect(control()!.getAttribute('aria-label')).toBe('4 new, next below: “Alpha”');
		expect(band()!.querySelectorAll('[data-doc-jump]')).toHaveLength(1);
	});

	it('is absent, band and all, when nothing is waiting or the document is covered', async () => {
		await open([BETA]);
		expect(band()).toBeNull();
		await open([ALPHA], { covered: true });
		expect(band()).toBeNull();
	});

	it('goes to the thread it points at, focused as an arrival lands it', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(control()).not.toBeNull());
		control()!.click();
		flushSync();
		await vi.waitFor(() => expect(get(focusedDocComment)).toBe('alpha'));
	});

	it('opens the image at its pin, then points at the next pin once that one is read', async () => {
		const onOpenImage = vi.fn();
		await open([PIN_ONE, PIN_TWO], { onOpenImage });
		await vi.waitFor(() => expect(control()).not.toBeNull());
		control()!.click();
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledTimes(1));
		expect(onOpenImage.mock.calls[0][0]).toMatchObject({ src: 'shot.png', focus: { id: 'pin-one' } });
		readThread('pin-one');
		// Its alt text is what the quote reads as, marks taken out the way a card's label does.
		await vi.waitFor(() => expect(control()!.getAttribute('aria-label')).toBe('2 new, next below: “shot”'));
		control()!.click();
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledTimes(2));
		expect(onOpenImage.mock.calls[1][0]).toMatchObject({ focus: { id: 'pin-two' } });
	});

	it('steps aside while the comments it opened are up, and is back, still counting, once they close', async () => {
		await open([ALPHA, BETA]);
		await vi.waitFor(() => expect(control()).not.toBeNull());
		control()!.click();
		await vi.waitFor(() => expect(get(focusedDocComment)).toBe('alpha'));
		await vi.waitFor(() => expect(control()).toBeNull());
		document.querySelector<HTMLButtonElement>('button[aria-label="Close comments"]')!.click();
		await vi.waitFor(() => expect(control()?.textContent).toContain('1 new'), { timeout: 2000 });
	});

	it('counts down as answers are read and leaves with the last', async () => {
		await open([ALPHA, PIN_ONE]);
		await vi.waitFor(() => expect(control()?.textContent).toContain('2 new'));
		readThread('alpha');
		await vi.waitFor(() => expect(control()?.textContent).toContain('1 new'));
		readThread('*');
		await vi.waitFor(() => expect(band()).toBeNull(), { timeout: 2000 });
	});

	it('counts an agent suggestion the person has not read beside an answer, in the one total', async () => {
		const suggestion = { ...root('sug', { from: BETA_AT, to: BETA_AT + 4 }, 'Beta', []), replacement: 'Beta closes the plan well.' };
		api.fetchComments.mockResolvedValue({
			comments: [ALPHA, { ...suggestion, author: 'Maya Chen', author_kind: 'agent', new_comment: true }],
			feedback: { open_count: 2, owner_label: null, undelivered_batches: [] },
		});
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(DocViewer, { target: host, props: { target } });
		await vi.waitFor(() => expect(get(docComments).artifact?.length).toBe(2));
		await vi.waitFor(() => expect(control()?.textContent).toContain('2 new'));
	});
});
