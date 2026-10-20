import * as Y from 'yjs';
import { get, type Writable } from 'svelte/store';
import type { DocComment } from './docComments';
import { encodeUpdate, textDigest, type DocumentFrame } from './documentWire';
import { liveDocumentBindings } from './liveDocuments';
import type { LiveDocumentSession } from './liveDocumentSession';
import { ReplaceableProbe } from './docCommentProbe';

/** A live row's placement (4.2). Rows without one are not live, or have no place the server tracks. */
export type Placement = 'placed' | 'placing' | 'not_placed';
type Rows = Writable<Record<string, DocComment[]>>;
type Positions = { from: Y.RelativePosition; to: Y.RelativePosition };
type Range = NonNullable<DocumentFrame['ranges']>[number];

/** The rows the server keeps a live range for: anchored, top level, not a review comment, not orphaned. */
const placeable = (row: DocComment) => !row.review && !!row.anchor && !row.orphaned;
const unplaced = (row: DocComment): DocComment => ({ ...row, placement: undefined, drawn: false });

/**
 * One live document's placement. Composed only by the comments store, and writes only its rows:
 * there is no second collection, timer or retry registry. Cleanliness is read from the session's
 * durability bookkeeping (4.1), never kept here.
 */
class LivePlacement {
	readonly session: LiveDocumentSession;
	private rows: Rows;
	private seq = 0;
	private inFlight: { seq: number; requestId: string; reload: boolean } | null = null;
	private marked: { reload: boolean } | null = null;
	private positions = new Map<string, Positions>();
	private server = new Map<string, string>();
	private clean: boolean;
	private destroyed = false;
	private stops: (() => void)[];
	private probe: ReplaceableProbe;

	constructor(session: LiveDocumentSession, rows: Rows) {
		this.session = session;
		this.rows = rows;
		this.clean = session.durability.clean;
		session.text.observe(this.moved);
		this.probe = new ReplaceableProbe(session, () => this.current(), change => this.write(change));
		this.stops = [session.onFrame(frame => this.frame(frame)), session.subscribe(() => this.observed())];
		if (this.current().some(placeable)) {
			this.write(rows => rows.map(row => placeable(row) ? { ...row, placement: 'placing', drawn: false } : row));
			this.ask();
		}
		this.probe.want();
	}

	holds(id: string): boolean { return this.current().some(row => row.id === id); }

	/** A comments load: rows whose server range is new or changed, or that are not placed, are asked for. */
	show(next: DocComment[]): void {
		const previous = new Map(this.current().map(row => [row.id, row]));
		let ask = false;
		const rows = next.map(row => {
			const old = previous.get(row.id);
			if (!placeable(row)) return old?.placementReloading || old?.placementReloadError
				? { ...row, placementReloading: old.placementReloading, placementReloadError: old.placementReloadError } : row;
			const key = `${row.from}:${row.to}:${row.anchor}`, changed = this.server.get(row.id) !== key;
			this.server.set(row.id, key);
			const drawn = old?.drawn ? { drawn: true, from: old.from, to: old.to } : { drawn: false };
			const reload = old?.placementReloading || old?.placementReloadError
				? { placementReloading: old.placementReloading, placementReloadError: old.placementReloadError } : {};
			if (!changed && old?.placement === 'placed' && old.drawn) return { ...row, ...drawn, ...reload, placement: 'placed' as const };
			ask = true;
			return { ...row, ...drawn, ...reload, placement: 'placing' as const };
		});
		const ids = new Set(next.map(row => row.id));
		for (const id of [...this.positions.keys()]) if (!ids.has(id)) this.positions.delete(id);
		this.rows.update(all => ({ ...all, [this.session.artifactId]: rows.map(row => previous.get(row.id)?.replaceable === undefined ? row
			: { ...row, replaceable: previous.get(row.id)!.replaceable }) }));
		if (ask) this.ask();
		this.probe.want();
	}

	retry(): void {
		this.write(rows => rows.map(row => row.placement === 'not_placed' ? { ...row, placement: 'placing' } : row));
		this.ask(true);
	}

	/** Re-resolve the live document's orphaned quotes through this placement lifecycle. */
	reload(): boolean {
		if (this.destroyed || this.session.connection !== 'connected' || this.current().some(row => row.placementReloading)) return false;
		this.markReloading();
		this.ask(true);
		return true;
	}

