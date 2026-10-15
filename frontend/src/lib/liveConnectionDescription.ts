export const liveConnectionDescriptionId = (artifactId: string | null | undefined): string | undefined =>
	artifactId ? `live-connection-${artifactId}` : undefined;
