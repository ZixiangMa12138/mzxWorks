"""ZME 钉钉机器人规范入口。"""

from zme_dingtalk_robot.app import run
from zme_dingtalk_robot.config import Settings


def main() -> None:
    run(Settings.from_env())


if __name__ == "__main__":
    main()
