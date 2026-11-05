export function agentOptionIndex(key: string, active: number, count: number): number | null {
	if (!count) return null;
	if (key === 'Home') return 0;
	if (key === 'End') return count - 1;
	if (key === 'ArrowDown') return (active + 1) % count;
	if (key === 'ArrowUp') return active <= 0 ? count - 1 : active - 1;
	return null;
}