	destroy(): void {
		this.destroyed = true;
		this.probe.destroy();
		this.settleReload();
		for (const stop of this.stops) stop();
		this.session.text.unobserve(this.moved);
		this.positions.clear();
		this.write(rows => rows.map(row => ({ ...(row.placement || row.drawn ? unplaced(row) : row), replaceable: undefined })));
	}

	private current(): DocComment[] { return get(this.rows)[this.session.artifactId] ?? []; }

	private write(change: (rows: DocComment[]) => DocComment[]): void {
		const id = this.session.artifactId;
		this.rows.update(all => all[id] ? { ...all, [id]: change(all[id]) } : all);
	}

	private markReloading(): void {
		this.write(rows => rows.map(row => row.anchor && !row.review
			// A row with no placement has never been drawn; keep its orphan eligibility while Reload is in flight.
			? {
				...row,
				placement: row.placement === undefined && !row.drawn ? undefined : 'placing',
				placementReloading: true,
				placementReloadError: false,
			}
			: row));
	}

	private ask(reload = false): void {
		if (this.inFlight) { this.marked = { reload: reload || !!this.marked?.reload }; return; }
		const seq = ++this.seq;
		const requestId = this.session.requestPositions(seq, reload);
		if (requestId) this.inFlight = { seq, requestId, reload };
		else if (reload) this.settleReload();
	}

	/** Sends the marked ask, if any (or a fresh one when `force`). */
	private next(force = false): void {
		const marked = this.marked;
		this.marked = null;
		if (marked || force) this.ask(!!marked?.reload);
	}

	/** A disconnect releases the request; rows keep their state and reconnect sync asks again. */
	private observed(): void {
		if (this.session.connection === 'connected') return;
		this.probe.lost();
		const reload = this.inFlight?.reload || this.marked?.reload;
		if (reload) this.settleReload();
		this.inFlight = null;
		this.marked = reload ? { reload: true } : null;
	}

	private frame(frame: DocumentFrame): void {
		if (this.probe.frame(frame)) return;
		this.placementFrame(frame);
		if (frame.type === 'doc_opened' || frame.type === 'doc_ack' || frame.type === 'doc_update') this.probe.want();
	}

	private placementFrame(frame: DocumentFrame): void {
		if (frame.type === 'doc_positions') {
			// Only the answer to the request in flight releases it; another store never reaches here.
			const request = this.inFlight;
			if (!request || request.seq !== frame.seq || request.requestId !== frame.request_id) return;
			this.inFlight = null;
			void this.answered(frame, request.reload);
		} else if (frame.type === 'doc_error') {
			if (!this.inFlight || frame.request_id !== this.inFlight.requestId || frame.kind === 'store_mismatch') return;
			const request = this.inFlight;
			this.inFlight = null;
			this.write(rows => rows.map(row => row.placement === 'placing'
				? { ...row, placement: 'not_placed', placementReloadError: request.reload ? row.placementReloading : row.placementReloadError }
				: request.reload && row.placementReloading ? { ...row, placementReloadError: true } : row));
			if (request.reload) this.settleReload();
			this.next();
		} else if (frame.type === 'doc_opened') {
			if (this.current().some(row => row.placement === 'placing' || row.placement === 'not_placed') || this.marked?.reload) {
				this.write(rows => rows.map(row => row.placement === 'not_placed' ? { ...row, placement: 'placing' } : row));
				if (this.marked?.reload) this.markReloading();
				this.next(true);
			}
		} else if (frame.type === 'doc_ack') {
			const became = this.session.durability.clean && !this.clean;
			this.clean = this.session.durability.clean;
			if (became && this.current().some(row => row.placement === 'placing')) this.ask();
		}
		if (frame.type !== 'doc_ack') this.clean = this.session.durability.clean;
	}

