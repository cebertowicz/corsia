/**
 * Warming the active thread's provider cache, and the toast that reports it.
 *
 * Extracted from `ChatPanel.svelte` unchanged: the panel had grown past its
 * size budget and this is a self-contained operation — dedupe key, abort of a
 * superseded warm, and the toast lifecycle — with no bearing on how the
 * transcript renders.
 */

import { get } from 'svelte/store';

import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';
import { activeModelId, findModelById, getModelDispatchId, modelAvailability } from '$lib/stores/models';

const warmDedupKeys = new Set<string>();
const warmAborts = new Map<string, AbortController>();

/** A finished turn changes the prefix, so its warm keys no longer describe it. */
export function clearWarmKeysForThread(threadId: string): void {
	const prefix = `${threadId}:`;
	for (const key of warmDedupKeys) {
		if (key.startsWith(prefix)) warmDedupKeys.delete(key);
	}
}

/** Warm the network model selected for the active thread. Safe to call often. */
export async function warmActiveThread(): Promise<void> {
	const sid = get(activeThreadId);
	if (!sid) return;

	const { models: modelsStore } = await import('$lib/stores/models');
	const allModels = get(modelsStore);
	const model = findModelById(allModels, get(activeModelId));
	if (!model || model.backend !== 'network' || modelAvailability(model).status !== 'ready') return;

	const threadMessages = get(messages).filter(message => message.thread_id === sid);
	const tail = threadMessages.at(-1);
	const prefixVersion = tail
		? `${tail.id ?? ''}:${tail.delivery_revision ?? ''}`
		: 'empty';
	const warmKey = `${sid}:${getModelDispatchId(model)}:${prefixVersion}`;
	if (warmDedupKeys.has(warmKey)) return;

	const prior = warmAborts.get(sid);
	if (prior) prior.abort();
	const controller = new AbortController();
	warmAborts.set(sid, controller);
	warmDedupKeys.add(warmKey);

	const { showWarmToast, completeWarmToast, dismissWarmToast } =
		await import('$lib/stores/warmToast');
	showWarmToast('Warming conversation…');
	const t0 = performance.now();
	try {
		const res = await fetch(`/api/threads/${sid}/warm?token=${get(token)}`, {
			method: 'POST',
			signal: controller.signal,
		});
		if (!res.ok) {
			warmDedupKeys.delete(warmKey);
			dismissWarmToast();
			return;
		}
		const data = await res.json().catch(() => ({} as any));
		if (data?.ok === false) {
			if (!data?.skipped) warmDedupKeys.delete(warmKey);
			dismissWarmToast();
			return;
		}
		if (data?.progress && data.progress.total_blocks > 0) {
			const p = data.progress;
			showWarmToast(
				`Warming conversation (${p.cold_blocks_at_start}/${p.total_blocks} blocks)`,
				'warming',
				undefined,
				{
					totalBlocks: p.total_blocks,
					coldBlocksAtStart: p.cold_blocks_at_start,
					blockSize: p.block_size,
				},
			);
		}
		completeWarmToast(Math.round((performance.now() - t0) / 1000));
	} catch {
		warmDedupKeys.delete(warmKey);
		dismissWarmToast();
	} finally {
		if (warmAborts.get(sid) === controller) warmAborts.delete(sid);
	}
}
