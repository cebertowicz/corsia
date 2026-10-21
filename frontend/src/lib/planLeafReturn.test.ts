import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { openPlanAsk, openPlanDiscussionLeaf, openPlanSubagentLeaf, returnToPlanNode } from '$lib/planLeafReturn';
import { registerNodeFocus, registerPlanSurface } from '$lib/planNodeFocus';
import { activeThreadId } from '$lib/stores/threads';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { paneCollapsed } from '$lib/stores/paneCollapsed';
import { paneOpen } from '$lib/stores/paneOpen';
import type { OperatorArtifactLink } from '$lib/api/plans';

describe('returnToPlanNode', () => {
	const cleanups: Array<() => void> = [];
	let show: ReturnType<typeof vi.fn<(threadId: string) => boolean>>;
	let focus: ReturnType<typeof vi.fn<(nodeId: string, stepId: string | null) => void>>;

	beforeEach(() => {
		activeThreadId.set('t1');
		show = vi.fn((_threadId: string) => true);
		focus = vi.fn((_nodeId: string, _stepId: string | null) => {});
		cleanups.push(registerPlanSurface(show), registerNodeFocus('t1', focus));
	});

	afterEach(() => {
		for (const off of cleanups.splice(0)) off();
	});

	it('hands the originating node to the active thread’s plan pane through the shared focus path', async () => {
		await returnToPlanNode({ sourceNodeId: 'node-1' });

		expect(show).toHaveBeenCalledWith('t1');
		expect(focus).toHaveBeenCalledOnce();
		expect(focus).toHaveBeenCalledWith('node-1', null);
	});

	it('carries the owning step of a nested subagent-plan node from the tab source', async () => {
		await returnToPlanNode({ sourceNodeId: 'sub', sourceStepId: 'spawn' });

		expect(focus).toHaveBeenCalledWith('sub', 'spawn');
	});

	it('does nothing without an originating node', async () => {
		await returnToPlanNode(null);

		expect(show).not.toHaveBeenCalled();
		expect(focus).not.toHaveBeenCalled();
	});

	it('does nothing without an active thread', async () => {
		activeThreadId.set(null);
		await returnToPlanNode({ sourceNodeId: 'node-1' });

		expect(show).not.toHaveBeenCalled();
	});
});

describe('openPlanAsk', () => {
	const artifact = (over: Partial<OperatorArtifactLink> = {}): OperatorArtifactLink => ({
		id: 'artifact-1', role: 'attention', integration: 'corsia', type: 'document',
		external_id: 'document-1', title: 'Review brief', path: '/brief.md', state: 'active', attention_state: 'raised',
		...over,
	});

	beforeEach(() => {
		threadWorkspaces.set({});
		paneOpen.set(true);
		paneCollapsed.set(false);
	});

	it('opens a document in the artifact surface', () => {
		const openDecision = vi.fn();
		openPlanAsk('t1', artifact(), 'Fallback title', openDecision);

		expect(openDecision).not.toHaveBeenCalled();
		expect(get(threadWorkspaces).t1.tabs[0]).toMatchObject({
			artifactId: 'artifact-1', path: '/brief.md', title: 'Review brief',
		});
	});

	it('opens a decision in the canonical attention surface, never as a document', () => {
		const openDecision = vi.fn();
		openPlanAsk('t1', artifact({ type: 'plan-decision', path: null }), 'Choose model', openDecision);

		expect(openDecision).toHaveBeenCalledOnce();
		expect(get(threadWorkspaces).t1).toBeUndefined();
	});

	const width = window.innerWidth;
	afterEach(() => { window.innerWidth = width; });

	it('keeps a docked plan pane as it is while opening documents, subagents, and discussion tabs', () => {
		window.innerWidth = 1440;
		const openDecision = vi.fn();
		openPlanAsk('t1', artifact(), 'Fallback title', openDecision);
		openPlanSubagentLeaf('t1', 'node-1', 'child-1', 'Worker');
		openPlanDiscussionLeaf('t1', 'artifact-2', 'Archive');

		expect(get(paneOpen)).toBe(true);
		expect(get(paneCollapsed)).toBe(false);
	});

	it.each([
		['document', () => openPlanAsk('t1', artifact(), 'Fallback title', vi.fn())],
		['subagent', () => openPlanSubagentLeaf('t1', 'node-1', 'child-1', 'Worker')],
		['discussion', () => openPlanDiscussionLeaf('t1', 'artifact-2', 'Archive')],
	])('closes an overlay plan pane so the opened %s tab is not behind it', (_kind, open) => {
		window.innerWidth = 1024;
		open();

		expect(get(paneOpen)).toBe(false);
		expect(get(paneCollapsed)).toBe(false);
	});
});
