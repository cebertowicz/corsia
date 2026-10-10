import { get, writable } from 'svelte/store';
import type { DocSaveStatus } from '$lib/stores/docSaveQueue';
import { parseWsTimestamp } from '$lib/time';
export {
	ArrivalQueue, REDUCED_HIGHLIGHT_FOR, REVEAL_BASE, REVEAL_CAP, REVEAL_PER_CHAR, revealDelay,
} from '$lib/docArrivalQueue';

export const AGENT_PRESENT_FOR = 5 * 60_000;

const ARRIVAL_KINDS = new Set<DocActivityKind>(['comment', 'suggestion', 'edit']);
const ACTIVITY_KINDS = new Set<DocActivityKind>(['read', 'edit', 'comment', 'suggestion', 'decision']);
export const AGENT_COLORS = ['#8b5cf6', '#2dd4bf', '#60a5fa', '#f59e0b', '#e0489e', '#22c55e', '#ef4444', '#06b6d4'];

export type DocActivityKind = 'read' | 'edit' | 'comment' | 'suggestion' | 'decision';
export type DocSpan = { from: number; to: number };

export interface DocActivityFrame {
	type: 'doc_activity';
	artifact_id: string;
	thread_id: string;
	agent: string;
	kind: DocActivityKind;
	span: DocSpan | null;
	comment_id: string | null;
	revision: string | null;
	at: string;
}

export interface DocAgentPresence {
	id: string;
	name: string;
	color: string;
	lastKind: DocActivityKind;
	lastSpan: DocSpan | null;
	lastAt: string;
	lastAtMs: number;
	highlightSpan: DocSpan | null;
	/** What the highlight announces; a comment anchor reveals the text beneath it. */
	highlightKind: DocActivityKind | null;
	arrivalActive: boolean;
}

interface ArtifactPresence {
	agents: Record<string, DocAgentPresence>;
	pending: Record<string, DocActivityFrame[]>;
}

interface DocPresenceState {
	artifacts: Record<string, ArtifactPresence>;
}

const EMPTY_STATE: DocPresenceState = { artifacts: {} };
export const docPresence = writable<DocPresenceState>(EMPTY_STATE);
const activityListeners = new Set<(frame: DocActivityFrame) => void>();

function validSpan(value: unknown): value is DocSpan {
	if (!value || typeof value !== 'object') return false;
	const span = value as Record<string, unknown>;
	return Number.isInteger(span.from) && Number.isInteger(span.to)
		&& Number(span.from) >= 0 && Number(span.to) >= Number(span.from);
}

function parsedFrame(value: unknown): DocActivityFrame | null {
	if (!value || typeof value !== 'object') return null;
	const frame = value as Record<string, unknown>;
	if (frame.type !== 'doc_activity'
		|| typeof frame.artifact_id !== 'string' || !frame.artifact_id
		|| typeof frame.thread_id !== 'string' || !frame.thread_id
		|| typeof frame.agent !== 'string' || !frame.agent
		|| typeof frame.kind !== 'string' || !ACTIVITY_KINDS.has(frame.kind as DocActivityKind)
		|| typeof frame.at !== 'string' || !Number.isFinite(parseWsTimestamp(frame.at))) return null;
	if (frame.span !== null && !validSpan(frame.span)) return null;
	if (frame.comment_id !== null && typeof frame.comment_id !== 'string') return null;
	if (frame.revision !== null && typeof frame.revision !== 'string') return null;
	return frame as unknown as DocActivityFrame;
}

export function agentColor(threadId: string): string {
	let hash = 0;
	for (const char of threadId) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
	return AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length];
}

export function recordDocActivity(value: unknown): boolean {
	const frame = parsedFrame(value);
	if (!frame) return false;
	const atMs = parseWsTimestamp(frame.at);
	docPresence.update((state) => {
		const current = state.artifacts[frame.artifact_id] ?? { agents: {}, pending: {} };
		const prior = current.agents[frame.thread_id];
		const isLatest = !prior || atMs >= prior.lastAtMs;
		const agent: DocAgentPresence = isLatest ? {
			id: frame.thread_id,
			name: frame.agent,
			color: prior?.color ?? agentColor(frame.thread_id),
			lastKind: frame.kind,
			lastSpan: frame.span ?? prior?.lastSpan ?? null,
			lastAt: frame.at,
			lastAtMs: atMs,
			highlightSpan: prior?.highlightSpan ?? null,
			highlightKind: prior?.highlightKind ?? null,
			arrivalActive: prior?.arrivalActive ?? false,
		} : prior;
		const pending = { ...current.pending };
		if (ARRIVAL_KINDS.has(frame.kind)) {
			pending[frame.thread_id] = [...(pending[frame.thread_id] ?? []), frame]
				.sort((a, b) => parseWsTimestamp(a.at) - parseWsTimestamp(b.at));
		}
		return {
			artifacts: {
				...state.artifacts,
				[frame.artifact_id]: {
					agents: { ...current.agents, [frame.thread_id]: agent },
					pending,
				},
			},
		};
	});
	for (const listener of activityListeners) listener(frame);
	return true;
}

