import asyncio
import unittest

from bot.services import telegram_avatars


class _Photo:
    file_id = "photo-file"


class _Photos:
    def __init__(self, photos):
        self.photos = photos
        self.total_count = len(photos)


class _File:
    file_path = "photos/avatar.jpg"
    file_size = 11


class _Downloaded:
    def read(self):
        return b"jpeg-bytes"


class _Bot:
    def __init__(self, photos):
        self.photos = photos
        self.profile_calls = 0

    async def get_user_profile_photos(self, telegram_id, limit):
        self.profile_calls += 1
        return _Photos(self.photos)

    async def get_file(self, file_id):
        assert file_id == "photo-file"
        return _File()

    async def download_file(self, file_path):
        assert file_path == "photos/avatar.jpg"
        return _Downloaded()


class TelegramAvatarTests(unittest.TestCase):
    def setUp(self):
        telegram_avatars._CACHE.clear()

    def test_loads_largest_photo_and_uses_cache(self):
        bot = _Bot([[ _Photo() ]])
        first = asyncio.run(telegram_avatars.get_profile_photo(bot, 1001))
        second = asyncio.run(telegram_avatars.get_profile_photo(bot, 1001))
        self.assertEqual(first, b"jpeg-bytes")
        self.assertEqual(second, b"jpeg-bytes")
        self.assertEqual(bot.profile_calls, 1)

    def test_no_photo_is_a_nonfatal_empty_result(self):
        bot = _Bot([])
        self.assertIsNone(asyncio.run(telegram_avatars.get_profile_photo(bot, 1002)))
        self.assertIsNone(asyncio.run(telegram_avatars.get_profile_photo(bot, 1002)))
        self.assertEqual(bot.profile_calls, 1)

    def test_missing_bot_keeps_session_working(self):
        self.assertIsNone(asyncio.run(telegram_avatars.get_profile_photo(None, 1003)))

    def test_telegram_failure_is_nonfatal(self):
        class FailingBot(_Bot):
            async def get_user_profile_photos(self, telegram_id, limit):
                raise RuntimeError("Telegram temporarily unavailable")

        bot = FailingBot([])
        self.assertIsNone(asyncio.run(telegram_avatars.get_profile_photo(bot, 1004)))


if __name__ == "__main__":
    unittest.main()
