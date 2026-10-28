import type { ContextUsage } from './contextUsageTypes';
import type { Checkpoint } from './checkpoints';
import type { ContextPreviewEntry } from './contextUsageByThread';

/** Who a preview belongs to, and when it must be read again. */
export function previewIdentity(
	threadId: string,
	modelId: string | null,
	measured: ContextUsage | null,
	latest: Checkpoint | null,
): { owner: string; key: string } {
	const owner = JSON.stringify([
		threadId,
		modelId,
		measured?.snapshot_id ?? null,
		measured?.token_source ?? null,
		measured?.token_usage_available ?? null,
		measured?.total_used ?? null,
		measured?.model_context_window ?? null,
	]);
	const checkpoint = [latest?.id ?? null, latest?.status ?? null, latest?.startedAt ?? null];
	return { owner, key: JSON.stringify([owner, checkpoint]) };
}

/** The reading both the donut and the sheet headline show: preview ?? measured. */
export function contextReading(
	preview: ContextPreviewEntry | undefined,
	owner: string,
	measured: ContextUsage | null,
	sheetOpen = false,
): ContextUsage | null {
	if (preview?.owner === owner && preview.usage) return preview.usage;
	if (sheetOpen && preview?.previous && (preview.owner === null || (preview.owner === owner && preview.loading))) {
		return preview.previous;
	}
	return measured;
}
