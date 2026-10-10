import { describe, expect, it } from 'vitest';
import {
	contextSourceActionLabel,
	contextSourceOwnerLabel,
	contextSourceOwnerTarget,
} from './contextSourceNavigation';

describe('contextSourceNavigation', () => {
	it('maps context source rows to their owning surfaces', () => {
		expect(contextSourceOwnerTarget({
			layer: 'project',
			ownerSurface: 'project-settings',
			stableReference: 'project://corsia/bootloader',
		})).toBe('project-settings');
			expect(contextSourceActionLabel({
				layer: 'project',
				ownerSurface: 'project-settings',
				stableReference: 'project://corsia/bootloader',
			})).toBe('Open Project settings');
			expect(contextSourceOwnerLabel({
				layer: 'project',
				ownerSurface: 'project-settings',
				stableReference: 'project://corsia/bootloader',
			})).toBe('Project settings');

		expect(contextSourceOwnerTarget({
			layer: 'model',
			ownerSurface: 'provider-settings',
			stableReference: 'model://qwen',
		})).toBe('providers');

			expect(contextSourceOwnerTarget({
				layer: 'system',
				ownerSurface: 'context-builder',
				stableReference: 'thread://s/system-prompt',
			})).toBe('system');
			expect(contextSourceOwnerLabel({
				layer: 'system',
				ownerSurface: 'context-builder',
				stableReference: 'thread://s/system-prompt',
			})).toBe('System context');
			expect(contextSourceActionLabel({
				layer: 'workspace',
				ownerSurface: 'context-builder',
				stableReference: 'workspace://context/PROJECT.md',
			})).toBe('Open Workspace settings');
			expect(contextSourceOwnerLabel({
				layer: 'workspace',
				ownerSurface: 'context-builder',
				stableReference: 'workspace://context/PROJECT.md',
			})).toBe('Workspace settings');
			expect(contextSourceOwnerTarget({
				layer: 'corsia',
				ownerSurface: 'system-tier',
				stableReference: 'package://prompts.system/v1',
			})).toBe('runtime-details');
			expect(contextSourceActionLabel({
				layer: 'corsia',
				ownerSurface: 'system-tier',
				stableReference: 'package://prompts.system/v1',
			})).toBe('Open details');

			expect(contextSourceOwnerLabel({
				layer: 'workspace',
				ownerSurface: 'context-builder',
				stableReference: 'thread://s/system-prompt',
			})).toBe('Workspace settings');
			expect(contextSourceOwnerLabel({
				layer: 'workspace',
				ownerSurface: 'workspace-settings',
				stableReference: 'workspace://context/PROJECT.md',
			})).toBe('Workspace settings');

		expect(contextSourceOwnerTarget({
			layer: 'summary',
			ownerSurface: 'summary-details',
			stableReference: 'summary://s-1',
		})).toBeNull();
		expect(contextSourceActionLabel({
			layer: 'summary',
			ownerSurface: 'summary-details',
			stableReference: 'summary://s-1',
		})).toBe('');

		expect(contextSourceOwnerTarget({
			layer: 'dynamic',
			ownerSurface: 'runtime-context',
			stableReference: 'thread://s/runtime-context',
		})).toBe('runtime-details');
		expect(contextSourceActionLabel({
			layer: 'dynamic',
			ownerSurface: 'runtime-context',
			stableReference: 'thread://s/runtime-context',
		})).toBe('Open details');
		expect(contextSourceOwnerLabel({
			layer: 'dynamic',
			ownerSurface: 'runtime-context',
			stableReference: 'thread://s/runtime-context',
		})).toBe('Runtime context');

		expect(contextSourceOwnerTarget({
			layer: 'dynamic',
			ownerSurface: 'agents',
			stableReference: 'thread://s/agents',
		})).toBe('project-settings');
		expect(contextSourceActionLabel({
			layer: 'dynamic',
			ownerSurface: 'agents',
			stableReference: 'thread://s/agents',
		})).toBe('Open Project settings');

		expect(contextSourceOwnerTarget({
			layer: 'project',
			ownerSurface: 'document-viewer',
			stableReference: '/Users/alice/Documents/projects/corsia/PROJECT.md',
		})).toBe('document');
		expect(contextSourceActionLabel({
			layer: 'project',
			ownerSurface: 'document-viewer',
			stableReference: '/Users/alice/Documents/projects/corsia/PROJECT.md',
		})).toBe('Open Document');
	});
});
