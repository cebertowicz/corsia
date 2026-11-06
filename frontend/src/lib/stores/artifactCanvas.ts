import { writable } from 'svelte/store';

/**
 * Transient presentation state for the artifact canvas.
 *
 * Deliberately *not* part of `ThreadArtifactWorkspace`: none of it is workspace
 * data that should persist or restore. `canvasCompact` is a measurement result
 * — written only by the component that observes the real canvas width, never
 * derived from the viewport or a media query, because the right drawer can
 * narrow the canvas below the split minima on a wide window.
 */
export const canvasCompact = writable(false);

/** Whether the compact tab sheet is open. One sheet exists at a time. */
export const artifactTabSheetOpen = writable(false);

/**
 * Whether the shell shows the Workspace panel over the canvas instead of taking layout beside it.
 * `PaneShell` owns the decision (`shouldOverlayPane` is its rule); anything asking what the canvas would
 * look like with the panel out of the way reads this instead of looking for the panel's class in the DOM.
 */
export const paneOverlay = writable(false);
