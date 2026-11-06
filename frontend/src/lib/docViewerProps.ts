import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { TabLoadSignal } from '$lib/artifactTabs';
import type { DocumentImageRequest } from '$lib/docViewerImages';

export type DocViewerProps = {
	target: DocViewerTarget | null;
	onClose?: () => void;
	escapeCloses?: boolean;
	/** Reports load progress against the tab instance that started it. */
	onSignal?: (signal: TabLoadSignal) => void;
	/** A newer revision exists than the one on screen. */
	stale?: boolean;
	onReload?: () => void;
	/** Opens an image of the document larger in the canvas, with its comments. */
	onOpenImage?: (request: DocumentImageRequest) => void;
	onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
	/** The canvas shows an image viewer over this document; it keeps its state but answers no keys. */
	covered?: boolean;
	/** An image covers the document, or it is still being put back after a tab return, so its replies are not on screen and must not be marked read. */
	readStampSuppressed?: boolean;
	/** The tab's reply landing was acted on, or had no readable replies; the tab drops it. */
	onLanded?: () => void;
};
