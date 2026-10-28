# Browser CLI runtime

`BrowserSessionOwner` owns managed browser admission, process identity, pending
commands and cleanup. `browser_cli` supplies the native protocol/client boundary;
`browser_identity` supplies one observation policy and kernel/filesystem facts.
Thread close, command cancellation/timeout, lease termination and backend shutdown
all use that same lifecycle owner.

## Supported contract

The producer protocol supports two verified families, `>=0.27.0,<0.28.0`
and `>=0.31.0,<0.32.0`. macOS dev/PATH launches additionally require the exact
0.31.2 release checksum pinned in `browser_executables`; packaged native copies
must authenticate against the running app's signing team. 0.31 differs only in extra `lifecycle`, `restoreStatus`
and `saveStatus` fields beside the close acknowledgement and command data, and a
`<session>.config` sidecar that outlives close. Operators may set
`CORSIA_AGENT_BROWSER_VERSION_RANGE` to a Python packaging version specifier after
verifying that build's producer contract. A policy override is not evidence of
compatibility. The native executable's parsed version must match the daemon
sidecar exactly. Script/npm launchers are rejected; select the native executable
inside the package. Canonical path and executable fingerprint are pinned and
rechecked before each command. A missing/replaced executable rejects the command
without poisoning an existing session's cleanup binding.

Missing, launcher, unsupported-version, replaced-executable and unsupported-mode
refusals are setup failures (`BrowserSetupError`), not cleanup state: they occur
before admission, name the cause and the fix step, and never write a cleanup
notice. The capability check and install hint apply the same executable
admission (`resolve_native`); the version is checked on first use.

The verified engine is local Chrome through native CDP. Provider, external-CDP,
auto-connect and other engine selections in environment, configuration or command
flags are refused before session effects. Native WebDriver/Appium close responses
do not carry the same awaited-cleanup guarantee and are not admitted here.

Agent shells run inside Safehouse, which cannot permit Chromium's own nested
Seatbelt sandbox: `sandbox_apply` fails with "Operation not permitted", the GPU
helper exits, and the browser traps at startup (the SIGTRAP crash reports seen
since macOS 27). The shell envelope therefore enables Safehouse's
`agent-browser` integration (the `~/.agent-browser` daemon sockets plus the
Chromium launch allowances) and passes `AGENT_BROWSER_ARGS=--no-sandbox` into
the jail, so Safehouse is the one sandbox. Chromium started by hand in an agent
shell also needs `--no-sandbox`; Playwright already passes it. The backend-owned
native tools run outside Safehouse and keep Chromium's own sandbox.

Commands use the stock contract:

```text
AGENT_BROWSER_SOCKET_DIR=<private-dir> AGENT_BROWSER_STREAM_PORT=<reserved> <native> --session <opaque> --cdp <verified-websocket-endpoint> --json <command> ...
```

Native snapshots, navigation, evaluation and screenshots are rendered back to
readable tool strings. Failure messages and logged phase/cause labels exclude
private session, thread, lease, environment and raw exception details. Numeric
PIDs are not globally replaced in page content, where they may match ordinary
prices or IDs. All inherited `AGENT_BROWSER_*` settings are removed before invoking any client,
including `--version`; only the owned socket/stream settings are restored. An explicit
private empty `--config` prevents home/project config discovery, including restore
and automatic state export.

The namespace and optional `CORSIA_BROWSER_INSTANCE_KEY` label a short random
mode-0700 socket directory below `/tmp/corsia-ab-<uid>`. It is created on the first browser
setup attempt, after executable resolution; constructing the process owner does
not touch the filesystem. The key is an identity, not a directory. Separate
backend owners use separate directories even with the same key, and ownership
is not rebuilt
from those directories after restart. Session IDs are random and never contain
thread IDs. There is no unsupported `--namespace` or `session info` command.

## Saved sign-ins and stealth

