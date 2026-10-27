import type { ActivityIndexRow } from '$lib/api/plans';
import type { ProjectInfo } from '$lib/stores/projects';
import { isNavAgent, type Thread } from '$lib/stores/threads';
import { planCardState, type PlanCardState } from '$lib/components/plans/planDisplay';
import {
	isInProgress,
	sortRows,
	timestamp,
} from '$lib/activityOrdering';

// Ordering is its own module; re-exported so Activity consumers keep one import.
export {
	nextPromotionExpiry,
	PROMOTION_QUIET_WINDOW_MS,
	rowTier,
	type ActivityRowTier,
} from '$lib/activityOrdering';

export type ThreadActivityState = 'active' | 'done';
export type ActivityOwnerType = 'agent' | 'thread';

export interface ResumeThread {
	id: string;
	title: string;
	projectId: string | null;
	ownerType: ActivityOwnerType;
	activity: ThreadActivityState | null;
	updatedAt: string;
	lastMessage: string | null;
}

export interface ActivityPlanRow {
	entry: ActivityIndexRow;
	thread: Thread | null;
	ownerType: ActivityOwnerType;
	state: PlanCardState;
}

export interface ActivityPlanGroup {
	id: string;
	projectId: string | null;
	/** `null` when the project id is not resolvable from the loaded catalog. */
	label: string | null;
	summary: ActivityGroupSummary;
	/** In progress: working or needs-you, and not paused. */
	current: ActivityPlanRow[];
	/** Open work that is not in progress: waiting, idle, blocked, paused or unknown. */
	waiting: ActivityPlanRow[];
	complete: ActivityPlanRow[];
}

interface ActivityScopeOptions {
	projectId?: string;
	limit?: number;
	/** Injected clock for promotion decay; defaults to the current time. */
	now?: number;
	/** Project catalog; a thread in an archived project is not offered. */
	projects?: ProjectInfo[];
}

function archivedProjectIds(projects: ProjectInfo[] | undefined): Set<string> {
	return new Set(
		(projects ?? []).filter((project) => project.status === 'archived').map((project) => project.id),
	);
}

/**
 * Rank the quiet app-open resume rail by recency alone.
 *
 * A surface named Activity must not misreport recency, so live/idle state is a
 * mark on the row and never reorders it (canonical Activity ordering contract).
 * This matters because the signals are not equally fresh: `active` is derived
 * from in-memory turn phase, while `done` is persisted to localStorage and
 * restored on load, so a days-old `done` mark used to outrank a thread touched
 * minutes ago. Absence of a live signal is not a demotion.
 */
export function buildResumeThreads(
	threads: Thread[],
	activity: Record<string, ThreadActivityState>,
	options: ActivityScopeOptions = {},
): ResumeThread[] {
	const limit = options.limit ?? 5;
	const archivedProjects = archivedProjectIds(options.projects);
	return threads
		.filter((thread) => (
			!thread.archived
			&& thread.spawned_by == null
			&& !(thread.project_id != null && archivedProjects.has(thread.project_id))
			&& (options.projectId === undefined || thread.project_id === options.projectId)
		))
		.map((thread) => ({
			id: thread.id,
			title: thread.title || 'Untitled chat',
			projectId: thread.project_id ?? null,
			ownerType: isNavAgent(thread) ? 'agent' as const : 'thread' as const,
			activity: activity[thread.id] ?? null,
			updatedAt: thread.updated_at,
			lastMessage: thread.last_message?.trim() || null,
		}))
		.sort((left, right) => (
			timestamp(right.updatedAt) - timestamp(left.updatedAt)
			|| left.id.localeCompare(right.id)
		))
		.slice(0, limit);
}


/**
 * The truthful compact distribution for a group header.
 *
 * Replaces a single "N active" count that actually counted every non-complete
 * row — idle, paused, blocked and review included — which is the same class of
 * falsehood as rendering review as motion. Zero-valued classes are omitted by
 * the consumer rather than printed as `0`.
 *
 * `left` is summed straight from the server's per-plan `progress.open`, so
 * this introduces no second counting path.
 */
