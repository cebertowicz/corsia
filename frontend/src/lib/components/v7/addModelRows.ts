import type { LocalModelRecommendation } from '$lib/conductorSources';

export type ModelGroup = { name: string; builds: LocalModelRecommendation[] };

/** Group fitting builds by model and order them by quality or estimated speed. */
export function modelGroups(models: LocalModelRecommendation[], sort: 'quality' | 'speed', recommended: string | null): ModelGroup[] {
	const groups = new Map<string, ModelGroup>();
	for (const model of models) {
		const group = groups.get(model.name) ?? { name: model.name, builds: [] };
		group.builds.push(model);
		groups.set(model.name, group);
	}
	const level = (group: ModelGroup) => Math.max(...group.builds.map((build) => build.smarts_level ?? 0));
	const smarts = (group: ModelGroup) => Math.max(...group.builds.filter((build) => (build.smarts_level ?? 0) === level(group)).map((build) => build.smarts ?? 0));
	const speed = (group: ModelGroup) => Math.max(...group.builds.map((build) => build.tokens_per_second ?? 0));
	return [...groups.values()].map((group) => ({
		...group,
		builds: group.builds.sort((a, b) => (a.quantization_bits ?? 0) - (b.quantization_bits ?? 0)),
	})).sort((a, b) => {
		const recommendedFirst = sort === 'quality'
			? Number(b.builds.some((build) => build.repository === recommended)) - Number(a.builds.some((build) => build.repository === recommended))
			: 0;
		return recommendedFirst || (sort === 'quality' ? level(b) - level(a) || smarts(b) - smarts(a) : speed(b) - speed(a));
	});
}

/** Label ties within a model without claiming a cross-model ranking. */
export function buildQuality(group: ModelGroup, build: LocalModelRecommendation): string {
	if (build.smarts_level == null) return '';
	const tied = group.builds.filter((other) => other.smarts_level === build.smarts_level)
		.sort((a, b) => (b.quantization_bits ?? 0) - (a.quantization_bits ?? 0));
	const rank = tied.findIndex((other) => other.repository === build.repository);
	const suffix = tied.length > 1 && rank < tied.length - 1
		? ` · ${rank === 0 ? 'best' : rank === 1 ? '2nd-best' : rank === 2 ? '3rd-best' : `${rank + 1}th-best`} build`
		: '';
	return `Quality ${build.smarts_level} of 5${suffix}`;
}
