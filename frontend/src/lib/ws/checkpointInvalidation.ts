import { get } from 'svelte/store';
import { token } from '../stores/auth';
import { activeThreadId } from '../stores/threads';
import {
	ensureContextPreview,
	getContextUsage,
	invalidateContextPreview,
	previewIdentity,
	setContextUsage,
} from '../stores/contextUsageByThread';
import { isDisplayedThread } from '../stores/displayedThreads';
import type { ContextUsage } from '../stores/contextUsageTypes';
import {
	invalidateCheckpoints,
	latestCheckpoint,
	type Checkpoint,
} from '../stores/checkpoints';
import { activeModelId } from '../stores/models';

// The sole cross-harness answer to whether an install's estimate was already
// applied: a later checkpoint_changed for that same install cannot overwrite a
// measured turn that landed since. Session storage keeps it across reconnect
// hint replay (#1868); the bounded map prevents abandoned threads accumulating.
const APPLIED_KEY = 'corsia.appliedCheckpointInstalls';
const APPLIED_LIMIT = 128;
let appliedInstall: Map<string, string> | null = null;

function applied(): Map<string, string> {
	if (appliedInstall) return appliedInstall;
	appliedInstall = new Map();
	try {
		const stored = JSON.parse(sessionStorage.getItem(APPLIED_KEY) ?? '{}');
		for (const [thread, id] of Object.entries(stored)) {
			if (typeof id === 'string') appliedInstall.set(thread, id);
		}
	} catch {
		// Unreadable or unavailable storage only loses the reload guard.
	}
	return appliedInstall;
}

function markApplied(threadId: string, checkpointId: string): void {
	const map = applied();
	map.set(threadId, checkpointId);
	while (map.size > APPLIED_LIMIT) map.delete(map.keys().next().value!);
	try {
		sessionStorage.setItem(APPLIED_KEY, JSON.stringify(Object.fromEntries(map)));
	} catch {
		// Storage full or unavailable: the in-memory guard still holds.
	}
}

/** The selected model previews the active thread; another displayed thread previews its own. */
function previewModelFor(threadId: string): string | null {
	return threadId === get(activeThreadId) ? get(activeModelId) : null;
}

/**
 * Re-read the next-turn preview after a checkpoint installs (#1562).
 *
 * The install has made the last measured turn stale, so the old preview is
 * retired and the new one read into the shared preview slot, which the donut
 * and the sheet both show, labelled as an estimate, until the next turn
 * measures again. Measured usage is never overwritten with an estimate. Only
 * the install of a checkpoint not yet applied is acted on.
 */
export async function refreshUsageAfterInstall(
	threadId: string,
	authToken: string,
	checkpoints: readonly Checkpoint[] | null,
): Promise<void> {
	// Only a fresh durable read counts; a failed read (null) proves nothing.
	if (!checkpoints) return;
	const latest = latestCheckpoint({ checkpoints, loading: false, error: null });
	if (!latest || latest.status !== 'installed') return;
	if (applied().get(threadId) === latest.id) return;
	if (!isDisplayedThread(threadId)) return;
	// Claimed before the read so a later hint for this install can never
	// overwrite a turn measured in the meantime.
	markApplied(threadId, latest.id);
	const before = getContextUsage(threadId);
	const modelId = previewModelFor(threadId);
	invalidateContextPreview(threadId);
	const read = await ensureContextPreview({
		threadId,
		authToken,
		modelId,
		...previewIdentity(threadId, modelId, before, latest),
	});
	if (read || !isDisplayedThread(threadId) || getContextUsage(threadId) !== before) return;
	// No preview: the pre-checkpoint count is known stale, so it must not keep
	// reading as current. Opening the sheet re-reads the preview and shows its
	// own error if the failure persists.
	if (before) setContextUsage(threadId, { ...before, token_usage_available: false, token_source: undefined });
}

/**
 * Handle a `checkpoint_changed` hint: re-read the durable checkpoint list (the
 * frame itself is never trusted for status), then refresh usage if that read
 * found a new install.
 */
export function handleCheckpointChanged(threadId: unknown): Promise<void> | null {
	if (typeof threadId !== 'string' || !threadId) return null;
	const authToken = get(token);
	if (!authToken) return null;
	return invalidateCheckpoints(threadId, authToken)
		.catch(() => null)
		.then(rows => refreshUsageAfterInstall(threadId, authToken, rows));
}

/** Test seam: what a page reload does, keeping only what session storage holds. */
export function dropInMemoryInstalls(): void {
	appliedInstall = null;
}

/** Test seam: forget which installs have been applied. */
export function resetAppliedInstalls(): void {
	appliedInstall = new Map();
	try {
		sessionStorage.removeItem(APPLIED_KEY);
	} catch {
		// Nothing stored to forget.
	}
}
