// jsdom implements neither the Web Animations API nor matchMedia, which Svelte
// transitions and the viewport stores rely on. Without these, mounting any
// component that transitions throws instead of rendering.
if (typeof Element !== 'undefined' && typeof Element.prototype.animate !== 'function') {
	Element.prototype.animate = function animate() {
		return {
			cancel() {},
			finish() {},
			play() {},
			pause() {},
			reverse() {},
			addEventListener() {},
			removeEventListener() {},
			currentTime: 0,
			playState: 'finished',
			finished: Promise.resolve(),
		} as unknown as Animation;
	};
}

if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
	window.matchMedia = ((query: string) => ({
		matches: false,
		media: query,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	})) as unknown as typeof window.matchMedia;
}

const localStorageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');

if (
	!localStorageDescriptor
	|| !('value' in localStorageDescriptor)
	|| typeof localStorageDescriptor.value?.getItem !== 'function'
) {
	const store = new Map<string, string>();
	(globalThis as unknown as { localStorage: Storage }).localStorage = {
		getItem: (k: string) => store.get(k) ?? null,
		setItem: (k: string, v: string) => { store.set(k, String(v)); },
		removeItem: (k: string) => { store.delete(k); },
		clear: () => store.clear(),
		key: (i: number) => Array.from(store.keys())[i] ?? null,
		get length() { return store.size; },
	} as Storage;
}
