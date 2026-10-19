import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ContextSheetSources from './ContextSheetSources.svelte';
import { PENDING_EDIT_LABEL, contextSheetSnapshotModel } from './contextSheetModel';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

function row(id: string, extra: Record<string, unknown> = {}) {
	return {
		source_id: id, origin_section: 'project', label: id, stable_reference: '',
		freshness: 'fresh', inclusion: 'included', owner_surface: '',
		metadata: {}, prompt_tokens: 10, ...extra,
	};
}

function usage(inventory: unknown[]) {
	return {
		model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
		history_tokens: 0, user_tokens: 0, total_used: 10,
		utilization: 0.1, was_trimmed: false, history_messages: 0,
		context_snapshot: { snapshot_id: 'pending', source_inventory: inventory },
	} as any;
}

function rows(inventory: unknown[]) {
	return contextSheetSnapshotModel(usage(inventory))!.sources;
}

describe('pending-edit marker model', () => {
	it('flags only rows the preview marked edited', () => {
		const model = rows([
			row('edited', { edited_since_install: true }),
			row('same', { edited_since_install: false }),
			row('unknown'),
		]);
		expect(model.map((r) => [r.id, r.pendingEdit === true])).toEqual([
			['edited', true], ['same', false], ['unknown', false],
		]);
	});

	it('does not treat a non-boolean value as edited', () => {
		expect(rows([row('x', { edited_since_install: 'true' })])[0].pendingEdit).toBeUndefined();
	});
});

describe('pending-edit marker display', () => {
	function render(inventory: unknown[], onOpenSource?: () => void) {
		host = document.createElement('div');
		document.body.appendChild(host);
		const snapshot = contextSheetSnapshotModel(usage(inventory))!;
		component = mount(ContextSheetSources, {
			target: host,
			props: { snapshot, expandedGroups: { project: true }, onOpenSource },
		});
		flushSync();
	}

	it('names the pending state on an actionable row', () => {
		render([row('edited', { edited_since_install: true }), row('same', { edited_since_install: false })], () => {});
		const buttons = [...host.querySelectorAll('button.ctx-source-row')];
		expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual([
			'Open Project settings for edited, Edited · applies at next checkpoint',
			'Open Project settings for same',
		]);
		expect(buttons[0].querySelector('.ctx-source-pending')?.textContent).toBe(PENDING_EDIT_LABEL);
	});

	it('adds row tags to an actionable source accessible name', () => {
		const skill = {
			id: 'skill:workspace:workspace::deploy-check', layer: 'workspace', category: 'Workspace',
			label: 'deploy-check', fullLabel: 'deploy-check', stableReference: '', status: 'included',
			reason: '', ownerSurface: '', ownerLabel: '', actionLabel: 'Show details', statusLabel: '',
			statusClass: '', deltaLabel: '', countLabel: '', tokenCount: 1, tokenLabel: '1 token',
			tags: ['name only'], kind: 'skill' as const,
		};
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(ContextSheetSources, {
			target: host,
			props: { snapshot: {
				sources: [skill],
				sourceGroups: [{ id: 'workspace', category: 'Workspace', sources: [skill], instructions: [], skills: [skill], instructionsTokenLabel: '', skillsTokenLabel: '1 token', tokenCount: 1, deltaLabel: '', countLabel: '', tokenLabel: '1 token' }],
			}, expandedGroups: { workspace: true }, onOpenSource: () => {} },
		});
		flushSync();

		expect(host.querySelector('button.ctx-source-row')?.getAttribute('aria-label')).toBe('Show details for deploy-check, name only');
	});

	it('shows the exact text on an edited row only', () => {
		expect(PENDING_EDIT_LABEL).toBe('Edited · applies at next checkpoint');
		render([row('edited', { edited_since_install: true }), row('same', { edited_since_install: false })]);
		const markers = host.querySelectorAll('.ctx-source-pending');
		expect(markers).toHaveLength(1);
		expect(markers[0].textContent).toBe('Edited · applies at next checkpoint');
		expect(markers[0].closest('.ctx-source-row')?.textContent).toContain('edited');
	});

	it('shows nothing when no row is edited', () => {
		render([row('a'), row('b', { edited_since_install: false })]);
		expect(host.querySelector('.ctx-source-pending')).toBeNull();
	});
});
