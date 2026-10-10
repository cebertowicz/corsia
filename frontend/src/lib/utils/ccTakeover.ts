export function prettifyCCModel(resolvedId: string | null | undefined): string {
	if (!resolvedId) return 'Claude Code';
	let s = resolvedId.split('[')[0].trim();
	if (s.startsWith('claude-')) s = s.slice('claude-'.length);
	const parts = s.split('-').filter(Boolean);
	if (parts.length === 0) return 'Claude Code';
	const name = parts[0].charAt(0).toUpperCase() + parts[0].slice(1);
	const version = parts.slice(1).join('.');
	return version ? `${name} ${version}` : name;
}
