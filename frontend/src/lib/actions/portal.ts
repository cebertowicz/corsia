/**
 * Moves `node` to `target` (default `document.body`) once it mounts, and
 * removes it again on destroy.
 *
 * A `position: fixed` element still resolves against a CSS containing block
 * an ancestor establishes with `transform`, `filter`, `backdrop-filter`, or
 * `perspective` — not the true viewport. Rendering the node as a DOM
 * descendant of `document.body` instead sidesteps any such ancestor.
 */
export function portal(node: HTMLElement, target: HTMLElement | string = document.body) {
	const targetEl = typeof target === 'string' ? document.querySelector(target) : target;
	if (!targetEl) throw new Error(`portal: no element matches "${target}"`);
	targetEl.appendChild(node);

	return {
		destroy() {
			node.parentNode?.removeChild(node);
		},
	};
}
