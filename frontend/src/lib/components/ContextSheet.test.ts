import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ContextSheet from './ContextSheet.svelte';
import { messages } from '$lib/stores/threads';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function checkpointState(
	status: 'running' | 'captured' | 'installed' | 'overtaken' | 'failed' | 'cancelled',
) {
	return {
		checkpoints: [{
			id: 'checkpoint-1', trigger: 'manual' as const, mode: 'warm' as const,
			status, writerModel: 'native/current',
			error: status === 'failed' ? 'provider_unavailable: raw detail' : null,
			startedAt: '2026-07-21T08:00:00Z',
		}],
		loading: false,
		error: null,
	};
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	messages.set([]);
	vi.restoreAllMocks();
});

describe('ContextSheet source owner actions', () => {
	it('shows a failed checkpoint and offers a fresh manual attempt without raw error copy', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		const onCheckpointNow = vi.fn();

		component = mount(ContextSheet, {
			target: host,
			props: {
				onCheckpointNow,
				checkpointState: checkpointState('failed'),
				usage: {
					model_context_window: 1000, system_tokens: 0,
					tool_def_tokens: 0, history_tokens: 20, user_tokens: 0,
					total_used: 20, utilization: 0.02,
					was_trimmed: false, history_messages: 1,
					token_usage_available: false,
					checkpoint_eligible: true,
				},
			},
		});
		flushSync();

		expect(host.textContent).toContain('Usage unavailable');
		expect(host.textContent).toContain('Checkpoint failed');
		expect(host.textContent).not.toContain('provider_unavailable');
		const checkpointActions = [...host.querySelectorAll<HTMLButtonElement>('button')]
			.filter(button => button.textContent?.trim() === 'Try checkpoint again');
		expect(checkpointActions).toHaveLength(1);
		checkpointActions[0].click();
		checkpointActions[0].click();
		expect(onCheckpointNow).toHaveBeenCalledOnce();
	});

	it.each(['running', 'captured'] as const)(
		'shows exact cancel and no manual action while a checkpoint is %s',
		(status) => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(ContextSheet, {
			target: host,
			props: {
				onCheckpointNow: vi.fn(),
				checkpointState: checkpointState(status),
				threadId: 'thread-1',
			},
		});
		flushSync();

		expect(host.querySelector('[data-checkpoint-cancel="checkpoint-1"]')).not.toBeNull();
		expect([...host.querySelectorAll('button')].map(button => button.textContent?.trim()))
			.not.toContain('Checkpoint Now');
	});

	it('offers Manual Checkpoint Now when no cutover owns admission', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		const onCheckpointNow = vi.fn();

		component = mount(ContextSheet, {
			target: host,
			props: {
				onCheckpointNow,
				checkpointState: {
					checkpoints: [],
					loading: false,
					error: null,
				},
				usage: {
					model_context_window: 1000, system_tokens: 0,
					tool_def_tokens: 0, history_tokens: 20, user_tokens: 0,
					total_used: 20, utilization: 0.02,
					was_trimmed: false, history_messages: 1,
					checkpoint_eligible: true,
				},
			},
		});
		flushSync();

		const action = [...host.querySelectorAll<HTMLButtonElement>('button')]
			.find(button => button.textContent?.trim() === 'Checkpoint Now');
		expect(action).toBeTruthy();
		action?.click();
		expect(onCheckpointNow).toHaveBeenCalledOnce();
		expect(host.textContent).not.toContain('Target model');
	});

	it('hides Checkpoint Now and shows freshness when the backend says nothing new has been sealed', () => {
		// #1952: checkpoints are automatic and Checkpoint Now is only an
		// optional shortcut, so when the backend says one isn't currently
		// possible the sheet shows how fresh the last one is, not a disabled
		// button inviting a no-op retry.
		host = document.createElement('div');
		document.body.appendChild(host);
		const onCheckpointNow = vi.fn();

		component = mount(ContextSheet, {
			target: host,
			props: {
				onCheckpointNow,
				usage: {
					model_context_window: 1000, system_tokens: 0,
					tool_def_tokens: 0, history_tokens: 20, user_tokens: 0,
					total_used: 20, utilization: 0.02,
					was_trimmed: false, history_messages: 1,
					token_usage_available: false,
					checkpoint_eligible: false,
				},
				checkpointState: checkpointState('installed'),
			},
		});
		flushSync();

		expect(host.textContent).not.toContain('Checkpoint Now');
		expect([...host.querySelectorAll('button')].map(button => button.textContent?.trim()))
			.not.toContain('Nothing new to checkpoint');
		expect(host.textContent).toMatch(/Checkpointed .* ago/);
		// One status line, not also the generic "Context saved" (#2050).
		expect(host.textContent).not.toContain('Context saved');
	});

	describe('while the preview loads (#1953)', () => {
		const lastKnownUsage = {
			model_context_window: 1000, system_tokens: 0,
			tool_def_tokens: 0, history_tokens: 400, user_tokens: 0,
			total_used: 400, utilization: 0.4,
			was_trimmed: false, history_messages: 3,
			token_usage_available: true, token_source: 'measured' as const,
		};

		function mountSheet(props: Record<string, unknown>) {
			host = document.createElement('div');
			document.body.appendChild(host);
			component = mount(ContextSheet, { target: host, props });
			flushSync();
		}

		it('keeps the last known usage visible and marks it as updating', () => {
			mountSheet({ usage: lastKnownUsage, loading: true });

			expect(host.textContent).not.toContain('Loading context');
			expect(host.textContent).toContain('40%');
			expect(host.querySelector('.ctx-updating')?.textContent).toContain('Updating');
		});

		it('says loading only when there is nothing to show yet', () => {
			mountSheet({ usage: null, loading: true });

			expect(host.textContent).toContain('Loading context');
			expect(host.querySelector('.ctx-updating')).toBeNull();
		});

		it('hides the checkpoint action until the preview answers eligibility', () => {
			mountSheet({
				usage: lastKnownUsage,
				loading: true,
				onCheckpointNow: vi.fn(),
				checkpointState: checkpointState('installed'),
			});

			expect(host.textContent).not.toContain('Checkpoint Now');
			expect(host.textContent).not.toMatch(/Checkpointed .* ago/);
			expect(host.querySelector('.ctx-checkpoint-now')).toBeNull();
		});

		it('says the shown usage is the last known one when the refresh fails', () => {
			mountSheet({ usage: lastKnownUsage, loading: false, refreshFailed: 'Context preview failed (500)' });

			expect(host.textContent).toContain('40%');
			expect(host.querySelector('.ctx-updating')?.textContent).toContain('Could not refresh');
		});
	});

	it('groups next-turn sources and keeps delivery proof out of the composer widget', () => {
		host = document.createElement('div');
		document.body.appendChild(host);

		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 1000,
					system_tokens: 10,
					tool_def_tokens: 0,
					history_tokens: 20,
					user_tokens: 5,
					total_used: 35,
					utilization: 0.035,
					was_trimmed: false,
					history_messages: 2,
					context_snapshot: {
						snapshot_id: 'ctx-project',
							content_hash: 'abcdef1234567890',
							delivery_leaves: [{ harness: 'native', destination_mode: 'prefix-snapshot' }],
							source_inventory: [
								{
									source_id: 'project-bootloader',
								origin_section: 'project',
								label: 'Project bootloader',
								stable_reference: 'project://corsia/bootloader',
								freshness: 'fresh',
								inclusion: 'included',
								reason: 'project bootloader files',
									owner_surface: 'project-settings',
								},
								{
									source_id: 'dynamic:agent-roster',
									origin_section: 'dynamic',
									label: 'Agent Responsibility Map',
									stable_reference: 'dynamic://agents',
									freshness: 'fresh',
									inclusion: 'included',
									owner_surface: 'agent-roster',
									metadata: {
										provider: 'agent_owner_map',
										agent_count: 2,
									},
								},
							],
						},
					source_artifacts: [
						{
							id: 'native-preview',
							kind: 'provider-bound-payload-preview',
							method: 'next-turn-preview',
							harness: 'native',
							snapshot_id: 'ctx-project',
							payload_hash: 'abcdef1234567890',
						},
					],
				},
			},
			});
			flushSync();

			const groups = [...host.querySelectorAll<HTMLDetailsElement>('.ctx-source-group')];
			expect(groups.map((el) => el.querySelector('.ctx-source-summary-main')?.textContent?.trim())).toEqual([
				'System 1',
				'Project 1',
				'History 2',
			]);
			expect(groups[0].querySelector('.ctx-source-count-label')?.textContent?.trim()).toBe('2 Agents');
			expect(groups[2].querySelector('.ctx-source-delta')).toBeNull();
			expect([...host.querySelectorAll('.ctx-source-group-token')].map((el) => el.textContent?.trim())).toEqual([
				'25 tokens',
			]);
			expect(groups.every((el) => !el.open)).toBe(true);
			groups[2].querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
			flushSync();
			expect(groups[2].open).toBe(true);
			expect(host.textContent).toContain('Agent Responsibility Map');
			expect(host.textContent).toContain('2 Agents');
			expect(host.textContent).not.toContain('+2');
			expect(host.textContent).not.toContain('Fresh');
			expect(host.textContent).not.toContain('Open Document');
			expect(host.textContent).not.toContain('Delivery proof');
			expect(host.textContent).not.toContain('next-turn-preview');
			expect(host.textContent).not.toContain('reconstructed');
			expect(host.textContent).not.toContain('Snapshot');
			expect(host.textContent).not.toContain('Record');
			expect(host.textContent).not.toContain('Hash');
			expect(host.textContent).not.toContain('Included');
		});

	it('renders source groups as collapsed clean indexes with one-line rows', () => {
		host = document.createElement('div');
		document.body.appendChild(host);

		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 1000,
					system_tokens: 10,
					tool_def_tokens: 0,
					history_tokens: 20,
					user_tokens: 5,
					total_used: 35,
					utilization: 0.035,
					was_trimmed: false,
					history_messages: 2,
					context_snapshot: {
						snapshot_id: 'ctx-clean-index',
						content_hash: 'abcdef1234567890',
						delivery_leaves: [],
						source_inventory: [
							{
								source_id: 'system-bootstrap:/Users/alice/Documents/AGENTS.md',
								origin_section: 'workspace',
								label: 'System context: /Users/alice/Documents/AGENTS.md',
								stable_reference: '/Users/alice/Documents/AGENTS.md',
								freshness: 'fresh',
								inclusion: 'included',
								reason: 'system bootstrap file',
								owner_surface: 'document-viewer',
								metadata: { name: '/Users/alice/Documents/AGENTS.md' },
								prompt_tokens: 42,
							},
							{
								source_id: 'project-bootloader',
								origin_section: 'project',
								label: 'Project bootloader: project-context.md',
								stable_reference: 'project://corsia/project-context.md',
								freshness: 'fresh',
								inclusion: 'included',
								reason: 'project bootloader files',
								owner_surface: 'project-settings',
								prompt_tokens: 1200,
							},
							{
								source_id: 'thread-bootloader',
								origin_section: 'thread',
								label: 'Thread bootloader: thread-context.md',
								stable_reference: 'thread://session/thread-context.md',
								freshness: 'fresh',
								inclusion: 'included',
								reason: 'thread bootloader files',
								owner_surface: 'document-viewer',
								prompt_tokens: 86,
							},
							{
								source_id: 'model:native',
								origin_section: 'model',
								label: 'Native model: omlx@local/Model-3-5',
								stable_reference: 'model://omlx@local/Model-3-5',
								freshness: 'fresh',
								inclusion: 'included',
								reason: 'selected harness model',
								owner_surface: 'provider-settings',
								metadata: { model: 'omlx@local/Model-3-5' },
							},
							{
								source_id: 'next-turn-preview',
								origin_section: 'preflight',
								label: 'Native next-turn preview',
								stable_reference: 'thread://s/next-turn-preview',
								freshness: 'preview',
								inclusion: 'included',
								reason: 'Projected from selected session runtime before provider dispatch',
								owner_surface: 'context-widget',
							},
							{
								source_id: 'dynamic:agent-roster',
								origin_section: 'dynamic',
								label: 'Agent Responsibility Map',
								stable_reference: 'dynamic://agents',
								freshness: 'fresh',
								inclusion: 'included',
								owner_surface: 'agent-roster',
								metadata: {
									provider: 'agent_owner_map',
									agent_count: 1,
								},
								prompt_tokens: 340,
							},
						],
					},
					source_artifacts: [],
				},
			},
		});
		flushSync();

		const groups = [...host.querySelectorAll<HTMLDetailsElement>('.ctx-source-group')];
		expect(groups.map((el) => el.querySelector('.ctx-source-summary-main')?.textContent?.trim())).toEqual([
			'System 1',
			'Workspace 1',
			'Project 1',
			'This thread 1',
			'History 2',
		]);
		expect([...host.querySelectorAll('.ctx-source-group-token')].map((el) => el.textContent?.trim())).toEqual([
			'340 tokens',
			'42 tokens',
			'1.2k tokens',
			'86 tokens',
			'25 tokens',
		]);
		expect(groups[0].querySelector('.ctx-source-count-label')?.textContent?.trim()).toBe('1 Agent');
		expect(groups[0].querySelector('.ctx-source-delta')).toBeNull();
		expect(host.querySelector('.ctx-row')).toBeNull();
		expect(groups.every((el) => !el.open)).toBe(true);
		expect(host.textContent).not.toContain('Native next-turn preview');
		expect(host.textContent).not.toContain('Native model');
		expect(host.textContent).not.toContain('selected harness model');
		expect(host.textContent).not.toContain('system bootstrap file');

		for (const group of groups) {
			group.querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		}
		flushSync();

		expect([...host.querySelectorAll('.ctx-source-label')].map((el) => el.textContent?.trim())).toEqual([
			'Agent Responsibility Map',
			'AGENTS.md',
			'project-context.md',
			'thread-context.md',
			'Conversation history',
			'Current message',
		]);
		expect([...host.querySelectorAll('.ctx-source-status')].map((el) => el.textContent?.trim())).toEqual([]);
		expect([...host.querySelectorAll('.ctx-source-token')].map((el) => el.textContent?.trim())).toEqual([
			'340 tokens',
			'42 tokens',
			'1.2k tokens',
			'86 tokens',
			'20 tokens',
			'5 tokens',
		]);
		expect(host.textContent).not.toContain('System context:');
		expect(host.textContent).not.toContain('Project bootloader:');
		expect(host.textContent).not.toContain('Thread bootloader:');
		expect(host.textContent).not.toContain('Fresh');
		expect(host.textContent).not.toContain('Preview status');
		expect(host.textContent).not.toContain('window:');
		expect(host.textContent).not.toContain('Open');
		expect(host.querySelector('.ctx-preview-note')).toBeNull();
	});

	it('keeps app action counts out of the Tools group header', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 1_000, system_tokens: 0, tool_def_tokens: 100,
					history_tokens: 0, user_tokens: 0, total_used: 100, utilization: 0.1,
					was_trimmed: false, history_messages: 0,
					context_snapshot: { source_inventory: [
						{ source_id: 'native:tool-index:github', origin_section: 'tools', label: 'github', inclusion: 'included', prompt_tokens: 30, metadata: { provider: 'mcp_index', server_name: 'github', display_name: 'GitHub', tool_count: 46 } },
						{ source_id: 'native:tool-index:linear', origin_section: 'tools', label: 'linear', inclusion: 'included', prompt_tokens: 10, metadata: { provider: 'mcp_index', server_name: 'linear', display_name: 'Linear', tool_count: 21 } },
						{ source_id: 'native:tool-definitions', origin_section: 'tools', label: 'CORSIA tools', inclusion: 'included', prompt_tokens: 60, metadata: { provider: 'corsia_tools' } },
					] },
				},
			},
		});
		flushSync();

		const tools = [...host.querySelectorAll<HTMLDetailsElement>('.ctx-source-group')]
			.find((group) => group.querySelector('.ctx-source-summary-main')?.textContent?.includes('Tools'));
		expect(tools?.querySelector('.ctx-source-group-meta')).toBeNull();
		expect(tools?.querySelector('summary')?.textContent).not.toMatch(/action/i);
	});

	it('renders measured usage independently from estimated source attribution', () => {
		host = document.createElement('div');
		document.body.appendChild(host);

		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 1_000_000,
					system_tokens: 20_000,
					tool_def_tokens: 0,
					history_tokens: 59_000,
					user_tokens: 0,
					total_used: 59_000,
					utilization: 0.059,
					token_usage_available: true,
					token_source: 'measured',
					preview: true,
					was_trimmed: false,
					history_messages: 256,
					context_snapshot: {
						snapshot_id: 'ctx-all-math',
						source_inventory: [
							{
								source_id: 'system:bootloader',
								origin_section: 'system',
								label: 'System context: AGENTS.md',
								stable_reference: '/workspace/AGENTS.md',
								freshness: 'fresh',
								inclusion: 'included',
								owner_surface: 'document-viewer',
								prompt_tokens: 20_000,
							},
							{
								source_id: 'project:bootloader',
								origin_section: 'project',
								label: 'Project context: PROJECT.md',
								stable_reference: '/workspace/PROJECT.md',
								freshness: 'fresh',
								inclusion: 'included',
								owner_surface: 'document-viewer',
								prompt_tokens: 42_000,
							},
							{
								source_id: 'dynamic:agent-roster',
								origin_section: 'dynamic',
								label: 'Agent Responsibility Map',
								stable_reference: 'dynamic://agents',
								freshness: 'fresh',
								inclusion: 'included',
								owner_surface: 'agent-roster',
								metadata: { provider: 'agent_owner_map', agent_count: 3 },
								prompt_tokens: 324,
							},
						],
					},
					source_artifacts: [],
				},
			},
		});
		flushSync();

		expect(host.querySelector('.ctx-headline')?.textContent).toContain('59k');
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('6%');
		expect(host.querySelector('.ctx-attribution-label')?.textContent).toContain('Estimated source attribution');
		expect([...host.querySelectorAll('.ctx-source-group-token')].map((el) => el.textContent?.trim())).toEqual([
			'20k tokens',
			'42k tokens',
			'59k tokens',
		]);
	});

	it('offers owner actions for context sources with editable owning surfaces', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		const onOpenSource = vi.fn();

		component = mount(ContextSheet, {
			target: host,
			props: {
				onOpenSource,
				usage: {
					model_context_window: 1000,
					system_tokens: 10,
					tool_def_tokens: 0,
					history_tokens: 20,
					user_tokens: 5,
					total_used: 35,
					utilization: 0.035,
					was_trimmed: false,
					history_messages: 2,
					context_snapshot: {
						snapshot_id: 'ctx-project',
						content_hash: 'abcdef1234567890',
						delivery_leaves: [{ harness: 'native', destination_mode: 'prefix-snapshot' }],
						source_inventory: [
							{
								source_id: 'project-bootloader',
								origin_section: 'project',
								label: 'Project bootloader',
								stable_reference: 'project://corsia/bootloader',
								freshness: 'fresh',
								inclusion: 'included',
								reason: 'project bootloader files',
								owner_surface: 'project-settings',
							},
						],
					},
				},
			},
		});
		flushSync();

		const group = host.querySelector<HTMLDetailsElement>('.ctx-source-group');
		expect(group?.open).toBe(false);
		group?.querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		flushSync();
		const row = host.querySelector<HTMLButtonElement>('button[aria-label="Open Project settings for Project bootloader"]');
		expect(row?.classList.contains('ctx-source-row')).toBe(true);
		expect(row?.textContent).not.toContain('Open');
		row?.click();

		expect(onOpenSource).toHaveBeenCalledWith(expect.objectContaining({
			id: 'project-bootloader',
			ownerSurface: 'project-settings',
			stableReference: 'project://corsia/bootloader',
		}));
	});

	it('marks snapshot-only context usage as unavailable instead of zero percent', () => {
		host = document.createElement('div');
		document.body.appendChild(host);

		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 200000,
					system_tokens: 0,
					tool_def_tokens: 0,
					history_tokens: 0,
					user_tokens: 0,
					total_used: null,
					utilization: null,
					token_usage_available: false,
					was_trimmed: false,
					history_messages: 0,
					context_snapshot: {
						snapshot_id: 'ctx-codex-source-truth',
						content_hash: 'abcdef1234567890',
						delivery_leaves: [{ harness: 'codex', destination_mode: 'thread-start-inject-resume' }],
						source_inventory: [{
							source_id: 'source-1',
							origin_section: 'project',
							label: 'Project brief',
							inclusion: 'included',
							prompt_tokens: 40_000,
						}],
					},
				},
			},
		});
		flushSync();

			expect(host.textContent).toContain('Usage unavailable');
			expect(host.textContent).not.toContain('0 of 200k');
			expect(host.textContent).not.toContain('codex / thread-start-inject-resume');
			expect(host.textContent).not.toContain('ctx-codex-source-truth');
			expect(host.querySelector('.ctx-attribution-label')?.textContent).toContain('Estimated source attribution');
		});

	it('renders an estimated gauge (not "unavailable") for a reconstructed snapshot', () => {
		host = document.createElement('div');
		document.body.appendChild(host);

		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 200000,
					system_tokens: 0,
					tool_def_tokens: 0,
					history_tokens: 0,
					user_tokens: 0,
					total_used: 42000,
					utilization: 0.21,
					token_usage_available: false,
					token_source: 'estimated',
					was_trimmed: false,
					history_messages: 0,
					context_snapshot: {
						snapshot_id: 'ctx-estimated',
						source_inventory: [{
							source_id: 'system:seed',
							origin_section: 'system',
							label: 'Claude Code seed file',
							stable_reference: 'thread://s/cc-seed',
							freshness: 'fresh',
							inclusion: 'included',
							owner_surface: 'cc-runtime',
							prompt_tokens: 42000,
						}],
					},
				},
			},
		});
		flushSync();

		// Estimates retain their label but now share the percentage and bar so
		// the sheet and composer describe the same next-turn occupancy.
		expect(host.textContent).not.toContain('Usage unavailable');
		expect(host.textContent).toContain('Estimated');
		expect(host.textContent).toContain('21%');
		expect(host.querySelector('.ctx-bar')).not.toBeNull();
	});

	it('never shows a boundary notice, however full the previewed context is', () => {
		// #1952: checkpoints are automatic and Checkpoint Now is only an
		// optional shortcut, so the sheet shows a boundary notice for none of
		// this — not "Checkpoint Now required", not a rebuild/revalidate grey
		// note. This replaced the earlier neutral-vs-amber design.
		for (const usage of [
			{
				model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
				history_tokens: 20, user_tokens: 5, total_used: 25,
				utilization: 0.025, preview: true,
				was_trimmed: true, history_messages: 2,
			},
			{
				model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
				history_tokens: 950, user_tokens: 0, total_used: 950,
				utilization: 0.95, preview: true,
				was_trimmed: false, history_messages: 40,
			},
		]) {
			host = document.createElement('div');
			document.body.appendChild(host);
			component = mount(ContextSheet, { target: host, props: { usage } });
			flushSync();

			expect(host.textContent).toContain('Next turn context');
			expect(host.textContent).not.toContain('Checkpoint Now required');
			expect(host.textContent).not.toContain('Checkpoint required');
			expect(host.textContent).not.toContain('Rebuilds');
			expect(host.textContent).not.toContain('Revalidates');
			expect(host.querySelector('.ctx-trimmed')).toBeNull();

			unmount(component as never);
			component = null;
			host.remove();
		}
	});

	it('clears live rebuild status after reconstruction despite historical warning rows', () => {
		messages.set([{
			id: 'historical-warning', thread_id: 'thread-1', role: 'system',
			content: 'Rebuilds before next send',
			metadata: { type: 'provider_history_warning' },
			created_at: '2026-07-21T08:00:00Z',
		}]);
		host = document.createElement('div');
		document.body.appendChild(host);

		component = mount(ContextSheet, {
			target: host,
			props: {
				usage: {
					model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
					history_tokens: 100, user_tokens: 0, total_used: 100, utilization: 0.1, preview: true,
					was_trimmed: false, history_messages: 4,
				},
			},
		});
		flushSync();

		expect(host.textContent).not.toContain('Rebuilds before next send');
		expect(host.textContent).not.toContain('Checkpoint Now required');
	});

	});
