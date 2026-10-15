import { describe, expect, it } from 'vitest';
import { contextSheetSnapshotModel, contextSheetUsageSummary, formatContextDelta } from './contextSheetModel';

describe('formatContextDelta', () => {
	it('preserves string deltas and formats structured counts', () => {
		expect(formatContextDelta({ delta: 'custom delta' })).toBe('custom delta');
		expect(formatContextDelta({ delta: { added: 2, removed: 1, changed: 3 } })).toBe('+2 -1 ~3');
		expect(formatContextDelta({ delta: { added: 0, removed: 0, changed: 0 } })).toBe('');
	});
});

describe('contextSheetSnapshotModel', () => {
	it('returns snapshot and source rows from the effective context snapshot', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 10,
			tool_def_tokens: 0,
			history_tokens: 20,
			user_tokens: 5,
			total_used: 35,
			utilization: 0.035,
			was_trimmed: false,
			history_messages: 2,
			continuity_mode: 'reconstructed',
			source_artifacts: [
				{
					kind: 'provider-bound-payload',
					harness: 'claude-code',
					method: 'cc-user-frame',
					snapshot_id: 'ctx-1234567890abcdef',
					content_hash: '1234567890abcdef',
					payload: { frame_text: 'User message:\nhello' },
				},
			],
			snapshot_id: 'ctx-1234567890abcdef',
			context_snapshot: {
				snapshot_id: 'ctx-1234567890abcdef',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [
					{ harness: 'native', destination_mode: 'prefix-snapshot' },
				],
				source_inventory: [
					{
						source_id: 'native-system-prompt',
						origin_section: 'system',
						label: 'Native system prompt',
						stable_reference: 'thread://s/system',
						freshness: 'fresh',
						inclusion: 'included',
						reason: 'native strict-prefix system prompt',
						owner_surface: 'context-builder',
					},
					{
						source_id: 'summary:s-1',
						origin_section: 'summary',
						label: 'Prior Coordination',
						stable_reference: 'summary://s-1',
						freshness: 'fresh',
						inclusion: 'omitted_limit',
						reason: 'max related summaries reached',
						owner_surface: 'summary-details',
						metadata: {
							generation_model: 'omlx@local/qwen',
							generation_source: 'configured-summary-default',
						},
					},
				],
			},
		});

		expect(model?.sources).toEqual(expect.arrayContaining([
				expect.objectContaining({
					id: 'native-system-prompt',
					category: 'System',
					label: 'Native system prompt',
					status: 'included',
				}),
				expect.objectContaining({
					id: 'summary:s-1',
					category: 'History',
					label: 'Prior Coordination',
					status: 'omitted_limit',
				}),
				expect.objectContaining({
					id: 'usage:conversation-history',
					category: 'History',
					label: 'Conversation history',
					tokenCount: 20,
				}),
				expect.objectContaining({
					id: 'usage:current-message',
					category: 'History',
					label: 'Current message',
					tokenCount: 5,
				}),
		]));
		expect(model?.sourceGroups.map((group) => ({
			category: group.category,
			ids: group.sources.map((source) => source.id),
		}))).toEqual([
			{ category: 'System', ids: ['native-system-prompt'] },
			{ category: 'History', ids: ['summary:s-1', 'usage:conversation-history', 'usage:current-message'] },
		]);
	});

	it('places role files and conductor templates under the scope that holds them', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 40,
			utilization: 0.04,
			was_trimmed: false,
			history_messages: 0,
			snapshot_id: 'ctx-roles',
			context_snapshot: {
				snapshot_id: 'ctx-roles',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [
					{
						source_id: 'role-context:global:global:agent:context/roles/agent/AGENT.md',
						origin_section: 'role',
						label: 'Agent · AGENT.md',
						stable_reference: '/workspace/context/roles/agent/AGENT.md',
						freshness: 'fresh',
						inclusion: 'included',
						reason: 'global context selected for agent role',
						owner_surface: 'document-viewer',
						prompt_tokens: 15,
						metadata: { provider: 'role_context', role: 'agent', scope: 'workspace' },
					},
					{
						source_id: 'conductor-template:core:v2',
						origin_section: 'conductor',
						label: 'Conductor core template (v2)',
						stable_reference: 'package://prompts.conductor/core-v2.md',
						freshness: 'packaged',
						inclusion: 'included',
						reason: 'designated core conductor context',
						owner_surface: 'conductor-designation',
						prompt_tokens: 25,
						metadata: { provider: 'conductor_template', template_kind: 'core' },
					},
				],
			},
		} as any);

		expect(model?.sourceGroups.map((group) => [group.category, group.tokenLabel])).toEqual([
			['System', '25 tokens'],
			['Workspace', '15 tokens'],
		]);
		expect(model?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				category: 'Workspace',
				label: 'AGENT.md',
				tags: ['agents only'],
				actionLabel: 'Open Document',
			}),
			expect.objectContaining({
				category: 'System',
				label: 'Conductor core template',
				fullLabel: 'Conductor core template (v2)',
				tags: ['agents only'],
			}),
		]));
	});

	it('groups dynamic context rows and exposes subtle delta labels', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 10,
			user_tokens: 0,
			total_used: 10,
			utilization: 0.01,
			was_trimmed: false,
			history_messages: 1,
			snapshot_id: 'ctx-dynamic',
			context_snapshot: {
				snapshot_id: 'ctx-dynamic',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [
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
						prompt_tokens: 300,
					},
					{
						source_id: 'dynamic:runtime',
						origin_section: 'dynamic',
						label: 'Runtime context',
						stable_reference: 'dynamic://runtime',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'context-widget',
						metadata: {
							provider: 'runtime_context',
							delta: { added: 1, removed: 1, changed: 2 },
						},
						prompt_tokens: 40,
					},
				],
			},
		});

		// Neither row names a scope: the map reaches a plain thread from CORSIA, and
		// a runtime row nobody has mapped lands in Other.
		expect(model?.sourceGroups.map((group) => group.category)).toEqual(['System', 'History', 'Other']);
		expect(model?.sourceGroups[0].tokenLabel).toBe('300 tokens');
		expect(model?.sourceGroups[0].countLabel).toBe('2 Agents');
		expect(model?.sourceGroups[2].tokenLabel).toBe('40 tokens');
		expect(model?.sourceGroups[2].deltaLabel).toBe('+1 -1 ~2');
		expect(model?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				id: 'usage:conversation-history',
				category: 'History',
				label: 'Conversation history',
				tokenCount: 10,
			}),
				expect.objectContaining({
					id: 'dynamic:agent-roster',
					category: 'System',
					label: 'Agent Responsibility Map',
					countLabel: '2 Agents',
				deltaLabel: '',
			}),
			expect.objectContaining({
				id: 'dynamic:runtime',
				category: 'Other',
				countLabel: '',
				deltaLabel: '+1 -1 ~2',
			}),
		]));
	});

	it('groups skills with existing omitted and error row grammar', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 58,
			utilization: 0.058,
			was_trimmed: false,
			history_messages: 0,
			snapshot_id: 'ctx-skills',
			context_snapshot: {
				snapshot_id: 'ctx-skills',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [
					{
						source_id: 'dynamic:skills-index',
						origin_section: 'dynamic',
						label: 'Skills',
						stable_reference: 'thread://s/skills',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'skills',
						metadata: {
							provider: 'skills',
							skill_count: 2,
							omitted_count: 1,
							error_count: 1,
						},
						prompt_tokens: 58,
					},
					{
						source_id: 'skill-error:global:global:broken',
						origin_section: 'dynamic',
						label: 'Skill error: broken',
						stable_reference: '/workspace/skills/broken/SKILL.md',
						freshness: 'error',
						inclusion: 'omitted',
						reason: 'skill validation failed',
						owner_surface: 'document-viewer',
						metadata: {
							provider: 'skills',
							errors: ['description is required'],
						},
					},
				],
			},
		} as any);

		// This install did not record its skills, so the index stays one row.
		expect(model?.sourceGroups).toHaveLength(1);
		expect(model?.sourceGroups[0].category).toBe('System');
		expect(model?.sourceGroups[0].countLabel).toBe('2 Skills · 1 Omitted · 1 Error');
		expect(model?.sources).toEqual([
			expect.objectContaining({
				id: 'dynamic:skills-index',
				category: 'System',
				statusLabel: 'Included',
				countLabel: '2 Skills · 1 Omitted · 1 Error',
			}),
			expect.objectContaining({
				id: 'skill-error:global:global:broken',
				category: 'System',
				statusLabel: 'Omitted',
				statusClass: 'omitted',
			}),
		]);
	});

	it('exposes compact per-source token labels when snapshot inventory has token counts', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 10,
			user_tokens: 0,
			total_used: 10,
			utilization: 0.01,
			was_trimmed: false,
			history_messages: 1,
			snapshot_id: 'ctx-source-tokens',
			context_snapshot: {
				snapshot_id: 'ctx-source-tokens',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [
					{
						source_id: 'project-bootloader:p1:PROJECT.md',
						origin_section: 'project',
						label: 'Project bootloader: PROJECT.md',
						stable_reference: '/workspace/projects/corsia/PROJECT.md',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'document-viewer',
						prompt_tokens: 42,
					},
					{
						source_id: 'dynamic:agent-roster',
						origin_section: 'dynamic',
						label: 'Agent Responsibility Map',
						stable_reference: 'thread://s/agents',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'agent-roster',
						metadata: { provider: 'agent_owner_map', agent_count: 2 },
						prompt_tokens: 1240,
					},
					{
						source_id: 'dynamic:runtime-context',
						origin_section: 'dynamic',
						label: 'Runtime context',
						stable_reference: 'thread://s/runtime-context',
						freshness: 'unavailable',
						inclusion: 'omitted',
						owner_surface: 'runtime-context',
						prompt_tokens: null,
					},
				],
			},
		} as any);

		expect(model?.sources).toEqual([
			expect.objectContaining({
				id: 'project-bootloader:p1:PROJECT.md',
				tokenCount: 42,
				tokenLabel: '42 tokens',
			}),
				expect.objectContaining({
					id: 'dynamic:agent-roster',
					label: 'Agent Responsibility Map',
				tokenCount: 1240,
				tokenLabel: '1.2k tokens',
			}),
			expect.objectContaining({
				id: 'dynamic:runtime-context',
				tokenCount: null,
				tokenLabel: '',
			}),
			expect.objectContaining({
				id: 'usage:conversation-history',
				tokenCount: 10,
				tokenLabel: '10 tokens',
			}),
		]);
	});

	it('adds compact history and tools rows from usage when inventory only lists managed sources', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 262144,
			system_tokens: 20000,
			tool_def_tokens: 512,
			history_tokens: 23456,
			user_tokens: 0,
			total_used: 52000,
			utilization: 0.198,
			preview: true,
			was_trimmed: false,
			history_messages: 23,
			context_snapshot: {
				snapshot_id: 'ctx-preview',
				source_inventory: [
					{
						source_id: 'system-bootstrap:AGENTS.md',
						origin_section: 'workspace',
						label: 'System context: AGENTS.md',
						stable_reference: '/workspace/AGENTS.md',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'document-viewer',
						prompt_tokens: 20000,
					},
				],
			},
		} as any);

		expect(model?.sourceGroups.map((group) => ({
			category: group.category,
			ids: group.sources.map((source) => source.id),
		}))).toEqual([
			{ category: 'Workspace', ids: ['system-bootstrap:AGENTS.md'] },
			{ category: 'Tools', ids: ['usage:tool-definitions'] },
			{ category: 'History', ids: ['usage:conversation-history'] },
		]);
		expect(model?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				id: 'usage:conversation-history',
				category: 'History',
				label: 'Conversation history',
				countLabel: '23 Messages',
				tokenLabel: '23k tokens',
			}),
			expect.objectContaining({
				id: 'usage:tool-definitions',
				category: 'Tools',
				label: 'Tool definitions',
				tokenLabel: '512 tokens',
			}),
		]));
	});

	it('reports a consistent measured headline overlay as measured', () => {
		const summary = contextSheetUsageSummary({
			model_context_window: 1_000_000,
			system_tokens: 100,
			tool_def_tokens: 200,
			history_tokens: 300,
			user_tokens: 10,
			total_used: 517_287,
			utilization: 0.517,
			token_usage_available: true,
			token_source: 'measured',
			preview: true,
			was_trimmed: false,
			history_messages: 2,
		});

		expect(summary.hasMeasuredUsage).toBe(true);
		expect(summary.tokenSource).toBe('measured');
		expect(summary.usedTokens).toBe(517_287);
		expect(summary.pct).toBe(52);
	});

	it('fails closed when measured provenance contradicts unavailable usage', () => {
		const summary = contextSheetUsageSummary({
			model_context_window: 1_000_000,
			system_tokens: 100,
			tool_def_tokens: 200,
			history_tokens: 300,
			user_tokens: 10,
			total_used: 517_287,
			utilization: 0.517,
			token_usage_available: false,
			token_source: 'measured',
			preview: true,
			was_trimmed: false,
			history_messages: 2,
		});

		expect(summary.hasMeasuredUsage).toBe(false);
		expect(summary.tokenSource).toBeNull();
		expect(summary.usedTokens).toBeNull();
		expect(summary.pct).toBeNull();
	});

	it('uses measured occupancy for the gauge while keeping attribution rows estimated', () => {
		const usage = {
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
		} as any;
		const model = contextSheetSnapshotModel(usage);
		const summary = contextSheetUsageSummary(usage, model);

		expect(model?.sourceGroups.map((group) => ({
			category: group.category,
			tokens: group.tokenLabel,
		}))).toEqual([
			{ category: 'System', tokens: '20k tokens' },
			{ category: 'Project', tokens: '42k tokens' },
			{ category: 'History', tokens: '59k tokens' },
		]);
		expect(summary.usedTokens).toBe(59_000);
		expect(summary.pct).toBe(6);
	});

	it('never synthesizes a provider-overhead row (implicit overhead is dead)', () => {
		const usage = {
			model_context_window: 10_000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 400,
			user_tokens: 0,
			total_used: 1_500,
			utilization: 0.06,
			// Legacy field: even if a stale payload still carries it, no row is drawn.
			provider_overhead_tokens: 500,
			preview: false,
			was_trimmed: false,
			history_messages: 2,
			context_snapshot: {
				snapshot_id: 'ctx-provider-overhead',
				source_inventory: [
					{
						source_id: 'system:bootloader',
						origin_section: 'system',
						label: 'System context: AGENTS.md',
						stable_reference: '/workspace/AGENTS.md',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'document-viewer',
						prompt_tokens: 600,
					},
				],
			},
		} as any;
		const model = contextSheetSnapshotModel(usage);

		expect(model?.sourceGroups.map((group) => group.category)).not.toContain('Provider');
		expect(model?.sources.some((row) => row.id === 'usage:provider-overhead')).toBe(false);
	});

	it('reports estimated (not measured) usage for a reconstructed snapshot', () => {
		const usage = {
			model_context_window: 200_000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 42_000,
			utilization: 0.21,
			token_usage_available: false,
			token_source: 'estimated',
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-estimated',
				source_inventory: [
					{
						source_id: 'system:seed',
						origin_section: 'system',
						label: 'Claude Code seed file',
						stable_reference: 'thread://s/cc-seed',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'cc-runtime',
						prompt_tokens: 70_000,
					},
				],
			},
		} as any;
		const model = contextSheetSnapshotModel(usage);
		const summary = contextSheetUsageSummary(usage, model);

		// Estimated rows preserve their provenance while still exposing the
		// common used/window gauge both the labelled sheet and chip render.
		expect(summary.hasMeasuredUsage).toBe(false);
		expect(summary.tokenSource).toBe('estimated');
		expect(summary.usedTokens).toBe(42_000);
		expect(summary.pct).toBe(21);
		expect(summary.level).toBe('ok');
	});

	it('treats an unlabeled next-turn preview as estimated, never measured', () => {
		const preview = {
			model_context_window: 200_000,
			system_tokens: 1_000,
			tool_def_tokens: 0,
			history_tokens: 30_000,
			user_tokens: 100,
			total_used: 43_900,
			utilization: 0.22,
			preview: true,
			was_trimmed: false,
			history_messages: 12,
			context_snapshot: {
				snapshot_id: 'ctx-preview',
				source_inventory: [{
					source_id: 'next-turn-preview',
					origin_section: 'preflight',
					label: 'Native next-turn preview',
					inclusion: 'included',
					owner_surface: 'context-widget',
				}],
			},
		} as any;

		const summary = contextSheetUsageSummary(preview);

		expect(summary.hasMeasuredUsage).toBe(false);
		expect(summary.tokenSource).toBe('estimated');
		expect(summary.usedTokens).toBe(43_900);
		expect(summary.pct).toBe(22);
	});

	it('reports measured usage when the harness reported provider token usage', () => {
		const usage = {
			model_context_window: 200_000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 42_000,
			utilization: 0.21,
			token_usage_available: true,
			token_source: 'measured',
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-measured',
				source_inventory: [
					{
						source_id: 'system:seed',
						origin_section: 'system',
						label: 'Claude Code seed file',
						stable_reference: 'thread://s/cc-seed',
						freshness: 'fresh',
						inclusion: 'included',
						owner_surface: 'cc-runtime',
						prompt_tokens: 42_000,
					},
				],
			},
		} as any;
		const summary = contextSheetUsageSummary(usage);

		expect(summary.hasMeasuredUsage).toBe(true);
		expect(summary.tokenSource).toBe('measured');
		expect(summary.pct).toBe(21);
	});

	it('gives the composer chip a labelled estimated percentage', () => {
		// InputBarControls prefixes the shared numeric estimate with '~', so the
		// chip and sheet agree without presenting it as a provider measurement.
		const estimated = {
			model_context_window: 200_000,
			total_used: 42_000,
			utilization: 0.21,
			token_usage_available: false,
			token_source: 'estimated',
			context_snapshot: {
				snapshot_id: 'ctx-estimated-chip',
				source_inventory: [{
					source_id: 'system:seed',
					origin_section: 'system',
					inclusion: 'included',
					prompt_tokens: 42_000,
				}],
			},
		} as any;
		const summary = contextSheetUsageSummary(estimated);
		const chip = { pct: summary.pct, utilization: summary.utilization };

		expect(chip.pct).toBe(21);
	});

	it('omits category token totals when no rows have prompt-token estimates', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 0,
			utilization: 0,
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-no-source-tokens',
				source_inventory: [
					{
						source_id: 'dynamic:runtime-context',
						origin_section: 'dynamic',
						label: 'Runtime context',
						stable_reference: 'thread://s/runtime-context',
						freshness: 'unavailable',
						inclusion: 'omitted',
						owner_surface: 'runtime-context',
						prompt_tokens: null,
					},
				],
			},
		} as any);

		expect(model?.sourceGroups[0]).toMatchObject({
			category: 'Other',
			tokenLabel: '',
		});
	});

	it('does not synthesize a second tools row when exact Native schemas are inventoried', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 262144,
			system_tokens: 0,
			tool_def_tokens: 512,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 512,
			utilization: 512 / 262_144,
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-native-tools',
				source_inventory: [{
					source_id: 'native:tool-definitions',
					origin_section: 'tools',
					label: 'Native tool definitions',
					stable_reference: 'thread://s/tool-definitions',
					freshness: 'fresh',
					inclusion: 'included',
					owner_surface: 'provider-settings',
					prompt_tokens: 512,
				}],
			},
		});

		expect(model?.sourceGroups).toHaveLength(1);
		expect(model?.sourceGroups[0].sources.map((source) => source.id)).toEqual([
			'native:tool-definitions',
		]);
	});

	it('groups managed agent-send framing under History', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 262144,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 17,
			utilization: 17 / 262_144,
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-agent-envelope',
				source_inventory: [{
					source_id: 'native:agent-send-envelope',
					origin_section: 'provider',
					label: 'Agent-send envelope',
					stable_reference: 'thread://s/agent-send-envelope',
					freshness: 'fresh',
					inclusion: 'included',
					owner_surface: 'message-format',
					prompt_tokens: 17,
				}],
			},
		});

		expect(model?.sourceGroups).toEqual([
			expect.objectContaining({
				category: 'History',
				sources: [expect.objectContaining({
					id: 'native:agent-send-envelope',
					tokenCount: 17,
				})],
			}),
		]);
	});

	it('returns null when no effective snapshot is present', () => {
		expect(contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 0,
			utilization: 0,
			was_trimmed: false,
			history_messages: 0,
		})).toBeNull();
	});


	it('carries source project scope for document-owned rows', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 10,
			user_tokens: 0,
			total_used: 10,
			utilization: 0.01,
			was_trimmed: false,
			history_messages: 1,
			snapshot_id: 'ctx-project-source',
			context_snapshot: {
				snapshot_id: 'ctx-project-source',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [
					{
						source_id: 'project-bootloader:corsia:PROJECT.md',
						origin_section: 'project',
						label: 'Project bootloader: PROJECT.md',
						stable_reference: '/Users/alice/Documents/projects/corsia/PROJECT.md',
						freshness: 'fresh',
						inclusion: 'included',
						reason: 'project bootloader file',
						owner_surface: 'document-viewer',
						metadata: {
							project_id: 'corsia',
							source_file: 'PROJECT.md',
						},
					},
				],
			},
		});

		expect(model?.sources[0]).toMatchObject({
				id: 'project-bootloader:corsia:PROJECT.md',
				actionLabel: 'Open Document',
				ownerLabel: 'Document',
				projectId: 'corsia',
			});
	});

	it('keeps technical model and preflight rows out of the composer source index', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 10,
			user_tokens: 0,
			total_used: 10,
			utilization: 0.01,
			was_trimmed: false,
			history_messages: 1,
			snapshot_id: 'ctx-model',
			context_snapshot: {
				snapshot_id: 'ctx-model',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [
					{
						source_id: 'model:codex',
						origin_section: 'model',
						label: 'Codex model: codex/gpt-5-codex',
						stable_reference: 'model://codex/gpt-5-codex',
						freshness: 'fresh',
						inclusion: 'included',
						reason: 'selected harness model',
						owner_surface: 'provider-settings',
						metadata: {
							harness: 'codex',
							model: 'codex/gpt-5-codex',
							execution_class: 'codex_main',
							is_subagent: false,
						},
					},
					{
						source_id: 'next-turn-preview',
						origin_section: 'preflight',
						label: 'Native next-turn preview',
						stable_reference: 'thread://s/next-turn-preview',
						freshness: 'preview',
						inclusion: 'blocked',
						reason: 'Projected from selected session runtime before provider dispatch',
						owner_surface: 'context-widget',
						metadata: {
							preflight: {
								status: 'blocked',
								continuity_mode: 'blocked',
								model_context_window: 1500,
								total_tokens: 2200,
							},
						},
					},
				],
			},
		});

		expect(model?.sources).toEqual([
			expect.objectContaining({
				id: 'usage:conversation-history',
				category: 'History',
				label: 'Conversation history',
				tokenCount: 10,
			}),
		]);
		expect(model?.sourceGroups.map((group) => group.category)).toEqual(['History']);
	});

	it('uses document names instead of absolute paths for primary source labels', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 10,
			utilization: 0.01,
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-paths',
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
					},
				],
			},
		});

		expect(model?.sources[0].label).toBe('AGENTS.md');
		expect(model?.sources[0].label).not.toContain('/Users/');
		expect(model?.sources[0].label).not.toContain('System context:');
	});
});

