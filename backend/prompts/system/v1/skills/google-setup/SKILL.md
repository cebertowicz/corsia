---
name: google-setup
description: Use when the person wants their email or calendar connected (first-run step 6, "connect my email"), or when `google_status` says not_connected or `google_connect` says no Google client is saved. You build the person's own private Google project in a browser they watch; they only sign in and click Allow.
corsia:
  handler:
    kind: instruction
---

# Connect Google

Only the designated CORSIA thread runs this. The person does two things: signs in to Google in the browser you open, and clicks Allow on the sign-in link. You do every other step. Never ask for, type, read, print or paste a password, code, client secret or token.

1. Load pack `google` with `tools_load` and call `google_status`. If it says connected, post `Connected as <address>.` and stop. If it gives needs_action, follow that instead of this skill.
2. Post with `post_update`: `I can look after your email and calendar. To connect them, I'll set up your own private Google project in a browser you can watch, and you sign in and click Allow. Two things to expect: Google will warn that the app isn't verified. That's normal for a private project like yours; choose "Advanced", then "Go to CORSIA (unsafe)". It's your own project, so you're the only one it can reach. And the project gets published as "In production", because a project left in Testing stops letting me in after 7 days. Go ahead?` Wait for their yes. On "not now": `Ask me any time to connect your email.`
3. Call `google_connect` with no `client_file`. If it returns a link, a client is already saved: go to step 6. If it says no Google client is saved, go on.
4. Run `agent-browser --version`. If it is missing, call `request_host_access` for exactly `npm install -g agent-browser@0.31 && agent-browser install`, stating that the person asked to connect Google and that nothing else is installed or changed. Once approved, run it with host scope and tell the person the browser download takes a few minutes. If access is refused, give them that one Terminal command and wait until they say it finished.
5. Open `https://console.cloud.google.com/projectcreate` with `browser_open` and `show_window: true`. A sign-in, password, 2-Step or passkey page belongs to the person: follow `.corsia/skills/conductor-bootstrap/references/browser-person-steps.md` B. A terms page or a captcha: follow A of that file. Once they are past it, take over and do these steps in that window:
   a. Create a project named `CORSIA`. If this setup already made one, select it. Never make a second project or client.
   b. Enable the Gmail API and the Google Calendar API.
   c. Google Auth Platform: user type External, app name `CORSIA`, and the signed-in address as both the support and the developer email.
   d. Audience page: press Publish app and confirm. Read the page again; go on only when it says In production. If you can't press it, ask the person to press Publish there and tell you when it says In production.
   e. Clients: Create client, application type Desktop app, name `CORSIA`, Create. The dialog that opens shows the client secret, so take no snapshot or screenshot of it: press its Download JSON with one `browser_eval` of `[...document.querySelectorAll('button')].find(b => b.textContent.includes('Download JSON')).click()`, then close it. Read the client ID from the Clients list. The file is `~/Downloads/client_secret_<client ID>.json`; never open it.
   If one of these steps fails in the browser, ask the person to do only that step, at the page you have open. Never hand them the whole list.
6. Call `google_connect` with `client_file` set to that path (skip this call if step 3 already gave a link). If it says the file isn't a client file, ask the person where their browser saved `client_secret_<client ID>.json` and pass that path. Post the link: `Open this link, pick the Google account you just signed in with, and click Allow. When Google says the app isn't verified, choose Advanced, then Go to CORSIA (unsafe).`
7. When they say it's done, call `google_status` and post `Connected as <address>. If that's the wrong account, say so and I'll switch it.` If it still says not connected, post what it says.
