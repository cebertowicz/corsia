import { flushSync, mount, unmount } from 'svelte';
import { expect, it } from 'vitest';
import ImageRegionThumb from './ImageRegionThumb.svelte';

it('names the kind of thumbnail in its accessible label', () => {
	const host = document.createElement('div');
	const component = mount(ImageRegionThumb, { target: host, props: {
		src: 'diagram.svg', alt: 'Diagram in Flow', kind: 'diagram', comments: [], colorOf: () => '#000',
	} });
	flushSync();
	expect(host.querySelector('button')?.getAttribute('aria-label')).toBe('Diagram in Flow: diagram with comments ');
	unmount(component);
});
