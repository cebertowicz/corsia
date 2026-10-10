export function haptic(durationMs = 8): void {
	try {
		(navigator as { vibrate?: (ms: number) => boolean }).vibrate?.(durationMs);
	} catch {
	}
}

export const haptics = {
	tap: () => haptic(8),
	threshold: () => haptic(12),
	commit: () => haptic(16),
} as const;