The first session of an owning user launches with a persistent Chrome profile
under the CORSIA state root (`browser-profile/<user>`). An unresolved owner gets
an ephemeral session. The profile is private state on the host-access hard floor and is never the
person's own Chrome profile. Chrome allows one process per profile, so a
concurrent second session starts from a `state save` copy of the holder's
sign-ins, stored `0600` under `browser-profile/copies/` and deleted on close.
Sign-ins made in a concurrent copy do not persist to the shared profile.
Separate CORSIA instances use their own private state and profiles. Saved profiles
are unavailable on Linux pending shell network isolation; requests fall back to
an ephemeral session and the first agent result says so. Pane results remain valid
JSON and do not consume the notice. Linux retains the previous native-owned launch
path, without the new backend-owned Chrome process or reserved CDP attachment.

The command path refuses `cookies`, `state`, `storage`, `auth`, `profiles`,
`network har|requests`, `stream`, `dashboard`, `connect`, `get cdp-url` and the
`--state` / `--profile` flags. Saved-profile
sessions also refuse agent-authored scripts, script-taking flags and extensions;
ephemeral sessions retain scripted extraction. Native tools select the saved
sign-in automatically; agents needing extraction without it can launch their
own ephemeral agent-browser session in the protected shell. Headless launches drop the
`AutomationControlled` blink feature, use a
user agent derived from the installed Chrome without the `Headless` token, and
run `browser_stealth_init.js` on every document. Launch flags apply only to a
session's first command.

Every managed navigation URL uses the pane's shared normalizer before native setup
or daemon admission: HTTP, HTTPS and `about:blank` only. The pinned 0.31.2 navigation
forms are `open`/`goto`/`navigate`, `read`, `tab new`, `diff url` (both operands),
`record start|restart`, `vitals`/`web-vitals` and `pushstate`, including commands
nested in `batch`. Tool-scheduled navigation passes the same gate. Local files,
including workspace documents and reviewed host files, are refused. Chrome also
blocks tested renderer links and location assignments from HTTP and `about:blank`
to file URLs, and rejects HTTP-to-file redirects.

Every macOS managed session uses a backend-owned Chrome process group. The captured
native Chrome defaults live in `browser_chrome`, including the mock keychain
and basic password store. The owner holds two distinct free ports from the
17-entry `browser_ports.RESERVED_PORTS` tuple of Chrome restricted ports at or above
1024, starts Chrome with its CDP port, verifies that the child owns the
loopback listener, and attaches the native daemon. Chrome's built-in unsafe-port
refusal covers page navigations, redirects, fetches and WebSockets, including later
popups; native CDP and stream clients can still connect. The shared launch-argument
parser refuses inherited `--explicitly-allowed-ports`, `--remote-allow-origins`,
`--remote-debugging-port`, `--remote-debugging-address` and `--user-data-dir`
overrides before admission and again when producing Chrome arguments. This keeps
listener and profile selection with the managed owner before Chrome starts, including
single-dash and split-value spellings. Remove those settings from `AGENT_BROWSER_ARGS`
to retry; other inherited settings are preserved. The inventory permits eight concurrent
sessions when free; other services occupying ports reduce that capacity, and
allocation skips busy ports. With fewer than two free ports, the allocator raises
"managed browser listener ports are unavailable; close an unused browser and retry"
and releases any partial reservation. The managed owner retires the failed startup
and reports that startup failed and its processes were closed. Its stream port is supplied
through the environment. A harmless `stream status` bootstraps the daemon;
its status and listener ownership are verified before restoring copied state or
running the first requested action. A fallback or ownership mismatch fails startup
and closes the owned processes. Releasing the reserved socket before daemon bind
still leaves a race: an intervening bind can cause a temporary fallback listener
outside the denied inventory. Later rejection does not prove zero exposure. Protected and leased macOS shell profiles, and local MCP profiles,
carry static TCP connect/bind denials for the inventory and Unix-socket denials
for the managed daemon root. These rules apply before a session exists: agent shells
and local MCP clients cannot connect to other loopback services or bind local TCP
listeners on the inventory,
including NFS (2049), SIP (5060/5061), X11 (6000) or IRC (6665–6669/6697), even if no
browser is using those ports. Other loopback ports stay available. Linux's separate
bubblewrap shell renderer does not consume these Seatbelt rules.

