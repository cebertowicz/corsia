import { get } from 'svelte/store';
import { imageRequestAt, type OpenImageRequest } from '$lib/cm/markdown-image';
import { imageContentUrl } from '$lib/artifactImage';
import { sharedImage, type ImageOccurrence } from '$lib/docImageComments';
import { liveFailureMessage } from '$lib/docViewerCommentDraft.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { regionColor } from '$lib/imageRegions';
import { token } from '$lib/stores/auth';
import type { DocComment } from '$lib/stores/docComments';
import { remapDocumentImage, type DocumentImageRequest } from '$lib/stores/documentImageViewer';
import { addDocImageComment } from '$lib/stores/imageRegionComments';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';

type Focus = { id: string; move?: boolean };

/** The document viewer's current state, each read at use. */
interface DocViewerImagesOptions {
	onOpenImage: () => ((request: DocumentImageRequest) => void) | undefined;
	root: () => HTMLElement | undefined;
	comments: () => DocComment[];
	artifactId: () => string | null;
	target: () => DocViewerTarget | null;
	readOnly: () => boolean;
	text: () => string;
	session: () => LiveDocumentSession | undefined;
	accent: () => string | null;
}

/**
 * A document's images with their comments (DCOL-FR13.3): an image opens larger with the comments on parts of it, and
 * one made there is anchored on its markdown. Markers, cards and drawer groups over such comments open the image.
 */
export function createDocViewerImages(options: DocViewerImagesOptions) {
	const projectId = () => options.target()?.projectId ?? null;
	function open(request: OpenImageRequest, focus?: Focus) {
		const artifactId = options.artifactId();
		options.onOpenImage()?.({ ...request, focus, comments: artifactId ? {
			artifactId, projectId: projectId(), readOnly: options.readOnly(),
			post: (from, to, region, body) => addDocImageComment({ artifactId, from, to, text: options.text(), session: options.session() }, region, body),
			failure: options.session() ? liveFailureMessage : undefined,
		} : undefined });
	}
	/** The one image all of `ids` are region comments on, when the viewer can open images. */
	function imageOf(ids: string[]): ImageOccurrence | null {
		return options.onOpenImage() ? sharedImage(options.comments(), ids) : null;
	}
	/** Opens the image comments are on; false when it does not draw, and the caller shows them in their cards instead. */
	async function openAt(image: ImageOccurrence, focus?: Focus): Promise<boolean> {
		const root = options.root();
		if (!options.onOpenImage() || !root) return false;
		const request = await imageRequestAt(root, image);
		if (!request) return false;
		open(request, focus);
		return true;
	}
	/** Opens the image all of `ids` are on, at `focus`; when they are not on one image or it does not draw, `otherwise`. */
	function openOr(ids: string[], focus: Focus | undefined, otherwise: () => void) {
		const image = imageOf(ids);
		if (!image) otherwise();
		else void openAt(image, focus).then((opened) => { if (!opened) otherwise(); });
	}
	/** The document changed under an open image: it keeps to its markdown. */
	function remap(mapPos: (pos: number) => number) {
		const target = options.target();
		if (target?.threadId) remapDocumentImage(target.threadId, target.instanceId, mapPos);
	}
	const groups = {
		srcOf: (comment: DocComment) => (comment.imageSrc ? imageContentUrl({ path: comment.imageSrc, projectId: projectId(), token: get(token) ?? '' }) : null),
		colorOf: (comment: DocComment) => regionColor(comment, projectId(), options.accent()),
		onOpen: (comment: DocComment, focus?: Focus) => openOr([comment.id], focus, () => {}),
	};
	return { open, openOr, remap, groups };
}
