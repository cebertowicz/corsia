import type { ContextUsage } from '$lib/stores/threads';
import type { ContextSheetSourceRow } from './contextSheetModel';
import { promptTokenLabel, signedPromptTokenLabel, pluralCount } from './contextSheetFormat';

export function usageSourceRows(usage: ContextUsage): ContextSheetSourceRow[] {
	const rows: ContextSheetSourceRow[] = [];
	if (usage.tool_def_tokens > 0) {
		// This synthetic row only fires when no measured inventory tool row
		// exists (contextSheetSnapshotModel filters it out otherwise), so it is
		// always the MCP tool-definition estimate, never a measured value —
		// label it as such rather than implying the same exactness as a
		// rendered-content row.
		rows.push({
			id: 'usage:tool-definitions',
			layer: 'tools',
			category: 'Tools',
			label: 'Tool definitions',
			stableReference: '',
			status: 'estimated',
			reason: 'Estimated CORSIA tool-definition size; this harness does not report it separately',
			ownerSurface: 'provider-settings',
			ownerLabel: 'Provider settings',
			actionLabel: '',
			statusLabel: 'Estimated',
			statusClass: 'preview',
			deltaLabel: '',
			countLabel: '',
			tokenCount: Math.round(usage.tool_def_tokens),
			tokenLabel: promptTokenLabel(Math.round(usage.tool_def_tokens)),
		});
	}
	const loadedTools = (usage.loaded_tool_tokens ?? []).filter((entry) => Number.isFinite(entry.tokens) && entry.tokens > 0);
	const loadedTotal = loadedTools.reduce((sum, entry) => sum + Math.round(entry.tokens), 0);
	if (usage.history_tokens > 0 || usage.history_messages > 0) {
		const tokenCount = usage.history_tokens > 0 ? Math.max(0, Math.round(usage.history_tokens) - loadedTotal) : null;
		rows.push({
			id: 'usage:conversation-history',
			layer: 'history',
			category: 'History',
			label: 'Conversation history',
			stableReference: '',
			status: 'included',
			reason: 'Retained conversation history included in the provider context',
			ownerSurface: 'conversation-history',
			ownerLabel: 'Conversation history',
			actionLabel: '',
			statusLabel: 'Included',
			statusClass: 'included',
			deltaLabel: '',
			countLabel: usage.history_messages > 0 ? pluralCount(usage.history_messages, 'Message') : '',
			tokenCount,
			tokenLabel: promptTokenLabel(tokenCount),
		});
	}
	for (const entry of loadedTools) {
		const tokenCount = Math.round(entry.tokens);
		const name = entry.display_name || entry.server;
		rows.push({
			id: `usage:loaded-tool:${entry.server}`, layer: 'history', category: 'History',
			label: `${name}, looked up`, stableReference: '', status: 'included',
			reason: 'Estimated cost of tool definitions this conversation looked up, already inside conversation history',
			ownerSurface: 'conversation-history', ownerLabel: 'Conversation history',
			actionLabel: '', statusLabel: 'Included', statusClass: 'included', deltaLabel: '',
			countLabel: '', tokenCount, tokenLabel: promptTokenLabel(tokenCount),
		});
	}
	if (usage.user_tokens > 0) {
		const tokenCount = Math.round(usage.user_tokens);
		rows.push({
			id: 'usage:current-message',
			layer: 'history',
			category: 'History',
			label: 'Current message',
			stableReference: '',
			status: 'included',
			reason: 'Current user message included in the provider context',
			ownerSurface: 'conversation-history',
			ownerLabel: 'Conversation history',
			actionLabel: '',
			statusLabel: 'Included',
			statusClass: 'included',
			deltaLabel: '',
			countLabel: '',
			tokenCount,
			tokenLabel: promptTokenLabel(tokenCount),
		});
	}
	const checkpoint = checkpointRow(usage);
	if (checkpoint) rows.push(checkpoint);
	const heldByHarness = heldByHarnessRow(usage);
	if (heldByHarness) rows.push(heldByHarness);
	return rows;
}

