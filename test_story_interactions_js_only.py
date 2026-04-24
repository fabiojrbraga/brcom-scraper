import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.scraper.instagram_scraper as instagram_scraper_module
from app.scraper.browser_use_agent import BrowserUseAgent
from app.scraper.instagram_scraper import InstagramScraper


class StoryInteractionsJsOnlyTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_use_story_flow_runs_js_only_without_llm(self):
        agent = BrowserUseAgent.__new__(BrowserUseAgent)
        agent.api_key = None

        dummy_session = object()
        observed_story_urls = []
        story_payload = {
            "story_url": "https://www.instagram.com/stories/pepoton.kids/1234567890123456789/",
            "view_count": 2,
            "viewer_users": [
                {
                    "user_username": "viewer1",
                    "user_url": "https://www.instagram.com/viewer1/",
                    "liked": True,
                }
            ],
            "liked_users": [
                {
                    "user_username": "viewer1",
                    "user_url": "https://www.instagram.com/viewer1/",
                }
            ],
        }

        async def fake_js_flow(**kwargs):
            callback = kwargs.get("on_story_collected")
            self.assertIsNotNone(callback)
            await callback(dict(story_payload))
            return {
                "profile_url": "https://www.instagram.com/pepoton.kids/",
                "stories_accessible": True,
                "story_posts": [dict(story_payload)],
                "total_story_posts": 1,
                "total_story_viewers": 1,
                "total_liked_users": 1,
                "total_collected": 1,
                "error": None,
            }

        async def on_story_collected(story_item):
            observed_story_urls.append(story_item["story_url"])

        async def fake_resolve_browserless_cdp_url():
            return "ws://dummy"

        agent._get_browserless_reconnect_url = lambda storage_state: None
        agent._get_browserless_session_info = lambda storage_state: {}
        agent._prepare_storage_state_for_browser_session = (
            lambda storage_state: (storage_state, None, None)
        )
        agent._resolve_browserless_cdp_url = fake_resolve_browserless_cdp_url
        agent._create_browser_session = (
            lambda cdp_url, storage_state=None, user_agent=None: dummy_session
        )
        agent._patch_event_bus_for_stop = lambda browser_session: (lambda: None)
        agent._scrape_story_interactions_via_js = fake_js_flow
        agent._detach_browser_session = AsyncMock()
        agent._cleanup_storage_state_temp_file = lambda path: None
        agent._toggle_ws_compression_mode = lambda reason: None
        agent._ensure_browser_session_connected = AsyncMock()
        agent._classify_agent_failure_error = BrowserUseAgent._classify_agent_failure_error.__get__(
            agent, BrowserUseAgent
        )
        agent._contains_protocol_error = BrowserUseAgent._contains_protocol_error.__get__(
            agent, BrowserUseAgent
        )
        agent._contains_rate_limit_error = BrowserUseAgent._contains_rate_limit_error.__get__(
            agent, BrowserUseAgent
        )
        agent._history_errors_text = BrowserUseAgent._history_errors_text.__get__(
            agent, BrowserUseAgent
        )
        agent._create_agent = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("LLM fallback should not run in stories_interactions")
        )

        result = await BrowserUseAgent.scrape_story_interactions(
            agent,
            profile_url="https://www.instagram.com/pepoton.kids/",
            storage_state={"cookies": [{"name": "sessionid", "value": "x"}]},
            max_interactions=10,
            on_story_collected=on_story_collected,
        )

        self.assertEqual(observed_story_urls, [story_payload["story_url"]])
        self.assertEqual(result["story_posts"][0]["story_url"], story_payload["story_url"])
        agent._detach_browser_session.assert_awaited_once()
        agent._ensure_browser_session_connected.assert_not_awaited()


class StoryInteractionsPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_stories_interactions_persists_each_story_during_callback(self):
        scraper = InstagramScraper.__new__(InstagramScraper)
        scraper._require_session_username = InstagramScraper._require_session_username.__get__(
            scraper, InstagramScraper
        )
        scraper._extract_username_from_url = InstagramScraper._extract_username_from_url.__get__(
            scraper, InstagramScraper
        )
        scraper._normalize_story_persist_user = InstagramScraper._normalize_story_persist_user.__get__(
            scraper, InstagramScraper
        )
        scraper._persist_story_interaction_item = InstagramScraper._persist_story_interaction_item.__get__(
            scraper, InstagramScraper
        )

        events = []
        persisted_payloads = []
        story_payload = {
            "story_url": "https://www.instagram.com/stories/pepoton.kids/1234567890123456789/",
            "view_count": 2,
            "viewer_users": [
                {
                    "user_username": "viewer1",
                    "user_url": "https://www.instagram.com/viewer1/",
                    "liked": True,
                },
                {
                    "user_username": "viewer2",
                    "user_url": "https://www.instagram.com/viewer2/",
                    "liked": False,
                },
            ],
            "liked_users": [
                {
                    "user_username": "viewer1",
                    "user_url": "https://www.instagram.com/viewer1/",
                }
            ],
        }

        async def fake_save_profile(db, profile_url, profile_info):
            events.append("save_profile")
            return SimpleNamespace(id="profile-1")

        async def fake_save_posts_and_interactions(db, profile_id, posts_data, interactions):
            events.append("persist_story")
            persisted_payloads.append(
                {
                    "profile_id": profile_id,
                    "posts_data": posts_data,
                    "interactions": interactions,
                }
            )

        async def fake_story_scrape(**kwargs):
            events.append("agent_before_callback")
            callback = kwargs.get("on_story_collected")
            self.assertIsNotNone(callback)
            await callback(dict(story_payload))
            events.append("agent_after_callback")
            return {
                "profile_url": "https://www.instagram.com/pepoton.kids/",
                "stories_accessible": True,
                "story_posts": [dict(story_payload)],
                "total_story_posts": 1,
                "total_story_viewers": 2,
                "total_liked_users": 1,
                "total_collected": 2,
                "error": None,
            }

        scraper._save_profile = fake_save_profile
        scraper._save_posts_and_interactions = fake_save_posts_and_interactions

        ensure_session_mock = AsyncMock(return_value={"cookies": [{"name": "sessionid", "value": "x"}]})
        scrape_story_mock = AsyncMock(side_effect=fake_story_scrape)

        with patch.object(
            instagram_scraper_module.browser_use_agent,
            "ensure_instagram_session",
            ensure_session_mock,
        ), patch.object(
            instagram_scraper_module.browser_use_agent,
            "scrape_story_interactions",
            scrape_story_mock,
        ):
            result = await InstagramScraper.scrape_stories_interactions(
                scraper,
                profile_url="https://www.instagram.com/pepoton.kids/",
                db=object(),
                session_username="pepoton.kids",
                max_interactions=10,
            )

        self.assertEqual(
            events,
            ["save_profile", "agent_before_callback", "persist_story", "agent_after_callback"],
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["job_result"], "success")
        self.assertEqual(result["summary"]["total_story_posts"], 1)
        self.assertEqual(result["summary"]["total_story_viewers"], 2)
        self.assertNotIn("story_posts", result)
        self.assertEqual(persisted_payloads[0]["profile_id"], "profile-1")
        self.assertEqual(
            persisted_payloads[0]["posts_data"][0]["post_url"],
            story_payload["story_url"],
        )

    async def test_scrape_stories_interactions_returns_partial_success_when_error_after_persistence(self):
        scraper = InstagramScraper.__new__(InstagramScraper)
        scraper._require_session_username = InstagramScraper._require_session_username.__get__(
            scraper, InstagramScraper
        )
        scraper._extract_username_from_url = InstagramScraper._extract_username_from_url.__get__(
            scraper, InstagramScraper
        )
        scraper._normalize_story_persist_user = InstagramScraper._normalize_story_persist_user.__get__(
            scraper, InstagramScraper
        )
        scraper._persist_story_interaction_item = InstagramScraper._persist_story_interaction_item.__get__(
            scraper, InstagramScraper
        )

        story_payload = {
            "story_url": "https://www.instagram.com/stories/pepoton.kids/1234567890123456789/",
            "view_count": 2,
            "viewer_users": [
                {
                    "user_username": "viewer1",
                    "user_url": "https://www.instagram.com/viewer1/",
                    "liked": True,
                }
            ],
            "liked_users": [
                {
                    "user_username": "viewer1",
                    "user_url": "https://www.instagram.com/viewer1/",
                }
            ],
        }

        scraper._save_profile = AsyncMock(return_value=SimpleNamespace(id="profile-1"))
        scraper._save_posts_and_interactions = AsyncMock()

        async def fake_story_scrape(**kwargs):
            callback = kwargs.get("on_story_collected")
            await callback(dict(story_payload))
            return {
                "profile_url": "https://www.instagram.com/pepoton.kids/",
                "stories_accessible": True,
                "story_posts": [dict(story_payload)],
                "total_story_posts": 1,
                "total_story_viewers": 1,
                "total_liked_users": 1,
                "total_collected": 1,
                "error": "story_open_failed",
            }

        with patch.object(
            instagram_scraper_module.browser_use_agent,
            "ensure_instagram_session",
            AsyncMock(return_value={"cookies": [{"name": "sessionid", "value": "x"}]}),
        ), patch.object(
            instagram_scraper_module.browser_use_agent,
            "scrape_story_interactions",
            AsyncMock(side_effect=fake_story_scrape),
        ):
            result = await InstagramScraper.scrape_stories_interactions(
                scraper,
                profile_url="https://www.instagram.com/pepoton.kids/",
                db=object(),
                session_username="pepoton.kids",
                max_interactions=10,
            )

        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["job_result"], "partial_success")
        self.assertEqual(result["error"], "story_open_failed")
        self.assertEqual(result["summary"]["total_story_posts"], 1)
        self.assertNotIn("story_posts", result)

    async def test_scrape_stories_interactions_returns_failed_when_nothing_persisted(self):
        scraper = InstagramScraper.__new__(InstagramScraper)
        scraper._require_session_username = InstagramScraper._require_session_username.__get__(
            scraper, InstagramScraper
        )
        scraper._extract_username_from_url = InstagramScraper._extract_username_from_url.__get__(
            scraper, InstagramScraper
        )
        scraper._normalize_story_persist_user = InstagramScraper._normalize_story_persist_user.__get__(
            scraper, InstagramScraper
        )
        scraper._persist_story_interaction_item = InstagramScraper._persist_story_interaction_item.__get__(
            scraper, InstagramScraper
        )

        scraper._save_profile = AsyncMock(return_value=SimpleNamespace(id="profile-1"))
        scraper._save_posts_and_interactions = AsyncMock()

        with patch.object(
            instagram_scraper_module.browser_use_agent,
            "ensure_instagram_session",
            AsyncMock(return_value={"cookies": [{"name": "sessionid", "value": "x"}]}),
        ), patch.object(
            instagram_scraper_module.browser_use_agent,
            "scrape_story_interactions",
            AsyncMock(
                return_value={
                    "profile_url": "https://www.instagram.com/pepoton.kids/",
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_story_viewers": 0,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "protocol_error",
                }
            ),
        ):
            result = await InstagramScraper.scrape_stories_interactions(
                scraper,
                profile_url="https://www.instagram.com/pepoton.kids/",
                db=object(),
                session_username="pepoton.kids",
                max_interactions=10,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["job_result"], "failed")
        self.assertEqual(result["error"], "protocol_error")
        self.assertEqual(result["summary"]["total_story_posts"], 0)
        self.assertNotIn("story_posts", result)


if __name__ == "__main__":
    unittest.main()
