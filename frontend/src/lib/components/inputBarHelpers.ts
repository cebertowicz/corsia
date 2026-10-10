import { plainErrorText } from '$lib/plainError';
import { resizeTextInputToContent } from '$lib/textInputResize';

/** Which composer sheet is open above the input bar; null when none is. */
export type SheetView = 'attachments' | 'model' | 'thinking' | 'context' | null;

export interface Attachment {
	file_id: string;
	filename: string;
	mime_type: string;
	url: string;
	is_image: boolean;
	preview_url?: string;
}

/** Upload ceilings owned by the server; never hardcoded in the client. */
export interface UploadLimits {
	max_bytes: number;
	max_image_bytes: number;
	max_video_bytes: number;
}

let uploadLimits: UploadLimits | null = null;

/** Fetch and cache the server's upload ceilings for this session. */
export async function loadUploadLimits(): Promise<UploadLimits | null> {
	if (uploadLimits) return uploadLimits;
	try {
		const res = await fetch('/api/upload/limits');
		if (!res.ok) return null;
		uploadLimits = (await res.json()) as UploadLimits;
	} catch {
		return null;
	}
	return uploadLimits;
}

/** Drop the cached ceilings so the next upload re-reads them from the server. */
export function resetUploadLimits() {
	uploadLimits = null;
}

function limitFor(file: File): { bytes: number; label: string } | null {
	if (!uploadLimits) return null;
	const bytes = file.type.startsWith('image/')
		? uploadLimits.max_image_bytes
		: file.type.startsWith('video/')
			? uploadLimits.max_video_bytes
			: uploadLimits.max_bytes;
	return { bytes, label: `${Math.round(bytes / (1024 * 1024))}MB` };
}

export async function uploadInputFile(
	file: File,
	authToken: string,
	threadId: string,
): Promise<{ attachment: Attachment | null; error: string }> {
	await loadUploadLimits();
	const limit = limitFor(file);
	if (limit && file.size > limit.bytes) {
		return { attachment: null, error: `${file.name} is too large (max ${limit.label})` };
	}
	const form = new FormData();
	form.append('file', file);
	try {
		const res = await fetch(
			`/api/upload?token=${encodeURIComponent(authToken)}&thread_id=${encodeURIComponent(threadId)}`,
			{
				method: 'POST',
				body: form,
			},
		);
		if (!res.ok) {
			const err = await res.json().catch(() => null);
			if (err?.detail !== undefined) console.error('[upload] rejected', err.detail);
			return {
				attachment: null,
				error: plainErrorText(err?.detail, 'Could not upload this file. Try again.'),
			};
		}
		const data = await res.json();
		const attachment: Attachment = {
			file_id: data.file_id,
			filename: data.filename,
			mime_type: data.mime_type,
			url: data.url,
			is_image: data.is_image,
		};
		if (data.is_image) attachment.preview_url = URL.createObjectURL(file);
		return { attachment, error: '' };
	} catch {
		return { attachment: null, error: 'Upload failed' };
	}
}

export async function uploadThreadAttachment(
	file: File,
	authToken: string,
	getThreadId: () => string | null,
	ensureThread: () => Promise<void>,
): Promise<{ attachment: Attachment | null; error: string }> {
	let threadId = getThreadId();
	if (!threadId) {
		await ensureThread();
		threadId = getThreadId();
	}
	if (!threadId) {
		return {
			attachment: null,
			error: 'Could not create a conversation for this attachment',
		};
	}
	return uploadInputFile(file, authToken, threadId);
}

export function insertTextAtCaret(inputEl: HTMLDivElement | undefined, text: string) {
	const sel = window.getSelection();
	if (!sel || sel.rangeCount === 0) {
		inputEl?.append(text);
		return;
	}
	const range = sel.getRangeAt(0);
	range.deleteContents();
	const node = document.createTextNode(text);
	range.insertNode(node);
	range.setStartAfter(node);
	sel.removeAllRanges();
	sel.addRange(range);
}

export function placeCaretAtEnd(el: HTMLElement) {
	el.focus();
	const range = document.createRange();
	range.selectNodeContents(el);
	range.collapse(false);
	const sel = window.getSelection();
	sel?.removeAllRanges();
	sel?.addRange(range);
}

/**
 * Publish the composer's height as `--input-bar-height` on its own container,
 * so only that composer's surrounding layout reads it — two mounted composers
 * (the chat and a subagent leaf, #1949) never overwrite each other.
 */
export function updateInputBarHeight(inputAreaEl: HTMLElement | undefined) {
	inputAreaEl?.parentElement?.style.setProperty('--input-bar-height', `${inputAreaEl.offsetHeight}px`);
}

/**
 * Grow the composer and publish its height; never touch the transcript.
 *
 * The reserved band under the transcript is `--input-bar-height`, and the
 * transcript re-pins its own live edge when that band grows.
 */
export function resizeInputBar(
    el: HTMLElement,
    inputAreaEl: HTMLElement | undefined,
    setMultiLine: (value: boolean) => void,
) {
    const newHeight = resizeTextInputToContent(el, 150);
    setMultiLine(newHeight > 40);
    updateInputBarHeight(inputAreaEl);
}

export function watchInputBarHeight(
	getInputAreaEl: () => HTMLElement | undefined,
	schedule: (fn: () => void) => void,
) {
	const update = () => updateInputBarHeight(getInputAreaEl());
	schedule(update);
	window.addEventListener('resize', update);
	window.visualViewport?.addEventListener('resize', update);
	let ro: ResizeObserver | null = null;
	const attachObserver = () => {
		const el = getInputAreaEl();
		if (typeof ResizeObserver === 'undefined' || !el) return;
		ro?.disconnect();
		ro = new ResizeObserver(() => {
			if (document.documentElement.dataset.drawerAnimating !== '1') update();
		});
		ro.observe(el);
	};
	attachObserver();
	schedule(attachObserver);
	return () => {
		window.removeEventListener('resize', update);
		window.visualViewport?.removeEventListener('resize', update);
		ro?.disconnect();
	};
}
