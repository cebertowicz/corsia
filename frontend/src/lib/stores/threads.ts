import { writable, derived, get } from 'svelte/store';
import { turnStates, type ThreadTurnState, type TurnIdentity } from '../ws/turnState';
import type { ThreadModelSwitchState } from './threadModelSwitch';
export interface Thread extends ThreadModelSwitchState {
	id: string;
	user_id: string;
	profile: string;
	title: string;
	model: string | null;
	account_id?: string;
	runtime_context_epoch?: number;
	project_id?: string | null;
	agent_enabled?: number | boolean | null;
	agent_responsibility?: string | null;
	agent_paths?: string[] | string | null;
	agent_domains?: string[] | string | null;
	agent_status?: 'active' | 'inactive' | string | null;
	agent_notes?: string | null;
	bootloader_files?: string[] | string | null;
	spawned_by?: string | null;
	archived?: number | null;
	reasoning_effort?: string | null;
	pinned?: number | null;
	created_at: string;
	updated_at: string;
	last_message?: string;
	/** Local only payload fields; `threadLocalOnly` is their one reader. */
	local_only?: number | boolean | null;
	local_only_effective?: boolean | null;
	local_only_cause?: 'inherited' | 'own' | 'project' | null;
	local_only_inherited_from_thread_id?: string | null;
	local_only_inherited_from_title?: string | null;
}

/**
 * Whether a thread is a persistent Agent for navigation purposes — the single
 * source of truth shared by the Agent zone (include) and the conversation list
 * (exclude), so an Agent never appears in both. Mirrors `/api/agents` intent:
 * enabled + active + a real responsibility, and not a hidden subagent/archived.
 */
export function isNavAgent(s: Thread): boolean {
	const enabled = s.agent_enabled === 1 || s.agent_enabled === true;
	const active = s.agent_status == null || s.agent_status === 'active';
	const hasResponsibility =
		typeof s.agent_responsibility === 'string' && s.agent_responsibility.trim() !== '';
	return enabled && active && hasResponsibility && s.spawned_by == null && !s.archived;
}

export type { ChatComment } from '../chatCommentTypes';
import type { ChatComment } from '../chatCommentTypes';

export interface Message {
	id: string;
	thread_id: string;
	role: 'user' | 'assistant' | 'system';
	content: string;
	toolHints?: string[];
	metadata?: any;
	created_at: string;
	seq?: number | null;
	typewriterPos?: number;
	/** Local presentation only; never persisted or inferred from history. */
	firstRunOpeningReveal?: boolean;
	reasoning?: string;
	_optimistic?: boolean;
	delivery_state?: import('../messageDelivery').MessageDeliveryState;
	delivery_error?: string | null;
	delivery_revision?: number;
	active_turn_id?: string | null;
	write_outcome?: 'accepted' | 'proved_no_write' | 'ambiguous' | null;
	delivering_reason?: string | null;
	delivery_blocked_since?: string | null;
	next_attempt_at?: string | null;
	/** Artifact cards the server computed for this message when it was read (display rows only). */
	cards?: import('../api/messageCards').MessageCard[];
	cards_more?: number;
	/** Server-computed transcript content after placed chat answers are removed. */
	chat_visible_content?: string;
	chat_comments?: ChatComment[];
	chat_answer_cids?: string[];
}

function validDeliveryRevision(value: unknown): number | null {
	return typeof value === 'number' && Number.isInteger(value) && value >= 1
		? value
		: null;
}

/**
 * Merge an incoming server row without allowing an older delivery transition
 * to overwrite a newer one. Legacy payloads with no revision retain the
 * historical last-frame-wins behavior until the server starts providing one.
 */
