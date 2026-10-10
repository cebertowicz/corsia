import { prettifyCCModel } from './ccTakeover';

/** Glyph shown in spawn chips and the input-bar subagent model chip. */
export function subagentGlyph(provider: string | undefined): string {
	if (provider === 'codex') return '⊙';
	if (provider === 'cc') return '⌘';
	return '◈';
}

/** Short model name for the input-bar subagent model chip. */
export function subagentModelLabel(
	model: string | null | undefined,
	provider: string | undefined,
): string {
	if (provider === 'cc') return prettifyCCModel(model);
	if (!model) return provider === 'codex' ? 'Codex' : 'Native';
	const short = model.split('/').pop() ?? model;
	return short.length > 20 ? short.slice(0, 18) + '…' : short;
}

/** Tooltip text for the input-bar subagent model chip. */
export function subagentTitle(provider: string | undefined): string {
	if (provider === 'cc') return 'Claude Code session model';
	if (provider === 'codex') return 'Codex session model';
	return 'Native thread model';
}
