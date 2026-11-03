import { ArrivalQueue, setPresenceHighlight, type DocActivityFrame, type DocSpan } from '$lib/docPresence';
import { releaseCommentArrival } from '$lib/stores/docCommentArrivals';
import type { SelectOutcome } from '$lib/cm/typed-reveal-arrival';

export interface PreparedPresenceArrival {
	chars: number;
	/** Display time before the reveal when no editor runs the select (sweep plus hold); defaults to a delay scaled by `chars`. */
	delayMs?: number;
	payload?: unknown;
	highlightSpan?: DocSpan | null;
}

export interface DocPresenceSurface {
	isViewing: () => boolean;
	prepare: (frame: DocActivityFrame) => Promise<PreparedPresenceArrival>;
	reveal: (frame: DocActivityFrame, payload: unknown) => void | Promise<void>;
	/**
	 * Run the arrival's select step in this surface's editor and report how it ended, or null when no
	 * editor can run one. When it runs it is the arrival's only highlight and clock.
	 */
	select?: (frame: DocActivityFrame, prepared: PreparedPresenceArrival) => Promise<SelectOutcome> | null;
	reducedMotion?: () => boolean;
	/** False while this surface's live text is away (opening or reconnecting), when nothing can type in it; true when it has none. */
	textCurrent?: () => boolean;
}

export interface RuntimeArrival {
	frame: DocActivityFrame;
	prepared?: PreparedPresenceArrival;
	failed?: boolean;
}

/** What one document's lanes read from it: the surface that presents arrivals now, and whether motion is reduced. */
export interface LaneHost {
	artifactId: string;
	activeSurface: () => DocPresenceSurface | null;
	reducedMotion: () => boolean;
}

function release(artifactId: string, arrival: RuntimeArrival): void {
	if ((arrival.frame.kind !== 'comment' && arrival.frame.kind !== 'suggestion') || !arrival.frame.comment_id) return;
	releaseCommentArrival(artifactId, arrival.frame.comment_id);
}

/** One agent's arrival lane on a document: prepare, select, highlight and reveal each arrival on the surface viewing it. */
export function createArrivalLane(host: LaneHost, threadId: string): ArrivalQueue<RuntimeArrival> {
	const { artifactId } = host;
	return new ArrivalQueue<RuntimeArrival>({
		isViewing: () => host.activeSurface() !== null,
		reducedMotion: () => host.reducedMotion(),
		paused: (waiting) => waiting.forEach((arrival) => release(artifactId, arrival)),
		prepare: async (arrival) => {
			const surface = host.activeSurface();
			if (!surface) return { value: arrival, chars: 0 };
			try {
				arrival.prepared = await surface.prepare(arrival.frame);
				return { value: arrival, chars: arrival.prepared.chars, delayMs: arrival.prepared.delayMs };
			} catch (error) {
				arrival.failed = true;
				console.error('document arrival preparation failed', error);
				return { value: arrival, chars: 0 };
			}
		},
		select: (arrival) => {
			if (arrival.failed || !arrival.prepared) return null;
			return host.activeSurface()?.select?.(arrival.frame, arrival.prepared) ?? null;
		},
		highlight: (arrival, selecting) => {
			if (arrival.failed) return;
			// The editor draws a select it runs itself; the presence mark would be a second highlight.
			if (selecting) { setPresenceHighlight(artifactId, threadId, null, true); return; }
			const span = arrival.prepared && 'highlightSpan' in arrival.prepared
				? arrival.prepared.highlightSpan ?? null
				: arrival.frame.span;
			setPresenceHighlight(artifactId, threadId, span, true, arrival.frame.kind);
		},
		reveal: async (arrival) => {
			try {
				if (arrival.failed) return;
				const surface = host.activeSurface();
				if (surface) await surface.reveal(arrival.frame, arrival.prepared?.payload);
			} finally {
				release(artifactId, arrival);
			}
		},
		clear: (arrival) => {
			setPresenceHighlight(artifactId, threadId, null, false);
			release(artifactId, arrival);
		},
	});
}
