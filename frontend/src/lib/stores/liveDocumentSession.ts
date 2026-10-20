import * as Y from 'yjs';
import { writable } from 'svelte/store';
import type { DocOpenRefusal } from '$lib/docOpenRefusal';
import { DocumentDurability } from './documentDurability';
import type { DeviceDocument } from './deviceDocument';
import type { DeviceRecovery } from './deviceRecovery';
import { liveUpdateAuthor, withLiveUpdateAuthor } from './liveUpdateAuthor';
import { decodeUpdate, encodeUpdate, type DocumentFrame, type HeldDocument, type PositionReservation } from './documentWire';

import { publicFailureMessage, LiveOperationError, type BlockingFailure } from './documentFailures';
import { DocumentFailureLedger } from './documentFailureLedger';

const SERVER = Symbol('server-document-update');
type Send = (frame: Record<string, unknown>) => boolean;
type Request = { resolve: (frame: DocumentFrame) => void; reject: (error: Error) => void; reservation?: boolean; landed?: () => void };

export class LiveDocumentSession {
	readonly doc: Y.Doc;
	readonly text: Y.Text;
	readonly durability: DocumentDurability;
	readonly artifactId: string;
	readonly storeId: string;
	writable: boolean;
	device: DeviceDocument | null = null;
	deviceError = '';
	recovery: DeviceRecovery[] = [];
	private stopDevice: (() => void) | null = null;
	connection: 'opening' | 'connected' | 'disconnected' | 'reconnecting' | 'refused' = 'opening';
	/** Set while a reopen was refused; reopening clears it. */
	refusal: DocOpenRefusal | null = null;
	storeError = '';
	fileError = '';
	private operationErrorKind: string | undefined;
	private ledger = new DocumentFailureLedger();
	held: HeldDocument | null = null;
	pending = false;
	textGeneration = 0;
	private vector: Uint8Array = new Uint8Array([0]);
	private send: Send;
	private updates = new Map<string, Record<string, unknown>>();
	private requests = new Map<string, Request>();
	private frameListeners = new Set<(frame: DocumentFrame) => void>();
	private state = writable<LiveDocumentSession>(this);
	readonly subscribe = this.state.subscribe;

	constructor(options: { artifactId: string; storeId: string; doc: Y.Doc; writable: boolean; send: Send }) {
		this.doc = options.doc;
		this.text = this.doc.getText('text');
		this.artifactId = options.artifactId;
		this.storeId = options.storeId;
		this.writable = options.writable;
		this.send = options.send;
		this.durability = new DocumentDurability(this.storeId);
		this.doc.on('update', this.updated);
	}

	private changed(): void { this.state.set(this); }

	get blockingFailures(): BlockingFailure[] {
		return this.ledger.rows;
	}

	get failureLines(): string[] {
		const store = this.ledger.message('store_refused') ?? this.storeError;
		const lines = store ? [`Not saved on the server: ${store}`] : [];
		if (this.fileError) lines.push(this.operationErrorKind === 'publication_failed' || this.operationErrorKind === 'setup_failed'
			? this.fileError : `Not saved to the file: ${this.fileError}`);
		for (const row of this.blockingFailures) if (row.kind !== 'store_refused' && !lines.includes(row.message)) lines.push(row.message);
		return lines;
	}

	/** Only the canonical matched successful open invokes this admission. */
	admitFailures(frame: DocumentFrame): void {
		if (frame.type !== 'doc_opened' || frame.live !== true || frame.artifact_id !== this.artifactId || frame.store_id !== this.storeId) return;
		this.ledger.admitIncarnation(frame.setup_incarnation);
		for (const clear of frame.failure_clears ?? []) this.ledger.clear(clear);
		if (Array.isArray(frame.blocking)) for (const row of frame.blocking) this.ledger.observe(row);
		this.changed();
	}

	attachDevice(copy: DeviceDocument, recovery: DeviceRecovery[], error = ''): void {
		this.stopDevice?.();
		this.device = copy;
		this.recovery = recovery;
		this.deviceError = error;
		this.stopDevice = copy.subscribe(() => this.changed());
		this.changed();
	}

	get deviceSaved(): boolean { return Boolean(this.device?.saved && !this.deviceError); }
	get leavePrompt(): boolean { return !this.durability.clean && !this.deviceSaved; }
	dismissRecovery(storeId: string): void {
		this.recovery = this.recovery.filter(copy => copy.storeId !== storeId);
		this.changed();
	}

