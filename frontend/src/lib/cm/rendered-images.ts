import { imageContentUrl } from '$lib/artifactImage';
import { drawImage, resolveImagePath, type ImageContext } from './markdown-image';

/**
 * Rendered markdown (a table's cells) as a live element, its images drawn as a standalone image is: a workspace path,
 * relative ones from the document's folder, loads from the content route and says why when it fails; anything else
 * (a web or `data:` URL, or no document to resolve against) shows as its alt text in link style and is never fetched.
 * The HTML is parsed in an inert template, so no `<img>` it holds loads before it is replaced.
 */
export function renderedFragment(html: string, context: ImageContext | null): DocumentFragment {
	const template = document.createElement('template');
	template.innerHTML = html;
	for (const img of template.content.querySelectorAll('img')) {
		const alt = img.getAttribute('alt') ?? '';
		const path = context ? resolveImagePath(context.docPath, img.getAttribute('src') ?? '') : null;
		if (!context || path === null) {
			const link = document.createElement('span');
			link.className = 'cm-md-link';
			link.textContent = alt || (img.getAttribute('src') ?? '');
			img.replaceWith(link);
			continue;
		}
		// Not a `.cm-md-image`: that is the standalone image a click opens and the document holds its place by.
		img.replaceWith(drawImage(document.createElement('span'), alt, imageContentUrl({ path, projectId: context.projectId, token: context.token, imageCacheKey: context.imageCacheKey })));
	}
	return template.content;
}