The existing native-browser CI job runs `test_browser_restricted_ports.py` on Linux.
It launches one real Chrome, then owns a throwaway listener on every inventory port
in turn and checks navigation, redirect, fetch, WebSocket and later-popup refusal
with unrestricted controls. Its test-only control sockets use OS-assigned ports so
none of the inventory under test is occupied by its own CDP listener. The macOS
managed-CDP/stream test shares these checks and requires the explicit native-browser
job opt-in, so ordinary integration collection does not launch missing dependencies.

On macOS, no browser executable runs before provenance is established. Dev/PATH
native releases are checked against their pinned SHA-256 and copied under
`browser-profile/runtime/`. A packaged app re-signs its bundled native dependency,
so that binary is copied into the same private state first, then the copy is
verified with `codesign --verify --strict` against an Apple signature and the
Team ID read from the running bundle's main executable. Ad hoc/unsigned app builds
and mismatched copies fail closed; failed copies are deleted. Identical packaged
bytes reuse a content-addressed private copy and are reverified. Partial dev copies
are atomically repaired from bytes matching the release digest. Every probe and
daemon uses the verified private copy. The system Google Chrome app is verified with `codesign --verify --deep` and Google's
designated requirement before its version probe and each launch. The protected
shell cannot write that app; a reviewed host lease could change it between check
and launch. Other signed Google app locations are copied with `ditto --norsrc`
and the private copy is verified.

If no installed app authenticates, the authenticated native installer downloads Chrome
for Testing directly from Google's HTTPS manifest/archive into a fresh private
`HOME`. In agent-browser 0.31.2, `install.rs::get_browsers_dir` uses
`dirs::home_dir()/.agent-browser/browsers`; its pinned dirs-sys 0.4.1 honors `HOME`.
Existing writable cache downloads are never trusted. An unsuccessful private
installation refuses setup with a clear Chrome requirement and removes its temporary
home once the installer has exited. A pending spawn retains its home until settlement;
an unverified exit retains it for investigation. Completed installations record the
pinned agent-browser version beside the copy. A missing, invalid or mismatched marker
discards the completed copy and installs fresh. The installer fetches the live Stable
manifest; it has no pinned Chrome version. There is no startup network freshness
check: the private Chrome copy ages between agent-browser pin bumps. Periodic refresh
remains follow-up work. Corrupt private app copies are replaced through staged,
authenticated copies; setup failures do not publish a partially initialized owner.
Linux's existing
unsandboxed, unauthenticated native launch remains a separate platform gap; saved
profiles are unavailable there.

## Observation and attempt outcomes

The same observation validates exact sidecars/socket artifacts, daemon birth,
executable, version, environment and immutable binding. First binding requires an
attempt timestamp compatible with process/sidecar creation (two-second platform
time tolerance). Reuse accepts unchanged startup files regardless of elapsed
time. PID reuse, changed locators and replacement daemons are never rebound.
Process introspection runs off the event loop; normal commands do not recursively
scan the process table for descendants.

Client outcomes are `exited`, `interrupted` or `unknown`; none means that detached
daemon effects are absent. Spawn exceptions can occur after execution. Pending
spawns and their eventual client reaping retain strong task references. Once a
late client settles, the owner can re-observe the bound daemon and close it.
A transport failure with no settlement evidence remains unresolved.

Only a complete JSON `success:false` rejection with native main's pre-daemon
`errorType` (`unknown_command`, `unknown_subcommand`, `missing_arguments`,
`invalid_value`, `invalid_session_name`), a normally settled client and fresh exact
artifact absence can retire a new rejected admission. Normal nonzero exit alone
cannot: native startup timeout can leave a detached child that publishes its
sidecars later. Such startup remains owned and can be re-observed on close.
Ordinary command errors with a live bound daemon preserve that session and return
the native `--json` `error` (or stderr) through the same redaction as success.

## Graceful close and recovery

Daemon cleanup sends no daemon-capable CLI command and no daemon process signal. It connects
to the bound Unix socket and feeds the connected peer's kernel PID through the
same identity observation before writing anything. Linux `SO_PEERCRED` and macOS
`LOCAL_PEERPID` prevent a replaced-then-restored pathname from redirecting close
to another process. Unavailable peer credentials fail closed.

The native newline-JSON close request carries a unique ID. Closure requires all
four facts:

1. The connected producer replies with the matching ID, `success:true` and
   `data:{closed:true}` after its awaited local-CDP manager close.