	/**
	 * One snapshot: the digest, the offset conversion and the cleanliness reading all describe the
	 * same state. A transaction, update or ack landing while the hash runs discards the answer.
	 */
	private async answered(frame: DocumentFrame, reload: boolean): Promise<void> {
		const { session } = this, generation = session.textGeneration, before = session.durability.snapshot();
		const text = session.text.toString();
		const digest = await textDigest(text);
		if (this.destroyed) return;
		if (frame.seq !== this.seq) {
			if (reload && !this.inFlight?.reload && !this.marked?.reload) {
				this.marked = { reload: true };
				if (!this.inFlight) this.next();
			}
			return;
		}
		const after = session.durability.snapshot();
		if (session.textGeneration !== generation || after.localGeneration !== before.localGeneration
			|| after.ackGeneration !== before.ackGeneration) {
			if (reload) this.marked = { reload: true };
			this.next(true);
			return;
		}
		if (digest === frame.digest) this.place(frame.ranges ?? [], text.length, reload);
		else if (session.durability.clean) {
			// The ordering rule says a clean tab holds exactly the answer's text, so this is a defect, reported once.
			console.error('Comment placement: the server text differs from a clean tab', {
				artifact_id: session.artifactId, store_id: session.storeId, seq: frame.seq,
				server_digest: frame.digest, browser_digest: digest,
				server_vector: frame.vector, browser_vector: encodeUpdate(Y.encodeStateVector(session.doc)),
			});
			this.write(rows => rows.map(row => row.placement === 'placing' ? { ...row, placement: 'not_placed' } : row));
		}
		// Otherwise the tab holds local-only changes: rows stay Placing until it next becomes clean.
		if (reload) {
			this.clearReloadErrors();
			this.settleReload();
		}
		this.next();
	}

	private place(ranges: Range[], length: number, reload = false): void {
		const found = new Map(ranges.filter(range => 0 <= range.from_off && range.from_off <= range.to_off && range.to_off <= length)
			.map(range => [range.id, range]));
		const keep = this.marked !== null; // a newer answer decides rows this one does not name
		const { text } = this.session;
		for (const [id, range] of found) {
			// Exclusive boundaries: text typed exactly at either edge lands outside the range.
			this.positions.set(id, { from: Y.createRelativePositionFromTypeIndex(text, range.from_off, 0),
				to: Y.createRelativePositionFromTypeIndex(text, range.to_off, -1) });
		}
		this.write(rows => rows.map(row => {
			if (!(reload ? row.anchor && !row.review : placeable(row))) return row;
			const range = found.get(row.id);
			if (range) return { ...row, placement: 'placed', drawn: true, from: range.from_off, to: range.to_off };
			if (keep) return row;
			this.positions.delete(row.id);
			return { ...unplaced(row), from: undefined, to: undefined };
		}));
		this.probe.want();
	}

	private clearReloadErrors(): void {
		this.write(rows => rows.some(row => row.placementReloading && row.placementReloadError)
			? rows.map(row => row.placementReloading ? { ...row, placementReloadError: false } : row)
			: rows);
	}

	private settleReload(): void {
		this.write(rows => rows.some(row => row.placementReloading)
			? rows.map(row => row.placementReloading ? { ...row, placementReloading: false } : row)
			: rows);
	}

	/** Drawn rows move only by their own relative positions, on every change to the text. */
	private moved = (): void => {
		if (!this.positions.size) return;
		const { doc } = this.session;
		this.write(rows => rows.map(row => {
			const at = row.drawn ? this.positions.get(row.id) : undefined;
			if (!at) return row;
			const from = Y.createAbsolutePositionFromRelativePosition(at.from, doc)?.index;
			const to = Y.createAbsolutePositionFromRelativePosition(at.to, doc)?.index;
			return from === undefined || to === undefined || (from === row.from && to === row.to) ? row : { ...row, from, to };
		}));
	};
}

/** Follows the live sessions; a reset or closed document drops its positions and Placing state. */
export function trackPlacements(rows: Rows) {
	const live = new Map<string, LivePlacement>();
	liveDocumentBindings.subscribe(bindings => {
		const sessions = new Map(Object.values(bindings).map(session => [session.artifactId, session]));
		for (const [id, placement] of live) {
			if (sessions.get(id) !== placement.session) { placement.destroy(); live.delete(id); }
		}
		for (const [id, session] of sessions) if (!live.has(id)) live.set(id, new LivePlacement(session, rows));
	});
	return {
		show(artifactId: string, next: DocComment[]): void {
			const placement = live.get(artifactId);
			if (placement) placement.show(next);
			else rows.update(all => ({ ...all, [artifactId]: next }));
		},
		retry(commentId: string): void {
			for (const placement of live.values()) if (placement.holds(commentId)) placement.retry();
		},
		reload(artifactId: string): boolean {
			return live.get(artifactId)?.reload() ?? false;
		},
	};
}