export function mergeMessageDelivery(existing: Message, incoming: Message): Message {
	const existingRevision = validDeliveryRevision(existing.delivery_revision);
	const incomingRevision = validDeliveryRevision(incoming.delivery_revision);
	if (
		existingRevision !== null
		&& incomingRevision !== null
		&& incomingRevision <= existingRevision
		&& existing.delivery_state !== undefined
	) {
		return {
			...existing,
			...incoming,
			delivery_state: existing.delivery_state,
			delivery_error: existing.delivery_error,
			delivery_revision: existing.delivery_revision,
			write_outcome: existing.write_outcome,
			...(incomingRevision < existingRevision
				? { active_turn_id: existing.active_turn_id, seq: existing.seq } : {}),
		};
	}
	return { ...existing, ...incoming };
}

export const threads = writable<Thread[]>([]);

/** Demote locally but keep responsibility, paths, domains and notes so re-promoting prefills them. */
export function demoteAgentLocally(threadId: string): void {
	const off = { agent_enabled: 0, agent_status: 'inactive' };
	threads.update((rows) => rows.map((r) => (r.id === threadId ? { ...r, ...off } : r)));
}

// A draft choice belongs to the current chat surface and is discarded when it changes.
export const draftAccountId = writable<string | null>(null);
const activeThreadIdStore = writable<string | null>(null);
export const activeThreadId = {
	subscribe: activeThreadIdStore.subscribe,
	set(id: string | null) {
		activeThreadIdStore.set(id);
		draftAccountId.set(null);
	},
	update(fn: (id: string | null) => string | null) { this.set(fn(get(activeThreadIdStore))); },
};
export const messages = writable<Message[]>([]);
/** Whether `threadId` has a live turn, read from the per-thread turn states. */
export function isThreadTurnActive(
	states: ReadonlyMap<string, ThreadTurnState>,
	threadId: string | null | undefined,
): boolean {
	return Boolean(threadId) && states.get(threadId!)?.phase === 'active';
}
export const isStreaming = derived(
	[turnStates, activeThreadId],
	([$turnStates, $activeThreadId]) => isThreadTurnActive($turnStates, $activeThreadId),
);

