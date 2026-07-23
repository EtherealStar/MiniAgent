"""MiniAgent 的终端入口；领域演示不在启动 UI 时执行。"""

from miniagent.ui import MiniAgentApp


def main() -> None:
    MiniAgentApp().run()


if __name__ == "__main__":
    main()
