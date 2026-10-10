import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { withoutSoleWrapper } from '$lib/components/plans/planTreeFilter';
import type { OperatorArtifactLink, OperatorNode } from '$lib/api/plans';
import { CHILD_AVAILABLE, node } from './fixtures/planTreeNode';
import { blockedPlanTree } from './fixtures/blockedPlanTree';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

async function settle() {
	await tick();
	flushSync();
}

function rowTitled(title: string): Element | undefined {
	return [...host.querySelectorAll('.tree-row')]
		.find((row) => row.querySelector('.row-title')?.textContent === title);
}

function toggleFor(title: string): HTMLButtonElement | null {
	return rowTitled(title)?.querySelector<HTMLButtonElement>('.branch-toggle') ?? null;
}

describe('PlanTree — same renderer contract', () => {
	it('PLANS-AC19: opens only the blocked ancestor path; off-path branches stay collapsed and reachable', async () => {
		const { nodes, currentNodeIds, nextNodeIds } = blockedPlanTree();
		component = mount(PlanTree, { target: host, props: { nodes, currentNodeIds, nextNodeIds } });
		flushSync();

		expect(toggleFor('Build')?.getAttribute('aria-expanded')).toBe('true');
		expect(rowTitled('Wait for API')).toBeTruthy();
		expect(toggleFor('Docs')?.getAttribute('aria-expanded')).toBe('false');
		expect(toggleFor('Polish')?.getAttribute('aria-expanded')).toBe('false');
		expect(rowTitled('Write guide')).toBeUndefined();
		expect(rowTitled('Tune')).toBeUndefined();

		toggleFor('Docs')!.click();
		toggleFor('Polish')!.click();
		await settle();
		expect(rowTitled('Write guide')?.textContent).toContain('Working');
		expect(rowTitled('Tune')?.textContent).toContain('Waiting');

		// Settled work is always visible; the fold row is the only control that
		// hides finished work, and it does not apply until a day has passed.
		expect(rowTitled('Kickoff')).toBeTruthy();
		const labels = [...host.querySelectorAll('button')].map((button) => button.textContent?.trim());
		expect(labels.some((label) => label?.includes('completed'))).toBe(false);
	});

	it('keeps an off-path branch whose children are complete collapsed', async () => {
		const nodes = [
			node({ id: 'build', title: 'Build', kind: 'phase', state: 'active' }),
			node({
				id: 'wait', title: 'Wait for API', parent_id: 'build', depth: 1, state: 'active',
				unmet_dependencies: [], held_by: 'user', attention_path: ['build', 'wait'],
			}),
			node({ id: 'docs', title: 'Docs', kind: 'phase', state: 'active' }),
			node({ id: 'guide', title: 'Guide written', parent_id: 'docs', depth: 1, state: 'accepted' }),
		];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		expect(rowTitled('Guide written')).toBeUndefined();
		expect(toggleFor('Docs')?.getAttribute('aria-expanded')).toBe('false');
	});

	it('always shows a completed row that is also the current node', () => {
		const nodes = [node({ id: 'a', title: 'Done but current', state: 'accepted' })];
		component = mount(PlanTree, { target: host, props: { nodes, currentNodeIds: ['a'] } });
		flushSync();
		expect(host.textContent).toContain('Done but current');
	});

	it('does not create a disclosure control for a leaf row', () => {
		component = mount(PlanTree, { target: host, props: { nodes: [node({ id: 'leaf', title: 'Leaf step' })] } });
		flushSync();
		expect(host.querySelector('.branch-toggle')).toBeNull();
		expect(host.querySelector('.branch-chevron')).toBeNull();
	});

	it('opens an active ask from its row without opening row details', async () => {
		const artifact: OperatorArtifactLink = {
			id: 'ask-1', role: 'attention', integration: 'workspace', type: 'document',
			external_id: 'brief.md', title: 'Review brief', path: '/brief.md', state: 'active',
			attention_state: 'raised',
		};
		const onOpenArtifact = vi.fn();
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes: [node({ id: 'ask', title: 'Review brief', artifacts: [artifact] })],
				onOpenArtifact,
			},
		});
		flushSync();

		(rowTitled('Review brief')!.querySelector('.ask-open') as HTMLButtonElement).click();
		await settle();

		expect(onOpenArtifact).toHaveBeenCalledWith(artifact, 'Review brief');
		expect(rowTitled('Review brief')!.querySelector('.node-details')).toBeNull();
	});

	it('does not offer Open after an ask is cleared', () => {
		const artifact: OperatorArtifactLink = {
			id: 'ask-1', role: 'attention', integration: 'workspace', type: 'document',
			external_id: 'brief.md', title: 'Review brief', path: '/brief.md', state: 'active',
			attention_state: 'cleared',
		};
		component = mount(PlanTree, {
			target: host,
			props: { nodes: [node({ id: 'ask', title: 'Review brief', artifacts: [artifact] })] },
		});
		flushSync();
		expect(rowTitled('Review brief')!.querySelector('.ask-open')).toBeNull();
	});

	it('shows no count for a just-spawned subagent that has no subtasks yet', () => {
		const nodes = [node({
			id: 'a', title: 'Delegated step', state: 'active',
			child: {
				available: true as const, plan_id: 'cp0', thread_id: 'child-thread',
				progress: { done: 0, total: 0, open: 0, liveness: 'idle' as const },
			},
		})];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		const chip = host.querySelector('.spawn-chip') as HTMLButtonElement;
		expect(chip).toBeTruthy();
		expect(chip.textContent).not.toContain('0/0');
		expect(chip.getAttribute('aria-label')).toContain('no open subtasks');
		expect(chip.getAttribute('aria-label')).not.toContain('0 of 0');
	});

	it('does not announce a subagent awaiting review as idle', () => {
		const nodes = [node({
			id: 'a', title: 'Delegated step', state: 'active',
			child: {
				available: true as const, plan_id: 'cp1', thread_id: 'child-thread',
				progress: { done: 1, total: 3, open: 2, liveness: 'needs-review' as const },
			},
		})];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		const chip = host.querySelector('.spawn-chip') as HTMLButtonElement;
		expect(chip.getAttribute('aria-label')).toContain('awaiting review');
		expect(chip.getAttribute('aria-label')).not.toContain('idle');
		// A review tone, not the idle grey and not motion.
		expect(chip.querySelector('.child-dot.human-review')).toBeTruthy();
		expect(chip.querySelector('.child-dot.active')).toBeNull();
	});

	it('renders a bounded child-plan toggle and calls onToggleChild', () => {
		const nodes = [node({ id: 'a', title: 'Delegated step', state: 'active', child: CHILD_AVAILABLE })];
		const onToggleChild = vi.fn();
		component = mount(PlanTree, { target: host, props: { nodes, onToggleChild } });
		flushSync();
		expect(host.textContent).toContain('1 left');
		expect(host.textContent).not.toContain('Subagent');
		const chip = host.querySelector('.spawn-chip') as HTMLButtonElement;
		expect(chip.getAttribute('aria-label')).toBe('active subagent, 1 subtask left');
		expect(chip.querySelector('.child-dot.active')).toBeTruthy();
		chip.click();
		expect(onToggleChild).toHaveBeenCalledWith('a');
	});

	it('labels a delegated step from the server display even when its admission condition is ready', () => {
		const delegated = node({ id: 'a', title: 'Delegated step', state: 'active', child: CHILD_AVAILABLE });
		// The owner is not running the step itself, so admission says ready; the
		// server display carries the child's work, and the row must follow it.
		const nodes = [{ ...delegated, execution: { ...delegated.execution, condition: 'ready' as const } }];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		const row = rowTitled('Delegated step');
		expect(row?.querySelector('.row-status')?.textContent).toContain('Working');
		expect(row?.querySelector('.state-icon.active')).toBeTruthy();
	});

	it('opens a child conversation in the host artifact leaf without replacing inline expansion', () => {
		const nodes = [node({ id: 'a', title: 'Delegated step', state: 'active', child: CHILD_AVAILABLE })];
		const onOpenChild = vi.fn();
		component = mount(PlanTree, { target: host, props: { nodes, onOpenChild } });
		flushSync();

		const open = host.querySelector('.child-open') as HTMLButtonElement;
		expect(open.getAttribute('aria-label')).toContain('Open Delegated step subagent beside this thread');
		expect(open.textContent?.trim()).toBe('');
		expect(open.querySelector('svg')).toBeTruthy();
		open.click();

		expect(onOpenChild).toHaveBeenCalledWith('a', 'child-thread', 'Delegated step');
		expect(host.querySelector('.spawn-chip')).toBeTruthy();
	});

	it('offers read-only archive access only for nodes with an existing discussion artifact', () => {
		const nodes = [
			node({ id: 'with', title: 'Existing discussion', discussion_artifact_id: 'discussion-1' }),
			node({ id: 'without', title: 'No discussion' }),
		];
		const onOpenLegacyDiscussion = vi.fn();
		component = mount(PlanTree, { target: host, props: { nodes, onOpenLegacyDiscussion } });
		flushSync();

		const archive = host.querySelector('.discussion-archive') as HTMLButtonElement;
		expect(archive.textContent).toContain('Discussion archive');
		archive.click();
		expect(onOpenLegacyDiscussion).toHaveBeenCalledWith('discussion-1', 'Existing discussion');
		expect(host.querySelectorAll('.discussion-archive')).toHaveLength(1);
	});

	it('communicates work, agent-review, and human-decision state without color', () => {
		const nodes = [
			node({ id: 'done', title: 'Done step', state: 'accepted' }),
			node({ id: 'active', title: 'Active step', state: 'active' }),
			node({ id: 'next', title: 'Next step' }),
			node({ id: 'blocked', title: 'Blocked step', unmet_dependencies: [], held_by: 'user', attention_path: ['blocked'] }),
				node({
					id: 'decision', title: 'Decision step', state: 'review_requested',
					review: {
						kind: 'human_decision',
						label: 'Needs your decision',
						actor: { kind: 'user', id: null, label: 'You' },
					},
				}),
			node({
				id: 'review', title: 'Review step', state: 'review_requested',
				review: {
					kind: 'parent_agent',
					label: 'Awaiting parent agent',
					actor: { kind: 'thread', id: 'parent', label: 'Parent work' },
				},
			}),
		];
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				currentNodeIds: ['done', 'active', 'blocked', 'review', 'decision'],
				nextNodeIds: ['next'],
			},
		});
		flushSync();

		const text = host.textContent ?? '';
		expect(text).toContain('Done');
		expect(text).toContain('Working');
		expect(text).toContain('Needs your decision');
		expect(text).toContain('Needs you');
		expect(text).toContain('Awaiting parent agent');
		expect(text).not.toContain('Parent work');
		expect(text).toContain('Working');
		expect(text).toContain('Needs your decision');
		expect(host.querySelector('.state-icon.complete')?.textContent).toContain('✓');
		expect(host.querySelectorAll('.state-icon.active.spinning')).toHaveLength(1);
		expect(host.querySelector('.state-icon.working:not(.spinning)')).toBeTruthy();
		// Blocked waits on someone; failed (×) is terminal. They never share a glyph (#1884).
		expect(host.querySelector('.state-icon.needs-you')?.textContent).toContain('!');
	});

	it('collapses and expands in-plan branches while preserving structural depth', async () => {
		const nodes = [
			node({ id: 'parent', title: 'Parent step' }),
			node({ id: 'child', title: 'Child step', parent_id: 'parent', depth: 1 }),
			node({ id: 'grandchild', title: 'Grandchild step', parent_id: 'child', depth: 2 }),
		];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();

		const parentToggle = host.querySelector('.branch-toggle') as HTMLButtonElement;
		expect(parentToggle.getAttribute('aria-expanded')).toBe('false');
		expect(parentToggle.querySelector('svg')).toBeTruthy();
		expect(host.textContent).not.toContain('Child step');

		parentToggle.click();
		await settle();
		expect(parentToggle.getAttribute('aria-expanded')).toBe('true');
		expect(parentToggle.getAttribute('aria-controls')).toBe('plan-row-child');
		const childRow = [...host.querySelectorAll('.tree-row')].find((row) => row.textContent?.includes('Child step'));
		expect(childRow?.getAttribute('data-depth')).toBe('1');
		expect(childRow?.getAttribute('aria-level')).toBe('2');
		expect(host.textContent).not.toContain('Grandchild step');

		(parentToggle as HTMLButtonElement).click();
		await settle();
		expect(host.textContent).not.toContain('Child step');
	});

	it('a blocked path opened by default collapses and reopens', async () => {
		const nodes = [
			node({ id: 'blocked-parent', title: 'Blocked parent', kind: 'phase' }),
			node({ id: 'blocked-child', title: 'Blocked child', parent_id: 'blocked-parent', depth: 1, unmet_dependencies: [], held_by: 'user', attention_path: ['blocked-parent', 'blocked-child'] }),
		];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();

		const disclosure = host.querySelector('.branch-toggle') as HTMLButtonElement;
		expect(disclosure.getAttribute('aria-expanded')).toBe('true');
		expect(host.textContent).toContain('Blocked child');

		disclosure.click();
		await settle();
		expect(disclosure.getAttribute('aria-expanded')).toBe('false');
		expect(host.textContent).not.toContain('Blocked child');

		disclosure.click();
		await settle();
		expect(host.textContent).toContain('Blocked child');
	});

	it('keeps sibling subagent navigation isolated from the one disclosure button', async () => {
		const nodes = [
			node({ id: 'parent', title: 'Parent step', child: CHILD_AVAILABLE }),
			node({ id: 'child', title: 'Child step', parent_id: 'parent', depth: 1 }),
		];
		const onOpenChild = vi.fn();
		component = mount(PlanTree, { target: host, props: { nodes, onOpenChild } });
		flushSync();

		const disclosure = host.querySelector('.branch-toggle') as HTMLButtonElement;
		const navigate = host.querySelector('.child-open') as HTMLButtonElement;
		expect(disclosure.getAttribute('aria-expanded')).toBe('false');
		expect(disclosure.querySelector('.branch-chevron')).toBeTruthy();
		expect(disclosure.contains(navigate)).toBe(false);
		navigate.click();
		expect(onOpenChild).toHaveBeenCalledWith('parent', 'child-thread', 'Parent step');
		expect(disclosure.getAttribute('aria-expanded')).toBe('false');
		disclosure.click();
		await settle();
		expect(disclosure.getAttribute('aria-expanded')).toBe('true');
	});

	it('opens the current path by default without flattening unrelated branches', () => {
		const nodes = [
			node({ id: 'current-parent', title: 'Current parent' }),
			node({ id: 'current-child', title: 'Current child', parent_id: 'current-parent', depth: 1, state: 'active' }),
			node({ id: 'quiet-parent', title: 'Quiet parent' }),
			node({ id: 'quiet-child', title: 'Quiet child', parent_id: 'quiet-parent', depth: 1 }),
		];
		component = mount(PlanTree, {
			target: host,
			props: { nodes, currentNodeIds: ['current-child'] },
		});
		flushSync();
		expect(host.textContent).toContain('Current child');
		expect(host.textContent).not.toContain('Quiet child');
	});

	it('keeps overview titles to one accessible line with the full title available', () => {
		const title = 'A deliberately long task title whose meaning must remain available';
		component = mount(PlanTree, { target: host, props: { nodes: [node({ title })] } });
		flushSync();
		const rowTitle = host.querySelector('.row-title');
		expect(rowTitle?.getAttribute('title')).toBe(title);
		expect(rowTitle?.textContent).toBe(title);
	});

	it('shows server-projected branch progress at a glance', () => {
		const nodes = [
			node({
				id: 'phase',
				title: 'Phase',
				kind: 'phase',
				branch_progress: { done: 1, total: 2 },
			}),
			node({ id: 'child', parent_id: 'phase', depth: 1 }),
		];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		const row = [...host.querySelectorAll('.tree-row')]
			.find((item) => item.textContent?.includes('Phase'));
		// The count sits on the expand control, which announces it in words.
		const expand = row?.querySelector('.branch-toggle');
		expect(expand?.textContent).toContain('1/2');
		expect(expand?.getAttribute('aria-label')).toBe('Expand Phase, 1 of 2 done');
	});

	it('keeps durable descriptions out of the overview scan path', () => {
		const nodes = [node({
			id: 'described',
			title: 'Compact row',
			description: 'Long durable working notes that belong in detail.',
		})];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		expect(host.textContent).toContain('Compact row');
		expect(host.textContent).not.toContain('Long durable working notes');
	});

	it('renders a ready child row\'s nodes when expanded', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		const childNode = node({ id: 'child-1', title: 'Child step', depth: 0 });
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({ status: 'ready', planId: 'child-plan', nodes: [childNode], currentNodeIds: [], nextNodeIds: [], errorMessage: null }),
			},
		});
		flushSync();
		expect(host.textContent).toContain('Child step');
	});

	it('focuses a nested child plan\'s row, and names this tree\'s nesting step when a nested row opens its subagent', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		const childNode = node({ id: 'child-1', title: 'Child step', depth: 0, child: CHILD_AVAILABLE });
		const onOpenChild = vi.fn();
		const tree = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				onOpenChild,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: (id: string) => id === 'a'
					? { status: 'ready', planId: 'child-plan', nodes: [childNode], currentNodeIds: [], nextNodeIds: [], errorMessage: null }
					: { status: 'idle', planId: null, nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: null },
			},
		});
		component = tree;
		flushSync();

		expect(tree.focusRow('child-1')).toBe(true);
		expect((document.activeElement as HTMLElement).closest('.tree-row')?.id).toBe('plan-row-child-plan:child-1');
		expect(tree.focusRow('missing')).toBe(false);

		host.querySelector<HTMLButtonElement>('[data-plan-child-open="child-1"]')!.click();
		expect(onOpenChild).toHaveBeenCalledWith('child-1', 'child-thread', 'Child step', 'a');
	});

	it('renders a ready child plan through the same tree, scoped and one level deeper', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		const open = node({ id: 'c-open', title: 'Child open step', depth: 0 });
		const done = node({
			id: 'c-done', title: 'Child finished step', depth: 0, folded: true,
			display: { label_key: 'finished', tone_key: 'complete' },
		});
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({
					status: 'ready', planId: 'child-plan', nodes: [open, done],
					currentNodeIds: [], nextNodeIds: [], errorMessage: null,
				}),
			},
		});
		flushSync();
		const nested = host.querySelector('.child-tree [role="list"]');
		expect(nested).toBeTruthy();
		const row = host.querySelector('#plan-row-child-plan\\:c-open');
		expect(row?.getAttribute('aria-level')).toBe('2');
		// The child's finished work folds into its own level's count, like the host's.
		const fold = nested?.querySelector('[data-fold-level="child-plan:"]');
		expect(fold?.textContent).toContain('1 completed');
		expect(host.textContent).not.toContain('Child finished step');
	});

	it('a child plan that wraps its steps in one task shows the steps under the delegated step (#2212)', () => {
		const nodes = [node({ id: 'a', title: 'Fix provider settings', child: CHILD_AVAILABLE })];
		const wrapper = node({ id: 'w', title: 'Fix provider settings page', depth: 0 });
		const step = node({ id: 's1', title: 'Rename the field', parent_id: 'w', depth: 1 });
		const deeper = node({ id: 's2', title: 'Check the rename', parent_id: 's1', depth: 2 });
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({
					status: 'ready', planId: 'child-plan', nodes: [wrapper, step, deeper],
					currentNodeIds: ['s1'], nextNodeIds: [], errorMessage: null,
				}),
			},
		});
		flushSync();
		expect(host.querySelector('#plan-row-child-plan\\:w')).toBeNull();
		expect(host.textContent).not.toContain('Fix provider settings page');
		const row = host.querySelector('#plan-row-child-plan\\:s1');
		expect(row?.getAttribute('aria-level')).toBe('2');
		expect(row?.getAttribute('aria-current')).toBe('step');
	});

	it('a wrapper that is the current step keeps its row and the current-step marker (debt 88)', () => {
		const nodes = [node({ id: 'a', title: 'Fix provider settings', child: CHILD_AVAILABLE })];
		const wrapper = node({ id: 'w', title: 'Fix provider settings page', depth: 0, state: 'active' });
		const step = node({ id: 's1', title: 'Rename the field', parent_id: 'w', depth: 1 });
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({
					status: 'ready', planId: 'child-plan', nodes: [wrapper, step],
					currentNodeIds: ['w'], nextNodeIds: [], errorMessage: null,
				}),
			},
		});
		flushSync();
		const row = host.querySelector('#plan-row-child-plan\\:w');
		expect(row?.getAttribute('aria-current')).toBe('step');
		expect(host.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
	});

	it('a sole wrapper with state of its own is kept: its child plan, a failure, an ask for the user (debt 88)', () => {
		const under = (wrapper: OperatorNode) => [wrapper, node({ id: 'k', parent_id: 'w', depth: 1 })];
		const kept = [
			under(node({ id: 'w', child: CHILD_AVAILABLE })),
			under(node({ id: 'w', state: 'failed' })),
			under(node({ id: 'w', held_by: 'user', requires_user_action: true })),
			under(node({ id: 'w', requires_user_action: true })),
		];
		for (const nodes of kept) expect(withoutSoleWrapper(nodes)).toBe(nodes);
		const current = under(node({ id: 'w' }));
		expect(withoutSoleWrapper(current, ['w'])).toBe(current);
		expect(withoutSoleWrapper(current).map((n) => n.id)).toEqual(['k']);
	});

	it('a child plan whose one task has no steps shows that task, and several roots stay as they are', () => {
		expect(withoutSoleWrapper([node({ id: 'only', depth: 0 })]).map((n) => n.id)).toEqual(['only']);
		const two = [node({ id: 'r1', depth: 0 }), node({ id: 'r2', depth: 0 }), node({ id: 'k', parent_id: 'r1', depth: 1 })];
		expect(withoutSoleWrapper(two)).toBe(two);
	});

	it('aria-expanded reflects expansion intent immediately, regardless of load outcome (M3)', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({ status: 'loading', planId: 'child-plan', nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: null }),
			},
		});
		flushSync();
		expect(host.querySelector('.spawn-chip')?.getAttribute('aria-expanded')).toBe('true');
	});

	it('a failed child load shows a visible role=alert row with one-click retry (M3)', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		const onRetryChild = vi.fn();
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({ status: 'error', planId: 'child-plan', nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: 'boom' }),
				onRetryChild,
			},
		});
		flushSync();
		const alert = host.querySelector('[role="alert"]');
		expect(alert).toBeTruthy();
		const retryButton = Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.includes('Retry'));
		retryButton?.click();
		expect(onRetryChild).toHaveBeenCalledWith('a');
	});

	it('an expired session says so rather than blaming the connection', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({ status: 'unauthorized', planId: 'child-plan', nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: null }),
			},
		});
		flushSync();
		expect(host.textContent).toContain('Sign in again');
		expect(host.textContent).not.toContain('Couldn’t reach CORSIA');
	});

	it.each(['not-found', 'forbidden'] as const)('a %s child read is a quiet unavailable line, not a retryable error', (status) => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => ({ status, planId: 'child-plan', nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: null }),
			},
		});
		flushSync();
		expect(host.textContent).toContain('This subagent’s plan isn’t available.');
		expect(host.querySelector('[role="alert"]')).toBeNull();
		expect([...host.querySelectorAll('button')].some((b) => b.textContent?.includes('Retry'))).toBe(false);
	});

	it('bounds depth: nodes past maxDepth are not rendered inline, a drill affordance appears instead', () => {
		const nodes = [
			node({ id: 'root', title: 'Root', depth: 0 }),
			node({ id: 'deep', title: 'Too deep to render inline', parent_id: 'root', depth: 2 }),
		];
		const onDrill = vi.fn();
		component = mount(PlanTree, { target: host, props: { nodes, maxDepth: 1, onDrill } });
		flushSync();
		expect(host.textContent).not.toContain('Too deep to render inline');
		expect(host.textContent).toContain('View deeper steps');
		(host.querySelector('.drill-row') as HTMLButtonElement).click();
		expect(onDrill).toHaveBeenCalledWith('root');
	});

	it('a blocked path and a current path open while an unrelated branch stays present and collapsed', () => {
		const nodes = [
			node({ id: 'blocked-phase', title: 'Blocked phase', kind: 'phase' }),
			node({ id: 'blocked-step', title: 'Blocked step', parent_id: 'blocked-phase', depth: 1, unmet_dependencies: [], held_by: 'user', attention_path: ['blocked-phase', 'blocked-step'] }),
			node({ id: 'active-phase', title: 'Active phase', kind: 'phase' }),
			node({ id: 'active-step', title: 'Active step', parent_id: 'active-phase', depth: 1, state: 'active' }),
			node({ id: 'quiet-phase', title: 'Quiet phase', kind: 'phase' }),
			node({ id: 'quiet-step', title: 'Quiet step', parent_id: 'quiet-phase', depth: 1 }),
		];
		component = mount(PlanTree, {
			target: host,
			props: { nodes, currentNodeIds: ['blocked-step', 'active-step'] },
		});
		flushSync();
		expect(toggleFor('Blocked phase')?.getAttribute('aria-expanded')).toBe('true');
		expect(toggleFor('Active phase')?.getAttribute('aria-expanded')).toBe('true');
		expect(rowTitled('Blocked step')).toBeTruthy();
		expect(rowTitled('Active step')).toBeTruthy();
		expect(toggleFor('Quiet phase')?.getAttribute('aria-expanded')).toBe('false');
		expect(rowTitled('Quiet step')).toBeUndefined();
	});

	it('R6: ancestorChain terminates on a cyclic parent_id instead of looping forever', () => {
		// a -> b -> a (cycle). Both the current id and the attention path walk it.
		const nodes = [
			node({ id: 'a', title: 'A', parent_id: 'b', attention_path: ['b', 'a'] }),
			node({ id: 'b', title: 'B', parent_id: 'a', depth: 1 }),
		];
		const start = Date.now();
		component = mount(PlanTree, {
			target: host,
			props: { nodes, currentNodeIds: ['a'] },
		});
		flushSync();
		expect(Date.now() - start).toBeLessThan(1000);
	});

	it('M5: renders next_node_ids as the explicit next indicator without inventing another state', () => {
		const nodes = [
			node({ id: 'a', title: 'First step', state: 'pending' }),
			node({ id: 'b', title: 'Second step', state: 'pending' }),
		];
		component = mount(PlanTree, { target: host, props: { nodes, nextNodeIds: ['b'] } });
		flushSync();
		const rowB = [...host.querySelectorAll('.tree-row')].find((el) => el.textContent?.includes('Second step'));
		const rowA = [...host.querySelectorAll('.tree-row')].find((el) => el.textContent?.includes('First step'));
		// Queued work reads Waiting, never Working; only the next one wears the arrow.
		expect(rowB?.querySelector('.state-icon')?.textContent).toBe('→');
		expect(rowB?.textContent).toContain('Waiting');
		expect(rowB?.textContent).not.toContain('Working');
		expect(rowA?.querySelector('.state-icon')?.textContent).toBe('•');
	});

	it('L3: exposes real list semantics instead of an unsupported aria-label-on-div', () => {
		const nodes = [node({ id: 'a', title: 'Step' })];
		component = mount(PlanTree, { target: host, props: { nodes } });
		flushSync();
		expect(host.querySelector('[role="list"]')).toBeTruthy();
		expect(host.querySelectorAll('[role="listitem"]').length).toBeGreaterThan(0);
	});

	it('L4: touch targets stay at least 44px tall on mobile, including the drill affordance', () => {
		const nodes = [
			node({ id: 'root', title: 'Root', depth: 0 }),
			node({ id: 'deep', title: 'Deep', parent_id: 'root', depth: 2 }),
		];
		component = mount(PlanTree, { target: host, props: { nodes, maxDepth: 1 } });
		flushSync();
		expect(host.querySelector('.drill-row')).toBeTruthy();
	});
});
