# First run, app path, step 7: How CORSIA gets better

Read this file when you start this step, even if you read it earlier. The rules every step follows are in `references/first-run-app.md`; read that too if it is no longer in front of you.

**Learning from the community.** Always. Post, word for word:

> CORSIA learns from your corrections. It tests changes to the workflows and skills behind them and keeps what works. You can use improvements other people have proven, and share yours if you choose.

Then raise the one decision from `setup.setup_decisions` titled `Use and share improvements with the CORSIA community?` as `references/raising-a-decision.md` says, with the `source_node_id` of the task it names if your plan has one, else your current plan task. Using the community's improvements needs no account.
- **The answer** is written to the profile for you when the decision is answered: **Use and share** writes Yes and Yes; **Only use** writes Yes and No; **Neither** writes No and No. On "not now", post `Ask me any time.`, write no line, and agents share nothing and take nothing until it is answered.
- **An answer given in chat instead of the card:** write it yourself, as two lines in the workspace's `context/50-USER-PROFILE.md`, replacing any line with the same label: `- **Use community improvements:** Yes` or `- **Use community improvements:** No`, and `- **Share improvements with the community:** Yes` or `- **Share improvements with the community:** No`. If they say they would only share, write No and Yes and treat it as sharing.
- **Neither:** post `Got it. Your agents keep their improvements here and don't take the community's. Ask me any time to change that.` and skip the rest of this step: no GitHub.
- **Only use:** no GitHub question. Skip the rest of this step.
- **Use and share, or only share, and not `github_ready`:** post one message, `Sharing goes through GitHub. A separate account keeps your agents' posts apart from yours. You'd accept GitHub's terms for it and answer for what it does.` Then raise the decision titled `How should your agents post on GitHub?` the same way. If they already said they have no GitHub account, raise the one titled `Make an account for your agents on GitHub?` instead. Never ask in chat whether they have an account. When `github_ready`, skip the rest of this step: the account is already set up. On "not now" post `Ask me any time.`

Both GitHub decisions use the `source_node_id` of the task they name if your plan has one, else your current plan task. Handle the answer by following skill `github` › `references/agent-account.md`, then mark this step done or skipped as appropriate. On `Use my account`, when GitHub's CLI is already signed in on this computer, agents use that sign-in: post `Your agents will post as [login].` Otherwise follow that file's "Posting as the person" path.
