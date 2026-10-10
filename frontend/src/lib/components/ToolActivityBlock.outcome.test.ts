import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ToolActivityBlock from './ToolActivityBlock.svelte';
import { settleActiveTool, startActiveTool } from '$lib/stores/activeTools';
import type { ActiveTool } from '$lib/stores/threads';

type ActivityTool = ComponentProps<typeof ToolActivityBlock>['tools'][number];

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function rows(tools: ActivityTool[]): { phrase: string; outcome: string | null }[] {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ToolActivityBlock, {
		target: host,
		props: { tools, blockKey: 'b', expandedTools: new Set(['b']), onToggle: () => {} },
	});
	flushSync();
	return [...host.querySelectorAll('.ta-row')].map((row) => ({
		phrase: row.querySelector('.ta-phrase')?.textContent?.trim() ?? '',
		outcome: row.querySelector('.ta-outcome')?.textContent?.trim() ?? null,
	}));
}

function summary(tools: ActivityTool[], live = false) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ToolActivityBlock, {
		target: host,
		props: { tools, live, blockKey: 'b', expandedTools: new Set(), onToggle: () => {} },
	});
	flushSync();
	const button = host.querySelector<HTMLButtonElement>('.ta-summary')!;
	return {
		label: button.getAttribute('aria-label'),
		hasCheck: Boolean(button.querySelector('.ta-check')),
		hasFailure: Boolean(button.querySelector('.ta-failure')),
		hasSpinner: Boolean(button.querySelector('.ta-spin')),
	};
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	host = null;
});

describe('tool row outcome', () => {
	it('renders the projected phrase and outcome, with a failure first line', () => {
		expect(rows([
			{ id: 'a', name: 'read_file', phrase: 'Reading a.md', outcome: 'done', status: 'done' },
			{
				id: 'b', name: 'bash', phrase: 'Running ls', outcome: 'failed',
				errorLine: 'Error executing bash: boom', result: 'Error executing bash: boom\nmore',
				status: 'done',
			},
			{ id: 'c', name: 'Bash', phrase: 'Running rm', outcome: 'refused', status: 'done' },
			{ id: 'd', name: 'bash', phrase: 'Running sleep', outcome: null, status: 'done' },
			{ id: 'e', name: 'bash', phrase: 'Running make', outcome: 'unknown', status: 'done' },
		])).toEqual([
			{ phrase: 'Reading a.md', outcome: 'done' },
			{ phrase: 'Running ls', outcome: 'failed — Error executing bash: boom' },
			{ phrase: 'Running rm', outcome: 'refused' },
			{ phrase: 'Running sleep', outcome: null },
			{ phrase: 'Running make', outcome: 'Outcome unknown' },
		]);
	});

	it('styles only failed and refused as bad; unknown stays neutral', () => {
		rows([
			{ id: 'a', name: 'bash', phrase: 'Running ls', outcome: 'failed', status: 'done' },
			{ id: 'b', name: 'bash', phrase: 'Running rm', outcome: 'refused', status: 'done' },
			{ id: 'c', name: 'bash', phrase: 'Running make', outcome: 'unknown', status: 'done' },
		]);
		const bad = [...host!.querySelectorAll('.ta-outcome')].map((el) => el.classList.contains('bad'));
		expect(bad).toEqual([true, true, false]);
	});

	it('an unknown row still opens to what the model received', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(ToolActivityBlock, {
			target: host,
			props: {
				tools: [{
					id: 'a', name: 'bash', phrase: 'Running make', outcome: 'unknown',
					result: 'No result was recorded before the turn ended', status: 'done',
				}],
				blockKey: 'b', expandedTools: new Set(['b', 'a']), onToggle: () => {},
			},
		});
		flushSync();
		expect(host.querySelector('.ta-detail')?.textContent).toContain(
			'No result was recorded before the turn ended'
		);
	});

	it('never reads a result string to invent a failure', () => {
		expect(rows([
			{ id: 'a', name: 'bash', phrase: 'Running ls', outcome: 'done', result: 'Error in output', status: 'done' },
		])).toEqual([{ phrase: 'Running ls', outcome: 'done' }]);
	});
});