	private updated = (update: Uint8Array, origin: unknown): void => {
		this.textGeneration++;
		if (origin !== SERVER) {
			this.durability.localTransaction();
			if (this.connection === 'connected' && this.writable) this.sendUpdate('doc_update', update);
		}
		this.changed();
	};

	private sendUpdate(type: string, update: Uint8Array): void {
		const id = crypto.randomUUID();
		const frame = { type, artifact_id: this.artifactId, store_id: this.storeId,
			message_id: id, request_id: id, update: encodeUpdate(update) };
		this.durability.messageBuilt(id);
		this.updates.set(id, frame);
		if (!this.send(frame)) this.disconnected();
	}

	opened(update: Uint8Array, vector: Uint8Array, held: HeldDocument | null = null, writable = this.writable): void {
		this.writable = writable;
		this.refusal = null;
		Y.applyUpdate(this.doc, update, SERVER);
		this.vector = vector;
		this.held = held;
		this.connection = 'connected';
		this.sync();
		this.changed();
	}

	sync(): void {
		if (this.connection !== 'connected' || !this.writable) return;
		// encodeStateAsUpdate includes the complete delete set even for equal vectors.
		this.sendUpdate('doc_sync', Y.encodeStateAsUpdate(this.doc, this.vector));
	}

	disconnected(): void {
		this.connection = 'disconnected';
		for (const request of this.requests.values()) request.reject(new Error('Disconnected; your text is still in this tab'));
		this.requests.clear();
		this.changed();
	}

	/** Reopening ends any refusal, so its Retry cannot ask twice; the session stays read-only until a live answer. */
	reconnecting(): void { this.connection = 'reconnecting'; this.refusal = null; this.changed(); }

	/** A reopen answered non-live or by a typed refusal: keep the text and device copy, read-only, and send nothing. */
	refuse(refusal: DocOpenRefusal): void {
		this.refusal = refusal;
		this.writable = false;
		this.connection = 'refused';
		for (const request of this.requests.values()) request.reject(new Error(refusal.message));
		this.requests.clear();
		this.changed();
	}

	retryStore(): void {
		if (this.connection !== 'connected') return;
		if (!this.updates.size) this.sync();
		else for (const frame of this.updates.values()) this.send(frame);
	}

	onFrame(listener: (frame: DocumentFrame) => void): () => void {
		this.frameListeners.add(listener);
		return () => this.frameListeners.delete(listener);
	}

	handle(frame: DocumentFrame): void {
		const requestId = frame.request_id ?? '';
		const reservationError = frame.type === 'doc_error' && this.requests.get(requestId)?.reservation === true;
		if (frame.artifact_id !== this.artifactId && !this.requests.has(requestId) && !reservationError) return;
		if (frame.store_id && frame.store_id !== this.storeId && !reservationError) return;
		if (reservationError || frame.type === 'doc_positions') this.requests.delete(requestId);
		if (frame.kind === 'publication_failed' || frame.kind === 'setup_failed') frame = { ...frame, message: publicFailureMessage(frame.kind, frame.message) };
		if (Array.isArray(frame.blocking)) frame = { ...frame, blocking: frame.blocking.map(row => ({ ...row,
			message: publicFailureMessage(row.kind, row.message) })) };
		if (frame.artifact_id === this.artifactId && frame.store_id === this.storeId) {
			if (frame.type === 'doc_error' && frame.blocking === true) this.ledger.observe(frame);
			if (frame.type === 'doc_cleared') this.ledger.clear(frame);
			if (frame.type === 'doc_saved' && Array.isArray(frame.blocking)) for (const row of frame.blocking) this.ledger.observe(row);
		}
		if (['doc_opened', 'doc_saved', 'doc_pending'].includes(frame.type)) {
			this.pending = frame.pending === true;
			this.held = frame.held ?? null;
			if (!this.pending && this.storeError === 'Written, not stored; retry to finish saving') this.storeError = '';
		}
		if (frame.type === 'doc_update' && frame.update) {
			const update = decodeUpdate(frame.update);
			withLiveUpdateAuthor(liveUpdateAuthor(frame.author), () => Y.applyUpdate(this.doc, update, SERVER));
			if (frame.vector) this.vector = decodeUpdate(frame.vector);
		} else if (frame.type === 'doc_ack') {
			this.durability.acknowledge(this.storeId, frame.message_ids ?? []);
			for (const id of frame.message_ids ?? []) this.updates.delete(id);
			if (this.durability.clean) { this.storeError = ''; this.updates.clear(); }
		} else if (frame.type === 'doc_saved') this.saved(frame);
		else if (frame.type === 'doc_error' && !reservationError) this.refused(frame);
		for (const listener of this.frameListeners) listener(frame);
		this.changed();
	}

