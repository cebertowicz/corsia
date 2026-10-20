import { describe, expect, it } from 'vitest';
import { localModelRecommendationMeta, relativeSpeedLabel } from './localModelRecommendationMeta';

describe('localModelRecommendationMeta', () => {
	it.each([
		[1.5, 'Much faster'], [1.1, 'Faster'], [1.0, null], [0.9, 'A bit slower'], [0.7, 'A bit slower'], [0.69, 'Slower'],
	])('formats the %s relative-speed band as %s', (value, expected) => {
		expect(relativeSpeedLabel(value)).toBe(expected);
	});

	it('formats structured oMLX recommender facts in the sheet order', () => {
		expect(localModelRecommendationMeta({
		context_window: 262144, quantization_bits: 4, download_size_gb: 16, relative_speed: 0.85,
	})).toEqual(['262k context', '4-bit', '16 GB', 'A bit slower']);
		expect(localModelRecommendationMeta({
		context_window: 262144, quantization_bits: 16, download_size_gb: 16,
	})).toEqual(['262k context', '16-bit', '16 GB']);
	});
});
