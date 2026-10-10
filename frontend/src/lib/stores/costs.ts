interface CostMeasurement {
	cost_usd: number | null;
	unknown_cost_rows?: number;
}

export type CostClass = 'local' | 'priced' | 'no_pricing';

export interface ModelEntry extends CostMeasurement {
	/** Full model identity; ``label`` is the catalog's name for it. */
	model: string;
	label: string;
	total_tokens: number;
	api_calls: number;
	cost_class?: CostClass;
	/** Set on a grouped row (``by_model_shown``) whose members include local models. */
	includes_local?: boolean;
}

export function formatCost(usd: number | null): string {
	if (usd === null) return '—';
	if (!usd) return '$0.00';
	if (usd >= 100) return `$${usd.toFixed(0)}`;
	if (usd >= 10) return `$${usd.toFixed(2)}`;
	if (usd >= 1) return `$${usd.toFixed(3)}`;
	if (usd >= 0.001) return `$${usd.toFixed(4)}`;
	return `$${usd.toFixed(6)}`;
}

export function formatKnownCost(usd: number | null, unknownRows = 0): string {
	if (!unknownRows) return formatCost(usd ?? 0);
	const missing = `${unknownRows} unpriced call${unknownRows === 1 ? '' : 's'}`;
	return usd === null ? `— (${missing})` : `${formatCost(usd)} + ${missing}`;
}

export function formatCostWithClass(usd: number | null, cls?: CostClass): string {
	if (cls === 'local') return '$0.00 (local)';
	if (cls === 'no_pricing') return '—';
	return formatCost(usd);
}
