import type { EditorView } from '@codemirror/view';
import type { DocActivityFrame, DocSpan } from '$lib/docPresence';

export type ActivityPosition = { direction: 'above' | 'below' | 'visible'; distance: number };
export type LocateActivity = (from: number, to: number) => ActivityPosition | null;
export type ActivityPill = { direction: 'above' | 'below'; count: number; span: DocSpan };

/** Ephemeral presentation only: no unread flag persists beyond this viewing session. */
export class UnseenDocActivity {
	private entries = new Map<string, DocSpan>();
	private recorded = new Set<string>();
	add(frame: DocActivityFrame, locate: LocateActivity): void {
		const key = JSON.stringify([frame.artifact_id, frame.thread_id, frame.kind, frame.at, frame.comment_id, frame.span]);
		if (this.recorded.has(key)) return;
		this.recorded.add(key);
		if (!frame.span) return;
		const location = locate(frame.span.from, frame.span.to);
		if (location && location.direction !== 'visible') this.entries.set(key, { ...frame.span });
	}
	seen(locate: LocateActivity): void {
		for (const [key, span] of this.entries) {
			const location = locate(span.from, span.to);
			if (!location || location.direction === 'visible') this.entries.delete(key);
		}
	}
	map(mapPos: (position: number) => number): void {
		for (const [key, span] of this.entries) this.entries.set(key, { from: mapPos(span.from), to: mapPos(span.to) });
	}
	clear(): void { this.entries.clear(); this.recorded.clear(); }
	pill(locate: LocateActivity): ActivityPill | null {
		const positioned = [...this.entries.values()].map((span) => ({ span, location: locate(span.from, span.to) }))
			.filter((item): item is { span: DocSpan; location: ActivityPosition } => !!item.location && item.location.direction !== 'visible')
			.sort((a, b) => a.location.distance - b.location.distance || Number(a.location.direction === 'above') - Number(b.location.direction === 'above'));
		const nearest = positioned[0];
		if (!nearest || nearest.location.direction === 'visible') return null;
		return { direction: nearest.location.direction, count: positioned.filter((item) => item.location.direction === nearest.location.direction).length, span: nearest.span };
	}
}

/** CodeMirror owns text layout; off-screen lines need its virtual block geometry. */
export function locateDocumentActivity(view: EditorView, clip: DOMRect, from: number, to: number): ActivityPosition | null {
	if (from < 0 || to < from || to > view.state.doc.length || clip.height <= 0) return null;
	const scroll = view.scrollDOM.getBoundingClientRect();
	const top = Math.max(scroll.top, clip.top), bottom = Math.min(scroll.bottom, clip.bottom);
	if (bottom <= top) return null;
	const start = view.coordsAtPos(from);
	const end = view.coordsAtPos(to);
	const spanTop = start?.top ?? view.documentTop + view.lineBlockAt(from).top;
	const spanBottom = end?.bottom ?? view.documentTop + view.lineBlockAt(to).bottom;
	if (spanBottom <= top) return { direction: 'above', distance: top - spanBottom };
	if (spanTop >= bottom) return { direction: 'below', distance: spanTop - bottom };
	return { direction: 'visible', distance: 0 };
}
