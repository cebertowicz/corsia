import { describe, expect, it } from 'vitest';
import { contextSheetSnapshotModel } from './contextSheetModel';

describe('contextSheetSnapshotModel origin categories', () => {
	it('maps workspace origin to Workspace', () => {
		const model = contextSheetSnapshotModel({
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 12,
			utilization: 0.012,
			was_trimmed: false,
			history_messages: 0,
			context_snapshot: {
				snapshot_id: 'ctx-legacy-workspace',
				content_hash: 'abcdef1234567890',
				delivery_leaves: [],
				source_inventory: [{
					source_id: 'system-bootstrap:context/AGENTS.md',
					origin_section: 'workspace',
					label: 'System context: context/AGENTS.md',
					stable_reference: 'workspace://context/AGENTS.md',
					freshness: 'fresh',
					inclusion: 'included',
					reason: 'legacy workspace context file',
					owner_surface: 'document-viewer',
					prompt_tokens: 12,
				}],
			},
		} as any);

		expect(model?.sourceGroups.map((group) => group.category)).toEqual(['Workspace']);
		expect(model?.sources[0]).toMatchObject({
			id: 'system-bootstrap:context/AGENTS.md',
			category: 'Workspace',
			label: 'AGENTS.md',
		});
	});
});
