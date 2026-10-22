/** Glyph shown in spawn chips. */
export function subagentGlyph(provider: string | undefined): string {
	if (provider === 'codex') return '⊙';
	if (provider === 'cc') return '⌘';
	return '◈';
}
