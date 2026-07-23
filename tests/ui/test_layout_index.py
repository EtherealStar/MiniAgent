from uuid import uuid4

from miniagent.ui.layout_index import VirtualLayoutIndex


def test_visible_range_uses_message_heights_and_overscan():
    ids = [uuid4() for _ in range(5)]
    index = VirtualLayoutIndex(ids, default_height=10)

    assert index.visible_range(scroll_y=21, viewport_height=10, overscan=1) == (1, 5)
    assert index.locate(25) == (ids[2], 5)


def test_height_updates_preserve_prefix_positions():
    first, second, third = uuid4(), uuid4(), uuid4()
    index = VirtualLayoutIndex((first, second, third), default_height=2)

    index.update_height(first, 5)

    assert index.prefix_height(second) == 5
    assert index.total_height == 9

