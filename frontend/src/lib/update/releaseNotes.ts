import type { UpdateChanges, UpdateOperation } from './model';
import type { UpdateStatus } from './store';

const RELEASE_NOTES_KEY = 'corsia:update:release-notes:';

export interface UpdateReleaseNotes {
	targetVersion: string;
	changes: UpdateChanges | null;
}

type IdScope = 'tab' | 'browser';

function idStorage(scope: IdScope): Storage | undefined {
	return scope === 'browser'
		? (typeof localStorage === 'undefined' ? undefined : localStorage)
		: (typeof sessionStorage === 'undefined' ? undefined : sessionStorage);
}

function savedIds(key: string, scope: IdScope): Set<string> {
	const storage = idStorage(scope);
	if (!storage) return new Set();
	try {
		const value = JSON.parse(storage.getItem(key) ?? '[]');
		return new Set(Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []);
	} catch {
		return new Set();
	}
}

export function hasId(key: string, id: string, scope: IdScope = 'tab'): boolean {
	return savedIds(key, scope).has(id);
}

/** Record an id and report whether it was new; `browser` scope is shared by every tab. */
export function rememberId(key: string, id: string, scope: IdScope = 'tab'): boolean {
	const ids = savedIds(key, scope);
	if (ids.has(id)) return false;
	ids.add(id);
	try { idStorage(scope)?.setItem(key, JSON.stringify([...ids].slice(-50))); } catch { /* storage is optional */ }
	return true;
}

// The server stops offering the change list once its checkout holds the update, which happens before the operation exists
// for this client; the list the person approved is kept here until the operation's own snapshot is written.
let approved: { targetVersion: string; changes: UpdateChanges } | null = null;

export function rememberApprovedNotes(status: UpdateStatus | null): void {
	approved = status?.target_version && status.changes ? { targetVersion: status.target_version, changes: status.changes } : null;
}

export function snapshotReleaseNotes(status: UpdateStatus): void {
	const operation = status.operation;
	const targetVersion = operation?.target_version ?? status.target_version;
	if (!operation?.operation_id || !targetVersion) return;
	const key = `${RELEASE_NOTES_KEY}${operation.operation_id}`;
	const changes = status.changes ?? (approved?.targetVersion === targetVersion ? approved.changes : null);
	try {
		// A reloaded page holds neither the offer nor the remembered list; it must not blank what an earlier page saved.
		if (!changes && JSON.parse(localStorage.getItem(key) ?? 'null')?.changes) return;
		localStorage.setItem(key, JSON.stringify({ targetVersion, changes }));
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
