import { get } from 'svelte/store';
import { activeModelId, getModelDispatchId, models, workspaceModels } from './modelsState';
import { plainErrorText } from '../plainError';
import { activeThreadId, threads } from './threads';
import { setContextUsage } from './contextUsageByThread';
import {
	applyPendingThreadModelSwitch,
	type PendingThreadModelSaveResult,
} from './threadModelSwitch';
import type { RunsOn, Runnable } from './modelTurn';

export type { PendingThreadModelSaveResult } from './threadModelSwitch';

export type ReasoningConfig =
	// `supports_auto` marks a provider that has its own "pick for me" mode, which
	// external harnesses and cloud providers do. Local models do not: we know the
	// level the server is configured to use, so the picker opens on that value.
	// An External model's levels come from its provider's published list, with
	// no known default (`default: null`) and no "Off" unless the list has one
	// (`supports_off: false`).
	| {
			type: 'effort_levels';
			values: string[];
			default: string | null;
			supports_auto?: boolean;
			supports_off?: boolean;
	  }
	| { type: 'token_budget'; min: number; max: number; default: number }
	| { type: 'boolean'; default: boolean };

export interface ModelCapabilities {
	context_window_max: number;
	supports_vision: boolean;
	supports_tools: boolean;
	supports_reasoning: boolean;
	reasoning_config: ReasoningConfig | null;
	// External models only: true when no published reasoning levels were found.
	reasoning_levels_unknown?: boolean;
}

export interface Model {
	id: string;
	name: string;
	display_name?: string;
	connection?: string;
	/** Harness rows only: the model on each account the scope permits. */
	runs_on?: RunsOn[];
	/** Harness rows only: whether the model can run for this scope, across the permitted accounts. */
	runnable?: Runnable;
	desc?: string;
	size: string;
	backend: string;
	provider?: string;
	speed: string;
	/** What the model is. Whether it is available is `modelAvailability(model)`. */
	status: 'ready' | 'available' | 'unavailable';
	status_reason?: string;
	/** Whether this model is served locally or by a private machine. */
	is_local?: boolean;
	/** Whether discovery reported this harness model on any account. */
	available?: boolean;
	cache_marker?: 'Cached';
	resolved_identity?: string;
	/** Catalog id of the listed row this still-accepted, unlisted selector aliases. */
	alias_of?: string;
	litellm_model?: string;
	capabilities?: ModelCapabilities;
	server_name?: string;
}

export { accountTurn, acceptsSend, heldByUsageLimit, modelAvailability } from './modelTurn';
export type { AccountTurn, ModelAvailability, RunsOn, Runnable } from './modelTurn';

export { activeModelId, getModelDispatchId, models, workspaceModels };

/**
 * Rows a picker offers: ready, and not an alias of a row it already offers.
 * An alias that is the current choice (`current`, by id or dispatch id) stays,
 * so a pinned setting or thread still shows as selected.
 */
export function isPickerMember(model: Model, current?: string | null): boolean {
	return !model.alias_of || modelMatchesId(model, current);
}

export function pickerModels(all: Model[], current?: string | null): Model[] {
	return all.filter((model) => model.status === 'ready' && isPickerMember(model, current));
}

let _lastChosenModelId: string | null = null;
const switchingThreadIds = new Set<string>();
const threadModelTruth = new Map<string, {
	model: string | null;
	updatedAt: string;
}>();

export interface CommittedThreadModelSaveResult {
	kind: 'committed';
	accepted: boolean;
	model: string;
	updatedAt: string;
	warning: string | null;
}

export type ThreadModelSaveResult =
	| CommittedThreadModelSaveResult
	| PendingThreadModelSaveResult;

export function beginThreadModelSwitch(threadId: string): boolean {
	if (switchingThreadIds.has(threadId)) return false;
	switchingThreadIds.add(threadId);
	return true;
}

export function finishThreadModelSwitch(threadId: string): void {
	switchingThreadIds.delete(threadId);
}

export function modelMatchesId(model: Model, id: string | null | undefined): boolean {
	if (!id) return false;
	return model.id === id || model.litellm_model === id;
}

export function findModelById(rows: Model[], id: string | null | undefined): Model | undefined {
	if (!id) return undefined;
	return rows.find((model) => modelMatchesId(model, id));
}

export function getLastChosenModelId(): string | null {
	return _lastChosenModelId;
}

export function setActiveModel(id: string) {
	activeModelId.set(id);
	_lastChosenModelId = id;
}

function normalizedServerTime(value: string): string {
	return value.endsWith('Z') ? value.slice(0, -1) : value;
}

