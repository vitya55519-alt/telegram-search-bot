"""Offline API tests. Run with: python -B -m unittest -v test_search."""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import URLError
from urllib.parse import quote

from telethon import functions, types, utils
from telethon.errors import ChannelPrivateError, FloodWaitError

import search_service


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
USER_ID = 42
USERNAME = "target_user"
CHECK = functions.channels.CheckSearchPostsFloodRequest
POSTS = functions.channels.SearchPostsRequest
CONTACTS = functions.contacts.SearchRequest
PROFILE = functions.users.GetFullUserRequest
RECOMMEND = functions.channels.GetChannelRecommendationsRequest
FULL_CHANNEL = functions.channels.GetFullChannelRequest


def channel(ident=100, username="public_news", *, broadcast=False, left=True):
    return types.Channel(
        id=ident, access_hash=ident * 100, title=f"Channel {ident}",
        photo=types.ChatPhotoEmpty(), date=NOW, username=username,
        broadcast=broadcast, megagroup=not broadcast, left=left,
    )


def group(ident=200, *, left=False):
    return types.Chat(
        id=ident, title=f"Group {ident}", photo=types.ChatPhotoEmpty(),
        participants_count=3, date=NOW, version=1, left=left,
    )


def message(entity, ident=1, *, sender=USER_ID, text="An ordinary message", **kwargs):
    if isinstance(sender, int):
        sender = types.PeerUser(sender)
    return types.Message(
        id=ident, peer_id=utils.get_peer(entity), from_id=sender,
        date=NOW + timedelta(seconds=ident), message=text, **kwargs,
    )


def quota(*, free=True, remains=10):
    return types.SearchPostsFlood(
        total_daily=10, remains=remains, stars_amount=100, query_is_free=free,
    )


def page(messages=(), chats=(), *, next_rate=None, search_flood=None):
    return types.messages.MessagesSlice(
        count=len(messages), messages=list(messages), chats=list(chats), users=[],
        next_rate=next_rate, search_flood=search_flood,
    )


def contacts(chats=(), users=()):
    return types.contacts.Found(
        my_results=[], results=[], chats=list(chats), users=list(users),
    )


