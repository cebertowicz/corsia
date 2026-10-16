import { describe, it, expect } from 'vitest';
import { buildPaneConfig } from '$lib/homePaneConfig';
import type { Thread } from '$lib/stores/threads';

const SCOPE = { id: 'proj-1', name: 'CORSIA' };

function makeThread(id: string): Thread {
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
	it('uses identical Plan and Artifacts sections for every chat thread', () => {
		const conductor = pane('chat', makeThread('conductor-thread'), SCOPE);
		const ordinary = pane('chat', makeThread('ordinary-thread'), SCOPE);
		expect(conductor.sections).toEqual(ordinary.sections);
		expect(conductor.sections.map((section) => section.kind)).toEqual(['plan', 'artifacts']);
	});

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

	it('shows Projects only for workspace Activity', () => {
		expect(pane('activity', null, null).sections.map((section) => section.kind)).toEqual(['projects']);
		expect(buildPaneConfig('activity', null, SCOPE)).toBeNull();
	});

	it('suppresses every pane while settings take over', () => {
		expect(buildPaneConfig('chat', makeThread('thread'), SCOPE, null, true)).toBeNull();
		expect(buildPaneConfig('activity', null, null, null, true)).toBeNull();
	});
});
