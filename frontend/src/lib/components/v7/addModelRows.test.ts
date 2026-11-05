import { describe, expect, it } from 'vitest';
import type { LocalModelRecommendation } from '$lib/conductorSources';
import { buildQuality, type ModelGroup } from './addModelRows';

function build(bits: number, quality = 4): LocalModelRecommendation {
	return { repository: `org/model-oQ${bits}e`, name: 'Model', quantization_bits: bits, smarts_level: quality } as LocalModelRecommendation;
}

describe('buildQuality', () => {
	it('ranks tied builds within the model, leaving the lowest plain', () => {
		for (const bits of [[4, 6], [4, 6, 8], [4, 6, 8, 10]]) {
			const builds = bits.map((bit) => build(bit));
			const group: ModelGroup = { name: 'Model', builds };
			const labels = [...builds].reverse().map((item) => buildQuality(group, item));
			const suffixes = ['best', '2nd-best', '3rd-best'];
			expect(labels).toEqual(bits.map((_, index) => index === bits.length - 1
				? 'Quality 4 of 5'
				: `Quality 4 of 5 · ${suffixes[index]} build`));
		}
	});

	it('does not add a ranking when the scores are distinct', () => {
		const builds = [build(4, 3), build(6, 4), build(8, 5)];
		const group: ModelGroup = { name: 'Model', builds };
		expect(builds.map((item) => buildQuality(group, item))).toEqual([
			'Quality 3 of 5', 'Quality 4 of 5', 'Quality 5 of 5',
		]);
	});
});
