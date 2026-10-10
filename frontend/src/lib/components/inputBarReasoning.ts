import type { ReasoningConfig } from '$lib/stores/models';

export type ReasoningSegment = { value: string | null; label: string };

const EFFORT_PHRASE: Record<string, string> = {
	auto: 'Auto',
	none: 'Off',
	minimal: 'Min',
	low: 'Low',
	medium: 'Med',
	high: 'High',
	xhigh: 'XHi',
	max: 'Max',
};

export function effortButtonLabel(level: string): string {
	return EFFORT_PHRASE[level] ?? (level[0].toUpperCase() + level.slice(1));
}

export function effortChipLabel(level: string): string {
	return effortButtonLabel(level).toUpperCase();
}

export function resolveReasoningEffort(
	raw: string | null | undefined,
	config: ReasoningConfig | null,
): string | null {
	if (!config) return null;
	if (config.type === 'boolean') {
		if (raw === 'none' || raw === 'medium') return raw;
		return config.default ? 'medium' : 'none';
	}
	if (config.type === 'effort_levels') {
		// Without a provider "auto" mode there is no unset state to fall back to:
		// an untouched picker shows the level the server is configured to use.
		const unset = config.supports_auto ? null : config.default;
		if (raw === null || raw === undefined) return unset;
		if (raw === 'none') return config.supports_off === false ? unset : 'none';
		return config.values.includes(raw) ? raw : unset;
	}
	if (raw === null || raw === undefined) return null;
	if (raw === 'none' || raw === 'low' || raw === 'medium' || raw === 'high') return raw;
	return null;
}

export function reasoningEffortLabel(
	config: ReasoningConfig | null,
	effort: string | null,
): string {
	if (!config) return '';
	if (config.type === 'boolean') {
		return effort === 'medium' ? 'ON' : 'OFF';
	}
	if (effort === null) {
		if (config.type === 'effort_levels' && !config.supports_auto && config.default) {
			return effortChipLabel(config.default);
		}
		return effortChipLabel('auto');
	}
	return effortChipLabel(effort);
}

export function reasoningSegments(config: ReasoningConfig | null): ReasoningSegment[] {
	if (!config) return [];
	if (config.type === 'boolean') {
		return [
			{ value: 'none', label: 'Off' },
			{ value: 'medium', label: 'On' },
		];
	}
	if (config.type === 'effort_levels') {
		const base: ReasoningSegment[] = [
			// Only offered by providers that actually have an auto mode; a local
			// model opens on its server-configured level instead.
			...(config.supports_auto ? [{ value: null, label: 'Auto' }] : []),
			...(config.supports_off === false ? [] : [{ value: 'none', label: 'Off' }]),
		];
		// 'none' is already in base — filter it from config.values to avoid a duplicate key
		// in the Svelte #each block (Codex includes 'none' in its values list; CC does not).
		const extra = config.values.filter(v => v !== 'none').map(value => ({ value, label: effortButtonLabel(value) }));
		return [...base, ...extra];
	}
	return [
		{ value: null, label: 'Auto' },
		{ value: 'none', label: 'Off' },
		{ value: 'low', label: 'Low' },
		{ value: 'medium', label: 'Med' },
		{ value: 'high', label: 'High' },
	];
}
