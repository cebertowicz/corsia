// Where each context row comes from. The sheet groups by this scope so a user
// can see which layer a token belongs to and where to change it.
import type { ContextSheetSourceRow } from './contextSheetModel';
import { promptTokenLabel } from './contextSheetFormat';

export const SCOPE_GROUPS = [
	{ id: 'system', name: 'System' },
	{ id: 'workspace', name: 'Workspace' },
	{ id: 'project', name: 'Project' },
	{ id: 'agent', name: 'Agent' },
	{ id: 'thread', name: 'This thread' },
	{ id: 'tools', name: 'Tools' },
	{ id: 'history', name: 'History' },
	{ id: 'other', name: 'Other' },
] as const;

export type ScopeGroupId = (typeof SCOPE_GROUPS)[number]['id'];

/** Categorical colours for the bar and the group labels; names and counts carry the meaning too. */
export const GROUP_COLOR: Record<ScopeGroupId, string> = {
	system: 'var(--violet)',
	workspace: 'var(--sky)',
	project: 'var(--teal)',
	agent: 'var(--violet-2)',
	thread: 'var(--violet-2)',
	tools: 'var(--orange)',
	history: 'var(--green)',
	other: 'var(--text-tertiary)',
};

const GROUP_NAME = new Map<string, ScopeGroupId>(SCOPE_GROUPS.map((group) => [group.name, group.id]));

export function scopeGroupId(category: string): ScopeGroupId {
	return GROUP_NAME.get(category) ?? 'other';
}

function text(value: unknown): string {
	return typeof value === 'string' ? value : '';
}

/** A bare scope name ("workspace", "thread") as the group it belongs to. */
function scopeName(scope: string, isAgent: boolean): string {
	if (scope === 'workspace') return 'Workspace';
	if (scope === 'project') return 'Project';
	if (scope === 'thread') return isAgent ? 'Agent' : 'This thread';
	return 'System';
}

/** The group name one inventory row belongs to; every row sits in exactly one. */
export function sourceScope(
	originSection: string,
	metadata: Record<string, unknown> | undefined,
	isAgent: boolean,
): string {
	const provider = text(metadata?.provider);
	if (provider === 'skills' || provider === 'role_context') {
		return scopeName(text(metadata?.scope), isAgent);
	}
	if (provider === 'agent_owner_map') return isAgent ? 'Agent' : 'System';
	if (originSection === 'conductor' || provider === 'conductor_template') return 'System';
	if (originSection === 'corsia' || originSection === 'system') return 'System';
	if (originSection === 'workspace') return 'Workspace';
	if (originSection === 'project') return 'Project';
	if (originSection === 'thread') return isAgent ? 'Agent' : 'This thread';
	if (originSection === 'role') return 'Agent';
	if (originSection === 'tool' || originSection === 'tools' || originSection === 'model' || originSection === 'preflight') return 'Tools';
	if (originSection === 'history' || originSection === 'summary' || originSection === 'provider') return 'History';
	return 'Other';
}

/** Role files, packaged conductor templates and the responsibility map reach agents only. */
export function sourceTags(originSection: string, metadata: Record<string, unknown> | undefined): string[] {
	const provider = text(metadata?.provider);
	const agentsOnly = provider === 'role_context'
		|| provider === 'conductor_template'
		|| (provider === 'skills' && Boolean(metadata?.role));
	return agentsOnly || originSection === 'conductor' ? ['agents only'] : [];
}

/** A display name without its ordering prefix ("10-") or version suffix ("(v1)"). */
export function cleanLabel(name: string): string {
	return name
		.replace(/^\d+[-_.]\s*/, '')
		.replace(/\s*\(v\d+\)\s*$/i, '')
		.trim() || name;
}

export function isAgentThread(agentFacts: Record<string, unknown> | undefined): boolean {
	if (!agentFacts) return false;
	const roles = agentFacts.roles;
	return (Array.isArray(roles) && roles.length > 0)
		|| agentFacts.receives_agent_role === true
		|| agentFacts.is_conductor === true;
}

interface ListedSkill {
	name: string;
	scope: string;
	scopeId: string;
	role: string;
	trial: boolean;
	tokens: number | null;
}