describe('contextSheetUsageSummary: budget vs model window (#1753)', () => {
	// An external harness keeps its own conversation. CORSIA can only fold it
	// at an operator budget, so between crossing that budget and the fold
	// installing, measured occupancy is honestly larger than the number the
	// widget counts against. The widget has to say which limit it is showing.
	const capped = (total: number) =>
		({
			model_context_window: 262_144,
			discovered_context_window: 1_000_000,
			total_used: total,
			token_source: 'measured',
			token_usage_available: true,
		}) as any;

	it('names the counted window a budget when the provider offers more', () => {
		const summary = contextSheetUsageSummary(capped(131_072));

		expect(summary.countsAgainstBudget).toBe(true);
		expect(summary.modelWindowTokens).toBe(1_000_000);
		expect(summary.windowTokens).toBe(262_144);
		expect(summary.pct).toBe(50);
		expect(summary.overBudget).toBe(false);
	});

	it('reports over-budget occupancy instead of clamping it to 100%', () => {
		const summary = contextSheetUsageSummary(capped(310_000));

		expect(summary.overBudget).toBe(true);
		expect(summary.pct).toBe(118);
		expect(summary.usedTokens).toBe(310_000);
		expect(summary.level).toBe('crit');
	});

	it('treats an equal discovered window as no budget at all', () => {
		const summary = contextSheetUsageSummary({
			model_context_window: 200_000,
			discovered_context_window: 200_000,
			total_used: 100_000,
			token_source: 'measured',
			token_usage_available: true,
		} as any);

		expect(summary.countsAgainstBudget).toBe(false);
		expect(summary.modelWindowTokens).toBe(null);
	});

	it('treats a missing discovered window as no budget (Native sends none)', () => {
		const summary = contextSheetUsageSummary({
			model_context_window: 200_000,
			total_used: 100_000,
			token_source: 'measured',
			token_usage_available: true,
		} as any);

		expect(summary.countsAgainstBudget).toBe(false);
		expect(summary.modelWindowTokens).toBe(null);
		expect(summary.pct).toBe(50);
	});
});

