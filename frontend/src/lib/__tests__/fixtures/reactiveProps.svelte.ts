/** Test-only: a reactive props bag so tests can change route props after mount. */
export function reactiveProps<T extends Record<string, unknown>>(initial: T): T {
	const bag = $state(initial);
	return bag;
}
