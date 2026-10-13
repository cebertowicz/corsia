import type { UpdateChanges, UpdateOperation } from './model';
import type { UpdateStatus } from './store';

const RELEASE_NOTES_KEY = 'corsia:update:release-notes:';

export interface UpdateReleaseNotes {
	targetVersion: string;
	changes: UpdateChanges | null;
}

function savedIds(key: string): Set<string> {
	if (typeof sessionStorage === 'undefined') return new Set();
	try {
		const value = JSON.parse(sessionStorage.getItem(key) ?? '[]');
		return new Set(Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []);
	} catch {
		return new Set();
	}
}

export function rememberId(key: string, id: string): boolean {
	const ids = savedIds(key);
	if (ids.has(id)) return false;
	ids.add(id);
	if (typeof sessionStorage !== 'undefined') {
		try { sessionStorage.setItem(key, JSON.stringify([...ids].slice(-50))); } catch { /* storage is optional */ }
	}
	return true;
}

export function snapshotReleaseNotes(status: UpdateStatus): void {
	const operation = status.operation;
	const targetVersion = operation?.target_version ?? status.target_version;
	if (!operation?.operation_id || !targetVersion) return;
	try {
		localStorage.setItem(`${RELEASE_NOTES_KEY}${operation.operation_id}`, JSON.stringify({ targetVersion, changes: status.changes ?? null }));
	} catch { /* release notes remain optional when storage is unavailable */ }
}

/** Find retained release notes for a completed update after the server clears its offer. */
export function releaseNotesFor(operation: UpdateOperation | null, status: UpdateStatus | null): UpdateReleaseNotes | null {
	const targetVersion = operation?.target_version ?? status?.target_version;
	if (status?.changes && targetVersion) return { targetVersion, changes: status.changes };
	if (!operation?.operation_id || typeof localStorage === 'undefined') return targetVersion ? { targetVersion, changes: null } : null;
	try {
		const value = JSON.parse(localStorage.getItem(`${RELEASE_NOTES_KEY}${operation.operation_id}`) ?? 'null');
		return typeof value?.targetVersion === 'string' ? { targetVersion: value.targetVersion, changes: value.changes ?? null } : null;
	} catch { return targetVersion ? { targetVersion, changes: null } : null; }
}
