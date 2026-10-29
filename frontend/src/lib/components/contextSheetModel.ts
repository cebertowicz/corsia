import type { ContextUsage } from '$lib/stores/threads';
import { contextSourceActionLabel, contextSourceOwnerLabel } from '$lib/contextSourceNavigation';
import { promptTokenLabel } from './contextSheetFormat';
import { usageSourceRows } from './contextSheetUsageRows';
import {
	SCOPE_GROUPS,
	cleanLabel,
	expandSkillsIndex,
	isAgentThread,
	scopeGroupId,
	sourceScope,
	sourceTags,
} from './contextSheetScopes';
export { contextSheetUsageSummary, type ContextSheetUsageSummary } from './contextUsageSummary';

/** The fixed marker text for a row whose file changed after it was installed. */
export const PENDING_EDIT_LABEL = 'Edited · applies at next checkpoint';

export interface ContextSheetSourceRow {
	id: string;
	layer: string;
	category: string;
	label: string;
	stableReference: string;
	filePath?: string;
	status: string;
	reason: string;
	ownerSurface: string;
	ownerLabel: string;
	projectId?: string | null;
	actionLabel: string;
	statusLabel: string;
	statusClass: string;
	deltaLabel: string;
	countLabel: string;
	tokenCount: number | null;
	tokenLabel: string;
	/** The file or skill name as stored, before the display label drops its prefix and version. */
	fullLabel?: string;
	/** Readable markers that sit beside the label ("agents only", "trial"); never colour-only. */
	tags?: string[];
	/** Skills are counted apart from the instruction files of the same scope. */
	kind?: 'instruction' | 'skill';
	/** An edit on disk the thread has not received; it applies at the next checkpoint. */
	pendingEdit?: boolean;
}

export interface ContextSheetSourceGroup {
	/** Stable across previews and renames: the scope, never the display name. */
	id: string;
	category: string;
	sources: ContextSheetSourceRow[];
	instructions: ContextSheetSourceRow[];
	skills: ContextSheetSourceRow[];
	instructionsTokenLabel: string;
	skillsTokenLabel: string;
	tokenCount: number;
	deltaLabel: string;
	countLabel: string;
	tokenLabel: string;
}

export interface ContextSheetSnapshotModel {
	sources: ContextSheetSourceRow[];
	sourceGroups: ContextSheetSourceGroup[];
}

type ContextSourceInventoryEntry = NonNullable<NonNullable<ContextUsage['context_snapshot']>['source_inventory']>[number];

function text(value: unknown, fallback = ''): string {
	return typeof value === 'string' && value.length > 0 ? value : fallback;
}

