import { updateScreenVisible } from '$lib/update/screenStore';

/** Compatibility name for the shared latch: the update screen owns visibility, recovery, and exit hold. */
export const restartOverlayVisible = updateScreenVisible;