class FakeClient:
    """Strict read-only fake; never creates or delegates to TelegramClient."""

    def __init__(self):
        self.dialogs = []
        self.histories = {}
        self.entities = {}
        self.posts = {}
        self.routes = {}
        self.requests = []
        self.scan_calls = []
        self.entity_calls = []
        self.message_calls = []
        self.defaults = {
            CHECK: quota(), POSTS: page(), CONTACTS: contacts(),
            PROFILE: SimpleNamespace(
                full_user=SimpleNamespace(personal_channel_id=None), chats=[],
            ),
            RECOMMEND: SimpleNamespace(chats=[]),
            FULL_CHANNEL: SimpleNamespace(
                full_chat=SimpleNamespace(linked_chat_id=None), chats=[],
            ),
        }

    def respond(self, request_type, *responses):
        self.routes[request_type] = deque(responses)

    def requests_of(self, request_type):
        return [r for r in self.requests if isinstance(r, request_type)]

    async def __call__(self, request):
        self.requests.append(request)
        kind = type(request)
        if kind not in self.defaults:
            raise AssertionError(f"Unexpected or mutating Telegram request: {kind}")
        if kind in self.routes:
            if not self.routes[kind]:
                raise AssertionError(f"Unexpected extra request/retry: {kind}")
            response = self.routes[kind].popleft()
        else:
            response = self.defaults[kind]
        if isinstance(response, BaseException):
            raise response
        return response

    async def iter_dialogs(self):
        for entity in self.dialogs:
            if isinstance(entity, BaseException):
                raise entity
            yield SimpleNamespace(entity=entity)

    def iter_messages(self, entity, **kwargs):
        self.scan_calls.append((entity, kwargs))
        key = utils.get_peer_id(entity)

        async def iterate():
            # Deliberately do not emulate sender filtering: the runner must also
            # verify each returned message's actual author, not trust the server.
            for item in self.histories.get(key, [])[:kwargs["limit"]]:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return iterate()

    async def get_entity(self, name):
        self.entity_calls.append(name)
        value = self.entities.get(name, ValueError("Unknown public reference"))
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_messages(self, entity, *, ids):
        key = (utils.get_peer_id(entity), ids)
        self.message_calls.append(key)
        value = self.posts.get(key, ValueError("Unavailable indexed post"))
        if isinstance(value, BaseException):
            raise value
        return value


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.user = types.User(id=USER_ID, username=USERNAME)
        self.chat = channel()

    def test_authored_requires_exact_direct_peer_user_id(self):
        self.assertTrue(search_service.is_authored(message(self.chat), USER_ID))
        forwarded = types.MessageFwdHeader(date=NOW, from_id=types.PeerUser(USER_ID))
        cases = {
            "other author": message(self.chat, sender=99),
            "forwarded by target": message(self.chat, fwd_from=forwarded),
            "forwarded from target": message(self.chat, sender=99, fwd_from=forwarded),
            "anonymous signature": message(self.chat, sender=None, post_author=USERNAME),
            "channel signature": message(
                self.chat, sender=types.PeerChannel(USER_ID), post_author=USERNAME,
            ),
            "chat id collision": message(self.chat, sender=types.PeerChat(USER_ID)),
            "nickname mention": message(self.chat, sender=99, text=f"@{USERNAME}"),
            "id mention": message(self.chat, sender=99, entities=[
                types.MessageEntityMentionName(offset=0, length=4, user_id=USER_ID),
            ]),
        }
        for label, candidate in cases.items():
            with self.subTest(label=label):
                self.assertFalse(search_service.is_authored(candidate, USER_ID))

    def test_exact_nickname_boundaries_and_case(self):
        positives = [f"@{USERNAME}", f"Hello (@{USERNAME})!", "@TARGET_USER",
                     f"@{USERNAME}.", f"\n@{USERNAME}\n"]
        negatives = [USERNAME, f"@{USERNAME}_extra", f"@{USERNAME}2",
                     f"@{USERNAME}X", f"prefix@{USERNAME}", f"é@{USERNAME}",
                     f"@{USERNAME}я", "@someone_else", ""]
        for text in positives + negatives:
            with self.subTest(text=text):
                self.assertEqual(
                    search_service.is_mention(message(self.chat, text=text), self.user),
                    text in positives,
                )

    def test_id_mentions_work_without_nickname_and_reject_other_user_ids(self):
        user = types.User(id=USER_ID)
        for mentioned_id in (USER_ID, 99):
            with self.subTest(mentioned_id=mentioned_id):
                candidate = message(self.chat, text="Name", entities=[
                    types.MessageEntityMentionName(offset=0, length=4, user_id=mentioned_id),
                ])
                self.assertEqual(search_service.is_mention(candidate, user), mentioned_id == USER_ID)
        self.assertFalse(search_service.is_mention(message(self.chat, text=None), user))


