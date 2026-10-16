"""What the always-on index says each connected server is for.

The rule is the one a weak model was checked against: a person's override, the
server's own initialize instructions, a one-tool server's description, then the
words its tool names repeat most. These pin it with real tool names.
"""

from __future__ import annotations

import pytest

from tests.test_tool_surface_disclosure import (
    _CaptureTool,
    _execute_as,
    _mcp_wrapper,
    _registry,
    _seed_thread,
    reachable_mcp_tools,
)


class _Tool(_CaptureTool):
    user_configured = True

    def __init__(
        self, server: str, raw: str, description: str = "", *, config=None,
        instructions=None, app_id: str | None = "reachable-app",
    ):
        super().__init__(f"mcp_{server}_{raw}")
        self.mcp_server_name = server
        self.mcp_tool_name = raw
        self.mcp_server_config = config or {}
        self.mcp_instructions = instructions
        self.mcp_app_id = app_id
        self._description = description

    @property
    def description(self) -> str:
        return self._description


@pytest.fixture(autouse=True)
def _fixture_mcp_tools_are_reachable(reachable_mcp_tools):
    """Use the disclosure fixture's reachable bound-app model for index tests."""


def _line(tmp_path, server: str, raws: list[str], description: str = "", **kwargs) -> str:
    registry = _registry(tmp_path, server)
    for raw in raws:
        registry.register(_Tool(server, raw, description, **kwargs))
    index = registry.get("tools_load").description
    return next(line for line in index.splitlines() if line.startswith(f"- {server.strip()}"))


FILESYSTEM = [
    "read_file", "read_text_file", "read_media_file", "read_multiple_files", "write_file",
    "edit_file", "create_directory", "list_directory", "list_directory_with_sizes",
    "directory_tree", "move_file", "search_files", "get_file_info", "list_allowed_directories",
]
MEMORY = [
    "create_entities", "create_relations", "add_observations", "delete_entities",
    "delete_observations", "delete_relations", "read_graph", "search_nodes", "open_nodes",
]


def test_word_rule_ranks_by_count_then_first_appearance_without_generic_verbs(tmp_path):
    assert _line(tmp_path, "filesystem", FILESYSTEM) == (
        "- filesystem: 14 tools; directory, files, text, media, sizes, tree"
    )
    assert _line(tmp_path, "memory", MEMORY) == (
        "- memory: 9 tools; entities, relations, observations, nodes, graph"
    )


def test_word_rule_splits_camel_case_and_drops_short_words(tmp_path):
    line = _line(tmp_path, "docs", ["getPageContent", "listPageBlocks", "id_to_url", "get_ids"])

    assert line == "- docs: 4 tools; page, content, blocks, url, ids"


def test_one_tool_server_says_tool_and_uses_its_first_description_line(tmp_path):
    line = _line(tmp_path, "fetch", ["fetch"], "Fetches a URL from the internet.\nMore detail.")

    assert line == "- fetch: 1 tool; Fetches a URL from the internet."


def test_server_instructions_beat_the_tools_and_keep_only_the_first_sentence(tmp_path):
    line = _line(
        tmp_path, "wiki", ["search_pages", "read_pages"],
        instructions="Use this server for the team wiki. Always cite the page.\nSecond paragraph.",
    )

    assert line == "- wiki: 2 tools; Use this server for the team wiki."


def test_a_person_override_replaces_the_line_whole_and_beats_instructions(tmp_path):
    override = "Billing system: invoices. Refunds need approval.\nNever run in production\x00" + "x" * 200

    line = _line(
        tmp_path, "billing", ["list_invoices", "refund"], config={"purpose": override},
        instructions="Ignored because a person wrote a purpose.",
    )

    purpose = line.split("; ", 1)[1]
    assert purpose.startswith("Billing system: invoices. Refunds need approval.")  # not cut to one sentence
    assert "\n" not in line and "\x00" not in line and len(purpose) <= 120
    assert "Ignored" not in line


def test_instructions_without_a_sentence_end_are_capped_at_the_line_width(tmp_path):
    line = _line(tmp_path, "wiki", ["a_b", "c_d"], instructions="word " * 100)

    assert len(line.split("; ", 1)[1]) <= 120


def test_a_server_name_loses_its_control_characters_in_the_line(tmp_path):
    registry = _registry(tmp_path)
    registry.register(_Tool("wi\nki\x00", "search_pages"))

    index = registry.get("tools_load").description

    assert "- wiki: 1 tool; " in index
    assert "\x00" not in index


@pytest.mark.asyncio
async def test_unbound_mcp_tool_is_absent_from_the_index_and_refused(isolated_db, tmp_path):
    _seed_thread(isolated_db, "unbound-purpose-thread")
    unbound = _mcp_wrapper()
    registry = _registry(tmp_path)
    registry.register(unbound)

    index = registry.get("tools_load").description
    refused = await _execute_as("unbound-purpose-thread", registry, "tool_call", {
        "name": unbound.name, "arguments": {},
    })

    assert unbound.name not in index
    assert "not available to this project" in refused
    unbound._session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_tools_load_server_lists_tools_in_discovery_order_with_the_tested_wording(tmp_path):
    registry = _registry(tmp_path)
    for raw, text in (("zeta_tool", "Last by name, first found.\nDetail."), ("alpha_tool", "x" * 300)):
        registry.register(_Tool("wiki", raw, text))

    listing = (await registry.get("tools_load").execute(server="wiki")).splitlines()

    assert listing[0] == "Tools on wiki. Load by name with tools_load(names=[...]), then call with tool_call."
    assert listing[1] == "- mcp_wiki_zeta_tool: Last by name, first found."
    assert listing[2] == "- mcp_wiki_alpha_tool: " + "x" * 120
