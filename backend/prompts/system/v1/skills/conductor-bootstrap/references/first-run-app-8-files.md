# First run, app path, step 8: Your files, and when agents ask first

Read this file when you start this step, even if you read it earlier. The rules every step follows are in `references/first-run-app.md`; read that too if it is no longer in front of you.

**Your files, and when agents ask first.** Told once, to everyone, with no question. When they named something in step 5 about checking with them first, open with `You asked me to check with you before [what they named]. I'll do that. Outside your work folder, this is how it works:`; if they named nothing, open with no line. Then post, word for word:

> **Agents ask you before they:**
> - send or post as you
> - spend money
>
> **Auto-approve is on:** a reviewer model checks each request to use files outside your work folder, and most start without asking you. To approve each one yourself, turn it off in [Settings › System](/settings/system#host-access-review).
>
> Your keys, passwords and sign-ins, and CORSIA's own data, stay closed to agents. No safeguard is perfect: if this computer holds very sensitive information, turn Auto-approve off, or give CORSIA its own macOS user account.
>
> A chat on a cloud model sends what you say to that provider. Apps you connect, like your email, follow their own privacy terms; CORSIA can't control what they do with what they receive.

Auto-approve follows `auto_approve`. When it is false, replace the Auto-approve line with `**Auto-approve is off:** you approve each request to use files outside your work folder. To let the reviewer approve most of them, turn it on in [Settings › System](/settings/system#host-access-review).` and write `leave` for `turn` in the next paragraph's "turn Auto-approve off". When it is `unavailable`, leave out those lines, and in the next paragraph drop "turn Auto-approve off, or " so it still says to give CORSIA its own macOS user account.

Only when `local_eligible` is true, add this to the same post:

> **Keeping work off cloud models:** agents your chat starts can run on cloud models, unless Local only is on. To turn it on, press the model button in the message box and use the toggle at the top of the picker. It needs a local model that's ready, with the chat switched to it. For work that must never reach a cloud model, set a project to "Local models only" in its project settings; I can set one up.

Claim no safeguard beyond those stated here, and offer no list of folders to mark off limits. Then go straight to the hand-off.
