import { imageContentUrl } from '$lib/artifactImage';
import { imageFailureReason } from '$lib/imageFailure';
import { resolveImagePath, type ImageContext } from './markdown-image';

/**
 * The images of rendered markdown (a table's cells) drawn as a standalone image is: a workspace path, relative ones
 * from the document's folder, loads from the content route and says why when it fails; anything else (a web or
 * `data:` URL, or no document to resolve against) shows as its alt text in link style and is never fetched.
 */
export function drawRenderedImages(root: HTMLElement, context: ImageContext | null) {
	for (const img of root.querySelectorAll('img')) {
		const alt = img.getAttribute('alt') ?? '';
		const path = context ? resolveImagePath(context.docPath, img.getAttribute('src') ?? '') : null;
		if (!context || path === null) {
			const link = document.createElement('span');
			link.className = 'cm-md-link';
			link.textContent = alt || (img.getAttribute('src') ?? '');
			img.replaceWith(link);
			continue;
		}
		const src = imageContentUrl({ path, projectId: context.projectId, token: context.token });
		img.decoding = 'async';
		img.addEventListener('error', async () => {
			const reason = await imageFailureReason(src);
			const note = document.createElement('span');
			note.className = 'cm-md-image-note';
			note.textContent = alt ? `${alt} · ${reason}` : reason;
			img.replaceWith(note);
		});
		img.src = src;
	}
}
