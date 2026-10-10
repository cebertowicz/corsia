/**
 * Backend the Vite dev server proxies /api and /ws to.
 *
 * There is deliberately no default: a fallback port can be a live production
 * backend (context/SAFETY-INVARIANTS.md: an unset target must not fall back
 * to production).
 */
export function readBackendUrl(env: Record<string, string | undefined>): string | undefined {
	return env.CORSIA_BACKEND_URL?.trim() || undefined;
}

/**
 * Called only when the dev server is created, so `vite build`, vitest,
 * `svelte-kit sync` and `svelte-check` (which load the config in serve mode
 * without a server) do not need the variable.
 */
export function requireBackendUrl(env: Record<string, string | undefined>): string {
	const url = readBackendUrl(env);
	if (!url) {
		throw new Error(
			'CORSIA_BACKEND_URL is not set. The dev frontend proxies to a backend you must name explicitly; ' +
				'export the URL of the dev or test backend you started, e.g. ' +
				'`export CORSIA_BACKEND_URL=http://127.0.0.1:<backend-port>`, then rerun `npm run dev`. ' +
				'Never point it at the production backend.',
		);
	}
	return url;
}