2. The bound daemon has exited.
3. Every exact native session artifact is absent.
4. The separately owned Chrome group has exited; its temporary profile is removed
   only after group absence is verified. Saved profiles remain intact.

A close call waits for a bounded period. Its single socket task remains owned
across timeout/cancellation, allowing the producer to finish cleanup. Repeating
`browser_close`, lease cleanup or shutdown consumes that same result. There is
no TERM/KILL escalation against the daemon. The separately owned Chrome is
closed through CDP to flush its profile, then its private process group is
terminated and checked for remaining helpers. A startup failure uses the same
owned-group cleanup.
Shutdown reflects current unresolved sessions; a past failure does not poison
later empty shutdown.

Missing acknowledgement, unexpected daemon death, changed identity or unknown
client settlement retain `death_unverified` when no pending work can supply
proof. A typed `pending` result and `resetting` state identify an unfinished
retained client/close task or an acknowledged close still awaiting daemon exit
and artifact removal. Allow that work to finish before a later bounded close.
Otherwise `operator_required` tells the agent to stop retries and inform the user.
An explicit later close can still consume genuinely new producer evidence.

The owner writes one durable system notice to the owning visible conversation
when operator investigation is required. Hidden children follow their existing
`spawned_by` lineage only within the same user and project; visible peer chats
keep their own notices. Live delivery reuses the stored-message broadcast path.
A retained report task preserves the durable receipt across caller cancellation.
A failed durable write leaves the receipt unset for a later reporting attempt;
a failed broadcast leaves the stored notice available on reload. Certified
closure records a recovery notice only if a blocker was reported. A reporting
failure cannot retain ownership after closure is certified. ProcessOwner keeps
browser lease-termination and shutdown failures current through one outcome
path rather than retaining them in managed-process failure history.

The notice and tool message give the user the recovery path: close any leftover
agent-browser process or the Chrome window it opened, then restart CORSIA, which
clears the backend-lifetime block; one later `browser_close` also clears it when
the producer can still acknowledge. A restart clears ownership but, like deleting
sidecars, killing a parent PID or finding no environment markers, cannot certify
descendant absence. Apart from the saved-profile orphan recovery below, no automatic
foreign-process signal, directory sweep or `close --all` is a recovery path. FileNotFoundError and PermissionError at the asynchronous
spawn boundary can follow actual execution; neither is no-effects proof.

The producer acknowledgement is the stock local-CDP cleanup contract. It is not
an independently enforced kernel containment boundary for every conceivable
helper process. macOS Chrome starts a new process session and can survive a backend
crash. Before a saved-profile launch, CORSIA reclaims a headless orphaned Chrome
group whose command line matches that exact profile. A headed orphan is left for
the person to close; a live or otherwise unverified holder is left alone and
reported with its process ID. Restart does not remove stale temporary profiles.
The allocator skips occupied ports until the inventory is exhausted.

## Verification and rollout

Focused subprocess tests cover client rejection, delayed detached startup,
communication/spawn failure, cancellation, retained late reaping, identity
changes, slow producer acknowledgement and repeated cleanup. Pipe fixtures make
the local transport seam explicit. `test_browser_real_socket.py` exercises actual
AF_UNIX peers and acknowledgement ordering on a permitted runner with no skip on
bind or credential failure.

The explicit native-browser job installs native agent-browser 0.31.2 only into
its hosted runner's temporary directory, uses the runner's Chrome and isolated
workspace/state/profile/socket roots, and exercises this owner through open,
delayed snapshot, failed navigation, evaluation, screenshot, close and a new
session. Generic integration suites do not require the native dependency. The
selected native job must pass; environment failures are not browser success.
Stock native adds `--no-sandbox` when `CI` exists, so the fixture removes that
process-local hint and verifies Chrome's command line retains its sandbox.

Review and exact-head CI remain release gates. Drain the old owner's sessions
before any supported install/restart; ownership is backend-lifetime state and is
not reconstructed from stale sidecars after restart. Moving this code or changing
an instance key is not an operational cleanup operation. Rollback also requires
preserving unresolved ownership evidence and using the supported lifecycle
procedure, never broad process killing.