class PublicReferenceTests(unittest.TestCase):
    def test_public_urls_normalize_names_and_deduplicate(self):
        text = (
            'https://t.me/Public_News/12 https://t.me/s/public_news/12 '
            'http://t.me/Public_News https://t.me/public_news/13?single=1 '
            '<a href="https://t.me/Other_Name/3#reply">post</a>'
        )
        self.assertEqual(search_service.public_references(text), [
            ("public_news", 12), ("public_news", None),
            ("public_news", 13), ("other_name", 3),
        ])

    def test_duckduckgo_uddg_and_html_entities_are_decoded(self):
        target = "https://t.me/s/Public_News/12?single=1"
        wrapped = "https://duckduckgo.com/l/?uddg=" + quote(target, safe="") + "&amp;rut=abc"
        self.assertEqual(search_service.public_references(wrapped), [("public_news", 12)])

    def test_url_scheme_and_hostname_are_case_insensitive_including_uddg(self):
        for target in ("HTTPS://T.ME/Public_News/12", "https://T.Me/public_news/12"):
            for wrapped in (False, True):
                with self.subTest(target=target, wrapped=wrapped):
                    text = ("https://duckduckgo.com/l/?uddg=" + quote(target, safe="")) if wrapped else target
                    self.assertEqual(search_service.public_references(text), [("public_news", 12)])

    def test_invalid_hosts_and_paths_are_not_public_references(self):
        invalid = [
            "https://t.me.evil.example/public_news/12",
            "https://evil-t.me/public_news/12",
            "https://t.me@evil.example/public_news/12",
            "https://not-t.me/public_news/12",
            "ftp://t.me/public_news/12",
            "https://t.me/public_news/not-a-message",
            "https://t.me/public_news/12/extra",
            "https://t.me/public_news/12oops",
            "https://t.me/public_news.evil/12",
            "https://t.me/ab/12",
            "https://t.me/" + "a" * 33 + "/12",
            "https://evil.example/https://t.me/public_news/12",
            "https://evil.example/?next=https://t.me/public_news/12",
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.assertEqual(search_service.public_references(text), [])

    def test_uddg_target_must_itself_have_valid_public_host_and_path(self):
        targets = [
            "https://t.me.evil.example/public_news/12",
            "https://evil.example/https://t.me/public_news/12",
            "https://t.me/public_news/12/extra",
            "https://t.me/+privateHash",
        ]
        for target in targets:
            with self.subTest(target=target):
                wrapped = "https://duckduckgo.com/l/?uddg=" + quote(target, safe="")
                self.assertEqual(search_service.public_references(wrapped), [])

    def test_private_invites_internal_links_and_reserved_actions_are_rejected(self):
        paths = ["+privateHash", "joinchat/privateHash", "joinchat", "s/joinchat",
                 "c/123456/12", "share?url=example", "proxy?server=example",
                 "socks", "addstickers/Pack", "addemoji/Pack", "invoice/abc"]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(search_service.public_references("https://t.me/" + path), [])


class SearchRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.user = types.User(id=USER_ID, access_hash=12345, username=USERNAME)
        self.runner = search_service.SearchRunner(self.client, self.user)
        self.indexed = self._patch("search_service.indexed_references", return_value=[])
        self.sleep = self._patch("search_service.asyncio.sleep", new_callable=AsyncMock)
        self.urlopen = self._patch(
            "search_service.urlopen", side_effect=AssertionError("Network is forbidden in tests"),
        )

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def history(self, entity, *messages):
        self.client.histories[utils.get_peer_id(entity)] = list(messages)

    def web(self, entity, *messages):
        name = entity.username
        self.client.entities["@" + name] = entity
        for item in messages:
            self.client.posts[(utils.get_peer_id(entity), item.id)] = item
        self.indexed.return_value.extend((name, item.id) for item in messages)

    def assert_sender_filtered(self, cap=300):
        for entity, kwargs in self.client.scan_calls:
            with self.subTest(peer=utils.get_peer_id(entity)):
                self.assertIs(kwargs["from_user"], self.user)
                self.assertEqual(kwargs["limit"], cap + 1)
                self.assertNotIn("search", kwargs)

    async def test_external_channel_and_linked_discussion_without_membership_keep_private_group(self):
        public = channel(101, "outside_news", broadcast=True)
        discussion = channel(102, "outside_talk")
        private = group(103)
        self.client.dialogs = [private]
        self.client.respond(CONTACTS, contacts([public]))
        self.client.respond(FULL_CHANNEL, SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=discussion.id), chats=[public, discussion],
        ))
        for entity in (public, discussion, private):
            self.history(entity, message(entity, text="No nickname here; found by sender ID"))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 3)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["external_checked"], 2)
        self.assertEqual(result["external_found"], 2)
        self.assertEqual(len(result["public"]), 2)
        self.assertEqual(len(result["private"]), 1)
        self.assertEqual(result["private"][0]["source"], "dialogs")
        self.assertIsNone(result["private"][0]["messages"][0]["link"])
        discovered = {item["name"]: item for item in result["discovered"]}
        self.assertEqual(discovered[discussion.title]["source"], "discussion")
        self.assertEqual(discovered[public.title]["messages"][0]["link"], "https://t.me/outside_news/1")
        self.assert_sender_filtered()
        self.indexed.assert_called_once_with(USERNAME)
        self.urlopen.assert_not_called()

    async def test_joined_private_supergroup_is_preserved_but_left_private_dialogs_are_not(self):
        private = channel(110, None, left=False)
        departed = channel(111, None, left=True)
        departed_group = group(112, left=True)
        self.client.dialogs = [private, departed, departed_group, self.user]
        self.history(private, message(private))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(result["external_found"], 0)
        self.assertEqual(len(self.client.scan_calls), 1)
        self.assertEqual(result["private"][0]["messages"][0]["link"], "https://t.me/c/110/1")

    async def test_external_private_channels_basic_groups_and_users_are_rejected(self):
        private = channel(120, None)
        basic = group(121)
        user = types.User(id=122, username="public_person")
        self.client.respond(CONTACTS, contacts([private, basic], [user]))
        self.indexed.return_value = [("private_alias", 1), ("basic_alias", 1), ("public_person", 1)]
        self.client.entities.update({"@private_alias": private, "@basic_alias": basic, "@public_person": user})
        result = await self.runner.run()
        self.assertEqual(result["external_found"], 0)
        self.assertEqual(result["total_messages"], 0)
        self.assertEqual(self.client.scan_calls, [])
        self.assertEqual(self.client.message_calls, [])

    async def test_private_linked_discussion_and_recommendations_are_not_scanned(self):
        public = channel(130, broadcast=True)
        private = channel(131, None)
        self.client.respond(CONTACTS, contacts([public]))
        self.client.respond(RECOMMEND, SimpleNamespace(chats=[private]))
        self.client.respond(FULL_CHANNEL, SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=private.id), chats=[public, private],
        ))
        self.history(public, message(public))
        result = await self.runner.run()
        self.assertEqual(result["external_found"], 1)
        self.assertEqual([entity.id for entity, _ in self.client.scan_calls], [public.id])

    async def test_profile_channel_and_public_recommendation_are_discovered(self):
        personal = channel(140, "personal_news", broadcast=True)
        related = channel(141, "related_group")
        self.client.respond(PROFILE, SimpleNamespace(
            full_user=SimpleNamespace(personal_channel_id=personal.id), chats=[personal],
        ))
        self.client.respond(RECOMMEND, SimpleNamespace(chats=[related]))
        self.history(personal, message(personal))
        self.history(related, message(related))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual({c["source"] for c in result["discovered"]}, {"profile", "recommendations"})

    async def test_marked_peer_ids_keep_chat_channel_and_same_message_ids_distinct(self):
        private, public = group(150), channel(150)
        self.client.dialogs = [private]
        self.client.respond(POSTS, page([
            message(private, 7, sender=99, text=f"@{USERNAME}"),
            message(public, 7, sender=99, text=f"@{USERNAME}"),
        ], [private, public]), page())
        for entity in (private, public):
            self.history(entity, message(entity, 8))
        result = await self.runner.run()
        self.assertNotEqual(utils.get_peer_id(private), utils.get_peer_id(public))
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual(len(result["mentions"]), 2)
        self.assertEqual(result["external_found"], 1)
        self.assertEqual(result["external_checked"], 1)
        self.assertEqual(len(result["public"]), 1)
        self.assertEqual(len(result["private"]), 1)
        self.assertEqual(len(self.client.scan_calls), 2)

    async def test_scan_defensively_rejects_forwards_anonymous_signatures_and_mentions(self):
        public = channel()
        self.client.respond(CONTACTS, contacts([public]))
        self.history(
            public, message(public, 1),
            message(public, 2, fwd_from=types.MessageFwdHeader(date=NOW)),
            message(public, 3, sender=None, post_author=USERNAME),
            message(public, 4, sender=types.PeerChannel(USER_ID), post_author=USERNAME),
            message(public, 5, sender=99, text=f"Hi @{USERNAME}"),
            message(public, 6, sender=99, text=f"@{USERNAME}_not_the_target"),
        )
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual([m["id"] for m in result["chats"][0]["messages"]], [1])
        self.assertEqual([m["id"] for m in result["mentions"]], [5])
        self.assert_sender_filtered()

    async def test_mentions_survive_even_when_no_actual_authored_messages_exist(self):
        public = channel()
        mention = message(public, sender=99, text=f"@{USERNAME}")
        self.client.respond(POSTS, page([mention], [public]), page())
        self.web(public, mention)
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 0)
        self.assertEqual(result["chats"], [])
        self.assertEqual(result["public"], [])
        self.assertEqual(result["private"], [])
        self.assertEqual(result["discovered"], [])
        self.assertEqual(result["external_found"], 1)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(len(result["mentions"]), 1)
        self.assertEqual(result["mentions"][0]["link"], "https://t.me/public_news/1")

    async def test_self_mentions_are_counted_only_as_authored_and_replace_mention_seed(self):
        public = channel()
        seed = message(public, 1, sender=99, text=f"@{USERNAME}")
        self.client.respond(POSTS, page([seed], [public]), page())
        self.history(public, message(public, 1, text=f"@{USERNAME}"),
                     message(public, 2, sender=99, text=f"@{USERNAME}"))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual([m["id"] for m in result["mentions"]], [2])

    async def test_duplicate_seeds_and_scan_messages_do_not_duplicate_chats_or_totals(self):
        public = channel(left=False, broadcast=True)
        first, second = message(public, 1), message(public, 2)
        self.client.dialogs = [public, public]
        self.client.respond(POSTS, page([first, first], [public]), page())
        self.client.respond(CONTACTS, contacts([public, public]))
        self.client.respond(PROFILE, SimpleNamespace(
            full_user=SimpleNamespace(personal_channel_id=public.id), chats=[public],
        ))
        self.client.respond(RECOMMEND, SimpleNamespace(chats=[public, public]))
        self.web(public, first, first)
        self.history(public, first, second, first)
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual(len(result["chats"]), 1)
        self.assertEqual([m["id"] for m in result["chats"][0]["messages"]], [2, 1])
        self.assertEqual(result["chats"][0]["source"], "dialogs")
        self.assertEqual(result["external_found"], 0)
        self.assertEqual(len(self.client.scan_calls), 1)
        self.assertEqual(self.client.entity_calls, ["@public_news"])

    async def test_per_chat_cap_uses_one_extra_message_and_reports_truncation(self):
        public = channel()
        self.runner = search_service.SearchRunner(self.client, self.user, per_chat=2)
        self.client.respond(CONTACTS, contacts([public]))
        self.history(public, *(message(public, n) for n in (5, 4, 3, 2, 1)))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual([m["id"] for m in result["chats"][0]["messages"]], [5, 4])
        self.assertTrue(result["chats"][0]["limited"])
        self.assertTrue(result["notes"])
        self.assert_sender_filtered(cap=2)

    async def test_public_and_web_seeds_cannot_bypass_per_chat_cap(self):
        public = channel()
        self.runner = search_service.SearchRunner(self.client, self.user, per_chat=2)
        self.client.respond(POSTS, page([message(public, n) for n in (5, 4, 3)], [public]), page())
        self.web(public, message(public, 2))
        self.history(public, message(public, 1))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertTrue(result["chats"][0]["limited"])
        self.assert_sender_filtered(cap=2)

    async def test_exactly_per_chat_messages_is_not_reported_as_truncated(self):
        public = channel()
        self.runner = search_service.SearchRunner(self.client, self.user, per_chat=2)
        self.client.respond(CONTACTS, contacts([public]))
        self.history(public, message(public, 1), message(public, 2))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertFalse(result["chats"][0]["limited"])

    async def test_max_chats_interleaves_external_and_joined_sources(self):
        public, private, extra = channel(160), group(161), channel(162, "extra_public")
        self.runner = search_service.SearchRunner(self.client, self.user, max_chats=2)
        self.client.dialogs = [private]
        self.client.respond(CONTACTS, contacts([public, extra]))
        for entity in (public, private, extra):
            self.history(entity, message(entity))
        result = await self.runner.run()
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["external_checked"], 1)
        self.assertEqual(result["external_found"], 2)
        self.assertEqual([utils.get_peer_id(e) for e, _ in self.client.scan_calls],
                         [utils.get_peer_id(public), utils.get_peer_id(private)])
        self.assertTrue(result["notes"])

    async def test_exhausted_nonfree_quota_never_searches_or_pays_but_other_sources_continue(self):
        private = group()
        self.client.dialogs = [private]
        self.history(private, message(private))
        self.client.respond(CHECK, quota(free=False, remains=0))
        result = await self.runner.run()
        self.assertEqual(self.client.requests_of(POSTS), [])
        self.assertEqual(len(self.client.requests_of(CHECK)), 1)
        self.assertEqual(len(self.client.requests_of(CONTACTS)), 1)
        self.assertEqual(result["total_messages"], 1)
        self.assertTrue(result["notes"])

    async def test_free_query_can_run_with_zero_remaining_quota_without_payment(self):
        self.client.respond(CHECK, quota(free=True, remains=0))
        await self.runner.run()
        requests = self.client.requests_of(POSTS)
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0].allow_paid_stars)

    async def test_available_nonfree_quota_never_authorizes_stars(self):
        self.client.respond(CHECK, quota(free=False, remains=1))
        await self.runner.run()
        request = self.client.requests_of(POSTS)[0]
        self.assertIsNone(request.allow_paid_stars)
        self.assertEqual(self.client.requests_of(CHECK)[0].query, f"@{USERNAME}")

    async def test_remaining_pages_stay_free_after_daily_quota_is_consumed(self):
        public = channel()
        self.client.respond(CHECK, quota(free=False, remains=1))
        self.client.respond(POSTS, page(
            [message(public)], [public], next_rate=10,
            search_flood=quota(free=False, remains=0),
        ), page())
        await self.runner.public_posts()
        requests = self.client.requests_of(POSTS)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(self.client.requests_of(CHECK)), 1)
        self.assertTrue(all(request.allow_paid_stars is None for request in requests))
        self.assertEqual(self.runner.result()["total_messages"], 1)

    async def test_quota_check_failure_does_not_attempt_public_search(self):
        self.client.respond(CHECK, ChannelPrivateError(request=None))
        await self.runner.run()
        self.assertEqual(self.client.requests_of(POSTS), [])
        self.assertEqual(len(self.client.requests_of(CONTACTS)), 1)
        self.assertTrue(self.runner.result()["notes"])

    async def test_public_pagination_passes_rate_marked_peer_id_limit_and_no_payment(self):
        public = channel()
        first, second = message(public, 12), message(public, 11)
        self.client.respond(POSTS,
                            page([first, types.MessageEmpty(id=999, peer_id=utils.get_peer(public))], [public], next_rate=700),
                            page([second], [public], next_rate=701), page())
        await self.runner.public_posts()
        requests = self.client.requests_of(POSTS)
        self.assertEqual(len(requests), 3)
        self.assertEqual((requests[0].offset_rate, requests[0].offset_id), (0, 0))
        self.assertIsInstance(requests[0].offset_peer, types.InputPeerEmpty)
        for request, expected_id, rate in ((requests[1], 12, 700), (requests[2], 11, 701)):
            self.assertEqual((request.offset_id, request.offset_rate), (expected_id, rate))
            self.assertIsInstance(request.offset_peer, types.InputPeerChannel)
            self.assertEqual(request.offset_peer.channel_id, public.id)
            self.assertEqual(request.offset_peer.access_hash, public.access_hash)
        for request in requests:
            self.assertEqual(request.query, f"@{USERNAME}")
            self.assertEqual(request.limit, 100)
            self.assertIsNone(request.allow_paid_stars)
        self.assertEqual(self.runner.result()["total_messages"], 2)

    async def test_public_pagination_falls_back_to_last_message_timestamp(self):
        public = channel()
        last = message(public, 17)
        self.client.respond(POSTS, page([last], [public]), page())
        await self.runner.public_posts()
        request = self.client.requests_of(POSTS)[1]
        self.assertEqual(request.offset_rate, int(last.date.timestamp()))
        self.assertEqual(request.offset_id, last.id)

    async def test_repeated_public_cursor_stops_and_deduplicates_messages(self):
        public = channel()
        repeated = page([message(public)], [public], next_rate=123)
        self.client.respond(POSTS, repeated, repeated)
        await self.runner.public_posts()
        self.assertEqual(len(self.client.requests_of(POSTS)), 2)
        self.assertEqual(self.runner.result()["total_messages"], 1)
        self.assertTrue(self.runner.result()["notes"])

    async def test_public_page_missing_last_peer_entity_keeps_prior_seed_and_stops(self):
        public, missing = channel(), channel(999)
        self.client.respond(POSTS, page([message(public), message(missing)], [public]))
        await self.runner.public_posts()
        self.assertEqual(len(self.client.requests_of(POSTS)), 1)
        self.assertEqual(self.runner.result()["total_messages"], 1)
        self.assertTrue(self.runner.result()["notes"])

    async def test_empty_or_nonmessage_public_page_stops_without_cursor(self):
        self.client.respond(POSTS, page([types.MessageEmpty(id=1, peer_id=types.PeerChannel(100))]))
        await self.runner.public_posts()
        self.assertEqual(len(self.client.requests_of(POSTS)), 1)
        self.assertEqual(self.runner.result()["total_messages"], 0)

    async def test_public_pagination_has_five_page_ceiling(self):
        public = channel()
        self.client.respond(POSTS, *(page([message(public, n)], [public], next_rate=n)
                                    for n in range(1, 6)))
        await self.runner.public_posts()
        self.assertEqual(len(self.client.requests_of(POSTS)), 5)
        self.assertEqual(self.runner.result()["total_messages"], 5)
        self.assertTrue(self.runner.result()["notes"])

    async def test_partial_dialog_failure_keeps_joined_group_and_continues_discovery(self):
        private, public = group(), channel()
        self.client.dialogs = [private, ChannelPrivateError(request=None)]
        self.client.respond(CONTACTS, contacts([public]))
        self.history(private, message(private))
        self.history(public, message(public))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual(result["checked"], 2)
        self.assertTrue(result["notes"])

    async def test_discovery_request_failures_keep_public_seed_and_joined_results(self):
        private, public = group(), channel(broadcast=True)
        self.client.dialogs = [private]
        self.client.respond(POSTS, page([message(public)], [public]), ChannelPrivateError(request=None))
        for kind in (CONTACTS, PROFILE, RECOMMEND, FULL_CHANNEL):
            self.client.respond(kind, ChannelPrivateError(request=None))
        self.history(private, message(private))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual(result["checked"], 2)
        self.assertGreaterEqual(len(result["notes"]), 5)

    async def test_indexed_search_failure_is_not_fatal_and_never_calls_web(self):
        self.indexed.side_effect = URLError("offline")
        public = channel()
        self.client.respond(CONTACTS, contacts([public]))
        self.history(public, message(public))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertTrue(result["notes"])
        self.urlopen.assert_not_called()

    async def test_bad_web_resolution_is_cached_and_does_not_hide_later_sources(self):
        public = channel()
        self.indexed.return_value = [("missing_name", 1), ("missing_name", 2)]
        self.client.entities["@missing_name"] = ChannelPrivateError(request=None)
        self.web(public, message(public))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(self.client.entity_calls, ["@missing_name", "@public_news"])

    async def test_unavailable_indexed_message_keeps_other_messages_and_warns(self):
        public = channel()
        self.web(public, message(public, 1), message(public, 2))
        self.client.posts[(utils.get_peer_id(public), 1)] = ValueError("Deleted message")
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(result["chats"][0]["messages"][0]["id"], 2)
        self.assertTrue(result["notes"])

    async def test_partial_scan_failure_keeps_messages_and_scans_next_group(self):
        first, second = group(170), group(171)
        self.client.dialogs = [first, second]
        self.history(first, message(first), ChannelPrivateError(request=None))
        self.history(second, message(second))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["checked"], 1)
        self.assertTrue(result["notes"])

    async def test_discovery_timeout_still_scans_already_found_sources(self):
        private = group()
        self.client.dialogs = [private, asyncio.TimeoutError()]
        self.history(private, message(private))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(result["checked"], 1)
        self.assertTrue(result["notes"])

    async def test_floodwait_during_public_discovery_stops_without_sleep_or_retry(self):
        public = channel()
        self.client.respond(POSTS, page([message(public)], [public]),
                            FloodWaitError(request=None, capture=37))
        result = await self.runner.run()
        self.assertEqual(result["retry_after"], 37)
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(len(self.client.requests_of(POSTS)), 2)
        self.assertEqual(self.client.requests_of(CONTACTS), [])
        self.assertEqual(self.client.scan_calls, [])
        self.indexed.assert_not_called()
        self.sleep.assert_not_awaited()
        self.assertTrue(result["notes"])

    async def test_floodwait_during_scan_keeps_partial_messages_and_stops_next_chat(self):
        first, second = group(180), group(181)
        self.client.dialogs = [first, second]
        self.history(first, message(first), FloodWaitError(request=None, capture=29))
        self.history(second, message(second))
        result = await self.runner.run()
        self.assertEqual(result["retry_after"], 29)
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.client.scan_calls), 1)
        self.sleep.assert_not_awaited()

    async def test_floodwait_resolving_web_source_is_not_retried(self):
        self.indexed.return_value = [("public_news", 1), ("other_news", 1)]
        self.client.entities["@public_news"] = FloodWaitError(request=None, capture=19)
        result = await self.runner.run()
        self.assertEqual(result["retry_after"], 19)
        self.assertEqual(self.client.entity_calls, ["@public_news"])
        self.assertEqual(self.client.message_calls, [])
        self.assertEqual(self.client.requests_of(CONTACTS), [])
        self.sleep.assert_not_awaited()

    async def test_floodwait_fetching_indexed_message_keeps_earlier_seed_without_retry(self):
        public = channel()
        self.web(public, message(public, 1), message(public, 2), message(public, 3))
        self.client.posts[(utils.get_peer_id(public), 2)] = FloodWaitError(request=None, capture=23)
        result = await self.runner.run()
        self.assertEqual(result["retry_after"], 23)
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(len(self.client.message_calls), 2)
        self.assertEqual(self.client.scan_calls, [])
        self.sleep.assert_not_awaited()

    async def test_user_without_username_still_scans_by_id_and_skips_nickname_discovery(self):
        self.user = types.User(id=USER_ID, access_hash=12345)
        self.runner = search_service.SearchRunner(self.client, self.user)
        private = group()
        self.client.dialogs = [private]
        self.history(private, message(private))
        result = await self.runner.run()
        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(self.client.requests_of(CHECK), [])
        self.assertEqual(self.client.requests_of(CONTACTS), [])
        self.indexed.assert_not_called()
        self.assert_sender_filtered()


if __name__ == "__main__":
    unittest.main()
