import type { DocComment } from '$lib/stores/docComments';

/** Image comments (DCOL-FR13.6): the thumbnail heads the list; tags and cards share number, colour, hover and selected-scroll state. */
export type ImageCommentSurface = { src: string; alt?: string; diagram?: boolean; colorOf: (comment: DocComment) => string; onMovePin?: (id: string) => void; onOpen?: () => void; showThumbnail?: boolean; imageBounds?: () => DOMRect | null; hoveredId?: string | null; onHover?: (id: string | null) => void; picks?: number; candidateOrder?: string[] | null };
/** A document's image comments (DCOL-FR13.6): grouped under each thumbnail, with cards carrying tag number and colour; `src` is null when the image has no file. */
export type DocImageComments = { srcOf: (comment: DocComment) => string | null; diagramOf?: (comment: DocComment) => { source: string; caption: string } | null; colorOf: (comment: DocComment) => string; onOpen: (comment: DocComment, focus?: { id: string; move?: boolean }) => void };
