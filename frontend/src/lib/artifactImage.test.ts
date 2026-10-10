import { describe, expect, it } from 'vitest';
import { imageContentUrl } from './artifactImage';

describe('the canvas never infers an image from a path', () => {
	it('exports only the URL builder; the backend alone says what is an image', async () => {
		const exported = await import('./artifactImage');
		expect(Object.keys(exported)).toEqual(['imageContentUrl']);
	});
});

describe('imageContentUrl', () => {
	it('requests the raw bytes of the path through the authenticated file route', () => {
		const url = new URL(
			imageContentUrl({ path: '/ws/a b.png', projectId: 'p1', token: 'tok', revision: 'r1' }),
			'http://x',
		);
		expect(url.pathname).toBe('/api/files/content');
		expect(url.searchParams.get('raw')).toBe('true');
		expect(url.searchParams.get('path')).toBe('/ws/a b.png');
		expect(url.searchParams.get('project_id')).toBe('p1');
		expect(url.searchParams.get('token')).toBe('tok');
		expect(url.searchParams.get('v')).toBe('r1');
	});

	it('omits the optional parts', () => {
		const url = new URL(imageContentUrl({ path: '/ws/a.gif', token: 't' }), 'http://x');
		expect(url.searchParams.has('project_id')).toBe(false);
		expect(url.searchParams.has('v')).toBe(false);
	});
});