function basename(value: string): string {
	const clean = value.split(/[?#]/, 1)[0]?.replace(/\\/g, '/') ?? '';
	const parts = clean.split('/').filter(Boolean);
	return parts.at(-1) ?? value;
}

function fileNameFromSource(source: ContextSourceInventoryEntry): string {
	const metadataName = text(source.metadata?.name);
	if (metadataName) return metadataName.includes('/') || metadataName.includes('\\') ? basename(metadataName) : metadataName;
	const stableReference = text(source.stable_reference);
	if (stableReference.startsWith('/')) return basename(stableReference);
	const label = text(source.label);
	const afterColon = label.includes(':') ? label.split(':').slice(1).join(':').trim() : '';
	if (afterColon.includes('/')) return basename(afterColon);
	if (afterColon) return afterColon;
	const id = text(source.source_id);
	if (id.includes('/')) return basename(id);
	return '';
}

function displaySourceLabel(source: ContextSourceInventoryEntry): string {
	const originSection = text(source.origin_section);
	const fileName = fileNameFromSource(source);
	const provider = text(source.metadata?.provider);
	if (provider === 'role_context' && fileName) return cleanLabel(fileName);
	if ((originSection === 'corsia' || originSection === 'system' || originSection === 'workspace' || originSection === 'project' || originSection === 'thread') && fileName) {
		return cleanLabel(fileName);
	}
	const label = text(source.label, text(source.source_id, 'Source'));
	if (label.includes('/Users/') || label.startsWith('/')) {
		const prefix = label.includes(':') ? `${label.split(':')[0]}: ` : '';
		return `${prefix}${basename(label)}`;
	}
	return cleanLabel(label);
}

function displayStatus(status: string): { label: string; className: string } {
	if (status === 'included') return { label: 'Included', className: 'included' };
	if (status === 'omitted_limit') return { label: 'Omitted', className: 'omitted' };
	if (status === 'omitted') return { label: 'Omitted', className: 'omitted' };
	if (status === 'redacted') return { label: 'Redacted', className: 'redacted' };
	if (status === 'blocked') return { label: 'Blocked', className: 'blocked' };
	if (status === 'proof') return { label: 'Proof', className: 'included' };
	if (status === 'preview') return { label: 'Preview', className: 'preview' };
	return { label: status || 'Unknown', className: status || 'unknown' };
}



export function formatContextDelta(metadata: Record<string, unknown> | undefined): string {
	if (!metadata) return '';
	const delta = metadata.delta;
	if (typeof delta === 'string') return delta;
	if (delta && typeof delta === 'object') {
		const row = delta as Record<string, unknown>;
		const added = typeof row.added === 'number' ? row.added : 0;
		const removed = typeof row.removed === 'number' ? row.removed : 0;
		const changed = typeof row.changed === 'number' ? row.changed : 0;
		const parts = [
			added > 0 ? `+${added}` : '',
			removed > 0 ? `-${removed}` : '',
			changed > 0 ? `~${changed}` : '',
		].filter(Boolean);
		return parts.join(' ');
	}
	return '';
}

function countLabel(metadata: Record<string, unknown> | undefined): string {
	if (!metadata) return '';
	if (metadata.provider === 'mcp_index' && typeof metadata.tool_count === 'number') {
		return `${Math.max(0, Math.round(metadata.tool_count))} actions`;
	}
	if (metadata.provider === 'agent_owner_map' && typeof metadata.agent_count === 'number') {
		const count = Math.max(0, Math.round(metadata.agent_count));
		return count === 1 ? '1 Agent' : `${count} Agents`;
	}
	if (metadata.provider === 'skills') {
		const skillCount = typeof metadata.skill_count === 'number' ? Math.max(0, Math.round(metadata.skill_count)) : 0;
		const omittedCount = typeof metadata.omitted_count === 'number' ? Math.max(0, Math.round(metadata.omitted_count)) : 0;
		const errorCount = typeof metadata.error_count === 'number' ? Math.max(0, Math.round(metadata.error_count)) : 0;
		const parts = [
			skillCount ? (skillCount === 1 ? '1 Skill' : `${skillCount} Skills`) : '',
			omittedCount ? `${omittedCount} Omitted` : '',
			errorCount ? `${errorCount} Error${errorCount === 1 ? '' : 's'}` : '',
		].filter(Boolean);
		return parts.join(' · ');
	}
	return '';
}

function sumTokens(sources: ContextSheetSourceRow[]): number {
	return sources.reduce((sum, source) => sum + (source.tokenCount ?? 0), 0);
}

function groupedSources(sources: ContextSheetSourceRow[]): ContextSheetSnapshotModel['sourceGroups'] {
	const buckets = new Map<string, ContextSheetSourceRow[]>(SCOPE_GROUPS.map((group) => [group.id, []]));
	for (const source of sources) buckets.get(scopeGroupId(source.category))?.push(source);
	return SCOPE_GROUPS
		.map((group) => ({ group, rows: buckets.get(group.id) ?? [] }))
		.filter(({ rows }) => rows.length > 0)
		.map(({ group, rows }) => {
			const orderedRows = group.id === 'tools'
				? [...rows].sort((left, right) => {
					if (left.label === 'CORSIA tools') return 1;
					if (right.label === 'CORSIA tools') return -1;
					return (right.tokenCount ?? 0) - (left.tokenCount ?? 0);
				})
				: rows;
			const skills = orderedRows.filter((row) => row.kind === 'skill');
			const instructions = orderedRows.filter((row) => row.kind !== 'skill');
			const tokenCount = sumTokens(orderedRows);
			return {
				id: group.id,
				category: group.name,
				sources: orderedRows,
				instructions,
				skills,
				instructionsTokenLabel: promptTokenLabel(sumTokens(instructions)),
				skillsTokenLabel: promptTokenLabel(sumTokens(skills)),
				tokenCount,
				deltaLabel: groupDeltaLabel(orderedRows),
				countLabel: groupCountLabel(orderedRows),
				tokenLabel: orderedRows.some((row) => row.tokenCount != null) ? promptTokenLabel(tokenCount) : '',
			};
		});
}

function visibleComposerSource(source: ContextSheetSourceRow): boolean {
	return source.layer !== 'model' && source.layer !== 'preflight' && source.layer !== 'user';
}

function groupDeltaLabel(sources: ContextSheetSourceRow[]): string {
	const totals = { added: 0, removed: 0, changed: 0 };
	for (const source of sources) {
		for (const match of source.deltaLabel.matchAll(/([+\-~])(\d+)/g)) {
			const amount = Number(match[2] ?? 0);
			if (match[1] === '+') totals.added += amount;
			if (match[1] === '-') totals.removed += amount;
			if (match[1] === '~') totals.changed += amount;
		}
	}
	return [
		totals.added > 0 ? `+${totals.added}` : '',
		totals.removed > 0 ? `-${totals.removed}` : '',
		totals.changed > 0 ? `~${totals.changed}` : '',
	].filter(Boolean).join(' ');
}

function groupCountLabel(sources: ContextSheetSourceRow[]): string {
	const counts = sources.map((source) => source.countLabel).filter(Boolean);
	return counts.join(' · ');
}

function projectId(metadata: Record<string, unknown> | undefined): string | null | undefined {
	const value = metadata?.project_id;
	return typeof value === 'string' ? value : undefined;
}

function promptTokenCount(source: ContextSourceInventoryEntry): number | null {
	const value = source.prompt_tokens;
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
	return Math.round(value);
}

function inventoryRow(source: ContextSourceInventoryEntry, isAgent: boolean): ContextSheetSourceRow {
	const rowProjectId = projectId(source.metadata);
	const originSection = text(source.origin_section, 'source');
	const status = text(source.inclusion, 'unknown');
	const statusDisplay = displayStatus(status);
	const ownerSurface = text(source.owner_surface);
	const stableReference = text(source.stable_reference);
	const filePath = text(source.metadata?.path);
	const tokenCount = promptTokenCount(source);
	return {
		id: text(source.source_id),
		layer: originSection,
		category: sourceScope(originSection, source.metadata, isAgent),
		label: source.metadata?.provider === 'mcp_index' ? text(source.metadata?.display_name, text(source.metadata?.server_name, text(source.label))) : displaySourceLabel(source),
		fullLabel: fileNameFromSource(source) || text(source.label),
		tags: sourceTags(originSection, source.metadata),
		...(source.edited_since_install === true ? { pendingEdit: true } : {}),
		kind: text(source.metadata?.provider) === 'skills' ? 'skill' : 'instruction',
		stableReference,
		filePath,
		status,
		reason: text(source.reason),
		ownerSurface,
		ownerLabel: contextSourceOwnerLabel({ layer: originSection, ownerSurface, stableReference, filePath }),
		...(rowProjectId !== undefined ? { projectId: rowProjectId } : {}),
		deltaLabel: formatContextDelta(source.metadata),
		actionLabel: contextSourceActionLabel({ layer: originSection, ownerSurface, stableReference, filePath }),
		statusLabel: statusDisplay.label,
		statusClass: statusDisplay.className,
		tokenCount,
		tokenLabel: promptTokenLabel(tokenCount),
		countLabel: countLabel(source.metadata),
	};
}

export function contextSheetSnapshotModel(usage: ContextUsage): ContextSheetSnapshotModel | null {
	const snapshot = usage.context_snapshot;
	if (!snapshot) return null;

	const isAgent = isAgentThread(snapshot.agent_facts);
	const inventorySources = (snapshot.source_inventory ?? []).flatMap((source) => {
		const row = inventoryRow(source, isAgent);
		if (!(row.id || row.label) || !visibleComposerSource(row)) return [];
		const listed = row.kind === 'skill' && row.status === 'included'
			? expandSkillsIndex(row, source.metadata, isAgent)
			: null;
		return listed ?? [row];
	});
	const inventoryHasToolDefinitions = inventorySources.some(
		(source) => (
			(source.layer === 'tool' || source.layer === 'tools')
			&& source.tokenCount !== null
		)
	);
	const syntheticSources = usageSourceRows(usage).filter((source) => (
		!inventorySources.some((row) => row.id === source.id)
		&& !(source.id === 'usage:tool-definitions' && inventoryHasToolDefinitions)
	));
	const sources = [...inventorySources, ...syntheticSources];

	return {
		sources,
		sourceGroups: groupedSources(sources),
	};
}
