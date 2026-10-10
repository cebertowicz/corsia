import { StateEffect, type EditorState, type Range, type Transaction } from '@codemirror/state';
import { Decoration, WidgetType } from '@codemirror/view';
import { agentColor } from '$lib/docPresence';
import { ghostHoldMs, hashSeed, mulberry32, selectSweepMs } from '$lib/typingPace';
import type { LiveUpdateAuthor } from '$lib/stores/liveUpdateAuthor';
import { hiddenCount, hiddenText, type Segment } from './typed-reveal-segments';
import { GHOST_MAX_CHARS, type Selecting } from './typed-reveal-select';
import type { Lane } from './typed-reveal-state';

/**
 * An agent edit still being generated, typed in its agent's lane before it exists.
 * Presentation only: the old text in [from, to) is covered by what has been typed of
 * `text`, which is never document text, so it cannot be selected, copied, edited or
 * saved. The lane paces it with its hidden text; the commit takes it over or drops it.
 */
export interface Preview {
	callId: string;
	slot: number;
	from: number;
	to: number;
	/** Everything streamed for this slot so far. */
	text: string;
	/** How much of `text` has typed in. */
	typed: number;
	/** The select step this preview began on its old text, if it got one. */
	selectKey: string | null;
}

/** A slot's text so far, placed at [from, to) in this editor's text; a null range only updates a placed slot. */
export interface PreviewSlot {
	callId: string; threadId: string; name: string; slot: number; from: number | null; to: number | null; text: string;
}
export const previewSlot = StateEffect.define<PreviewSlot>();
/** The call ended for any reason: its preview goes at once. */
export const endPreview = StateEffect.define<{ callId: string }>();

/** Hidden text and untyped preview text together: what the lane still has to type. */
export const backlogCount = (lane: Lane) =>
	hiddenCount(lane.segments) + lane.previews.reduce((sum, preview) => sum + preview.text.length - preview.typed, 0);

export const backlogText = (state: EditorState, lane: Lane) =>
	hiddenText(state, lane.segments) + lane.previews.map((preview) => preview.text.slice(preview.typed)).join('');

/** Type `count` more preview characters, in arrival order. */
export function typePreviews(previews: readonly Preview[], count: number): Preview[] {
	let left = count;
	return previews.map((preview) => {
		const take = Math.max(0, Math.min(left, preview.text.length - preview.typed));
		left -= take;
		return take ? { ...preview, typed: preview.typed + take } : preview;
	});
}

export function withPreviewSlot(lanes: readonly Lane[], value: PreviewSlot,
	state: EditorState, inView: (from: number, to: number) => boolean): Lane[] {
	const own = lanes.find((lane) => lane.threadId === value.threadId);
	const existing = own?.previews.find((preview) => preview.callId === value.callId && preview.slot === value.slot);
	if (!existing && value.from === null) return [...lanes];
	const length = state.doc.length;
	const from = value.from === null ? existing!.from : Math.min(Math.max(0, value.from), length);
	const to = value.to === null ? existing!.to : Math.min(Math.max(from, value.to), length);
	const lane: Lane = own ?? {
		threadId: value.threadId, name: value.name, color: agentColor(value.threadId),
		segments: [], caret: null, generation: 0, typing: false, quiet: false, select: null, previews: [],
	};
	const grew = value.text.length > (existing?.text.length ?? 0);
	let select = lane.select;
	let selectKey = existing?.selectKey ?? null;
	if (!existing && !select && to > from && to - from <= GHOST_MAX_CHARS && inView(from, to)) {
		selectKey = `preview:${value.callId}:${value.slot}`;
		select = {
			key: selectKey, from, to, ghost: null, shown: 0, length: to - from, sweepMs: selectSweepMs(to - from),
			holdMs: ghostHoldMs(mulberry32(hashSeed(`${value.threadId}:${from}:${to - from}`))),
		} satisfies Selecting;
	}
	const preview: Preview = { callId: value.callId, slot: value.slot, from, to, text: value.text,
		typed: Math.min(existing?.typed ?? 0, value.text.length), selectKey };
	const previews = existing ? lane.previews.map((other) => other === existing ? preview : other) : [...lane.previews, preview];
	const next: Lane = { ...lane, name: value.name, select, previews, generation: lane.generation + (grew ? 1 : 0), quiet: false };
	return [...lanes.filter((other) => other !== own), next];
}