function count(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.round(value) : null;
}

// The installed checkpoint is counted in the headline but has no inventory
// row of its own in a preview; without this row the History group is short.
function checkpointRow(usage: ContextUsage): ContextSheetSourceRow | null {
	const tokens = count(usage.preflight?.checkpoint_tokens);
	const inventory = usage.context_snapshot?.source_inventory ?? [];
	if (usage.preview !== true || !tokens || inventory.some((row) => row.source_id === 'history:portable-checkpoint')) {
		return null;
	}
	return {
		id: 'usage:checkpoint-summary',
		layer: 'history',
		category: 'History',
		label: 'Checkpoint summary',
		stableReference: '',
		status: 'included',
		reason: 'Installed checkpoint included in the provider context',
		ownerSurface: 'conversation-history',
		ownerLabel: 'Conversation history',
		actionLabel: '',
		statusLabel: 'Included',
		statusClass: 'included',
		deltaLabel: '',
		countLabel: '',
		tokenCount: tokens,
		tokenLabel: promptTokenLabel(tokens),
	};
}

// Native's delta is tokenizer/framing noise around an otherwise-exact prefix;
// surfacing every few-token wobble would just be UI noise, so it only shows
// once it is large enough to mean something. Claude Code and Codex have no
// such exact accounting to fall back on, so their (usually much larger) gap
// always shows once there is a measured total to reconcile against.
const _NATIVE_MATERIAL_DELTA_TOKENS = 256;
const _NATIVE_MATERIAL_DELTA_RATIO = 0.01;

// The reason string is free-text for display; harness identity for behavior
// must come from the structured snapshot field, not a prose match against it.
function harnessFromUsage(usage: ContextUsage): string {
	const value = usage.context_snapshot?.harness_model_facts?.harness;
	return typeof value === 'string' ? value : '';
}

function heldByHarnessRow(usage: ContextUsage): ContextSheetSourceRow | null {
	const delta = usage.attribution_delta_tokens;
	if (typeof delta !== 'number' || !Number.isFinite(delta) || delta === 0) return null;
	const isNativeVariance = harnessFromUsage(usage) === 'native';
	if (isNativeVariance) {
		const total = typeof usage.total_used === 'number' && Number.isFinite(usage.total_used)
			? usage.total_used
			: 0;
		const material = Math.abs(delta) >= _NATIVE_MATERIAL_DELTA_TOKENS
			|| (total > 0 && Math.abs(delta) / total >= _NATIVE_MATERIAL_DELTA_RATIO);
		if (!material) return null;
	}
	// A negative delta means the rows above already add up to more than the
	// provider billed (estimate rounding, mostly on Native) — render that
	// honestly instead of clamping it to zero or hiding it.
	const overcounts = delta < 0;
	const tokenCount = Math.round(delta);
	// Native's delta is pure tokenizer/framing rounding, not harness-held
	// content, and CC/Codex tool definitions now have their own row above
	// (when the estimate succeeds) — so neither case is honestly described as
	// harness-held transcript/system-prompt/tool-definition content anymore.
	const label = overcounts
		? 'Rows exceed provider total (estimate overcounts)'
		: isNativeVariance
			? 'Provider accounting variance (tokenizer/framing)'
			: 'Other harness context (not itemized)';
	return {
		id: 'usage:attribution-delta',
		layer: 'provider',
		category: 'Other',
		label,
		stableReference: '',
		status: 'included',
		reason: overcounts
			? 'Estimated and measured rows add up to more than the provider-reported total; some attribution overlaps'
			: (usage.attribution_delta_reason || 'unobservable harness/provider overhead'),
		ownerSurface: 'provider-settings',
		ownerLabel: 'Provider settings',
		actionLabel: '',
		statusLabel: overcounts ? 'Over-estimate' : 'Included',
		statusClass: overcounts ? 'preview' : 'included',
		deltaLabel: '',
		countLabel: '',
		tokenCount,
		tokenLabel: signedPromptTokenLabel(tokenCount),
	};
}
