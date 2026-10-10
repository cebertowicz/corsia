import { readable, type Readable } from 'svelte/store';

const MOBILE_QUERY = '(max-width: 768px)';

/** Environments without `matchMedia` (SSR, jsdom) report the query as unmatched. */
function supported(): boolean {
	return typeof window !== 'undefined' && typeof window.matchMedia === 'function';
}

function createMatchMediaStore(query: string): Readable<boolean> {
	return readable<boolean>(
		supported() && window.matchMedia(query).matches,
		(set) => {
			if (!supported()) return;
			const mql = window.matchMedia(query);
			const handler = (e: MediaQueryListEvent | MediaQueryList) => set(e.matches);
			set(mql.matches);
			mql.addEventListener('change', handler);
			return () => mql.removeEventListener('change', handler);
		}
	);
}

export const isMobile = createMatchMediaStore(MOBILE_QUERY);

/** True when the user asked for reduced motion — transitions become instant. */
export const prefersReducedMotion = createMatchMediaStore('(prefers-reduced-motion: reduce)');