export interface ActivityGroupSummary {
	moving: number;
	needsYou: number;
	waiting: number;
	left: number;
}

function summarize(rows: ActivityPlanRow[]): ActivityGroupSummary {
	const summary: ActivityGroupSummary = { moving: 0, needsYou: 0, waiting: 0, left: 0 };
	for (const row of rows) {
		if (row.state === 'finished') continue;
		// Counts follow the sections the rows are shown in: `left` is the open
		// work of the In progress rows only, so a paused plan never adds to it.
		if (!isInProgress(row)) summary.waiting += 1;
		else if (row.entry.progress.liveness === 'advancing') summary.moving += 1;
		else if (row.state === 'needs-you') summary.needsYou += 1;
		if (isInProgress(row)) summary.left += row.entry.progress.open;
	}
	return summary;
}

/**
 * The group's display name, or `null` when it cannot be told truthfully.
 *
 * A generic "Project" placeholder would be both a falsehood and a navigation
 * hazard: the jump affordance passes this name into the scope, where it is
 * snapshotted, so an unresolved label would persist as the scope's name and
 * every unresolved project would render as the same heading.
 */
function groupLabel(
	projectId: string | null,
	projects: Map<string, ProjectInfo>,
): string | null {
	if (projectId === null) return 'Workspace';
	return projects.get(projectId)?.name ?? null;
}

/**
 * Convert the scope projection into the user-facing hierarchy.
 *
 * Scope-root plans are structural and never become peer rows. Thread plans are
 * grouped by project at workspace level and remain direct thread/agent rows
 * inside a project. Completed work is kept, but separated for disclosure.
 */
export function buildActivityGroups(
	entries: ActivityIndexRow[],
	threads: Thread[],
	projects: ProjectInfo[],
	options: ActivityScopeOptions = {},
): ActivityPlanGroup[] {
	const threadById = new Map(threads.map((thread) => [thread.id, thread]));
	const projectsById = new Map(projects.map((project) => [project.id, project]));
	const grouped = new Map<string, ActivityPlanRow[]>();

	for (const entry of entries) {
		const threadId = entry.owner.thread_id;
		if (!threadId || entry.plan.owner_kind !== 'thread') continue;
		if (options.projectId !== undefined && entry.owner.project_id !== options.projectId) {
			continue;
		}
		const groupId = options.projectId ?? entry.owner.project_id ?? 'workspace';
		const thread = threadById.get(threadId) ?? null;
		const rows = grouped.get(groupId) ?? [];
		rows.push({
			entry,
			thread,
			ownerType: thread && isNavAgent(thread) ? 'agent' : 'thread',
			state: planCardState(entry),
		});
		grouped.set(groupId, rows);
	}

	const now = options.now ?? Date.now();
	const groups = [...grouped.entries()].map(([id, rows]) => {
		const projectId = id === 'workspace' ? null : id;
		const open = rows.filter((row) => row.state !== 'finished');
		const current = sortRows(open.filter(isInProgress), now);
		const waiting = sortRows(open.filter((row) => !isInProgress(row)), now);
		const complete = sortRows(rows.filter((row) => row.state === 'finished'), now);
		return {
			id,
			projectId,
			label: groupLabel(projectId, projectsById),
			summary: summarize(rows),
			current,
			waiting,
			complete,
		};
	});

	return groups.sort((left, right) => {
		if (left.projectId === null) return right.projectId === null ? 0 : 1;
		if (right.projectId === null) return -1;
		// An unresolved label cannot order against a name; keep those last, in a
		// stable id order rather than letting identical placeholders collide.
		if (left.label === null || right.label === null) {
			if (left.label === right.label) return left.id.localeCompare(right.id);
			return left.label === null ? 1 : -1;
		}
		return left.label.localeCompare(right.label);
	});
}