/** The lane without one call's previews, and without the select step a preview of it began. */
export function withoutCall(lane: Lane, callId: string): Lane {
	const ending = lane.previews.filter((preview) => preview.callId === callId);
	if (!ending.length) return lane;
	const select = lane.select && ending.some((preview) => preview.selectKey === lane.select!.key) ? null : lane.select;
	return { ...lane, select, previews: lane.previews.filter((preview) => preview.callId !== callId) };
}

/**
 * Carry previews through a change. The ends map outward, so a commit replacing the old
 * text leaves the preview spanning the text that replaced it. The user acting inside a
 * preview drops it there (map rules 6-9).
 */
export function carriedPreviews(previews: readonly Preview[], transaction: Transaction,
	touched: (segment: Segment) => boolean): Preview[] {
	if (!previews.length || (!transaction.docChanged && !transaction.selection)) return [...previews];
	return previews.flatMap((preview) => {
		if (touched({ start: preview.from, from: preview.from, to: preview.to })) return [];
		const from = transaction.changes.mapPos(preview.from, -1);
		return [{ ...preview, from, to: Math.max(from, transaction.changes.mapPos(preview.to, 1)) }];
	});
}

/**
 * A commit naming a previewed call takes its previews over: where the committed text
 * begins with what was streamed, only the part not yet typed is hidden, so nothing types
 * twice. Otherwise the preview is dropped and the commit reveals as any agent write.
 */
export function handOver(lane: Lane, author: LiveUpdateAuthor, state: EditorState): { lane: Lane; claimed: Segment[] } {
	if (!author.callId) return { lane, claimed: [] };
	const claimed: Segment[] = [];
	for (const preview of lane.previews) {
		if (preview.callId !== author.callId) continue;
		if (state.doc.sliceString(preview.from, preview.to).startsWith(preview.text)) {
			claimed.push({ start: preview.from, from: preview.from + preview.typed, to: preview.to });
		}
	}
	return { lane: withoutCall(lane, author.callId), claimed };
}

/** Typed preview text, drawn over the old text it replaces. */
export class PreviewText extends WidgetType {
	constructor(private readonly text: string, private readonly color: string, private readonly threadId: string) { super(); }

	eq(other: PreviewText): boolean {
		return this.text === other.text && this.color === other.color && this.threadId === other.threadId;
	}

	toDOM(): HTMLElement {
		const span = document.createElement('span');
		span.className = 'cm-presence-preview cm-presence-arrival';
		span.style.setProperty('--presence-color', this.color);
		span.dataset.threadId = this.threadId;
		span.textContent = this.text;
		return span;
	}

	ignoreEvent(): boolean { return true; }
}

/** Where the lane's caret sits while a preview types: after the preview typing now. */
export function previewCaret(lane: Lane): number | null {
	const typing = lane.previews.find((preview) => preview.typed < preview.text.length && preview.selectKey !== lane.select?.key);
	const shown = typing ?? lane.previews.filter((preview) => preview.selectKey !== lane.select?.key).at(-1);
	return shown ? shown.to : null;
}

export function previewMarks(lane: Lane): Array<Range<Decoration>> {
	return lane.previews.flatMap((preview) => {
		if (lane.select && preview.selectKey === lane.select.key) return [];
		const widget = new PreviewText(preview.text.slice(0, preview.typed), lane.color, lane.threadId);
		return preview.to > preview.from
			? [Decoration.replace({ widget }).range(preview.from, preview.to)]
			: [Decoration.widget({ widget, side: -2 }).range(preview.from)];
	});
}
