import { publicFailureMessage } from './stores/documentFailures';

/** The viewer's existing disclosure when a refused tab shows the file snapshot. */
export const SNAPSHOT_REFUSAL_MESSAGE = "This tab shows the last saved version of the file. Changes from this browser that weren't saved yet aren't shown here.";

function openRefusalMessage(code: string, message: string): string {
	return code === 'setup_failed' && message === SNAPSHOT_REFUSAL_MESSAGE
		? message : publicFailureMessage(code, message);
}

export interface DocOpenRefusal {
	/** not_connected (after the socket wait) and not_live (a reconnect answered non-live) are the browser's own. */
	code: 'store_damaged' | 'store_unavailable' | 'setup_failed' | 'not_connected' | 'not_live';
	title: string;
	message: string;
	retryable?: boolean;
}

export function docOpenRefusal(value: unknown): DocOpenRefusal | null {
	if (!value || typeof value !== 'object') return null;
	const detail = value as Record<string, unknown>;
	if (!['store_damaged', 'store_unavailable', 'setup_failed', 'not_connected'].includes(String(detail.code))
		|| typeof detail.title !== 'string' || typeof detail.message !== 'string') return null;
	return { ...detail, message: openRefusalMessage(String(detail.code), detail.message) } as unknown as DocOpenRefusal;
}

const UNAVAILABLE_TITLE = 'Live editing is unavailable for this document.';

/** Why a live open answered as a non-live file; the tab stays editable through the legacy save. */
export type LiveUnavailable = 'store_damaged';

/** Read the reason off an HTTP live_open response or a doc_opened frame; a live answer has none. */
export function liveUnavailable(value: { live?: boolean; live_unavailable?: string } | null | undefined): LiveUnavailable | null {
	return value?.live !== true && value?.live_unavailable === 'store_damaged' ? 'store_damaged' : null;
}

/** The action-free header row of a new open that answered non-live. Product Experience approved this copy (2026-10-03). */
export const LIVE_UNAVAILABLE_NOTICE: Record<LiveUnavailable, { title: string; message: string }> = {
	store_damaged: {
		title: UNAVAILABLE_TITLE,
		message: "Its saved editing history could not be read. You can edit and save this file without live editing. Earlier changes stored only in that history or a browser copy aren't shown here.",
	},
};

/**
 * What an existing live session shows when its reconnect answers non-live. It never takes
 * the non-live path: it keeps its text and device copy, read-only, with Copy and no Retry
 * (design 5.7(a) revision 16, countercase 1).
 */
export function sessionRefusal(reason: LiveUnavailable | null): DocOpenRefusal {
	return reason === 'store_damaged'
		// Product Experience approved this copy (08:22 PDT).
		? { code: 'store_damaged', title: UNAVAILABLE_TITLE, message: 'Its saved editing history could not be read. Text in this tab is kept. Copy it to keep working.' }
		// Product Experience approved this copy (12:28 PDT) for the retained current-session editor only.
		: { code: 'not_live', title: UNAVAILABLE_TITLE, message: 'Text in this tab is kept. Copy it to keep working.', retryable: false };
}

/** `detail.code` of the 415 a text open answers for an image file (`backend/api/files_load.py`). */
export const IMAGE_FILE_CODE = 'image_file';

/** The backend refused to open this file as text because it is an image. */
export class ImageFileError extends Error {
	constructor() {
		super('This file is an image');
		this.name = 'ImageFileError';
	}
}

export class DocOpenError extends Error {
	readonly code: DocOpenRefusal['code'];
	readonly title: string;
	constructor(detail: DocOpenRefusal) {
		super(openRefusalMessage(detail.code, detail.message));
		this.code = detail.code;
		this.title = detail.title;
	}
}

/** The typed error for a failed text open's `detail`, or null when it is neither a refusal nor an image. */
export function docOpenFailure(detail: unknown): DocOpenError | ImageFileError | null {
	const refusal = docOpenRefusal(detail);
	if (refusal) return new DocOpenError(refusal);
	return (detail as { code?: unknown } | undefined)?.code === IMAGE_FILE_CODE ? new ImageFileError() : null;
}
