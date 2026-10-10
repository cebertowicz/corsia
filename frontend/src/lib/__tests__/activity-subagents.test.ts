import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ActivityView from '$lib/components/activity/ActivityView.svelte';
import { threads, activeThreadId, type Thread } from '$lib/stores/threads';
import { _resetProjectsForTests, _setProjectsForTests } from '$lib/stores/projects';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { uiScope } from '$lib/stores/uiScope';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { resetPlanRuntimeForTests } from '$lib/stores/plans.svelte';
import { resetObserversForTests } from '$lib/stores/planQuery.svelte';
import { decodeActivityIndexEnvelope } from '$lib/api/activityIndexDecode';
import type { ActivityIndexEnvelope, ActivityIndexRow } from '$lib/api/plans';
import { planDisplay } from './fixtures/operatorPlan';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

vi.mock('$lib/ws', () => ({ on: () => () => {} }));

function thread(id: string): Thread {
	return {
		id, user_id: 'user-1', profile: 'personal', title: id, model: null,
		project_id: 'p1', created_at: '2026-08-14T12:00:00Z', updated_at: '2026-08-14T12:00:00Z',
	};
}

function row(
	threadId: string,
	labelKey: 'working' | 'waiting' | 'finished',
	open: number,
	priorityAt = '2026-08-14T12:00:00Z',
	hasNodes = true,
): ActivityIndexRow {
	return {
		plan: {
			id: `plan-${threadId}`, owner_kind: 'thread', owner_id: threadId,
			state: 'active', revision: 1, updated_at: priorityAt,
		},
		owner: { label: `Plan for ${threadId}`, project_id: 'p1', thread_id: threadId },
		progress: { open, liveness: labelKey === 'working' ? 'advancing' : 'idle' },
		has_nodes: hasNodes,
		display: labelKey === 'working'
			? planDisplay('working', 'active')
			: labelKey === 'finished' ? planDisplay('finished', 'complete') : planDisplay('waiting', 'waiting'),
		priority_at: priorityAt,
	};
}

function payload(plans: ActivityIndexRow[]): ActivityIndexEnvelope {
	return {
		contract_version: 1,
		representation: 'activity_scope_index',
		as_of: '2026-08-14T12:00:00Z',
		target: { kind: 'project_scope', id: 'p1', label: 'CORSIA' },
		revision_token: '[REDACTED]',
		plans,
		attention: { awaiting_count: 0 },
	} as unknown as ActivityIndexEnvelope;
}

async function settle() {
	await tick();
	flushSync();
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

async function mountActivity(plans: ActivityIndexRow[]) {
	const body = payload(plans);
	vi.stubGlobal('fetch', vi.fn(async () => ({
		ok: true,
		status: 200,
		json: async () => body,
		headers: { get: (key: string) => key === 'ETag' ? '"activity"' : null },
	})));
	component = mount(ActivityView, {
		target: host,
		props: { projectId: 'p1', scopeName: 'CORSIA', onSelectThread: vi.fn(), onOpenChild: vi.fn() },
	});
	await settle();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	resetPlanRuntimeForTests();
	resetObserversForTests();
	token.set('test-bearer');
	connectionState.set('connected');
	activeThreadId.set(null);
	uiScope.set(null);
	_resetTurnStateForTests();
	_setProjectsForTests([{ id: 'p1', name: 'CORSIA', status: 'active', accent_color: null }]);
	threads.set([thread('lead')]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	resetObserversForTests();
	_resetProjectsForTests();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('Activity rows', () => {
	it('render without rolled-up subagent UI', async () => {
		await mountActivity([row('lead', 'working', 2)]);

		expect(host.querySelectorAll('.plan-row')).toHaveLength(1);
		expect(host.textContent).not.toContain('subagent');
		expect(host.querySelector('.subagents, .subagent, .subagent-name')).toBeNull();
	});
});

describe('Activity index decode', () => {
	it('rejects an unexpected subagents field', () => {
		const lead = row('lead', 'working', 1);

		expect(decodeActivityIndexEnvelope(payload([lead]))).not.toBeNull();
		expect(decodeActivityIndexEnvelope(payload([
			{ ...lead, subagents: [] } as unknown as ActivityIndexRow,
		]))).toBeNull();
	});
});