describe('tool group summary outcome', () => {
	const read = (id: string, outcome: ActivityTool['outcome'], status: ActivityTool['status'] = 'done') => ({
		id, name: 'read_file', object: `/${id}.md`, phrase: `Reading ${id}.md`, outcome, status,
	});

	it('keeps the all-done summary unchanged', () => {
		const result = summary([read('a', 'done')]);
		expect(result).toMatchObject({ label: 'Read a.md', hasCheck: true, hasFailure: false });
	});

	it('marks failed calls and names their count', () => {
		const result = summary([read('a', 'failed')]);
		expect(result).toMatchObject({ label: 'Read a.md · 1 failed', hasCheck: false, hasFailure: true });
	});

	it('marks refused calls and names their count', () => {
		const result = summary([read('a', 'refused')]);
		expect(result).toMatchObject({ label: 'Read a.md · 1 refused', hasCheck: false, hasFailure: true });
	});

	it('shows unknown calls neutrally without a success check', () => {
		const result = summary([read('a', 'unknown')]);
		expect(result).toMatchObject({ label: 'Read a.md · 1 unknown', hasCheck: false, hasFailure: false });
	});

	it('shows every non-success count while failure wins the summary mark', () => {
		const result = summary([read('a', 'failed'), read('b', 'refused'), read('c', 'unknown')]);
		expect(result).toMatchObject({
			label: 'Read 3 files · 1 failed · 1 refused · 1 unknown', hasCheck: false, hasFailure: true,
		});
	});

	it('keeps a running group active when it has no failed outcome', () => {
		const result = summary([read('a', null, 'running')], true);
		expect(result).toMatchObject({ label: 'Read a.md', hasCheck: false, hasFailure: false, hasSpinner: true });
	});

	it('keeps spinning while a call still runs after an earlier failure', () => {
		const result = summary([read('a', 'failed'), read('b', null, 'running')], true);
		expect(result).toMatchObject({
			label: 'Read 2 files · 1 failed', hasCheck: false, hasFailure: false, hasSpinner: true,
		});
	});

	it('shows an interrupted call with no count and no success check', () => {
		const result = summary([read('a', 'done'), read('b', null, 'running')], false);
		expect(result).toMatchObject({
			label: 'Read 2 files', hasCheck: false, hasFailure: false, hasSpinner: false,
		});
	});

	it('shows a finished call with no stored result with no count and no success check', () => {
		const result = summary([read('a', 'done'), read('b', null)]);
		expect(result).toMatchObject({ label: 'Read 2 files', hasCheck: false, hasFailure: false });
	});
});

describe('startActiveTool', () => {
	it('carries the projected phrase and object onto the running row', () => {
		expect(startActiveTool({
			tool_id: 't', name: 'read_file', hint: '', phrase: 'Reading a.md', object: '/w/a.md', args: '{}',
		})).toEqual({
			id: 't', name: 'read_file', hint: '', phrase: 'Reading a.md', object: '/w/a.md',
			status: 'running', args: '{}',
		});
	});
});

describe('settleActiveTool', () => {
	it('takes the done frame phrase from final args over a streamed partial one', () => {
		const running: ActiveTool = {
			id: 't', name: 'read_file', hint: '', status: 'running', phrase: 'Reading',
		};
		expect(settleActiveTool(running, {
			phrase: 'Reading a.md', object: '/w/a.md', outcome: 'failed',
			error_line: 'Error: gone', result: 'Error: gone',
		})).toMatchObject({
			status: 'done', phrase: 'Reading a.md', object: '/w/a.md',
			outcome: 'failed', error_line: 'Error: gone',
		});
	});

	it('keeps the start phrase when the done frame carries none', () => {
		const running: ActiveTool = {
			id: 't', name: 'bash', hint: '', status: 'running', phrase: 'Running ls',
		};
		const settled = settleActiveTool(running, { outcome: 'done', result: 'ok' });
		expect(settled.phrase).toBe('Running ls');
		expect(settled.error_line).toBeUndefined();
	});
});
