# Host-access boundary

Identify the zone before acting:

| Zone | Rule |
|---|---|
| Workspace, registered sources, verified linked worktrees | Ordinary protected access; use supported file and shell tools. A protected base checkout still requires a feature worktree. |
| Host resources outside ordinary scope, excluding the safety floor | Use the supported reviewed host-access route when the authorized task requires them. |
| CORSIA/Safehouse private state, protected credentials/SSH material, raw devices, control files, active sandbox profile/binary, launch-agent plists, root/home writes | Hard safety floor; a lease never opens it. Report a product-boundary gap rather than bypassing it. |

`<workspace>/.corsia/context/` and `.corsia/skills/` are generated System projections. Propose upstream changes to their packaged sources; never edit these projections or seek a lease to override them. This does not reserve every path under `.corsia`.

Always pass an explicit shell working directory. Before escalating an ordinary-path refusal, check the current workspace/repository binding; migration can leave a stale session path. An unrelated tool failure is not a scope gap. If an active lease is ignored, report infrastructure failure rather than repeatedly requesting leases. Respect the refusal's boundary and continue independent authorized work.