	private saved(frame: DocumentFrame): void {
		this.held = frame.held ?? null;
		const request = this.requests.get(frame.request_id ?? '');
		this.requests.delete(frame.request_id ?? '');
		if (frame.server_saved === false) {
			this.storeError = 'Written, not stored; retry to finish saving';
			request?.reject(new Error(this.storeError));
		} else {
			this.fileError = '';
			this.operationErrorKind = undefined;
			request?.landed?.();
			request?.resolve(frame);
		}
	}

	private refused(frame: DocumentFrame): void {
		const reason = publicFailureMessage(frame.kind, frame.message);
		const request = this.requests.get(frame.request_id ?? '');
		if (request) {
			// Stamped refusals belong only to the versioned owner observation.
			if (frame.failure_generation === undefined && frame.setup_revision === undefined) {
				this.fileError = reason;
				this.operationErrorKind = frame.kind;
			}
			this.requests.delete(frame.request_id!);
			request.reject(new LiveOperationError(frame.kind, reason));
		} else if ((frame.kind === 'store_refused' && frame.failure_generation === undefined) || this.updates.has(frame.request_id ?? '')) this.storeError = reason;
	}

	private request(type: string, fields: Record<string, unknown> = {}, landed?: () => void): Promise<DocumentFrame> {
		if (!this.writable) return Promise.reject(new Error('This document is read-only'));
		if (this.connection !== 'connected') return Promise.reject(new Error('Disconnected; keep your device copy'));
		this.sync();
		const id = crypto.randomUUID();
		return new Promise((resolve, reject) => {
			this.requests.set(id, { resolve, reject, landed });
			if (!this.send({ type, artifact_id: this.artifactId, store_id: this.storeId, request_id: id, ...fields })) {
				this.disconnected();
			}
		});
	}

	/**
	 * Ask for comment positions (4.2). Consumers match artifact, store, request id and sequence
	 * on replies, and the request id on refusals; a reservation's id is tracked so that only its
	 * own refusal is admitted whatever store it names. Null when disconnected: reconnect asks again.
	 */
	requestPositions(seq: number, reload = false, reserve?: PositionReservation): string | null {
		if (this.connection !== 'connected') return null;
		const id = crypto.randomUUID();
		if (!this.send({ type: 'doc_positions', artifact_id: this.artifactId, store_id: this.storeId, request_id: id, seq, reload,
			...(reserve ? { reserve } : {}) })) {
			this.disconnected();
			return null;
		}
		if (reserve) this.requests.set(id, { reservation: true, resolve: () => {}, reject: () => {} });
		return id;
	}

	/** Forget a reservation request whose caller no longer awaits a reply. */
	cancelReservationRequest(id: string | null): void {
		if (id) this.requests.delete(id);
	}

	/** Give back a range a capture reserved but no comment will use; the server ignores an id it no longer holds. */
	releaseReservation(clientId: string): void {
		if (this.connection === 'connected') this.send({ type: 'doc_release', artifact_id: this.artifactId, store_id: this.storeId, client_id: clientId });
	}

	flush(): Promise<DocumentFrame> {
		const generation = this.durability.snapshot().localGeneration;
		return this.request('doc_flush', {}, () => this.durability.written(generation));
	}
	retry(): Promise<DocumentFrame> { this.retryStore(); return this.request('doc_flush', { retry: true }); }
	replace(text: string): Promise<DocumentFrame> { return this.request('doc_replace', { text }); }
	choose(choice: 'corsia' | 'file'): Promise<DocumentFrame> { return this.request('doc_choice', { choice }); }

	close(): void {
		this.sync();
		if (this.connection === 'connected') this.send({ type: 'doc_close', artifact_id: this.artifactId, store_id: this.storeId });
	}

	destroy(): void {
		this.connection = 'disconnected';
		this.changed();
		this.doc.off('update', this.updated);
		this.stopDevice?.();
		this.stopDevice = null;
		this.frameListeners.clear();
		for (const request of this.requests.values()) request.reject(new Error('The document was closed'));
		this.requests.clear();
	}
}
