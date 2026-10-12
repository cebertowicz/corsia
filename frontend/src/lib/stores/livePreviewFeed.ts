import type { Text } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { endPreview, previewSlot, type PreviewMode } from '$lib/cm/typed-reveal-preview';
import { textDigest, type DocumentFrame } from './documentWire';

/** Anything that delivers this document's frames; the live document session does. */
export interface PreviewFrameSource {
	onFrame(listener: (frame: DocumentFrame) => void): () => void;
}

interface PreviewPart { slot: number; from: number | null; to: number | null; at: number; text: string }
interface PreviewFrame {
	type: string; call_id?: unknown; thread_id?: unknown; name?: unknown; kind?: unknown; digest?: unknown; slots?: unknown; outcome?: unknown;
}

const MODES = new Set<unknown>(['edit', 'comment', 'suggest']);

/**
 * How long a comment or suggestion preview stays after its call settles, for that call's arrival (a
 * separate channel, in no fixed order with this end) to take it over.
 */
export const CLAIM_GRACE_MS = 5_000;

function parts(value: unknown): PreviewPart[] | null {
	if (!Array.isArray(value)) return null;
	const valid = value.every((part) => part && typeof part === 'object' && Number.isInteger(part.slot) && Number.isInteger(part.at)
		&& typeof part.text === 'string' && (part.from === null || Number.isInteger(part.from)) && (part.to === null || Number.isInteger(part.to)));
	return valid ? value as PreviewPart[] : null;
}

/**
 * Feed a document's streamed edit previews into its editor's typing lanes. Each slot's
 * text is joined here in arrival order, whatever happens to placement; a frame's ranges
 * are used only when it was computed on exactly the text this editor shows (its digest),
 * so a slot waits rather than landing on the wrong characters. An end applies at once
 * (a settled comment or suggestion after its claim grace), and nothing for that call is
 * drawn after it. A frame names what the call makes; an edit when it names nothing.
 */
export function attachPreviewFeed(view: EditorView, source: PreviewFrameSource): () => void {
	const texts = new Map<string, string>();
	const ended = new Set<string>();
	/** Calls whose queued frames must not be placed: every end but a settled comment or suggestion, which a queued frame may still reach. */
	const cancelled = new Set<string>();
	const modes = new Map<string, PreviewMode>();
	const graces = new Set<ReturnType<typeof setTimeout>>();
	let digested: { doc: Text; digest: Promise<string> } | null = null;
	let queue: Promise<void> = Promise.resolve();
	let stopped = false;

	const digestOf = (doc: Text) => {
		if (digested?.doc !== doc) digested = { doc, digest: textDigest(doc.toString()) };
		return digested.digest;
	};

	const finish = (callId: string) => {
		cancelled.add(callId);
		for (const key of [...texts.keys()]) if (key.startsWith(`${callId}\n`)) texts.delete(key);
		modes.delete(callId);
		view.dispatch({ effects: endPreview.of({ callId }) });
	};

	const end = (callId: string, outcome?: unknown) => {
		ended.add(callId);
		const mode = modes.get(callId);
		if (outcome === 'settled' && (mode === 'comment' || mode === 'suggest')) {
			// Its text and kind stay too: a frame still queued for placement is drawn, not lost to a fast settle.
			const timer = setTimeout(() => {
				graces.delete(timer);
				finish(callId);
			}, CLAIM_GRACE_MS);
			graces.add(timer);
			return;
		}
		finish(callId);
	};

	const place = async (callId: string, threadId: string, name: string, digest: string, slots: PreviewPart[]) => {
		const doc = view.state.doc;
		const matches = (await digestOf(doc)) === digest && view.state.doc === doc;
		if (stopped || cancelled.has(callId)) return;
		const effects = slots.flatMap((part) => {
			const text = texts.get(`${callId}\n${part.slot}`);
			if (text === undefined) return [];
			const placed = matches && part.from !== null && part.to !== null;
			return [previewSlot.of({ callId, threadId, name, slot: part.slot, text, mode: modes.get(callId),
				from: placed ? part.from : null, to: placed ? part.to : null })];
		});
		if (effects.length) view.dispatch({ effects });
	};

	const stop = source.onFrame((raw) => {
		const frame = raw as unknown as PreviewFrame;
		if (frame.type !== 'doc_preview' && frame.type !== 'doc_preview_end') return;
		const callId = frame.call_id;
		if (typeof callId !== 'string' || !callId || ended.has(callId)) return;
		if (frame.type === 'doc_preview_end') { end(callId, frame.outcome); return; }
		const slots = parts(frame.slots);
		const { thread_id: threadId, name, digest, kind = 'edit' } = frame;
		if (!slots || typeof threadId !== 'string' || typeof name !== 'string' || typeof digest !== 'string' || !MODES.has(kind)) return;
		// The newest kind wins, so slots still queued under an earlier one are placed in the call's current form.
		modes.set(callId, kind as PreviewMode);
		for (const part of slots) {
			const key = `${callId}\n${part.slot}`;
			const sofar = texts.get(key) ?? '';
			// `at` counts UTF-16 units, as string length does. A piece that does not continue exactly
			// where the last one ended would draw a gap.
			if (part.at !== sofar.length) { end(callId); return; }
			texts.set(key, sofar + part.text);
		}
		queue = queue.then(() => place(callId, threadId, name, digest, slots)).catch((error) => console.error('document preview failed', error));
	});

	return () => {
		stopped = true;
		stop();
		graces.forEach((timer) => clearTimeout(timer));
		graces.clear();
	};
}
