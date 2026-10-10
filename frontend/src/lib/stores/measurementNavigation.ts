import { get, writable } from 'svelte/store';
import { measurementScope, syncMeasurementUrl, type MeasurementScope } from './measurement';
import { MEASUREMENT_VIEW } from './measurementAddress';

/**
 * Where the Measurement page returns to when Close is pressed.
 *
 * The origin is only ever set by a doorway. A rail link or a direct URL leaves
 * it null, so Close falls back to Activity and never invents a thread to go
 * back to (PL-2).
 */
export type MeasurementOrigin = { kind: 'thread'; threadId: string } | { kind: 'activity' };

export const measurementOrigin = writable<MeasurementOrigin | null>(null);

/** The lane a thread doorway is opened from. A conductor's lane is its project. */
export type LaneEntry = {
	threadId: string;
	agentId: string | null;
	projectId: string | null;
	isConductor: boolean;
};

export function laneEntryScope(entry: LaneEntry): MeasurementScope {
	if (entry.isConductor) return { project: entry.projectId ?? 'all', agent: null };
	return { project: entry.projectId ?? 'all', agent: entry.agentId };
}

/** Where the sidebar link points: the current project's scope, or All work outside any project. */
export function measurementRailHref(projectId: string | null): string {
	const params = new URLSearchParams({ view: MEASUREMENT_VIEW });
	if (projectId) params.set('project', projectId);
	return `/?${params.toString()}`;
}

/**
 * Rail entry always opens the current project's scope (All work outside a
 * project), never the last-used selection or the open thread's lane (PL-1).
 */
export function openMeasurementFromRail(projectId: string | null): void {
	measurementOrigin.set(null);
	measurementScope.set({ project: projectId ?? 'all', agent: null });
	syncMeasurementUrl();
}

export function openMeasurementFromThread(entry: LaneEntry): void {
	measurementOrigin.set({ kind: 'thread', threadId: entry.threadId });
	measurementScope.set(laneEntryScope(entry));
	syncMeasurementUrl();
}

export function openMeasurementFromProject(projectId: string): void {
	measurementOrigin.set({ kind: 'activity' });
	measurementScope.set({ project: projectId, agent: null });
	syncMeasurementUrl();
}

/** Deterministic Close target: the recorded doorway, else Activity. */
export function measurementCloseOrigin(): MeasurementOrigin | null {
	const origin = get(measurementOrigin);
	measurementOrigin.set(null);
	return origin;
}
