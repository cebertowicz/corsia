import { createSubscriber } from 'svelte/reactivity';
import type { ImageViewportPlace } from '$lib/artifactTabTypes';
import { remapOccurrence } from '$lib/docImageComments';
import type { DocumentImageRequest } from '$lib/docViewerImages';
import type { TextDocumentPlace } from '$lib/documentImagePlace';

/** An image of a document shown larger over its tab, and the camera the user left it at. */
export type TabDocumentImage = DocumentImageRequest & { camera?: ImageViewportPlace };

export type TabViewState = {
	documentPlace?: TextDocumentPlace;
	imagePlace?: ImageViewportPlace;
	/** The one record of which document image a tab shows: a tab switch keeps it, Back, collapse and close drop it. */
	documentImage?: TabDocumentImage;
};

// Tab camera/place is intentionally outside the serialized workspace: it is per tab instance and dies on close/reload.
const tabViewStates = new Map<string, TabViewState>();

// Places and cameras are read once, when their view mounts or its image loads. The open document image is drawn
// from this state directly, so opening, moving or dropping one is announced to the effects reading it.
const imageListeners = new Set<() => void>();
const trackDocumentImages = createSubscriber((update) => {
	imageListeners.add(update);
	return () => imageListeners.delete(update);
});
function announceDocumentImages() {
	for (const update of imageListeners) update();
}

function tabViewKey(threadId: string, instanceId: string): string {
	return `${threadId}\u0000${instanceId}`;
}

export function tabViewState(threadId: string, instanceId: string): TabViewState | null {
	return tabViewStates.get(tabViewKey(threadId, instanceId)) ?? null;
}

function setTabViewState(threadId: string, instanceId: string, change: (current: TabViewState) => TabViewState): void {
	const key = tabViewKey(threadId, instanceId);
	const { documentImage, ...rest } = change(tabViewStates.get(key) ?? {});
	const next: TabViewState = documentImage ? { ...rest, documentImage } : rest;
	if (Object.keys(next).length) tabViewStates.set(key, next);
	else tabViewStates.delete(key);
}

export function recordTabDocumentPlace(threadId: string, instanceId: string, place: TextDocumentPlace): void {
	setTabViewState(threadId, instanceId, (current) => ({ ...current, documentPlace: { ...place } }));
}

export function recordTabImagePlace(threadId: string, instanceId: string, place: ImageViewportPlace): void {
	setTabViewState(threadId, instanceId, (current) => ({ ...current, imagePlace: { ...place } }));
}

/** The document image a tab shows. Read in an effect or derived, it follows the image opening, moving and closing. */
export function tabDocumentImage(threadId: string, instanceId: string): TabDocumentImage | null {
	trackDocumentImages();
	return tabViewState(threadId, instanceId)?.documentImage ?? null;
}

/** Shows an image of the tab's document over it, at Fit: only where it is and what it opens on are kept, never the comments. */
export function openTabDocumentImage(threadId: string, instanceId: string, request: DocumentImageRequest): void {
	const { src, alt, from, to, scrollTop, top, focus } = request;
	setTabViewState(threadId, instanceId, (current) => ({ ...current, documentImage: { src, alt, from, to, scrollTop, top, focus } }));
	announceDocumentImages();
}

export function recordTabDocumentImageCamera(threadId: string, instanceId: string, camera: ImageViewportPlace): void {
	setTabViewState(threadId, instanceId, (current) => (
		current.documentImage ? { ...current, documentImage: { ...current.documentImage, camera: { ...camera } } } : current
	));
}

/** The tab's document changed under its image: the image keeps to its markdown, so its comments and Back stay with it. */
export function remapTabDocumentImage(threadId: string, instanceId: string, mapPos: (pos: number) => number): void {
	const image = tabViewState(threadId, instanceId)?.documentImage;
	if (!image) return;
	const moved = remapOccurrence(image, mapPos);
	if (moved === image) return;
	setTabViewState(threadId, instanceId, (current) => ({ ...current, documentImage: moved }));
	announceDocumentImages();
}

export function clearTabDocumentImage(threadId: string, instanceId: string): void {
	if (!tabViewState(threadId, instanceId)?.documentImage) return;
	setTabViewState(threadId, instanceId, (current) => ({ ...current, documentImage: undefined }));
	announceDocumentImages();
}

/** A closed tab takes its places and its open image with it. */
export function clearTabViewState(threadId: string, instanceId: string): void {
	const hadImage = !!tabViewState(threadId, instanceId)?.documentImage;
	tabViewStates.delete(tabViewKey(threadId, instanceId));
	if (hadImage) announceDocumentImages();
}
