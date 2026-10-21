import { humanCtx } from './providerModelUtils';

export type LocalModelRecommendationMeta = {
	context_window: number;
	download_size_gb: number;
	quantization_bits?: number;
	relative_speed?: number;
	tokens_per_second?: number;
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

/** oMLX's estimated decode speed; the relative label is the fallback when it gives none. */
function speedLabel(model: LocalModelRecommendationMeta): string | null {
	const tps = model.tokens_per_second;
	if (tps !== undefined && Number.isFinite(tps) && tps > 0) return `~${Math.round(tps)} tokens/s est.`;
	return relativeSpeedLabel(model.relative_speed);
}

/** The one display formatter for an oMLX recommendation's structured metadata. */
export function localModelRecommendationMeta(model: LocalModelRecommendationMeta): string[] {
	return [
		`${humanCtx(model.context_window)} context`,
		quantizationLabel(model.quantization_bits),
		`${model.download_size_gb} GB`,
		speedLabel(model),
	].filter((value): value is string => Boolean(value));
}
