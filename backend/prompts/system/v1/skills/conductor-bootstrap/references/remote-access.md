# Use CORSIA away from this Mac

Follow this when the user said yes to phone or other-computer access in first run (task `Remote access`), and any time the user asks to open CORSIA on their phone or another computer, on any install. Remote access means a private network until CORSIA has a relay. Settings and decision text name no outside service; you name Tailscale and the app stores in chat, from here.

Never change a Tailscale mapping that is not CORSIA's, never open CORSIA to the internet (no `funnel`, no `--tcp`, no `--set-path` mapping, and nothing added to a port with Funnel on), never run `tailscale serve reset`, and never sign anyone in. Every command on the Mac is a reviewed host request the user approves; if one is refused, say what you could not do and leave the step recorded.

## Explain first, then ask

Before you ask anything or request host access, post once, in plain words: `Tailscale is a free app that links your own devices into a private network, so your phone can reach CORSIA on this Mac without opening anything to the internet. The alternative is opening a port on your home network, which I don't recommend: it exposes this Mac to anyone who finds it. With Tailscale only devices signed in to your account can connect. Want me to set it up?` Then end your turn. Only a yes starts row 1; not now closes the task as written below.

## Facts that shape every step

- Anyone on the private network can reach CORSIA's sign-in page, so the user signs in to Tailscale with their own account, not a work or shared one. CORSIA's own sign-in stays the gate.
- CORSIA listens on every network interface unless the user chose "this computer only" at install, so it may also answer on the local network. When the recorded install choice is "reachable from other devices", say so in the done message. Read it from `corsia_api(method="GET", path="/api/system")` once that response carries the recorded choice; when it does not carry one yet, leave the local-network line out.
- A Tailscale mapping is CORSIA's only when its `/` path targets `http://127.0.0.1:[port]`, with `[port]` CORSIA's own listening port, and Funnel is off for its https port. Always find CORSIA's mapping by that target, never by its https port. A mapping on any other path (a webhook path such as `/webhooks/github`) is not CORSIA's, even when it targets CORSIA's port. Confirm on first use which `corsia_api` field reports CORSIA's own port; if none does, ask the user for the port in the address bar.
- An https port with Funnel on is public: everything mapped on it is on the internet. Funnel is on for a port when the serve configuration's `AllowFunnel` sets `[name].ts.net:[https port]` to true (unconfirmed); no `AllowFunnel`, or no entry for that port, means off. Never reuse that port, add a mapping to it or change it, except row 0a's removal of CORSIA's `/`; skip it as you skip a port that serves something else. When you cannot tell whether Funnel is on for a port, treat it as on: you cannot tell when the serve configuration could not be read or its shape differs from this.
- `[address]` is `https://[name].ts.net`, with `:[https port]` added when the port is not 443.

## Commands

Confirm on first use each command marked (unconfirmed). If its output or message differs from what is written here, stop, report the actual output to the user in one line, and continue from what the command really did. Never present an unconfirmed value as fact.

| Purpose | Command |
|---|---|
| Read status, and where the command-line tool is | `tailscale status --json`; the app's own command-line tool lives inside the app bundle (path unconfirmed: check `/Applications/Tailscale.app/Contents/MacOS/Tailscale`) |
| Read serve configuration | `tailscale serve status --json` (shape unconfirmed: expect mappings keyed by https port, each with a target) |
| Install only the Mac app, only if it is missing | `brew install --cask tailscale-app` (unconfirmed; it must install only the app) |
| Turn on CORSIA's address | `tailscale serve --bg --https=[https port] http://127.0.0.1:[port]` with the https port and CORSIA's own port |
| Check the address | `curl -sS --max-time 10 [address]/api/health` from the Mac |
| Undo CORSIA's mapping only | `tailscale serve --https=[https port] off` (unconfirmed: use the exact equivalent that removes one mapping, never `reset`) |
| Take CORSIA's `/` off a port with Funnel on | `tailscale serve --https=[Funnel port] --set-path=/ off` (unconfirmed: it must remove only `/` and leave the port's other paths, such as a webhook path) |
| When https is not yet turned on for their network | `tailscale serve` prints a link and a message (wording unconfirmed); relay the exact link it prints, never guess it |

