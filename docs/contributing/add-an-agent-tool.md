# Add an agent tool

Use this guide to add a built-in tool that CORSIA's agents can call, from its definition to registration and tests. For a tool supplied by an external MCP server instead, see `BACKEND.md`.

1. **Define the tool.** Put the implementation in `backend/tools/`, usually in a module for its domain. Subclass `Tool` from `backend/tools/base.py` and provide a name, a description, a JSON Schema for parameters, and an async `execute` method returning a string. For example, this is the shape of the existing read tool in `backend/tools/workspace_git.py` (its execution delegates to a helper):

   ```python
   class WorkspaceHistoryTool(Tool):
       @property
       def name(self) -> str:
           return "workspace_history"

       @property
       def description(self) -> str:
           return "Read workspace path history; refuses fenced paths and their containing folders."

       @property
       def parameters(self) -> dict[str, Any]:
           return {"type": "object", "properties": {"path": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}, "required": ["path"]}

       async def execute(self, **kwargs: Any) -> str:
           # Check the current thread and arguments, then call the history helper.
           ...
   ```

2. **Describe and validate the call.** The model receives the tool's `description` and `parameters` through `Tool.to_schema`. Explain when to use it, its inputs and meaningful limits there; the catalog summary is a shorter discovery description. The registry decodes and validates model arguments against the supported schema rules before execution. Add checks in the implementation for domain constraints, identity and direct calls that need them; return useful `Error:` messages for expected failures. The history tool checks the thread, path and count and reports expected helper errors. See `backend/tools/base.py`, `backend/tools/registry.py` and `backend/tools/workspace_git.py`.

3. **Register and catalog it.** Import and register the instance in the appropriate group in `backend/core/agent.py`, or call a domain registration function there as `backend/tools/register_messages.py` does. Add a `ToolCatalogEntry` for each name in `backend/tools/catalog_entries.py` or an imported domain entries module. The workspace Git entries live alongside their tools and are imported into the catalog. Supply a category, summary and activity, and choose the other metadata for the tool. See `backend/tools/catalog_entry.py` and `backend/tools/catalog.py` for the fields and pack mappings. A reachable core tool is advertised with its full schema; a reachable non-core tool is discovered in its category's pack via `tools_load` and called through `tool_call`. A new category also needs a pack mapping. Registration alone does not make an uncatalogued built-in reachable through that surface.

4. **Enforce the right access.** Catalog `authority` describes the classification; it does not enforce permission. Check the current thread and enforce the relevant scope in the tool or its backing service, using the existing domain's checks. The registry supplies execution context and applies shared gates; do not accept model-supplied identity as authority. For example, workspace history checks its thread's project fence, while Messages checks whether the connected account reaches that thread. Cover denied access as well as success. Depending on the tool, connected-account, host access, external-action or other domain gates may also apply; inspect the existing tool for that domain.

5. **Add tests and guidance.** Add focused tests for the behavior, invalid inputs, errors and access boundaries, as in `backend/tests/test_workspace_git_tools.py`. The catalog coverage test in `backend/tests/test_agent_tool_surface.py` checks for registered tools missing catalog entries and stale entries. Put call-specific guidance in the tool description. If a wider workflow needs agent instructions, packaged System skills live under `backend/prompts/system/v1/skills/`; contributor changes to `backend/prompts/system/` require maintainer approval. A System skill is not required for every tool.

6. **Run the checks and prepare the pull request.** Follow [Testing and review](testing.md) for disposable state, the local gate and review. Run your tool's focused tests and the catalog coverage test below. Follow [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for issue, claim, CLA and pull request requirements.

## Check it yourself

With the disposable environment and Python from [Testing and review](testing.md), from the repository root run:

```bash
PYTHONPATH=backend .venv/bin/python -m pytest -q backend/tests/test_agent_tool_surface.py::test_external_harness_surface_is_direct_and_cataloged backend/tests/test_workspace_git_tools.py
```

The second file tests the existing example; replace it with your tool's tests as well. These checks do not prove every tool-specific permission or integration path. This guide was checked against the existing examples and tests; adding a new tool end to end using it has not been verified.

Checked against the code at `7e567f087`.
