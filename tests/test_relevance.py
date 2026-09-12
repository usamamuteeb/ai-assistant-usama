"""Pure relevance-filter behavior."""
from __future__ import annotations

from src.tools.relevance import select_relevant_tools


TOOLS = [
    {"name": "search_memory", "description": "Search saved notes and memories."},
    {"name": "calendar_list", "description": "List calendar meetings and events."},
    {"name": "system_stats", "description": "Report CPU memory and disk usage."},
    {"name": "web_search", "description": "Search the public web."},
    {"name": "filesystem", "description": "Read and write workspace files."},
    {"name": "generate_image", "description": "Generate an image, picture, drawing, art, or illustration."},
]


def test_keyword_overlap_ranks_the_matching_tool_first():
    selected = select_relevant_tools("show CPU and disk usage", TOOLS, 1, 1, set())

    assert [tool["name"] for tool in selected] == ["system_stats"]


def test_core_tools_are_always_selected_even_without_keyword_overlap():
    selected = select_relevant_tools("hello there", TOOLS, 4, 1, {"filesystem", "search_memory"})

    assert {"filesystem", "search_memory"}.issubset({tool["name"] for tool in selected})


def test_image_generation_can_be_pinned_as_a_core_tool():
    selected = select_relevant_tools("hello there", TOOLS, 4, 1, {"generate_image"})

    assert "generate_image" in {tool["name"] for tool in selected}


def test_ambiguous_request_honors_the_minimum_tool_floor():
    selected = select_relevant_tools("please help", TOOLS, 4, 3, set())

    assert len(selected) >= 3


# Bug: before this test, core tools were prepended and then max_tools additional
# non-core tools were included, so the returned total could exceed max_tools.
def test_selection_never_exceeds_max_tools():
    selected = select_relevant_tools("hello", TOOLS, 3, 1, {"filesystem", "search_memory"})

    assert len(selected) <= 3
