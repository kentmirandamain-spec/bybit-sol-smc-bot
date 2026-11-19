import unittest

import telegram_bot


class TelegramTests(unittest.TestCase):
    def test_close_needs_a_second_confirm(self):
        reply, until, action = telegram_bot.handle_text("/close", 0, now=1000)
        self.assertEqual(action, "")
        self.assertIn("confirm", reply)
        reply, until, action = telegram_bot.handle_text("/close confirm", until, now=1030)
        self.assertEqual(action, "close")
        reply, until, action = telegram_bot.handle_text("/close confirm", until, now=1000)
        self.assertEqual(action, "")

    def test_other_chats_are_ignored(self):
        seen = []
        bot = telegram_bot.Telegram("token", "42")

        def fake(method, payload):
            if method == "getUpdates":
                return {"result": [
                    {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/pause"}},
                    {"update_id": 2, "message": {"chat": {"id": "42"}, "text": "/pause"}},
                ]}
            return {}

        bot._call = fake
        bot.poll(lambda text: seen.append(text) or "ok")
        self.assertEqual(seen, ["/pause"])


if __name__ == "__main__":
    unittest.main()