## States

A step that waits on the person's hands is their task, per skill `conductor-bootstrap` section 8: add it before the message that waits. Titles below.

| # | State | Say | Do |
|---|---|---|---|
| 0 | The address already answers through CORSIA's mapping (found by row 1's host request) | `CORSIA is already on your private network at [address].` | Note the https port of CORSIA's mapping. Mark the task done |
| 0a | The address answers through a port with Funnel on (found by row 1's host request, or by the check after this reference changes) | When that port's `/` targets CORSIA's port: `CORSIA is reachable from the internet at [address], through Tailscale Funnel on port [Funnel port]. An earlier version of this setup could have put it there. I'll take CORSIA off that port and leave everything else on it alone; you'll see one request to approve.` Otherwise: `CORSIA is reachable from the internet at [address], through Tailscale Funnel on port [Funnel port], by a mapping that isn't CORSIA's, so I've left it alone. If you didn't mean that, turn Funnel off for that port in Tailscale.` | Never call it private. Only when `/` targets CORSIA's port, in one host request: run the command that takes CORSIA's `/` off that port, then read the serve configuration again and confirm `/` is gone and the port's other paths are unchanged. If that request is refused, say `CORSIA is still on the internet at [address]. Ask me to take it off any time.` and stop. Then go to row 5 without checking the address again; row 5 skips that port |
| 1 | Start | `First I'll check this Mac, and install Tailscale there if it's missing; you'll see one request to approve.` | Request host access once, for two ordered actions: read Tailscale's status, its serve configuration and whether CORSIA's address answers; then install only the Tailscale app if it is missing. Already answering goes to row 0, or to row 0a when it answers through a port with Funnel on. Otherwise the next message starts with the phone line: `Install Tailscale on your phone from the App Store or Google Play, and sign in with your own account, not a work or shared one: anyone on that network can reach CORSIA's sign-in page.` Already installed goes to row 3, 4 or 5 |
| 1a | At row 5, 443 already serves something that is not CORSIA's, or has Funnel on | Row 6's message adds, for each port skipped: `Your private address already serves something else on port [skipped port], so CORSIA uses port [https port] and leaves yours alone.` or, when Funnel is on: `Port [skipped port] on your Tailscale address is open to the internet (Funnel), so CORSIA uses port [https port] and stays off it.` | Decided inside row 5's host request, which reads the serve configuration just before it runs `serve`. Take the first https port in the order 443, 8443, 10000 that has Funnel off and is free. CORSIA's mapping on any of the three is reused, never moved; a mapping on a port with Funnel on is never CORSIA's. Note the https port in the task; every later step and message uses it. Never change or remove the person's mapping |
| 1b | At row 5, 443, 8443 and 10000 each serve something that is not CORSIA's or have Funnel on | `Your private address already serves other things on all three ports CORSIA can use, or has them open to the internet, so I've left them alone and phone access stays off.` | Row 5's request runs no serve command. Say it in chat, add no task, give the person no port to free, and close the task with reason `All three private ports in use`. Never replace a mapping on any of the three |
| 2 | The install was refused or failed | `I couldn't install it. Get it from tailscale.com/download on this Mac, then tell me.` | Add their task `Install Tailscale on this Mac`. This is the one step that sends the user to a download page |
| 3 | Installed, not signed in on the Mac | `Open Tailscale from the menu bar and sign in with your own account, the same one as on your phone. Tell me when you're in.` | Add their task `Sign in to Tailscale on this Mac`. Check the sign-in state through the same reviewed host access, then close it (`Signed in`) |
| 4 | Signed in; private https not yet turned on for their network | `Tailscale needs one switch turned on: [link it printed]. Turn it on, then tell me.` | Add their task `Turn on https in Tailscale`. Relay the exact link the command prints. Never guess it. Close it once the address answers |
| 5 | Ready | (One host request to approve.) | In one request: read the serve configuration and pick the https port: the first of 443, 8443, 10000 with Funnel off that is free or already holds CORSIA's mapping (reused), none under row 1b. Never pick a port with Funnel on, even one whose mapping targets CORSIA's port. Then run exactly `tailscale serve --bg --https=[https port] http://127.0.0.1:[port]` with that port and CORSIA's own port. Then fetch `[address]/api/health` from the Mac |
| 6 | The address answers | `Open [address] on your phone and sign in. Then Share › Add to Home Screen (iPhone) or Install app (Android). It's on your private network [network name].` When the recorded choice is "reachable from other devices", one more line: `CORSIA also answers on your local network, as you chose at install.` | Mark the task done |
| 7 | The address does not answer | `The address isn't answering yet. I've undone the change.` plus the one reason you saw, then `Ask me to try again when you're ready.` | In one request, read `tailscale serve status --json` and remove the mapping only if the recorded https port still holds CORSIA's mapping (`/` targets `http://127.0.0.1:[port]`, Funnel off); otherwise remove nothing, so undo never takes a mapping the person put there after setup. Add their task `Say try again for phone access` |
| 8 | The user stops replying | Nothing more. The wrap-up's `Still waiting on you` line names it | Their task stays open |
| 9 | CORSIA restarts, or you checkpoint, mid-step | Nothing, unless the result changed | The mapping should survive a restart of CORSIA, Tailscale or the Mac (unconfirmed). On your next wake, read the recorded step and, in one host request, `tailscale serve status --json`. If the recorded https port still holds CORSIA's mapping, rerun the address check and go to row 6 or 7. If not, continue from row 5 |
| 10 | CORSIA's own port changes later | Nothing: the address does not contain CORSIA's port, so the saved icon keeps working | See "When CORSIA's port changes" |

If host access is refused at the `serve` step, say `I can't set up the address without that approval. Ask me again when you want it.` and leave the task open. If the user says not now, close their task (`You chose not now`) and this task with `You chose not now. Ask me any time.`

## When CORSIA's port changes

A mapping left on the old port would expose whatever later listens there, so repoint it as soon as you know the port moved (the user tells you, or your next wake finds the address failing):

1. In one host request, read `tailscale serve status --json` and find CORSIA's mapping by its target, `/` on `http://127.0.0.1:[old port]` with Funnel off, never by its https port. If none matches, stop: nothing of CORSIA's is exposed. A mapping on another path that targets the old port is not CORSIA's: leave it, and tell the user in one line that it still points at the old port. A `/` on a port with Funnel on that targets the old port is row 0a's case.
2. Run `tailscale serve --bg --https=[that https port] http://127.0.0.1:[new port]`. Whether `serve` replaces a mapping on a port already in use or refuses is unconfirmed: if it refuses, run `tailscale serve --https=[that https port] off` for that one mapping, then run the line above. If the user would rather not keep the address, only remove the mapping.
3. Read `tailscale serve status --json` again and confirm the mapping now points at `http://127.0.0.1:[new port]` (or is gone). Then check `[address]/api/health` from the Mac and tell the user the result in one line.

## After this reference changes

Earlier versions of this setup could add CORSIA's `/` to a port with Funnel on. When a System-change notice lists this file and the `Remote access` task is done, check once: say `I'm checking that phone access to CORSIA isn't open to the internet; you'll see one request to approve.`, then in one host request read `tailscale serve status --json`. A port with Funnel on whose `/` targets CORSIA's port goes to row 0a. Otherwise say nothing more. If the request is refused or the configuration cannot be read, say `I couldn't check whether CORSIA is open to the internet. Ask me to check any time.`
