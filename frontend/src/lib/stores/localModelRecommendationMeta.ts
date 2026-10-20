import { humanCtx } from './providerModelUtils';

export type LocalModelRecommendationMeta = {
	context_window: number;
	download_size_gb: number;
	quantization_bits?: number;
	relative_speed?: number;
};

/** The oMLX recommender's relative decode speed, expressed for a Settings decision. */
export function relativeSpeedLabel(relativeSpeed: number | undefined): string | null {
	if (relativeSpeed === undefined || !Number.isFinite(relativeSpeed)) return null;
	if (relativeSpeed >= 1.5) return 'Much faster';
	if (relativeSpeed >= 1.1) return 'Faster';
	if (relativeSpeed > 0.9) return null;
	if (relativeSpeed >= 0.7) return 'A bit slower';
	return 'Slower';
}

function quantizationLabel(bits: number | undefined): string | null {
	if (bits === undefined || !Number.isFinite(bits)) return null;
	return `${Number.isInteger(bits) ? bits : Number(bits.toFixed(1))}-bit`;
}

/** The one display formatter for an oMLX recommendation's structured metadata. */
export function localModelRecommendationMeta(model: LocalModelRecommendationMeta): string[] {
	return [
		`${humanCtx(model.context_window)} context`,
		quantizationLabel(model.quantization_bits),
		`${model.download_size_gb} GB`,
		relativeSpeedLabel(model.relative_speed),
	].filter((value): value is string => Boolean(value));
}
