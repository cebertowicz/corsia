import type { ModelState, ProviderModel } from './providerModels';

const TRANSITIONAL: ReadonlySet<ModelState> = new Set([
	'loading', 'cache_warming', 'queued', 'error',
]);

export function preserveTransitional(prev: ProviderModel[] | undefined, next: ProviderModel[]): ProviderModel[] {
	if (!prev) return next;
	const prevById = new Map(prev.map(m => [m.id, m]));
	return next.map(m => {
		const old = prevById.get(m.id);
		if (!old || !TRANSITIONAL.has(old.state)) return m;
		if (old.state === 'cache_warming') {
			if (m.state !== 'loaded') return m;
			return {
				...m,
				state: old.state,
				progress: old.progress,
				meta: old.meta,
				jobId: old.jobId,
				lastError: old.lastError,
			};
		}
		if (m.state === 'loaded') return m;
		return {
			...m,
			state: old.state,
			progress: old.progress,
			meta: old.meta,
			jobId: old.jobId,
			lastError: old.lastError,
		};
	});
}

export function dedupNetworkModels(models: ProviderModel[]): ProviderModel[] {
	const byId = new Map<string, ProviderModel>();
	for (const m of models) {
		const prev = byId.get(m.id);
		if (!prev) { byId.set(m.id, m); continue; }
		const prevIsLoaded = prev.state === 'loaded';
		const curIsLoaded = m.state === 'loaded';
		if (curIsLoaded && !prevIsLoaded) { byId.set(m.id, m); continue; }
		if (!curIsLoaded && prevIsLoaded) continue;
		if (m.dbId && !prev.dbId) byId.set(m.id, m);
	}
	return Array.from(byId.values());
}

export function capabilities(raw: { vision?: boolean; tool_use?: boolean; reasoning?: boolean }): ProviderModel['caps'] {
	const caps: ProviderModel['caps'] = [];
	if (raw.vision) caps.push('vision');
	if (raw.reasoning) caps.push('reasoning');
	if (raw.tool_use) caps.push('tools');
	return caps;
}

export function ctxAndSizeMeta(
	ctx: number | undefined,
	bytes: number | undefined,
	quantBits?: number | null,
	checkpointWindow?: number | null,
): string {
	const parts: string[] = [];
	if (ctx) parts.push(`${humanCtx(ctx)} context`);
	if (checkpointWindow) parts.push(`checkpoints at ${humanCtx(checkpointWindow)}`);
	if (quantBits) parts.push(`${quantBits}-bit`);
	if (bytes) parts.push(humanBytes(bytes));
	return parts.join(' · ');
}

export function omlxAdminUrl(mgmtBase?: string): string | undefined {
	return mgmtBase?.replace(/\/api\/?$/, '');
}

/** A saved window or the CORSIA default sets where this row checkpoints. */
export function checkpointsBelowProvider(row: {
	context_window_override?: number | null;
	context_window_source?: string | null;
}): boolean {
	return row.context_window_override != null || row.context_window_source === 'default';
}

export function humanCtx(n: number): string {
	if (n >= 1_000_000 && n % 1_000_000 === 0) return `${n / 1_000_000}M`;
	if (n >= 1000) return `${Math.round(n / 1000)}k`;
	return `${n}`;
}

export function humanBytes(b: number): string {
	const gb = b / (1024 * 1024 * 1024);
	if (gb >= 1) return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
	const mb = b / (1024 * 1024);
	return `${Math.round(mb)} MB`;
}
