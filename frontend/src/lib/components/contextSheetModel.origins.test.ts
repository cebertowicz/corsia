import { describe, expect, it } from 'vitest';
import { contextSheetSnapshotModel } from './contextSheetModel';
// Every origin_section the backend emits; test_context_origin_sections_contract.py
// fails when a producer adds one, so a new value cannot reach the sheet unmapped.
import emitted from '../__tests__/fixtures/context-origin-sections.contract.json';

// The group each emitted value lands in, or null where the composer sheet
// hides the row (model facts, preflight and the provider-bound user task).
// "Other" is the visible fallback for a value no rule names; listing it here
// keeps that a recorded choice.
const EXPECTED: Record<string, string | null> = {
	conductor: 'System',
	corsia: 'System',
	dynamic: 'Other',
	evaluation: 'Other',
	history: 'History',
	model: null,
	preflight: null,
	project: 'Project',
	provider: 'History',
	role: 'Agent',
	summary: 'History',
	system: 'System',
	thread: 'This thread',
	tools: 'Tools',
	user: null,
	workspace: 'Workspace',
};

function categoryOf(originSection: string): string | undefined {
	const model = contextSheetSnapshotModel({
		model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0, history_tokens: 0,
		user_tokens: 0, total_used: 1, utilization: 0.001,
		was_trimmed: false, history_messages: 0,
		context_snapshot: {
			snapshot_id: 'origins',
			source_inventory: [{
				source_id: `origin:${originSection}`, origin_section: originSection,
				label: originSection, stable_reference: '', freshness: 'fresh',
				inclusion: 'included', owner_surface: '', metadata: {},
			}],
		},
	});
	return model?.sources.find(source => source.id === `origin:${originSection}`)?.category;
}

describe('sourceCategory covers every emitted origin_section', () => {
	it('has a recorded group for each value the backend emits', () => {
		expect(Object.keys(EXPECTED).filter(key => key !== 'summary').sort())
			.toEqual(expect.arrayContaining([...emitted].sort()));
		for (const origin of emitted) expect(origin in EXPECTED, origin).toBe(true);
	});

	it.each(Object.entries(EXPECTED))('groups %s under %s', (origin, category) => {
		expect(categoryOf(origin) ?? null).toBe(category);
	});

	it('falls back to Other for a value nobody has mapped', () => {
		expect(categoryOf('not-a-real-origin')).toBe('Other');
	});
});
