import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ContextSheetSources from './ContextSheetSources.svelte';
import { contextSheetSnapshotModel } from './contextSheetModel';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	const snapshot = contextSheetSnapshotModel({
		model_context_window: 10000, system_tokens: 0, tool_def_tokens: 0,
		history_tokens: 1000, user_tokens: 50, total_used: 1050, utilization: 0.105,
		was_trimmed: false, history_messages: 6, context_snapshot: { source_inventory: [] },
		loaded_tool_tokens: [{ server: 'github', display_name: 'GitHub', tokens: 400 }],
	} as any)!;
	component = mount(ContextSheetSources, {
		target: host, props: { snapshot, expandedGroups: { history: true } },
	});
	flushSync();
}

describe('looked-up rows in the History group', () => {
	it('wraps only the looked-up label, so its "(estimated)" is never cut off', () => {
		render();
		const labels = [...host.querySelectorAll('.ctx-source-label')];

		expect(labels.map((label) => [label.textContent, label.classList.contains('wrap')])).toEqual([
			['Conversation history', false],
			['GitHub, looked up (estimated)', true],
			['Current message', false],
		]);
	});

	it('shows no status chip and keeps the count cell so token figures share one edge', () => {
		render();
		const rows = [...host.querySelectorAll('.ctx-source-row')];

		expect(host.querySelector('.ctx-source-status')).toBeNull();
		expect(rows.map((row) => row.querySelectorAll('.ctx-source-count-label').length)).toEqual([1, 1, 1]);
	});
});
