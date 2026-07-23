"""流式 Markdown 块缓存的验收测试（textual-ui.md §9.1）。"""

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text

from miniagent.ui.render_cache import MarkdownBlockCache, split_closed_blocks


def test_blank_line_outside_fence_is_a_block_boundary():
    closed, tail = split_closed_blocks("第一段\n\n第二段")
    # 作为块边界的空行被消费，不计入已闭合前缀。
    assert closed == "第一段\n"
    assert tail == "第二段"


def test_blank_lines_inside_an_open_fence_are_not_split():
    source = "```py\na = 1\n\nb = 2\n"
    closed, tail = split_closed_blocks(source)
    # 围栏未闭合，内部空行属于代码内容，整体都是未闭合尾部。
    assert closed == ""
    assert tail == source


def test_closed_fence_followed_by_blank_line_becomes_closed_prefix():
    closed, tail = split_closed_blocks("```py\na = 1\n```\n\n下一段")
    assert closed == "```py\na = 1\n```\n"
    assert tail == "下一段"


def test_tilde_fence_is_also_recognized():
    closed, tail = split_closed_blocks("~~~\ncode\n~~~\n\ntail")
    assert closed == "~~~\ncode\n~~~\n"
    assert tail == "tail"


def _markdown_blocks(group: Group) -> list[Markdown]:
    return [item for item in group.renderables if isinstance(item, Markdown)]


def test_cache_does_not_reparse_closed_blocks_when_prefix_grows():
    cache = MarkdownBlockCache()
    first = cache.render("一\n\n二\n\n三")
    first_blocks = _markdown_blocks(first)
    # 两个已闭合块 + 未闭合尾部临时实例。
    assert len(first_blocks) == 3

    second = cache.render("一\n\n二\n\n三四")
    second_blocks = _markdown_blocks(second)
    # 已闭合块实例被复用（构造即解析，实例相同即未重复解析）。
    assert second_blocks[0] is first_blocks[0]
    assert second_blocks[1] is first_blocks[1]
    # 未闭合尾部每次重新解析。
    assert second_blocks[2] is not first_blocks[2]


def test_cache_rebuilds_when_closed_prefix_changes():
    cache = MarkdownBlockCache()
    first = cache.render("一\n\n二")
    rebuilt = cache.render("X\n\n二")
    assert _markdown_blocks(rebuilt)[0] is not _markdown_blocks(first)[0]


def test_empty_source_renders_a_placeholder_text():
    group = MarkdownBlockCache().render("")
    assert len(group.renderables) == 1
    assert isinstance(group.renderables[0], Text)
