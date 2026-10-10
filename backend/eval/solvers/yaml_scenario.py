
from __future__ import annotations

import os
from typing import Any

import httpx

from eval.default_endpoint import DEFAULT_EVAL_ENDPOINT
from eval.credentials import AUTH_QUERY_KEY, resolve_eval_token

from ._scenario_helpers import (
    build_target,
    build_task_state,
    normalize_score,
    parse_scorer_decl,
)
from .yaml_scenario_types import ScenarioResult, ScenarioStatus, ScoreResult
from .yaml_scenario_mode import (
    extract_span_attrs,
    extract_turn_inputs,
    is_mode_b,
    resolve_metadata_model,
)

_extract_span_attrs = extract_span_attrs
_extract_turn_inputs = extract_turn_inputs
_is_mode_b = is_mode_b

_DEFAULT_ENDPOINT = os.environ.get(
    "CORSIA_EVAL_ENDPOINT", DEFAULT_EVAL_ENDPOINT,
)
_DEFAULT_TIMEOUT = float(os.environ.get("CORSIA_EVAL_TIMEOUT", "180"))



class YamlScenarioSolver:

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint or _DEFAULT_ENDPOINT
        self._token_override = token
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._transport = transport

    async def run(self, sample: Any) -> ScenarioResult:
        if is_mode_b(sample):
            return await self._run_mode_b(sample)
        return await self._run_mode_a(sample)

    async def _run_mode_a(self, sample: Any) -> ScenarioResult:
        scenario_id = str(getattr(sample, "id", None) or "unknown")
        metadata = dict(getattr(sample, "metadata", None) or {})
        prompt = self._extract_prompt(sample)

        response, transport_error = await self._call_endpoint(prompt, metadata)
        if transport_error is not None:
            return ScenarioResult(
                scenario_id=scenario_id,
                status=ScenarioStatus.ERROR,
                endpoint_response=response or {},
                errors=[transport_error],
                mode="A",
            )

        if isinstance(response, dict) and response.get("error"):
            return ScenarioResult(
                scenario_id=scenario_id,
                status=ScenarioStatus.ERROR,
                endpoint_response=response,
                errors=[str(response["error"])],
                mode="A",
            )

        blocked, unknown, callables = self._resolve_scorers(sample)

        if not callables and not blocked and not unknown:
            return ScenarioResult(
                scenario_id=scenario_id,
                status=ScenarioStatus.PASS,
                endpoint_response=response,
                mode="A",
            )

        outcomes = await self._run_scorers(callables, sample, response)

        if blocked or unknown:
            status = ScenarioStatus.BLOCKED
        elif any(not o.passed for o in outcomes.values()):
            status = ScenarioStatus.FAIL
        else:
            status = ScenarioStatus.PASS

        return ScenarioResult(
            scenario_id=scenario_id,
            status=status,
            scorer_outcomes=outcomes,
            endpoint_response=response,
            blocked_scorers=blocked,
            unknown_scorers=unknown,
            mode="A",
        )

    async def _run_mode_b(self, sample: Any) -> ScenarioResult:
        scenario_id = str(getattr(sample, "id", None) or "unknown")
        metadata = dict(getattr(sample, "metadata", None) or {})
        turn_responses, turn_attrs, errors = await self._run_turn_sequence(
            sample, metadata, scenario_id,
        )

        if errors:
            return ScenarioResult(
                scenario_id=scenario_id,
                status=ScenarioStatus.ERROR,
                endpoint_response=turn_responses[-1] if turn_responses else {},
                errors=errors,
                turn_attrs=turn_attrs,
                turn_responses=turn_responses,
                mode="B",
            )

        final_response = turn_responses[-1] if turn_responses else {}

        blocked, unknown, callables = self._resolve_scorers(sample)
        if not callables and not blocked and not unknown:
            return ScenarioResult(
                scenario_id=scenario_id,
                status=ScenarioStatus.PASS,
                endpoint_response=final_response,
                turn_attrs=turn_attrs,
                turn_responses=turn_responses,
                mode="B",
            )

        outcomes = await self._run_scorers(
            callables, sample, final_response, turn_attrs=turn_attrs,
        )

        if blocked or unknown:
            status = ScenarioStatus.BLOCKED
        elif any(not o.passed for o in outcomes.values()):
            status = ScenarioStatus.FAIL
        else:
            status = ScenarioStatus.PASS

        return ScenarioResult(
            scenario_id=scenario_id,
            status=status,
            scorer_outcomes=outcomes,
            endpoint_response=final_response,
            blocked_scorers=blocked,
            unknown_scorers=unknown,
            turn_attrs=turn_attrs,
            turn_responses=turn_responses,
            mode="B",
        )

    async def _run_turn_sequence(
        self,
        sample: Any,
        metadata: dict[str, Any],
        scenario_id: str,
    ) -> tuple[list[dict], list[dict], list[str]]:
        turn_inputs = extract_turn_inputs(sample)
        if not turn_inputs:
            single = self._extract_prompt(sample)
            turn_inputs = [single] if single else [""]

        model_for_metadata = resolve_metadata_model(metadata, sample)
        turn_attrs: list[dict[str, Any]] = []
        turn_responses: list[dict[str, Any]] = []
        errors: list[str] = []

        for idx, prompt in enumerate(turn_inputs):
            per_turn_metadata = dict(metadata)
            per_turn_metadata.setdefault("turn_index", idx)
            per_turn_metadata.setdefault("scenario", scenario_id)
            if model_for_metadata and "model" not in per_turn_metadata:
                per_turn_metadata["model"] = model_for_metadata

            response, transport_error = await self._call_endpoint(
                prompt, per_turn_metadata,
            )
            if transport_error is not None:
                errors.append(f"turn {idx}: {transport_error}")
                turn_responses.append(response or {})
                turn_attrs.append({"turn_index": idx, "error": transport_error})
                break
            if isinstance(response, dict) and response.get("error"):
                errors.append(f"turn {idx}: {response['error']}")
                turn_responses.append(response)
                turn_attrs.append({
                    "turn_index": idx,
                    "error": str(response["error"]),
                    **extract_span_attrs(response),
                })
                break
            turn_responses.append(response)
            attrs = {"turn_index": idx, **extract_span_attrs(response)}
            turn_attrs.append(attrs)

        return turn_responses, turn_attrs, errors

    @staticmethod
    def _extract_prompt(sample: Any) -> str:
        inp = getattr(sample, "input", None)
        if isinstance(inp, str):
            return inp
        if inp is None:
            return ""
        return str(inp)

    async def _call_endpoint(
        self,
        prompt: str,
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        body = {"input": prompt, "metadata": metadata}
        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        # Outside the try: a missing credential is a setup error that must
        # stop the run, not a per-sample endpoint failure.
        auth = {AUTH_QUERY_KEY: resolve_eval_token(self._token_override)}

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(
                    self._endpoint,
                    params=auth,
                    json=body,
                )
                resp.raise_for_status()
                return resp.json(), None
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.text[:500]
            except (UnicodeDecodeError, httpx.ResponseNotRead) as decode_err:
                detail = f"<could not decode response body: {decode_err!s}>"
            return None, f"HTTP {e.response.status_code}: {detail}"
        except httpx.HTTPError as e:
            return None, f"transport error: {e!s}"
        except Exception as e:
            return None, f"unexpected error: {e!s}"

    @staticmethod
    def _resolve_scorers(
        sample: Any,
    ) -> tuple[list[str], list[str], list[tuple[str, Any]]]:
        from eval.scorers.resolve import MODE_DEFAULTS, SCORER_REGISTRY

        meta: dict[str, Any] = dict(getattr(sample, "metadata", None) or {})
        decls = meta.get("scorers")
        if decls is None:
            mode = meta.get("mode")
            decls = MODE_DEFAULTS.get(mode, []) if mode else []

        blocked: list[str] = []
        unknown: list[str] = []
        callables: list[tuple[str, Any]] = []

        for raw in decls:
            parsed = parse_scorer_decl(raw)
            if parsed is None:
                continue
            name, args = parsed
            if name not in SCORER_REGISTRY:
                unknown.append(name)
                continue
            factory = SCORER_REGISTRY[name]
            if factory is None:
                blocked.append(name)
                continue
            try:
                scorer_cb = factory(**args) if args else factory()
            except TypeError:
                try:
                    scorer_cb = factory()
                except TypeError:
                    blocked.append(name)
                    continue
            callables.append((name, scorer_cb))

        return blocked, unknown, callables

    @staticmethod
    async def _run_scorers(
        callables: list[tuple[str, Any]],
        sample: Any,
        response: dict[str, Any],
        *,
        turn_attrs: list[dict[str, Any]] | None = None,
    ) -> dict[str, ScoreResult]:
        state = build_task_state(sample, response)
        target = build_target(sample)
        if turn_attrs is not None:
            state.store.set("turn_attrs", list(turn_attrs))

        outcomes: dict[str, ScoreResult] = {}
        for name, scorer_cb in callables:
            try:
                score = await scorer_cb(state, target)
            except Exception as e:
                outcomes[name] = ScoreResult(
                    name=name,
                    passed=False,
                    explanation=f"scorer {name} raised: {e!s}",
                    raw_value=None,
                )
                continue
            outcomes[name] = normalize_score(name, score)
        return outcomes

__all__ = [
    "ScenarioResult",
    "ScenarioStatus",
    "ScoreResult",
    "YamlScenarioSolver",
]