export function onRecordedDocActivity(listener: (frame: DocActivityFrame) => void): () => void {
	activityListeners.add(listener);
	return () => activityListeners.delete(listener);
}

export function takePendingArrivals(artifactId: string, threadId: string): DocActivityFrame[] {
	let taken: DocActivityFrame[] = [];
	docPresence.update((state) => {
		const artifact = state.artifacts[artifactId];
		if (!artifact?.pending[threadId]?.length) return state;
		taken = artifact.pending[threadId];
		const pending = { ...artifact.pending };
		delete pending[threadId];
		return { artifacts: { ...state.artifacts, [artifactId]: { ...artifact, pending } } };
	});
	return taken;
}

export function setPresenceHighlight(
	artifactId: string,
	threadId: string,
	span: DocSpan | null,
	arrivalActive = span !== null,
	kind: DocActivityKind | null = null,
): void {
	docPresence.update((state) => {
		const artifact = state.artifacts[artifactId];
		const agent = artifact?.agents[threadId];
		const sameSpan = agent?.highlightSpan?.from === span?.from && agent?.highlightSpan?.to === span?.to;
		if (!artifact || !agent || (sameSpan && agent.arrivalActive === arrivalActive && agent.highlightKind === kind)) return state;
		// A comment moves the agent caret to its anchor.
		const lastSpan = kind === 'comment' && span ? { ...span } : agent.lastSpan;
		return {
			artifacts: {
				...state.artifacts,
				[artifactId]: {
					...artifact,
					agents: {
						...artifact.agents,
						[threadId]: { ...agent, highlightSpan: span, highlightKind: span ? kind : null, arrivalActive, lastSpan },
					},
				},
			},
		};
	});
}

export function presenceFor(artifactId: string, now = Date.now()): DocAgentPresence[] {
	const artifact = get(docPresence).artifacts[artifactId];
	if (!artifact) return [];
	return Object.values(artifact.agents)
		.filter((agent) => now - agent.lastAtMs < AGENT_PRESENT_FOR)
		.sort((a, b) => b.lastAtMs - a.lastAtMs);
}

export function prunePresence(now = Date.now()): void {
	docPresence.update((state) => {
		let changed = false;
		const artifacts: Record<string, ArtifactPresence> = {};
		for (const [artifactId, artifact] of Object.entries(state.artifacts)) {
			const agents = Object.fromEntries(Object.entries(artifact.agents)
				.filter(([, agent]) => now - agent.lastAtMs < AGENT_PRESENT_FOR));
			const liveIds = new Set(Object.keys(agents));
			const pending = Object.fromEntries(Object.entries(artifact.pending)
				.filter(([threadId]) => liveIds.has(threadId)));
			if (Object.keys(agents).length) artifacts[artifactId] = { agents, pending };
			if (Object.keys(agents).length !== Object.keys(artifact.agents).length) changed = true;
		}
		return changed ? { artifacts } : state;
	});
}

/** The span a comment anchors in the document, or null when it has none or no longer matches the text. */
export function commentAnchor(comment: { orphaned?: boolean; from?: number; to?: number } | null | undefined): DocSpan | null {
	return comment && !comment.orphaned && comment.from !== undefined && comment.to !== undefined
		? { from: comment.from, to: comment.to } : null;
}

/**
 * True when the canvas of `threadId` actually shows `artifactId` to a visible
 * page. `displayed` is the one holder of what the canvas shows (`displayedDocument`):
 * the active document tab, in a layout that displays it. Focus is not
 * visibility, so clicking in chat does not end viewing in split layout.
 */
export function isViewing(
	artifactId: string | null,
	threadId: string | null,
	displayed: { threadId: string; value: { showing: string | null } } | null,
	visibility: DocumentVisibilityState,
): boolean {
	if (!artifactId || !threadId || visibility !== 'visible') return false;
	return displayed?.threadId === threadId && displayed.value.showing === artifactId;
}

const ACTIVITY_VERBS: Record<DocActivityKind, string> = {
	read: 'read',
	edit: 'edited',
	comment: 'commented',
	suggestion: 'suggested a change',
	decision: 'decided on a suggestion',
};

export function presenceDetail(agent: DocAgentPresence, now = Date.now()): string {
	const elapsed = Math.max(0, now - agent.lastAtMs);
	let age: string;
	if (elapsed < 1_000) age = 'less than a second ago';
	else if (elapsed < 60_000) age = `${Math.floor(elapsed / 1_000)} s ago`;
	else age = `${Math.floor(elapsed / 60_000)} min ago`;
	return `${ACTIVITY_VERBS[agent.lastKind]} ${age}`;
}

export function shouldReloadPresenceEdit(
	draft: Pick<{ editText: string; savedText: string; status: DocSaveStatus }, 'editText' | 'savedText' | 'status'> | undefined,
): boolean {
	return !draft || (draft.editText === draft.savedText && draft.status !== 'saving' && draft.status !== 'error');
}

export function resetDocPresenceForTests(): void {
	docPresence.set(EMPTY_STATE);
	activityListeners.clear();
}
