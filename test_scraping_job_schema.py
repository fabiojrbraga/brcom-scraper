import unittest

from pydantic import ValidationError

from app.schemas import ScrapingJobCreate


class ScrapingJobSchemaTest(unittest.TestCase):
    def test_recent_likes_accepts_300_like_users_per_post_from_n8n_payload(self):
        request = ScrapingJobCreate.model_validate(
            {
                "profile_url": "https://www.instagram.com/pepoton.kids/",
                "flow": "recent_likes",
                "max_posts": 3,
                "recent_days": "3",
                "max_like_users_per_post": "300",
                "collect_like_user_profiles": "false",
                "session_username": "pepoton.kids",
            }
        )

        self.assertEqual(request.max_like_users_per_post, 300)
        self.assertEqual(request.recent_days, 3)
        self.assertFalse(request.collect_like_user_profiles)

    def test_like_users_per_post_rejects_values_above_300(self):
        with self.assertRaises(ValidationError):
            ScrapingJobCreate.model_validate(
                {
                    "profile_url": "https://www.instagram.com/pepoton.kids/",
                    "flow": "recent_likes",
                    "max_like_users_per_post": 301,
                    "session_username": "pepoton.kids",
                }
            )


if __name__ == "__main__":
    unittest.main()
