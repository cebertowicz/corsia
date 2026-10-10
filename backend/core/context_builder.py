"""Context builder — assembles system prompt + messages for LLM calls."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import platform
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.installed_context import installed_parts, installed_project_context
from core.media_projection import ValidatedMediaProjection
from core.project_context import load_project_context
from core.project_header import project_parts
from core.workspace_context_sources import WORKSPACE_CONTEXT_FILES, workspace_context_files
from core.turns_instrument import context_build_span, set_attrs


@dataclass(frozen=True)
class SystemPromptComponent:
    """One non-overlapping component of an exact system-prompt render."""

    component_id: str
    origin_section: str
    label: str
    stable_reference: str
    owner_surface: str
    content: str
    joined: bool = False  # no separator before it


@dataclass(frozen=True)
class SystemPromptRender:
    """Exact system bytes plus the components that produced them."""

    content: str
    components: tuple[SystemPromptComponent, ...]


def _native_image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return OpenAI-shaped image blocks from one native provider request."""
    return [
        block
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if (
            isinstance(block, dict)
            and block.get("type") == "image_url"
            and isinstance(block.get("image_url"), dict)
        )
    ]


def native_message_image_count(messages: list[dict[str, Any]]) -> int:
    """Count all images already present in one native provider request."""
    return len(_native_image_blocks(messages))


def enforce_native_image_request_limits(
    messages: list[dict[str, Any]],
    model: str | None,
) -> list[dict[str, Any]]:
    """Re-limit existing data images when a request crosses a provider threshold."""
    from core.image_resize import resize_image_for_provider
    from infra.config import PROVIDER_IMAGE_LIMITS
    from llm.capabilities import provider_from_model

    provider = provider_from_model(model) if model else "unknown"
    limits = PROVIDER_IMAGE_LIMITS.get(provider)
    blocks = _native_image_blocks(messages)
    image_count = len(blocks)
    threshold = limits.get("multi_image_threshold") if limits else None
    if not threshold or image_count <= threshold:
        return messages

    for block in blocks:
        url = block["image_url"].get("url")
        if not isinstance(url, str) or not url.startswith("data:image/"):
            continue
        header, separator, encoded = url.partition(",")
        if separator != "," or not header.endswith(";base64"):
            raise RuntimeError(f"Invalid native image data URL for {provider}")
        mime_type = header.removeprefix("data:").removesuffix(";base64")
        try:
            data = base64.b64decode(encoded, validate=True)
            data, mime_type = resize_image_for_provider(
                data,
                mime_type,
                limits,
                image_count=image_count,
            )
        except ValueError as exc:
            raise RuntimeError(f"Image too large for {provider}: {exc}") from exc
        block["image_url"]["url"] = (
            f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
        )
    return messages


def render_context_system_prompt(
    context: Any,
    *,
    project_id: str | None = None,
    kb_query: str | None = None,
) -> SystemPromptRender:
    """Render one context object, retaining a truthful legacy-test fallback.

    Production ``ContextBuilder`` instances return structured components. Small
    eval/test contexts that only implement ``build_system_prompt`` are captured
    as one exact component; their prompt is never reconstructed or counted a
    second time.
    """
    renderer = getattr(context, "render_system_prompt", None)
    if callable(renderer):
        rendered = renderer(project_id=project_id, kb_query=kb_query)
        if isinstance(rendered, SystemPromptRender):
            return rendered

    content = context.build_system_prompt(project_id=project_id)
    return SystemPromptRender(
        content=content,
        components=(
            SystemPromptComponent(
                component_id="core",
                origin_section="system",
                label="Native system core",
                stable_reference="corsia://context/system-core",
                owner_surface="context-builder",
                content=content,
            ),
        ),
    )


