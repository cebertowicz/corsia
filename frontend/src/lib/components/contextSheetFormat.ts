import { humanTokens } from '$lib/format';

export function promptTokenLabel(tokens: number | null): string {
	return tokens === null ? '' : `${humanTokens(tokens)} tokens`;
}

export function signedPromptTokenLabel(tokens: number): string {
	return tokens < 0 ? `-${promptTokenLabel(-tokens)}` : promptTokenLabel(tokens);
}

export function pluralCount(count: number, singular: string, plural = `${singular}s`): string {
	const safe = Math.max(0, Math.round(count));
	return safe === 1 ? `1 ${singular}` : `${safe} ${plural}`;
}