// Issue #1817: CC/Codex threads report a measured total the individual rows
// never explain (no native-style tool_def_tokens, opaque history). The
// backend now carves a CORSIA MCP tool-def estimate into tool_def_tokens and
// reports the remainder as attribution_delta_tokens; this plainly-labeled
// "Other harness context" row is what makes the rows sum to the provider
// total (#1952: same mechanism now also feeds a preview's fixed margin).
describe('held-by-the-harness row', () => {
	function ccUsage(overrides: Record<string, unknown> = {}) {
		return {
			model_context_window: 200_000,
			system_tokens: 100,
			tool_def_tokens: 3_000,
			history_tokens: 0,
			user_tokens: 50,
			total_used: 40_000,
			utilization: 0.2,
			token_usage_available: true,
			token_source: 'measured',
			was_trimmed: false,
			history_messages: 0,
			attribution_delta_tokens: 36_850,
			attribution_delta_reason: 'unobservable harness/provider overhead',
			context_snapshot: {
				snapshot_id: 'ctx-cc-harness-row',
				harness_model_facts: { harness: 'codex' },
				source_inventory: [{
					source_id: 'system:seed',
					origin_section: 'system',
					inclusion: 'included',
					prompt_tokens: 100,
				}],
			},
			...overrides,
		} as any;
	}

	it('shows the harness row and the carved-out tool-definitions row for Claude Code/Codex', () => {
		const model = contextSheetSnapshotModel(ccUsage());

		expect(model?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				id: 'usage:tool-definitions',
				category: 'Tools',
				statusLabel: 'Estimated',
				statusClass: 'preview',
				tokenCount: 3_000,
			}),
			expect.objectContaining({
				id: 'usage:attribution-delta',
				category: 'Other',
				label: 'Other harness context (not itemized)',
				statusClass: 'included',
				tokenCount: 36_850,
				tokenLabel: '37k tokens',
			}),
		]));
		// Rows sum exactly to the provider-reported total.
		const sum = model?.sources.reduce((total, source) => total + (source.tokenCount ?? 0), 0);
		expect(sum).toBe(40_000);
	});

	it('keys the Native-variance gate on the structured harness field, not the reason text', () => {
		// A non-Native harness whose reason text happens to read like Native's
		// should still get the ordinary (non-suppressed) harness treatment.
		const spoofed = contextSheetSnapshotModel(ccUsage({
			attribution_delta_tokens: -40,
			attribution_delta_reason: 'native framing/tokenizer variance',
		}));
		expect(spoofed?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				id: 'usage:attribution-delta',
				label: 'Rows exceed provider total (estimate overcounts)',
			}),
		]));

		// A harness structurally marked native, even with an unrelated reason
		// string, must still get Native's small-delta suppression.
		const relabeled = contextSheetSnapshotModel(nativeUsage({
			attribution_delta_tokens: -40,
			attribution_delta_reason: 'unobservable harness/provider overhead',
		}));
		expect(relabeled?.sources.some((source) => source.id === 'usage:attribution-delta')).toBe(false);
	});

	it('renders a negative delta honestly as an over-estimate, not clamped or hidden', () => {
		const model = contextSheetSnapshotModel(ccUsage({
			attribution_delta_tokens: -500,
		}));

		expect(model?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				id: 'usage:attribution-delta',
				label: 'Rows exceed provider total (estimate overcounts)',
				statusClass: 'preview',
				statusLabel: 'Over-estimate',
				tokenCount: -500,
				tokenLabel: '-500 tokens',
			}),
		]));
	});

	it('hides a zero delta (nothing left unexplained)', () => {
		const model = contextSheetSnapshotModel(ccUsage({ attribution_delta_tokens: 0 }));

		expect(model?.sources.some((source) => source.id === 'usage:attribution-delta')).toBe(false);
	});

	it('omits the row entirely when the total is unavailable (no attribution_delta_tokens)', () => {
		const usage = ccUsage();
		delete usage.attribution_delta_tokens;
		delete usage.attribution_delta_reason;
		usage.total_used = null;
		usage.token_usage_available = false;
		usage.tool_def_tokens = 0;

		const model = contextSheetSnapshotModel(usage);

		expect(model?.sources.some((source) => source.id === 'usage:attribution-delta')).toBe(false);
		expect(model?.sources.some((source) => source.id === 'usage:tool-definitions')).toBe(false);
	});

	function nativeUsage(overrides: Record<string, unknown> = {}) {
		return ccUsage({
			tool_def_tokens: 0,
			context_snapshot: {
				snapshot_id: 'ctx-native-variance',
				harness_model_facts: { harness: 'native' },
				source_inventory: [],
			},
			...overrides,
		});
	}

	it('suppresses a small Native tokenizer-variance delta as immaterial noise', () => {
		const model = contextSheetSnapshotModel(nativeUsage({
			attribution_delta_tokens: -40,
			attribution_delta_reason: 'native framing/tokenizer variance',
		}));

		expect(model?.sources.some((source) => source.id === 'usage:attribution-delta')).toBe(false);
	});

	it('still shows a large Native delta once it is material', () => {
		const model = contextSheetSnapshotModel(nativeUsage({
			attribution_delta_tokens: -5_000,
			attribution_delta_reason: 'native framing/tokenizer variance',
		}));

		expect(model?.sources).toEqual(expect.arrayContaining([
			expect.objectContaining({
				id: 'usage:attribution-delta',
				label: 'Rows exceed provider total (estimate overcounts)',
				tokenCount: -5_000,
			}),
		]));
	});

	it('breaks connected-app tool costs into sorted action rows that reconcile', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000, system_tokens: 0, tool_def_tokens: 100,
			history_tokens: 0, user_tokens: 0, total_used: 100, utilization: 0.1,
			was_trimmed: false, history_messages: 0,
			context_snapshot: { source_inventory: [
				{ source_id: 'native:tool-index:github', origin_section: 'tools', label: 'github', inclusion: 'included', prompt_tokens: 30, metadata: { provider: 'mcp_index', server_name: 'github', display_name: 'GitHub', tool_count: 46 } },
				{ source_id: 'native:tool-index:linear', origin_section: 'tools', label: 'linear', inclusion: 'included', prompt_tokens: 10, metadata: { provider: 'mcp_index', server_name: 'linear', display_name: 'Linear', tool_count: 4 } },
				{ source_id: 'native:tool-definitions', origin_section: 'tools', label: 'CORSIA tools', inclusion: 'included', prompt_tokens: 60, metadata: { provider: 'corsia_tools' } },
			] },
		});
		const tools = model?.sourceGroups.find((group) => group.id === 'tools');
		expect(tools?.sources.map((row) => row.label)).toEqual(['GitHub', 'Linear', 'CORSIA tools']);
		expect(tools?.sources.map((row) => row.countLabel)).toEqual(['46 actions', '4 actions', '']);
		expect(tools?.tokenCount).toBe(100);
	});

	it('carves looked-up app definitions out of History without moving any total', () => {
		const usage = {
			model_context_window: 10000, system_tokens: 0, tool_def_tokens: 0,
			history_tokens: 1000, user_tokens: 50, total_used: 1050, utilization: 0.105,
			was_trimmed: false, history_messages: 6, context_snapshot: { source_inventory: [] },
			loaded_tool_tokens: [
				{ server: 'github', display_name: 'GitHub', tokens: 400 },
				{ server: 'linear', display_name: 'Linear', tokens: 150 },
			],
		};
		const model = contextSheetSnapshotModel(usage);
		const history = model?.sourceGroups.find((group) => group.id === 'history');
		const rows = Object.fromEntries((history?.sources ?? []).map((row) => [row.label, row.tokenCount]));
		expect(rows).toMatchObject({
			'Conversation history': 450, 'GitHub, looked up (estimated)': 400, 'Linear, looked up (estimated)': 150, 'Current message': 50,
		});
		expect(history?.tokenCount).toBe(1050);
		expect(usage.total_used).toBe(1050);
		const looked = (history?.sources ?? []).filter((row) => row.label.includes('looked up'));
		expect(looked.every((row) => row.wrapLabel === true && row.statusClass === 'included')).toBe(true);
		expect((history?.sources ?? []).find((row) => row.label === 'Conversation history')?.wrapLabel).toBeUndefined();
	});

	it('shows History unchanged when nothing was looked up', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 10000, system_tokens: 0, tool_def_tokens: 0,
			history_tokens: 1000, user_tokens: 0, total_used: 1000, utilization: 0.1,
			was_trimmed: false, history_messages: 6, context_snapshot: { source_inventory: [] },
		});
		const history = model?.sourceGroups.find((group) => group.id === 'history');
		expect(history?.sources.map((row) => row.label)).toEqual(['Conversation history']);
		expect(history?.tokenCount).toBe(1000);
	});

});