function compareServerTimes(left: string, right: string): number {
	const normalizedLeft = normalizedServerTime(left);
	const normalizedRight = normalizedServerTime(right);
	if (normalizedLeft === normalizedRight) return 0;
	const leftMs = Date.parse(`${normalizedLeft}Z`);
	const rightMs = Date.parse(`${normalizedRight}Z`);
	if (!Number.isNaN(leftMs) && !Number.isNaN(rightMs) && leftMs !== rightMs) {
		return leftMs < rightMs ? -1 : 1;
	}
	return normalizedLeft < normalizedRight ? -1 : 1;
}

function newestDisplayTime(current: string | undefined, incoming: string): string {
	if (!current) return incoming;
	return compareServerTimes(incoming, current) >= 0 ? incoming : current;
}

export function reconcileThreadModelSnapshots<
	T extends { id: string; model: string | null; updated_at: string },
>(rows: T[]): T[] {
	return rows.map((row) => {
		if (!row.id || !row.updated_at) return row;
		const current = threadModelTruth.get(row.id);
		let resolved = row;
		if (!current) {
			threadModelTruth.set(row.id, {
				model: row.model,
				updatedAt: row.updated_at,
			});
		} else {
			const order = compareServerTimes(row.updated_at, current.updatedAt);
			if (order > 0 || (order === 0 && row.model === current.model)) {
				threadModelTruth.set(row.id, {
					model: row.model,
					updatedAt: row.updated_at,
				});
			} else {
				resolved = {
					...row,
					model: current.model,
					updated_at: newestDisplayTime(row.updated_at, current.updatedAt),
				};
			}
		}
		if (get(activeThreadId) === row.id && resolved.model) {
			setActiveModel(resolved.model);
		}
		return resolved;
	});
}

export function acceptThreadModelTruth(
	threadId: string,
	model: string,
	updatedAt: string,
): boolean {
	if (!threadId || !model || !updatedAt) return false;
	const activeModelBefore = get(activeModelId);
	const current = threadModelTruth.get(threadId);
	if (current) {
		const order = compareServerTimes(updatedAt, current.updatedAt);
		if (order < 0 || (order === 0 && current.model !== model)) return false;
	}
	threadModelTruth.set(threadId, { model, updatedAt });
	threads.update((rows) => {
		const updated = rows.map((thread) => (
			thread.id === threadId
				? {
					...thread,
					model,
					updated_at: newestDisplayTime(thread.updated_at, updatedAt),
				}
				: thread
		));
		updated.sort((a, b) => compareServerTimes(b.updated_at, a.updated_at));
		return updated;
	});
	if (get(activeThreadId) === threadId) {
		setActiveModel(model);
		if (activeModelBefore !== model) setContextUsage(threadId, null);
	}
	return true;
}

/** The accepted model of a thread, including one the thread list does not hold. */
export function threadModelFor(threadId: string): string | null {
	return threadModelTruth.get(threadId)?.model ?? null;
}

/** Bind a viewed thread to its saved selector; an unbound thread stays unbound. */
export function bindActiveThreadModel(
	threadId: string,
	rows: Array<{ id: string; model: string | null }>,
	hiddenModel?: string,
): void {
	const thread = rows.find(row => row.id === threadId);
	const selected = thread?.model ?? threadModelFor(threadId) ?? hiddenModel ?? null;
	activeModelId.set(selected);
}

export function _resetThreadModelTruthForTests(): void {
	threadModelTruth.clear();
}

export { loadModels } from './modelsLoad';

// A switch is never a question: when the thread does not fit the destination,
// the server checkpoints on the model being left and carries on, answering 202
// with the operation to follow. The 428 that used to come back here — and the
// dialog that asked which model should fold — are gone.
export async function saveThreadModel(
	threadId: string,
	modelId: string,
	token: string,
	/** 'setup' marks the model setup itself gave the first chat; a person's own pick sends nothing. */
	source?: 'setup',
): Promise<ThreadModelSaveResult> {
	const res = await fetch(`/api/threads/${threadId}?token=${token}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ model: modelId, ...(source ? { source } : {}) }),
	});
	const body = await res.json().catch(() => null);
	if (!res.ok) {
		// A serialized detail object was reaching a toast verbatim. The user gets
		// a sentence; the machine detail stays where machine detail belongs.
		if (body?.detail !== undefined) console.error('[models] switch rejected', body.detail);
		throw new Error(plainErrorText(body?.detail, 'Could not switch model. Try again.'));
	}
	if (res.status === 202) {
		return applyPendingThreadModelSwitch(threadId, body);
	}
	const committedModel = body?.model;
	const updatedAt = body?.model_updated_at;
	if (
		typeof committedModel !== 'string'
		|| !committedModel
		|| typeof updatedAt !== 'string'
		|| !updatedAt
	) {
		throw new Error('Model switch response did not include committed model truth');
	}
	return {
		kind: 'committed',
		accepted: acceptThreadModelTruth(threadId, committedModel, updatedAt),
		model: committedModel,
		updatedAt,
		warning: typeof body?.warning === 'string' ? body.warning : null,
	};
}