function threadTime(thread: Thread): number {
	const raw = thread.updated_at || thread.created_at;
	return raw ? new Date(raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z').getTime() : 0;
}

function isHiddenCreatedThread(thread: Thread): boolean {
	// Subagent-ness derives from a single source of truth: parent presence.
	// A spawned worker (spawned_by set) is hidden from the sidebar.
	return thread.spawned_by != null;
}

function visibleThreadRows(rows: Thread[], thread: Thread): Thread[] {
	const withoutCurrent = rows.filter((s) => s.id !== thread.id);
	const merged = [{ ...thread }, ...withoutCurrent];
	merged.sort((a, b) => threadTime(b) - threadTime(a));
	return merged;
}

export function upsertVisibleThread(thread: Thread): void {
	if (isHiddenCreatedThread(thread)) return;
	threads.update((rows) => visibleThreadRows(rows, thread));
}

export function adoptCreatedThread(thread: Thread): void {
	upsertVisibleThread(thread);
}

export interface ActiveTool {
	id: string;
	name: string;
	hint: string;
	status: 'running' | 'done';
	// Backend-projected row fields, named as the frames and resume snapshot carry them.
	phrase?: string;
	object?: string;
	outcome?: 'done' | 'failed' | 'refused' | 'unknown';
	error_line?: string;
	args?: string;
	result?: string;
}

export const liveReasoningByThread = writable<Map<string, string>>(new Map());
export const streamContentByThread = writable<Map<string, string>>(new Map());
export const activeToolsByThread = writable<Map<string, ActiveTool[]>>(new Map());

export interface GenerationSequence extends TurnIdentity {
	seq: number;
}

export interface PendingGenerationDeltas {
	identity: TurnIdentity | null;
	deltas: any[];
}

export const lastAppliedSeqByThread = writable<Map<string, GenerationSequence>>(new Map());
export const pendingDeltasByThread = writable<Map<string, PendingGenerationDeltas>>(new Map());

export const liveReasoning = derived(
	[liveReasoningByThread, activeThreadId],
	([$by, $sid]) => ($sid && $by.get($sid)) || ''
);
export const streamContent = derived(
	[streamContentByThread, activeThreadId],
	([$by, $sid]) => ($sid && $by.get($sid)) || ''
);
export const activeTools = derived(
	[activeToolsByThread, activeThreadId],
	([$by, $sid]) => ($sid && $by.get($sid)) || []
);

export function setThreadReasoning(threadId: string, value: string) {
	liveReasoningByThread.update(m => { const n = new Map(m); n.set(threadId, value); return n; });
}
export function appendThreadReasoning(threadId: string, chunk: string) {
	liveReasoningByThread.update(m => {
		const n = new Map(m);
		n.set(threadId, (n.get(threadId) || '') + chunk);
		return n;
	});
}
export function setThreadContent(threadId: string, value: string) {
	streamContentByThread.update(m => { const n = new Map(m); n.set(threadId, value); return n; });
}
export function appendThreadContent(threadId: string, chunk: string) {
	streamContentByThread.update(m => {
		const n = new Map(m);
		n.set(threadId, (n.get(threadId) || '') + chunk);
		return n;
	});
}
export function setThreadActiveTools(threadId: string, tools: ActiveTool[]) {
	activeToolsByThread.update(m => { const n = new Map(m); n.set(threadId, tools); return n; });
}
export function updateThreadActiveTools(threadId: string, updater: (tools: ActiveTool[]) => ActiveTool[]) {
	activeToolsByThread.update(m => {
		const n = new Map(m);
		n.set(threadId, updater(n.get(threadId) || []));
		return n;
	});
}
export function clearThreadBuffers(threadId: string) {
	liveReasoningByThread.update(m => { const n = new Map(m); n.delete(threadId); return n; });
	streamContentByThread.update(m => { const n = new Map(m); n.delete(threadId); return n; });
	activeToolsByThread.update(m => { const n = new Map(m); n.delete(threadId); return n; });
	lastAppliedSeqByThread.update(m => { const n = new Map(m); n.delete(threadId); return n; });
	pendingDeltasByThread.update(m => { const n = new Map(m); n.delete(threadId); return n; });
}

export const reasoningEffort = writable<string | null>(null);

/** Optimistically rename a thread and restore its prior title if persistence fails. */
export async function saveThreadTitle(threadId: string, title: string, token: string): Promise<void> {
	let previousTitle = '';
	threads.update((rows) => rows.map((thread) => {
		if (thread.id !== threadId) return thread;
		previousTitle = thread.title;
		return { ...thread, title };
	}));
	try {
		const response = await fetch(`/api/threads/${threadId}?token=${encodeURIComponent(token)}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ title }),
		});
		if (!response.ok) throw new Error(`Rename failed (${response.status})`);
	} catch (error) {
		threads.update((rows) => rows.map((thread) => (
			thread.id === threadId ? { ...thread, title: previousTitle } : thread
		)));
		throw error;
	}
}

export async function saveThreadProject(threadId: string, projectId: string | null, token: string) {
	let prev: string | null = null;
	threads.update(ss => ss.map(s => {
		if (s.id === threadId) { prev = s.project_id ?? null; return { ...s, project_id: projectId }; }
		return s;
	}));
	try {
		const res = await fetch(`/api/threads/${threadId}?token=${token}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ project_id: projectId }),
		});
		if (!res.ok) throw new Error(`PATCH /api/threads/${threadId} -> ${res.status}`);
	} catch (err) {
		threads.update(ss => ss.map(s => s.id === threadId ? { ...s, project_id: prev } : s));
		throw err;
	}
}

export type { ContextUsage } from './contextUsageTypes';

export const activeThread = derived(
	[threads, activeThreadId],
	([$threads, $id]) => $threads.find(s => s.id === $id) ?? null
);
