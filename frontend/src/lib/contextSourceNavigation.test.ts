import { describe, expect, it } from 'vitest';
import { contextSourceActionLabel, contextSourceFilePath, contextSourceOwnerLabel, contextSourceOwnerTarget } from './contextSourceNavigation';

describe('context source navigation', () => {
	it('opens a System projection using its metadata path', () => {
		const source = { layer: 'corsia', ownerSurface: 'system-tier', stableReference: 'package://context/example', filePath: '/workspace/.corsia/context/example.md' };
		expect(contextSourceOwnerTarget(source)).toBe('document');
		expect(contextSourceFilePath(source)).toBe('/workspace/.corsia/context/example.md');
		expect(contextSourceActionLabel(source)).toBe('Open Document');
	});
	it('opens document paths and retains settings navigation', () => {
		const source = { layer: 'project', ownerSurface: 'document-viewer', stableReference: '/workspace/project/context.md' };
		expect(contextSourceOwnerTarget(source)).toBe('document');
		expect(contextSourceFilePath(source)).toBe(source.stableReference);
		expect(contextSourceOwnerTarget({ layer: 'project', ownerSurface: 'project-settings', stableReference: 'project://example' })).toBe('project-settings');
	});
	it('keeps every settings route with its labels', () => {
		const routes = [
			[{ layer: 'project', ownerSurface: 'project-settings', stableReference: 'project://corsia/bootloader' }, 'project-settings', 'Open Project settings', 'Project settings'],
			[{ layer: 'dynamic', ownerSurface: 'agents', stableReference: 'thread://s/agents' }, 'project-settings', 'Open Project settings', 'Project settings'],
			[{ layer: 'model', ownerSurface: 'provider-settings', stableReference: 'model://qwen' }, 'providers', 'Open Providers', 'Providers'],
			[{ layer: 'workspace', ownerSurface: 'context-builder', stableReference: 'workspace://context/PROJECT.md' }, 'workspace-settings', 'Open Context settings', 'Context settings'],
			[{ layer: 'workspace', ownerSurface: 'workspace-settings', stableReference: 'workspace://context/PROJECT.md' }, 'workspace-settings', 'Open Context settings', 'Context settings'],
			[{ layer: 'system', ownerSurface: 'context-builder', stableReference: 'thread://s/system-prompt' }, 'system', 'Open System', 'System context'],
			[{ layer: 'project', ownerSurface: 'document-viewer', stableReference: '/workspace/project/context.md' }, 'document', 'Open Document', 'Document'],
		] as const;
		for (const [source, target, action, owner] of routes) {
			expect(contextSourceOwnerTarget(source)).toBe(target);
			expect(contextSourceActionLabel(source)).toBe(action);
			expect(contextSourceOwnerLabel(source)).toBe(owner);
		}
	});
	it('opens a file path when no settings surface owns the row', () => {
		const source = { layer: 'thread', ownerSurface: 'thread-bootloader', stableReference: '/workspace/project/.threads/t/RESPONSIBILITY.md' };
		expect(contextSourceOwnerTarget(source)).toBe('document');
		expect(contextSourceActionLabel(source)).toBe('Open Document');
		expect(contextSourceOwnerTarget({ ...source, stableReference: 'thread://t/bootloader' })).toBeNull();
	});
	it('has no action for the earlier-chat summary', () => {
		const source = { layer: 'summary', ownerSurface: 'summary-details', stableReference: 'summary://s-1' };
		expect(contextSourceOwnerTarget(source)).toBeNull();
		expect(contextSourceActionLabel(source)).toBe('');
		expect(contextSourceOwnerLabel(source)).toBe('');
	});
	it('has no action for generated sources without a file', () => {
		for (const ownerSurface of ['skills', 'portable-checkpoint', 'tool-registry', 'runtime-context', 'conductor-designation']) {
			const source = { layer: 'dynamic', ownerSurface, stableReference: 'generated://example' };
			expect(contextSourceOwnerTarget(source)).toBeNull();
			expect(contextSourceActionLabel(source)).toBe('');
		}
	});
});
