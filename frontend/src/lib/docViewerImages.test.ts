import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocComment } from '$lib/stores/docComments';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { DocFeedback } from '$lib/docFeedback.svelte';

const imageRequestAt = vi.fn();
const remapTabDocumentImage = vi.fn();
vi.mock('$lib/cm/markdown-image', () => ({ imageRequestAt: (...args: unknown[]) => imageRequestAt(...args) }));
vi.mock('$lib/stores/tabViewState', () => ({ remapTabDocumentImage: (...args: unknown[]) => remapTabDocumentImage(...args) }));

const { createDocViewerImages } = await import('./docViewerImages');

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
function comment(id: string, extra: Partial<DocComment> = {}): DocComment {
	return { id, anchor: '![Mock](a.png)', author: 'you', authorKind: 'user', text: id, at: 'now', resolved: false, replies: [], ...extra };
}
const onImage = (id: string, from = 10, to = 24) => comment(id, { region: BOX, regionNumber: 1, imageSrc: '/ws/a.png', from, to });
const TARGET: DocViewerTarget = { instanceId: 'tab-1', path: 'doc.md', name: 'doc.md', projectId: 'p1', threadId: 't1', artifactId: 'a1' };
const batch = { pendingCount: 0 } as unknown as DocFeedback;
const DRAWN = { src: '/ws/a.png', alt: 'Mock', from: 10, to: 24, scrollTop: 0, top: 0 };

function viewer(overrides: { onOpenImage?: ((request: unknown) => void) | null; target?: DocViewerTarget | null; artifactId?: string | null; imageCacheKey?: string; tabThreadId?: string | null } = {}) {
	const onOpenImage = overrides.onOpenImage === undefined ? vi.fn() : overrides.onOpenImage;
	const showInText = vi.fn();
	const imageCacheKey = { value: overrides.imageCacheKey ?? 'open-1' };
	const comments = [onImage('one'), onImage('two'), onImage('later', 40, 54), comment('text', { from: 10, to: 24 })];
	const images = createDocViewerImages({
		onOpenImage: () => onOpenImage ?? undefined, root: () => document.createElement('div'), comments: () => comments,
		artifactId: () => (overrides.artifactId === undefined ? 'a1' : overrides.artifactId), target: () => (overrides.target === undefined ? TARGET : overrides.target), tabThreadId: () => (overrides.tabThreadId === undefined ? 't1' : overrides.tabThreadId), readOnly: () => false,
		text: () => '', session: () => undefined, accent: () => null, feedback: () => batch, showInText,
		imageCacheKey: () => imageCacheKey.value,
	});
	return { images, onOpenImage, showInText, imageCacheKey };
}

describe('opening a document image from its comments', () => {
	beforeEach(() => { imageRequestAt.mockReset(); remapTabDocumentImage.mockReset(); });

	it('opens the image all the comments are on, at the focused one', async () => {
		imageRequestAt.mockResolvedValue(DRAWN);
		const { images, onOpenImage } = viewer();
		const otherwise = vi.fn();
		images.openOr(['one'], { id: 'one' }, otherwise);
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledOnce());
		expect(imageRequestAt).toHaveBeenCalledWith(expect.any(HTMLElement), { from: 10, to: 24 });
		expect(onOpenImage).toHaveBeenCalledWith({ ...DRAWN, focus: { id: 'one' } });
		expect(otherwise).not.toHaveBeenCalled();
	});

	it('gives the comments of a shown image from the document as it is now, and none without an artifact', () => {
		expect(viewer().images.comments()).toEqual(expect.objectContaining({ artifactId: 'a1', projectId: 'p1', readOnly: false, feedback: batch }));
		expect(viewer({ artifactId: null }).images.comments()).toBeUndefined();
	});

	it('says why an image comment was refused in a document that is not live, as a live one does', () => {
		const failure = viewer().images.comments()!.failure!;
		expect(failure(new Error('400 {"detail":"the image shots/a.png couldn’t be found.","kind":"region_target"}')))
			.toBe('Couldn’t post the comment: the image shots/a.png couldn’t be found. Your draft is kept.');
		expect(failure(new TypeError('Failed to fetch'))).toBe('Couldn’t post the comment. Your draft is kept. Try again.');
	});

	it('uses the current document cache key for a comment thumbnail', () => {
		const { images, imageCacheKey } = viewer({ imageCacheKey: 'open-1' });
		const first = images.groups.srcOf(onImage('one'))!;
		expect(new URL(first, 'http://x').searchParams.get('v')).toBe('open-1');
		imageCacheKey.value = 'refresh-1';
		const refreshed = images.groups.srcOf(onImage('one'))!;
		expect(new URL(refreshed, 'http://x').searchParams.get('v')).toBe('refresh-1');
	});

	it('falls back when the comments are not all on one image, without trying to draw', () => {
		const { images, onOpenImage } = viewer();
		for (const ids of [['one', 'later'], ['one', 'text'], ['missing']]) {
			const otherwise = vi.fn();
			images.openOr(ids, undefined, otherwise);
			expect(otherwise).toHaveBeenCalledOnce();
		}
		expect(imageRequestAt).not.toHaveBeenCalled();
		expect(onOpenImage).not.toHaveBeenCalled();
	});

	it('falls back when the image does not draw', async () => {
		imageRequestAt.mockResolvedValue(null);
		const { images, onOpenImage } = viewer();
		const otherwise = vi.fn();
		images.openOr(['one', 'two'], undefined, otherwise);
		await vi.waitFor(() => expect(otherwise).toHaveBeenCalledOnce());
		expect(onOpenImage).not.toHaveBeenCalled();
	});

	it('falls back when the viewer cannot open images', () => {
		const { images } = viewer({ onOpenImage: null });
		const otherwise = vi.fn();
		images.openOr(['one'], { id: 'one' }, otherwise);
		expect(otherwise).toHaveBeenCalledOnce();
		expect(imageRequestAt).not.toHaveBeenCalled();
	});

	it('shows a drawer group’s comment in the document when its image does not draw, for the thumbnail and Move pin', async () => {
		imageRequestAt.mockResolvedValue(null);
		const { images, onOpenImage, showInText } = viewer();
		images.groups.onOpen(onImage('one'));
		await vi.waitFor(() => expect(showInText).toHaveBeenCalledWith('one'));
		images.groups.onOpen(onImage('two'), { id: 'two', move: true });
		await vi.waitFor(() => expect(showInText).toHaveBeenCalledWith('two'));
		expect(onOpenImage).not.toHaveBeenCalled();
	});

	it('keeps an open image on its markdown when the document changes, only for a document in a thread', () => {
		const mapPos = (pos: number) => pos + 1;
		viewer().images.remap(mapPos);
		expect(remapTabDocumentImage).toHaveBeenCalledWith('t1', 'tab-1', mapPos);
		viewer({ tabThreadId: null }).images.remap(mapPos);
		expect(remapTabDocumentImage).toHaveBeenCalledOnce();
	});

	it('keeps the image held by the leaf’s tab on its markdown when the leaf shows another thread’s document', () => {
		const mapPos = (pos: number) => pos + 1;
		viewer({ tabThreadId: 'host-thread' }).images.remap(mapPos);
		expect(remapTabDocumentImage).toHaveBeenCalledWith('host-thread', 'tab-1', mapPos);
	});
});
