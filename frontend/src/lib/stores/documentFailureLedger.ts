import { FAILURE_KINDS, failureKind, publicFailureMessage, type BlockingFailure, type FailureKind, type FailureObservation } from './documentFailures';

/**
 * The versioned blocking failures one live document has observed. A failure counts only when its version is newer
 * than the clear threshold seen for its kind and than the failure already held; a setup failure also has to come
 * from the setup incarnation the document last opened under.
 */
export class DocumentFailureLedger {
	private setupIncarnation = '';
	private failures = new Map<FailureKind, BlockingFailure>();
	private clears = new Map<FailureKind, number>();

	get rows(): BlockingFailure[] {
		return FAILURE_KINDS.flatMap(kind => this.failures.has(kind) ? [this.failures.get(kind)!] : []);
	}

	message(kind: FailureKind): string | undefined { return this.failures.get(kind)?.message; }

	/** A new setup incarnation forgets the earlier setup failure and its clear threshold. */
	admitIncarnation(incarnation: string | undefined): void {
		if (!incarnation || incarnation === this.setupIncarnation) return;
		this.setupIncarnation = incarnation;
		this.failures.delete('setup_failed');
		this.clears.delete('setup_failed');
	}

	observe(row: FailureObservation): void {
		const kind = failureKind(row.kind);
		if (!kind || (kind === 'setup_failed' && row.setup_incarnation !== this.setupIncarnation)) return;
		const version = kind === 'setup_failed' ? row.setup_revision : row.failure_generation;
		if (version === undefined || !Number.isSafeInteger(version) || version < 0) return;
		if (version <= (this.clears.get(kind) ?? -1) || version < (this.failures.get(kind)?.version ?? -1)) return;
		this.failures.set(kind, { kind, message: publicFailureMessage(kind, row.message), version });
	}

	clear(row: FailureObservation): void {
		const kind = failureKind(row.kind), threshold = row.clears_through;
		if (!kind || (kind === 'setup_failed' && row.setup_incarnation !== this.setupIncarnation)) return;
		if (threshold === undefined || !Number.isSafeInteger(threshold) || threshold < 0) return;
		this.clears.set(kind, Math.max(threshold, this.clears.get(kind) ?? -1));
		const active = this.failures.get(kind);
		if (active && active.version <= threshold) this.failures.delete(kind);
	}
}
