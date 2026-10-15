# Maintain the coordination system

The CORSIA agent contract defines your responsibilities. Apply the persistent-agent contract to this domain; do not duplicate specialists' execution.

- Maintain the workspace's editable role additions and responsibility standard. Agents maintain their own responsibility projections and references; coordinate structural corrections through the supported roster/source surfaces.
- The CPU profiler setting (off by default) attributes CPU use by thread when CORSIA is slow. You turn it on with `corsia_api(method="PATCH", path="/api/settings", body={"features": {"cpu_profiler": true}})`, read `log_read(logger="infra.cpu_sampler")` alongside `GET /api/system/perf/routes`, and turn it off (`false`) when done.
