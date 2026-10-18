import { describe, expect, it } from 'vitest';
import { contextSheetSnapshotModel } from './contextSheetModel';
import { SCOPE_GROUPS, cleanLabel, sourceScope } from './contextSheetScopes';

function source(id: string, originSection: string, tokens: number | null, metadata: Record<string, unknown> = {}) {
	return {
		source_id: id,
		origin_section: originSection,
		label: id,
		stable_reference: '',
		freshness: 'fresh',
		inclusion: 'included',
		owner_surface: '',
		metadata,
		prompt_tokens: tokens,
	};
}

function usage(inventory: unknown[], overrides: Record<string, unknown> = {}) {
	return {
		model_context_window: 100_000,
		system_tokens: 0,
		tool_def_tokens: 0,
		history_tokens: 0,
		user_tokens: 0,
		total_used: 0,
		utilization: 0,
		was_trimmed: false,
		history_messages: 0,
		context_snapshot: { snapshot_id: 'scopes', source_inventory: inventory },
		...overrides,
	} as any;
}

const SKILLS_META = {
	provider: 'skills',
	scope: 'project',
	skills: [
		{ name: 'alpha', scope: 'project', scope_id: 'p', role: '', prompt_tokens: 30 },
		{ name: 'beta', scope: 'workspace', scope_id: 'w', role: '', prompt_tokens: 20, lesson_id: 'l1' },
	],
};

describe('scope grouping', () => {
	it('orders groups System to Other and hides empty ones', () => {
		const model = contextSheetSnapshotModel(usage([
			source('h', 'history', 10),
			source('s', 'system', 5),
			source('p', 'project', 7),
		]));
		const names = model!.sourceGroups.map((group) => group.category);
		expect(names).toEqual(['System', 'Project', 'History']);
		expect(names).toEqual(SCOPE_GROUPS.map((group) => group.name).filter((name) => names.includes(name)));
	});

	it('shows the Agent group only for threads with roles', () => {
		expect(sourceScope('thread', {}, false)).toBe('This thread');
		expect(sourceScope('thread', {}, true)).toBe('Agent');
		expect(sourceScope('dynamic', { provider: 'agent_owner_map' }, false)).toBe('System');
		expect(sourceScope('dynamic', { provider: 'agent_owner_map' }, true)).toBe('Agent');
	});

	it('expands the skills index into one row per skill with trial tag and subtotals', () => {
		const model = contextSheetSnapshotModel(usage([source('skills', 'project', 60, SKILLS_META)]));
		const rows = model!.sources.filter((row) => row.kind === 'skill');
		expect(rows.map((row) => [row.label, row.category, row.tokenCount])).toEqual([
			['Skills index', 'System', 10],
			['alpha', 'Project', 30],
			['beta', 'Workspace', 20],
		]);
		expect(rows.find((row) => row.label === 'beta')?.tags).toContain('trial');
		const project = model!.sourceGroups.find((group) => group.category === 'Project');
		expect(project?.skills.map((row) => row.label)).toEqual(['alpha']);
		expect(project?.skillsTokenLabel).toBe('30 tokens');
		expect(rows.reduce((sum, row) => sum + (row.tokenCount ?? 0), 0)).toBe(60);
	});

	it('keeps 41 skill rows reconciled with a name-only overflow tag', () => {
		const skills = Array.from({ length: 41 }, (_, index) => ({
			name: `skill-${index}`,
			scope: 'workspace',
			scope_id: 'workspace',
			role: '',
			listed: index === 40 ? 'name' : 'full',
			prompt_tokens: 1,
		}));
		const model = contextSheetSnapshotModel(usage([
			source('skills', 'dynamic', 100, { provider: 'skills', skills }),
		]));
		const rows = model!.sources.filter((row) => row.kind === 'skill');

		expect(rows.reduce((sum, row) => sum + (row.tokenCount ?? 0), 0)).toBe(100);
		expect(rows.some((row) => row.tokenCount !== null && row.tokenCount < 0)).toBe(false);
		expect(rows.find((row) => row.label === 'skill-40')?.tags).toContain('name only');
	});

	it('keeps one skills row when the install did not record its skills', () => {
		const model = contextSheetSnapshotModel(usage([source('skills', 'project', 60, { provider: 'skills', scope: 'project' })]));
		expect(model!.sources.filter((row) => row.id.startsWith('skill:'))).toEqual([]);
		expect(model!.sources).toHaveLength(1);
	});

	it('makes group totals equal the headline for a backend-shaped preview', () => {
		const model = contextSheetSnapshotModel(usage(
			[source('s', 'system', 400), source('p', 'project', 300), source('h', 'history', 100)],
			{
				preview: true,
				total_used: 800,
				preflight: {},
			},
		));
		const groups = model!.sourceGroups;
		expect(groups.reduce((sum, group) => sum + group.tokenCount, 0)).toBe(800);
	});
});

describe('cleanLabel', () => {
	it('drops ordering prefixes and version suffixes', () => {
		expect(cleanLabel('10-INTENT-AUTHORITY.md (v1)')).toBe('INTENT-AUTHORITY.md');
		expect(cleanLabel('PROJECT.md')).toBe('PROJECT.md');
		expect(cleanLabel('20')).toBe('20');
	});
});
