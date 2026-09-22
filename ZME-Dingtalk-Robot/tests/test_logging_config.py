"""统一日志配置与凭证脱敏测试。"""

import logging
from pathlib import Path
import tempfile
import unittest

from zme_dingtalk_robot.logging_config import configure_logging, redact_secrets


class LoggingConfigTest(unittest.TestCase):
    def tearDown(self) -> None:
        logging.shutdown()
        logging.getLogger().handlers.clear()

    def test_redacts_credentials_but_keeps_normal_content(self) -> None:
        source = (
            "content='查询张三' access_token=secret-token "
            "CODEX_API_KEY=sk-secret client_secret=ding-secret "
            "Authorization: Bearer bearer-secret "
            "{\"appSecret\":\"json-secret\",\"ticket\":\"stream-ticket\"} "
            "url=https://example.test/path?access_token=query-secret&next=1"
        )

        result = redact_secrets(source)

        self.assertIn("查询张三", result)
        self.assertNotIn("secret-token", result)
        self.assertNotIn("sk-secret", result)
        self.assertNotIn("ding-secret", result)
        self.assertNotIn("bearer-secret", result)
        self.assertNotIn("json-secret", result)
        self.assertNotIn("stream-ticket", result)
        self.assertNotIn("query-secret", result)
        self.assertIn("<redacted>", result)

    def test_writes_console_compatible_rotating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "logs" / "robot.log"
            configure_logging("INFO", log_file, max_bytes=1024, backup_count=2)
            logger = logging.getLogger("test")

            logger.info(
                "模型反馈=%s API_KEY=%s",
                "回答内容",
                "secret-value",
                extra={"request_id": "message-1"},
            )
            for handler in logging.getLogger().handlers:
                handler.flush()

            content = log_file.read_text(encoding="utf-8")

        self.assertIn("request_id=message-1", content)
        self.assertIn("回答内容", content)
        self.assertNotIn("secret-value", content)


if __name__ == "__main__":
    unittest.main()
