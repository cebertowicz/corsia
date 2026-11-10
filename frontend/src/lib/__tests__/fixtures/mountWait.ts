// A wait that covers a document editor or chat page mounting in jsdom: the mount is synchronous jsdom CSS work (CodeMirror's
// base-theme stylesheet parse and the mount-time computed-style read), 200-320 ms alone, 680-740 ms on a loaded host, past 1 s under the full parallel suite.
export const MOUNT_WAIT_MS = 5_000;
