import type { View } from '$lib/homePaneConfig';

export interface HeaderProps {
	onToggleSidebar: () => void;
	onTogglePane: () => void;
	/** Reports how far the right controls reach in from the header's inline end, so the canvas tabs can stop before them. */
	onRightControlsWidth?: (width: number) => void;
	/** Opens the Measurement surface; the doorway writes its own scope first (PL-2). */
	onMeasurement?: () => void;
	activeView: View;
	conductorThreadId: string | null;
	/** False where the view has no right pane; the panel toggle would open nothing. */
	hasPane?: boolean;
	/** Width reserved by an expanded artifact canvas and its divider. */
	artifactHeaderInset?: number | null;
}
