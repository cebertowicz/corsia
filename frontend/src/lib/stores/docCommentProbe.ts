import { type DocComment, rangeFor } from './docCommentProjection';
import { textDigest, type DocumentFrame } from './documentWire';
import type { LiveDocumentSession } from './liveDocumentSession';

/** An unplaced comment: anchored, open, and its quote is not where the browser holds it (never one still waiting for its first place). */
const unplaced = (row: DocComment, text: string) => !!row.anchor && !row.review && !row.resolved && rangeFor(row, text) === null
	&& !((row.placement === 'placing' || row.placement === 'not_placed') && !row.drawn);

/**
 * Asks the server, in a read-only probe of its own re-anchor rule, which unplaced comments Place again could place,
 * so the drawer offers it only for those. Composed by one live document's placement; it keeps no timer: a probe is
 * asked only when the tab holds exactly the server's text (clean) and something is unplaced, one at a time, and an
 * answer for text that has since changed is dropped. A comment without an answer, or after a failed probe, reads as
 * re-placeable, which is the drawer as it was before probes.
 */
export class ReplaceableProbe {
	private seq = 0;
	private inFlight: { requestId: string; generation: number } | null = null;
	private again = false;
	private destroyed = false;

	constructor(
		private readonly session: LiveDocumentSession,
		private readonly read: () => DocComment[],
		private readonly write: (change: (rows: DocComment[]) => DocComment[]) => void,
	) {}

	/** The text or the comments may have changed: probe if the text has settled and a comment is unplaced. */
	want(): void {
		if (this.destroyed || this.session.connection !== 'connected' || !this.session.durability.clean) return;
		if (this.inFlight) { this.again = true; return; }
		if (!this.read().some(row => unplaced(row, this.session.text.toString()))) return;
		const requestId = this.session.requestProbe(++this.seq);
		if (requestId) this.inFlight = { requestId, generation: this.session.textGeneration };
	}

	/** True when the frame is this probe's answer or refusal. */
	frame(frame: DocumentFrame): boolean {
		const probe = this.inFlight;
		if (!probe || frame.request_id !== probe.requestId || (frame.type !== 'doc_positions' && frame.type !== 'doc_error')) return false;
		this.inFlight = null;
		if (frame.type === 'doc_error') {
			this.again = false;
			this.write(rows => rows.map(row => row.replaceable === undefined ? row : { ...row, replaceable: undefined }));
		} else void this.answered(frame, probe.generation);
		return true;
	}

	/** A disconnect drops the request; the reconnect's `doc_opened` asks again. */
	lost(): void {
		this.inFlight = null;
		this.again = false;
	}

	destroy(): void {
		this.destroyed = true;
		this.lost();
	}

	private async answered(frame: DocumentFrame, generation: number): Promise<void> {
		const { session } = this;
		const text = session.text.toString();
		const digest = await textDigest(text);
		const current = !this.destroyed && session.textGeneration === generation && digest === frame.digest && session.durability.clean;
		if (current) {
			const found = new Set((frame.ranges ?? []).map(range => range.id));
			this.write(rows => rows.map(row => {
				const replaceable = unplaced(row, text) ? found.has(row.id) : undefined;
				return replaceable === row.replaceable ? row : { ...row, replaceable };
			}));
		}
		// Text that moved under the question is asked again; a digest that disagrees with the same text is not retried here.
		const again = this.again || session.textGeneration !== generation;
		this.again = false;
		if (again) this.want();
	}
}
