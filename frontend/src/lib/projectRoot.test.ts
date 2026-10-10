import { describe, expect, it } from 'vitest';

import { isProjectRootChoice } from './projectRoot';

describe('projectRoot', () => {
	it('accepts only direct children of the projects folder', () => {
		const root = '/workspace/projects';
		expect(isProjectRootChoice(root, '/workspace/projects/corsia')).toBe(true);
		expect(isProjectRootChoice(`${root}/`, '/workspace/projects/corsia/')).toBe(true);
	});

	it('rejects the projects folder itself, folders outside it, and nested folders', () => {
		const root = '/workspace/projects';
		expect(isProjectRootChoice(root, '/workspace/projects')).toBe(false);
		expect(isProjectRootChoice(root, '/workspace')).toBe(false);
		expect(isProjectRootChoice(root, '/repo/new-root')).toBe(false);
		expect(isProjectRootChoice(root, '/workspace/projects/corsia/source')).toBe(false);
		expect(isProjectRootChoice(root, '/workspace/projects-other/corsia')).toBe(false);
		expect(isProjectRootChoice('', '/workspace/projects/corsia')).toBe(false);
	});
});
