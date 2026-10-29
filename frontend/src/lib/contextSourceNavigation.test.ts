import { describe, expect, it } from 'vitest';
import { contextSourceActionLabel, contextSourceFilePath, contextSourceOwnerTarget } from './contextSourceNavigation';

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
	it('has no action for generated sources without a file', () => {
		for (const ownerSurface of ['skills', 'portable-checkpoint', 'tool-registry', 'runtime-context']) {
			const source = { layer: 'dynamic', ownerSurface, stableReference: 'generated://example' };
			expect(contextSourceOwnerTarget(source)).toBeNull();
			expect(contextSourceActionLabel(source)).toBe('');
		}
	});
});