class ContextBuilder:
    """Builds the system prompt and messages for agent turns."""

    WORKSPACE_FILES = list(WORKSPACE_CONTEXT_FILES)

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def build_system_prompt(
        self,
        project_id: str | None = None,
        kb_query: str | None = None,
    ) -> str:
        """Build the system prompt from bootstrap files, memory, and project context."""
        return self.render_system_prompt(
            project_id=project_id,
            kb_query=kb_query,
        ).content

    def render_system_prompt(
        self,
        project_id: str | None = None,
        kb_query: str | None = None,
        installed: list | None = None,
    ) -> SystemPromptRender:
        """Return the one exact render; ``installed`` rows replace file reads."""
        # ``kb_query`` remains in this compatibility seam for callers with the
        # old signature. Knowledge retrieval is explicit through KB tools and
        # never part of automatic system-prompt assembly.
        del kb_query
        from core.system_tier import system_tier_components
        components = list(system_tier_components(self.workspace, installed))
        identity = self._get_identity()
        components.append(
            SystemPromptComponent(
                component_id="identity",
                origin_section="system",
                label="CORSIA runtime envelope",
                stable_reference="corsia://runtime/envelope",
                owner_surface="context-builder",
                content=identity,
            )
        )
        workspace_context = self._load_workspace_files() if installed is None else (
            "\n\n".join(installed_parts(installed, "workspace")))
        if workspace_context:
            components.append(
                SystemPromptComponent(
                    component_id="workspace",
                    origin_section="workspace",
                    label="Workspace context",
                    stable_reference="corsia://context/workspace",
                    owner_surface="context-builder",
                    content=workspace_context,
                )
            )

        if project_id:
            project_ctx = self._load_project_context(project_id) if installed is None else (
                installed_project_context(self.workspace, project_id, installed))
            if project_ctx:
                for cid, label, ref, content, joined in project_parts(project_id, project_ctx):
                    components.append(
                        SystemPromptComponent(
                            component_id=cid, origin_section="project", label=label,
                            stable_reference=ref, owner_surface="context-builder",
                            content=content, joined=joined,
                        )
                    )

        rendered_components = tuple(
            component
            if index == 0 or component.joined
            else replace(component, content=f"\n\n---\n\n{component.content}")
            for index, component in enumerate(components)
        )
        return SystemPromptRender(
            content="".join(
                component.content for component in rendered_components
            ),
            components=rendered_components,
        )

    def _get_identity(self) -> str:
        """Return the small factual envelope not owned by bootloader policy."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return (
            "# CORSIA Runtime\n\n"
            f"Host: {runtime}\n"
            f"Workspace: {workspace_path}\n"
            f"Projects directory: {workspace_path}/projects/"
        )

    def _load_workspace_files(self) -> str:
        """Load all workspace context files from the workspace."""
        parts = []

        for source in workspace_context_files(self.workspace):
            if source.exists:
                content = source.path.read_text(encoding="utf-8")
                parts.append(f"## {source.name}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _load_project_context(self, project_id: str) -> str | None:
        """Load project-scoped context files. Delegates to context_project module."""
        return load_project_context(self.workspace, project_id)

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | ValidatedMediaProjection | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        project_id: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        with context_build_span() as _cb_span:
            messages: list[dict[str, Any]] = []

            system_prompt = self.build_system_prompt(project_id=project_id)
            messages.append({"role": "system", "content": system_prompt})

            messages.extend(history)

            user_content = self._build_user_content(current_message, media, model=model)
            messages.append({"role": "user", "content": user_content})

            system_prompt_hash = hashlib.sha256(
                system_prompt.encode("utf-8")
            ).hexdigest()
            from core.token_estimation import _text_tokens

            set_attrs(_cb_span, {
                "context.prompt_tokens": _text_tokens(system_prompt, model),
                "context.kb_recent_count": 0,
                "context.system_prompt_size": len(system_prompt),
                "context.system_prompt_hash": system_prompt_hash,
                "context.messages_count": len(messages),
                "context.has_project": bool(project_id),
                "context.media_count": (
                    len(media.images)
                    if isinstance(media, ValidatedMediaProjection)
                    else len(media or [])
                ),
            })
            return messages

    def _build_user_content(
        self,
        text: str,
        media: list[str] | ValidatedMediaProjection | None,
        model: str | None = None,
    ) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        from llm.capabilities import provider_from_model
        from infra.config import PROVIDER_IMAGE_LIMITS

        provider = provider_from_model(model) if model else "unknown"
        limits = PROVIDER_IMAGE_LIMITS.get(provider)

        images = []
        if isinstance(media, ValidatedMediaProjection):
            sources = [
                (image.data, image.mime_type)
                for image in media.images
            ]
        else:
            def _path_sources():
                for path in media:
                    p = Path(path)
                    mime, _ = mimetypes.guess_type(path)
                    if p.is_file() and mime and mime.startswith("image/"):
                        yield p.read_bytes(), mime

            sources = list(_path_sources())

        image_count = len(sources)
        for raw_bytes, mime in sources:

            if limits is not None:
                try:
                    from core.image_resize import resize_image_for_provider
                    resized_bytes, out_mime = resize_image_for_provider(
                        raw_bytes, mime, limits, image_count=image_count
                    )
                except ValueError as e:
                    raise RuntimeError(
                        f"Image too large for {provider}: {e}"
                    ) from e
                mime = out_mime
            else:
                resized_bytes = raw_bytes

            b64 = base64.b64encode(resized_bytes).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> list[dict[str, Any]]:
        """Add one text tool result to the native message list."""
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages

    def add_tool_media(
        self,
        messages: list[dict[str, Any]],
        media: list[Any],
    ) -> list[dict[str, Any]]:
        """Follow a complete tool-result batch with its admitted images."""
        if media:
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image.data_url()},
                    }
                    for image in media
                ],
            })
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant"}
        msg["content"] = content

        if tool_calls:
            msg["tool_calls"] = tool_calls

        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content

        messages.append(msg)
        return messages
