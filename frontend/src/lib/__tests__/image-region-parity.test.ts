import { describe, expect, it } from 'vitest';
import { agentColor } from '$lib/docPresence';
import { placeTag, placeTags } from '$lib/imageRegions';
import { projectColor } from '$lib/utils/projectColors';
import contract from './fixtures/image-region-parity.contract.json';

// The marked-up copy an agent gets back is drawn by backend ports of these viewer
// rules; tests/test_image_region_parity.py holds the ports to the same fixture.
describe('image region parity contract', () => {
	it.each(contract.agent_colors)('agentColor($thread_id)', ({ thread_id, color }) => {
		expect(agentColor(thread_id)).toBe(color);
	});

	it.each(contract.project_colors)('projectColor($project_id)', ({ project_id, color }) => {
		expect(projectColor(project_id)).toBe(color);
	});

	it.each(contract.tag_placements)('placeTag: $case', ({ region, width, height, radius, gap, expected }) => {
		const tag = placeTag(region, { width, height }, radius, gap);
		expect(tag.side).toBe(expected.side);
		expect(tag.x).toBeCloseTo(expected.x, 3);
		expect(tag.y).toBeCloseTo(expected.y, 3);
	});

	it.each(contract.tag_sets)('placeTags: $case', ({ regions, width, height, radius, gap, dot, expected }) => {
		const tags = placeTags(regions, { width, height }, radius, gap, dot);
		expect(tags.map((tag) => tag.side)).toEqual(expected.map((tag) => tag.side));
		tags.forEach((tag, i) => {
			expect(tag.x).toBeCloseTo(expected[i].x, 3);
			expect(tag.y).toBeCloseTo(expected[i].y, 3);
		});
	});
});
