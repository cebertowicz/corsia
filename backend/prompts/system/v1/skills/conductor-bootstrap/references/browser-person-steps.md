# Steps in the browser that belong to the person

Use this whenever you open a page in the shared browser (`browser_open` without `show_window`) and a step on it is theirs: a captcha, a sign-in, a password, a verification code. The person watches and acts there. You fill only what the person has already told you (their saved email); everything else is theirs. Never solve, bypass or work around a captcha.

`browser_wait` takes a selector (it waits for it to appear) or milliseconds; it has no URL mode. So you watch with a loop, never with a single wait. "Read the URL and title" always means exactly one call, `browser_eval` with the script `[location.href, document.title]`, character for character; no other script, and no other tool, reads a page that is the person's.

## A. Captcha or "verify you are human", no password field

1. `post_update` once, before you wait: what the page is and that they solve it in the shared browser. Example: `GitHub is asking you to prove you're human. Solve it in the shared browser and I'll continue when it clears.`
2. Loop: `browser_wait` a few thousand milliseconds, then read the page URL and title with that one `browser_eval` (never form fields, never a snapshot of inputs). A wait that times out is not a failure; go round again.
3. Stop when the URL or title shows the page moved on, then carry on with the step that follows.
4. After about 5 minutes without a change, stop looping. `post_update` one line: `Tell me when you're past it and I'll pick up from there.` End your turn.

## B. Sign-in, password, 2-Step, passkey, card-entry, phone-verification or verification-code page

The shared browser is the person's. Call `browser_person_step` with the short action they must complete, then end your turn. CORSIA adds it to their attention list and wakes you when the page changes. Never type, fill or read a password or a code yourself.

A link you post in chat opens in the person's own default browser; to show them a page in the shared browser, open it with `browser_open`, never by posting its link. CORSIA's own connect sign-ins (`google_connect`, `microsoft_connect`) return to localhost with an authorization code: post their link in chat, never open it in the agent browser, and don't watch it. Admin-portal sign-ins (Entra, Google Cloud console) are opened in the agent browser as written.

A page that has both a captcha and a password field is a B page.

## C. When they reply instead

If the person replies before the page moves on, or after you stopped looping, read the URL and title once, then take over from the page they reached. If it is still a sign-in or captcha page, say what you see in one line and ask what they'd like to do; never retry for them.

## D. A step the person finishes with an account

When the next page is the one you were waiting for (an API-key page, a consent screen that has been accepted), say what that page means and what they do next, in one `post_update`. Never read a key, a token or a code from the page.
