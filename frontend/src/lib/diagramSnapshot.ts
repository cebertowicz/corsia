/** Creates a temporary URL for a rendered diagram snapshot. */
export function createDiagramSnapshot(markup: string): string {
	return URL.createObjectURL(new Blob([markup], { type: 'image/svg+xml' }));
}

/** Releases a temporary diagram snapshot URL. */
export function releaseDiagramSnapshot(src: string | null | undefined): void {
	if (src?.startsWith('blob:')) URL.revokeObjectURL(src);
}