function listedSkills(metadata: Record<string, unknown> | undefined): ListedSkill[] | null {
	const raw = metadata?.skills;
	if (!Array.isArray(raw)) return null;
	const listed: ListedSkill[] = [];
	for (const entry of raw) {
		if (!entry || typeof entry !== 'object') return null;
		const row = entry as Record<string, unknown>;
		const name = text(row.name);
		if (!name) return null;
		const tokens = typeof row.prompt_tokens === 'number' && Number.isFinite(row.prompt_tokens)
			? Math.max(0, Math.round(row.prompt_tokens))
			: null;
		listed.push({
			name,
			scope: text(row.scope),
			scopeId: text(row.scope_id),
			role: text(row.role),
			trial: typeof row.lesson_id === 'string' && row.lesson_id.length > 0,
			tokens,
		});
	}
	return listed;
}

/**
 * One row per listed skill, in the scope that holds it, plus the index's own
 * header as a System row so the rows still add up to the block's count.
 * Returns null when the install did not record its skills (or a skill has no
 * estimate): the block then stays one honest row.
 */
export function expandSkillsIndex(
	index: ContextSheetSourceRow,
	metadata: Record<string, unknown> | undefined,
	isAgent: boolean,
): ContextSheetSourceRow[] | null {
	if (index.tokenCount === null) return null;
	const listed = listedSkills(metadata);
	if (listed === null || listed.some((skill) => skill.tokens === null)) return null;
	const rows: ContextSheetSourceRow[] = listed.map((skill) => {
		const category = scopeName(skill.scope, isAgent);
		const tags = [...(skill.role ? ['agents only'] : []), ...(skill.trial ? ['trial'] : [])];
		return {
			...index,
			id: `skill:${skill.scope}:${skill.scopeId}:${skill.role}:${skill.name}`,
			category,
			label: skill.name,
			fullLabel: skill.name,
			stableReference: '',
			reason: `${category} skill${skill.trial ? ' (trial, unproven)' : ''}`,
			countLabel: '',
			tokenCount: skill.tokens,
			tokenLabel: promptTokenLabel(skill.tokens),
			tags,
			kind: 'skill',
		};
	});
	const header = index.tokenCount - rows.reduce((sum, row) => sum + (row.tokenCount ?? 0), 0);
	if (header > 0) {
		rows.unshift({
			...index,
			id: `${index.id}:header`,
			category: 'System',
			label: 'Skills index',
			fullLabel: 'Skills index',
			countLabel: '',
			tokenCount: header,
			tokenLabel: promptTokenLabel(header),
			tags: [],
			kind: 'skill',
		});
	}
	return rows;
}

export interface BarSegment {
	id: string;
	name: string;
	tokens: number;
	/** Width as a percentage of the window, unrounded. */
	share: number;
	percent: number;
}

export interface BarLayout {
	/** One segment per non-empty group; empty unless the rows add up to the headline exactly. */
	segments: BarSegment[];
	/** Set when they do not: the rows' sum, shown beside the headline total instead of a split bar. */
	mismatch: { itemized: number; used: number } | null;
}

interface BarGroup {
	id: string;
	category: string;
	tokenCount: number;
}

/**
 * The grouped bar only when the displayed rows add up to the
 * headline total exactly, so its fill can never disagree with the headline.
 * Otherwise no split is drawn and the gap is disclosed, never absorbed.
 */
export function barLayout(groups: BarGroup[], usedTokens: number, windowTokens: number): BarLayout {
	if (windowTokens <= 0 || groups.length === 0) return { segments: [], mismatch: null };
	const total = groups.reduce((sum, group) => sum + group.tokenCount, 0);
	// An overcount row is negative: the groups can then sum to the total while the
	// positive segments overshoot it, so a negative group is never a reconciled bar.
	const negative = groups.some((group) => group.tokenCount < 0);
	if (negative || total !== usedTokens) {
		const itemized = groups.reduce((sum, group) => sum + Math.max(0, group.tokenCount), 0);
		return { segments: [], mismatch: { itemized, used: usedTokens } };
	}
	const segments = groups
		.filter((group) => group.tokenCount > 0)
		.map((group) => {
			const share = (group.tokenCount / windowTokens) * 100;
			return {
				id: group.id,
				name: group.category,
				tokens: group.tokenCount,
				share,
				percent: Math.round(share * 10) / 10,
			};
		});
	return { segments, mismatch: null };
}
