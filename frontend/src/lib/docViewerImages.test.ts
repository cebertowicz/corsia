import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocComment } from '$lib/stores/docComments';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { DocFeedback } from '$lib/docFeedback.svelte';

const imageRequestAt = vi.fn();
const remapDocumentImage = vi.fn();
vi.mock('$lib/cm/markdown-image', () => ({ imageRequestAt: (...args: unknown[]) => imageRequestAt(...args) }));
vi.mock('$lib/stores/documentImageViewer', () => ({ remapDocumentImage: (...args: unknown[]) => remapDocumentImage(...args) }));

const { createDocViewerImages } = await import('./docViewerImages');

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
function comment(id: string, extra: Partial<DocComment> = {}): DocComment {
	return { id, anchor: '![Mock](a.png)', author: 'you', authorKind: 'user', text: id, at: 'now', resolved: false, replies: [], ...extra };
}
const onImage = (id: string, from = 10, to = 24) => comment(id, { region: BOX, regionNumber: 1, imageSrc: '/ws/a.png', from, to });
const TARGET: DocViewerTarget = { instanceId: 'tab-1', path: 'doc.md', name: 'doc.md', projectId: 'p1', threadId: 't1', artifactId: 'a1' };
const batch = { pendingCount: 0 } as unknown as DocFeedback;
const DRAWN = { src: '/ws/a.png', alt: 'Mock', from: 10, to: 24, scrollTop: 0, top: 0 };

function viewer(overrides: { onOpenImage?: ((request: unknown) => void) | null; target?: DocViewerTarget | null } = {}) {
	const onOpenImage = overrides.onOpenImage === undefined ? vi.fn() : overrides.onOpenImage;
	const comments = [onImage('one'), onImage('two'), onImage('later', 40, 54), comment('text', { from: 10, to: 24 })];
	const images = createDocViewerImages({
		onOpenImage: () => onOpenImage ?? undefined, root: () => document.createElement('div'), comments: () => comments,
		artifactId: () => 'a1', target: () => (overrides.target === undefined ? TARGET : overrides.target), readOnly: () => false,
		text: () => '', session: () => undefined, accent: () => null, feedback: () => batch,
	});
	return { images, onOpenImage };
}

describe('opening a document image from its comments', () => {
	beforeEach(() => { imageRequestAt.mockReset(); remapDocumentImage.mockReset(); });

	it('opens the image all the comments are on, at the focused one, with its comments', async () => {
		imageRequestAt.mockResolvedValue(DRAWN);
		const { images, onOpenImage } = viewer();
		const otherwise = vi.fn();
		images.openOr(['one'], { id: 'one' }, otherwise);
		await vi.waitFor(() => expect(onOpenImage).toHaveBeenCalledOnce());
		expect(imageRequestAt).toHaveBeenCalledWith(expect.any(HTMLElement), { from: 10, to: 24 });
		expect(onOpenImage).toHaveBeenCalledWith(expect.objectContaining({ ...DRAWN, focus: { id: 'one' }, comments: expect.objectContaining({ artifactId: 'a1', projectId: 'p1', readOnly: false, feedback: batch }) }));
		expect(otherwise).not.toHaveBeenCalled();
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

	it('keeps an open image on its markdown when the document changes, only for a document in a thread', () => {
		const mapPos = (pos: number) => pos + 1;
		viewer().images.remap(mapPos);
		expect(remapDocumentImage).toHaveBeenCalledWith('t1', 'tab-1', mapPos);
		viewer({ target: { ...TARGET, threadId: null } }).images.remap(mapPos);
		expect(remapDocumentImage).toHaveBeenCalledOnce();
	});
});
