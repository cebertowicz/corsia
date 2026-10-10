import { describe, expect, it } from 'vitest';
import artifactLeafSource from './ArtifactLeaf.svelte?raw';

describe('ArtifactLeaf expanded layout', () => {
	it('makes every leaf flex to fill its fixed-width column', () => {
		expect(artifactLeafSource).toContain(
			'height: 100%; display: flex; flex: 1; flex-direction: column; min-width: 0;',
		);
		expect(artifactLeafSource).toContain('.leaf.compact { flex: 1; border-left: none; }');
	});
});
