import { describe, expect, it } from 'vitest';
import { buildPaneConfig } from '$lib/homePaneConfig';
import type { Thread } from '$lib/stores/threads';

const SCOPE = { id: 'proj-1', name: 'CORSIA' };

function makeThread(id = 'thread-1'): Thread {
	return {
		id,
		user_id: 'u1',
		profile: 'personal',
		title: 'Test',
		model: null,
		project_id: undefined,
		created_at: new Date().toISOString(),
		updated_at: new Date().toISOString(),
	};
}

function pane(...args: Parameters<typeof buildPaneConfig>) {
	const config = buildPaneConfig(...args);
	if (!config) throw new Error('expected a pane');
	return config;
}

describe('buildPaneConfig', () => {
	it('keeps Plan and Artifacts when only the active thread ID is available', () => {
		expect(pane('chat', null, SCOPE, 'unloaded-thread').sections.map((section) => section.kind))
			.toEqual(['plan', 'artifacts']);
	});

	it('uses scope sections only on a scoped chat landing', () => {
		expect(pane('chat', null, SCOPE).sections.map((section) => section.title)).toEqual(['Roadmap', 'Documents']);
	});

	it('has no chat pane without a thread or scope', () => {
		expect(buildPaneConfig('chat', null, null)).toBeNull();
	});

	it('labels an unscoped chat thread Workspace and keeps settings out of the pane', () => {
		const config = pane('chat', makeThread(), null);
		expect(config.label).toBe('Workspace');
		expect(config).not.toHaveProperty('gear');
	});

	it('keeps thread context out of the right pane', () => {
		const thread = { ...makeThread(), bootloader_files: JSON.stringify(['/workspace/thread.md']) };
		const config = pane('chat', thread, SCOPE);
		expect(config.sections.map((section) => section.title)).not.toContain('Thread context');
		expect(config.sections.map((section) => section.kind)).toEqual(['plan', 'artifacts']);
	});

	it('shows Projects only for workspace Activity', () => {
		expect(pane('activity', null, null).sections.map((section) => section.kind)).toEqual(['projects']);
		expect(buildPaneConfig('activity', null, SCOPE)).toBeNull();
	});

	it('has no pane for measurement, settings, or documents in either scope', () => {
		for (const view of ['measurement', 'settings', 'documents'] as const) {
			expect(buildPaneConfig(view, null, null)).toBeNull();
			expect(buildPaneConfig(view, null, SCOPE)).toBeNull();
		}
	});

	it('suppresses every pane while settings take over', () => {
		expect(buildPaneConfig('chat', makeThread(), SCOPE, null, true)).toBeNull();
		expect(buildPaneConfig('chat', makeThread(), null, null, true)).toBeNull();
		expect(buildPaneConfig('activity', null, null, null, true)).toBeNull();
	});
});
