from __future__ import annotations

import os
import sys
import random
import logging
from pathlib import Path
from copy import deepcopy
from enum import Enum, auto
from datetime import timedelta
from typing import Any, Dict, Literal, NewType, TYPE_CHECKING

from yarl import URL

from version import __version__

if TYPE_CHECKING:
    from collections import abc  # noqa
    from typing_extensions import TypeAlias


# True if we're running from a built EXE (or a Linux AppImage), False inside a dev build
IS_APPIMAGE = "APPIMAGE" in os.environ and os.path.exists(os.environ["APPIMAGE"])
IS_PACKAGED = hasattr(sys, "_MEIPASS") or IS_APPIMAGE
# logging special levels
CALL: int = logging.INFO - 1
logging.addLevelName(CALL, "CALL")
# site-packages venv path changes depending on the system platform
if sys.platform == "win32":
    SYS_SITE_PACKAGES = "Lib/site-packages"
else:
    # On Linux, the site-packages path includes a versioned 'pythonX.Y' folder part
    # The Lib folder is also spelled in lowercase: 'lib'
    version_info = sys.version_info
    SYS_SITE_PACKAGES = f"lib/python{version_info.major}.{version_info.minor}/site-packages"
# scripts venv path changes depending on the system platform
if sys.platform == "win32":
    SYS_SCRIPTS = "Scripts"
else:
    SYS_SCRIPTS = "bin"


def _resource_path(relative_path: Path | str) -> Path:
    """
    Get an absolute path to a bundled resource.

    Works for dev and for PyInstaller.
    """
    if IS_APPIMAGE:
        base_path = Path(sys.argv[0]).resolve().parent
    elif IS_PACKAGED:
        # PyInstaller's folder where the one-file app is unpacked
        meipass: str = getattr(sys, "_MEIPASS")
        base_path = Path(meipass)
    else:
        base_path = WORKING_DIR
    return base_path.joinpath(relative_path)


def _merge_vars(base_vars: JsonType, vars: JsonType) -> None:
    # NOTE: This modifies base in place
    for k, v in vars.items():
        if k not in base_vars:
            base_vars[k] = v
        elif isinstance(v, dict):
            if isinstance(base_vars[k], dict):
                _merge_vars(base_vars[k], v)
            elif base_vars[k] is Ellipsis:
                # unspecified base, use the passed in var
                base_vars[k] = v
            else:
                raise RuntimeError(f"Var is a dict, base is not: '{k}'")
        elif isinstance(base_vars[k], dict):
            raise RuntimeError(f"Base is a dict, var is not: '{k}'")
        else:
            # simple overwrite
            base_vars[k] = v
    # ensure none of the vars are ellipsis (unset value)
    for k, v in base_vars.items():
        if v is Ellipsis:
            raise RuntimeError(f"Unspecified variable: '{k}'")


# Base Paths
if IS_APPIMAGE:
    SELF_PATH = Path(os.environ["APPIMAGE"]).resolve()
else:
    # NOTE: pyinstaller will set sys.argv[0] to its own executable when building
    # NOTE: sys.argv[0] will point to gui.py when running the gui.py directly for GUI debug
    # detect these and use __file__ and main.py redirection instead
    SELF_PATH = Path(sys.argv[0]).resolve()
    if SELF_PATH.stem == "pyinstaller" or SELF_PATH.name == "gui.py":
        SELF_PATH = Path(__file__).with_name("main.py").resolve()
WORKING_DIR = SELF_PATH.parent
# Development paths
VENV_PATH = Path(WORKING_DIR, "env")
SITE_PACKAGES_PATH = Path(VENV_PATH, SYS_SITE_PACKAGES)
SCRIPTS_PATH = Path(VENV_PATH, SYS_SCRIPTS)
# Translations path
# NOTE: These don't have to be available to the end-user, so the path points to the internal dir
LANG_PATH = _resource_path("lang")
# Other Paths
LOG_PATH = Path(WORKING_DIR, "log.txt")
DUMP_PATH = Path(WORKING_DIR, "dump.dat")
LOCK_PATH = Path(WORKING_DIR, "lock.file")
CACHE_PATH = Path(WORKING_DIR, "cache")
CACHE_DB = Path(CACHE_PATH, "mapping.json")
COOKIES_PATH = Path(WORKING_DIR, "cookies.jar")
SCAN_PATH = Path(WORKING_DIR, "drops_scan.json")
GAMES_CATALOGUE_PATH = Path(WORKING_DIR, "games_catalogue.json")
# Games shipped with the app: every title a drops campaign has ever been seen for. The scan
# has no way to ask Twitch which games carry drops, so it starts from this list and grows it.
GAMES_SEED_PATH = _resource_path("games_seed.json")
# The seed is refreshed from the fork's repository, so the game list can be updated without a
# new build: the bundled copy is only the fallback for a first start or a machine without network.
# The feeds live on their own branch: it carries data only, so publishing them can never drag
# half-finished code into the branch the builds come from.
GAMES_SEED_URL = "https://raw.githubusercontent.com/RoninOnis/TwitchDropsMiner/feeds/games_seed.json"
GAMES_SEED_CACHE_PATH = Path(WORKING_DIR, "games_seed.downloaded.json")
# Known campaigns, published by the maintainer: a game with a campaign that is running or
# starting soon is probed deep on every scan, so its drops show up without waiting for the
# slice rotation to reach it
CAMPAIGNS_URL = "https://raw.githubusercontent.com/RoninOnis/TwitchDropsMiner/feeds/campaigns.json"
CAMPAIGNS_CACHE_PATH = Path(WORKING_DIR, "campaigns.downloaded.json")
# how far ahead a campaign counts as "coming up" when reading the feed, in days
CAMPAIGNS_LOOKAHEAD_DAYS: int = 3
# Drop scan refresh interval, in minutes, as offered in the settings
SCAN_INTERVAL_CHOICES: tuple[int, ...] = (5, 10, 30, 60)
DEFAULT_SCAN_INTERVAL_MINUTES: int = 30
# How many live channels of a game the scan asks about. Twitch only reports a campaign
# on some of the channels streaming it (measured: 5 out of 60 on a busy game), so a
# shallow probe misses the campaign on most scans.
SCAN_CHANNELS_PER_GAME: int = 10
# ... games the user actually wants get a much deeper probe, since a missed campaign
# there is the difference between mining the drops and not seeing them at all
SCAN_PRIORITY_CHANNELS_PER_GAME: int = 50
# Games from the published campaign list. Deep enough to catch a campaign, without
# asking 50 channels of every one of them before the inventory can show anything.
SCAN_FEED_CHANNELS_PER_GAME: int = 20
# Games of the catalogue probed per scan, and category-search pages fetched per scan
SCAN_CATALOGUE_SLICE: int = 150
SCAN_SEED_REQUESTS: int = 5
# Bootstrap: while the catalogue holds fewer games than this, every scan grows it and
# probes it much harder, so a fresh install reaches usable coverage in minutes
SCAN_BOOTSTRAP_CATALOGUE: int = 1500
# A game that ran a campaign within this many days is probed before the long tail
SCAN_KNOWN_DROPPER_DAYS: int = 14
# Per scan, how many games that have live drops-enabled channels but reported no campaign
# are asked again with the deeper channel probe
SCAN_ESCALATION_GAMES: int = 60
SCAN_BOOTSTRAP_SLICE: int = 60
SCAN_BOOTSTRAP_SEED_REQUESTS: int = 8
# Games that never had any drops are dropped from the catalogue after this many days
CATALOGUE_KEEP_DAYS: int = 30
# The catalogue is grown by asking Twitch's category search for these terms, a few pages
# per scan: nothing on Twitch lists the games that carry drops, and the popular games list
# never reaches the niche titles.
# NOTE: the search returns 10 games per query and ignores both cursor and limit, so the
# terms have to differ for the harvest to bring in anything new - two-letter combinations
# yield almost no duplicates, while the first pages of a single letter are the popular
# games the scan already knows about.
_ALNUM: str = "abcdefghijklmnopqrstuvwxyz0123456789"
CATALOGUE_SEED_QUERIES: tuple[str, ...] = tuple(
    first + second for first in _ALNUM for second in _ALNUM
) + tuple(_ALNUM)
SETTINGS_PATH = Path(WORKING_DIR, "settings.json")
# Pseudo-game carrying the drops that individual channels offer (badges, emotes, sub
# rewards). Not a real Twitch game: it exists so these campaigns can reuse the normal
# campaign machinery, with their ACL pinning them to the offering channel.
CHANNEL_DROPS_GAME_ID: int = -1
CHANNEL_DROPS_GAME_NAME: str = "Drops on channels"
# Typing
JsonType = Dict[str, Any]
URLType = NewType("URLType", str)
GQLOperation: TypeAlias = "GQLQuery | GQLPersistedQuery"
TopicProcess: TypeAlias = "abc.Callable[[int, JsonType], Any]"
# Values
MAX_INT = sys.maxsize
MAX_EXTRA_MINUTES = 15
# A campaign that can still be finished, but with less than this much spare time,
# is farmed before everything else. The spare time is what is left after the watch
# time for every remaining drop.
URGENT_BUFFER_MINUTES = 30
BASE_TOPICS = 2
MAX_WEBSOCKETS = 8
WS_TOPICS_LIMIT = 50
TOPICS_PER_CHANNEL = 2
MAX_TOPICS = (MAX_WEBSOCKETS * WS_TOPICS_LIMIT) - BASE_TOPICS
MAX_CHANNELS = MAX_TOPICS // TOPICS_PER_CHANNEL
# Misc
DEFAULT_LANG = "English"
# Intervals and Delays
PING_INTERVAL = timedelta(minutes=3)
PING_TIMEOUT = timedelta(seconds=10)
ONLINE_DELAY = timedelta(seconds=120)
WATCH_INTERVAL = timedelta(seconds=20)
# Strings
WINDOW_TITLE = f"Twitch Drops Miner v{__version__}"
# Logging
LOGGING_LEVELS = {
    0: logging.ERROR,
    1: logging.WARNING,
    2: logging.INFO,
    3: CALL,
    4: logging.DEBUG,
}
FILE_FORMATTER = logging.Formatter(
    "{asctime}.{msecs:03.0f}:\t{levelname:>7}:\t{message}",
    style='{',
    datefmt="%Y-%m-%d %H:%M:%S",
)
OUTPUT_FORMATTER = logging.Formatter("{levelname}: {message}", style='{', datefmt="%H:%M:%S")


class ClientInfo:
    def __init__(self, client_url: URL, client_id: str, user_agents: str | list[str]) -> None:
        self.CLIENT_URL: URL = client_url
        self.CLIENT_ID: str = client_id
        self.USER_AGENT: str
        if isinstance(user_agents, list):
            self.USER_AGENT = random.choice(user_agents)
        else:
            self.USER_AGENT = user_agents

    def __iter__(self):
        return iter((self.CLIENT_URL, self.CLIENT_ID, self.USER_AGENT))


class ClientType:
    WEB = ClientInfo(
        URL("https://www.twitch.tv"),
        "kimne78kx3ncx6brgo4mv6wki5h1ko",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
        ),
    )
    MOBILE_WEB = ClientInfo(
        URL("https://m.twitch.tv"),
        "r8s4dac0uhzifbpu9sjdiwzctle17ff",
        [
            # Chrome versioning is done fully on android only,
            # other platforms only use the major version
            (
                "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 16; SM-A205U) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 16; SM-A102U) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 16; SM-G960U) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 16; SM-N960U) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 16; LM-Q720) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Linux; Android 16; LM-X420) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.7204.158 Mobile Safari/537.36"
            ),
        ]
    )
    ANDROID_APP = ClientInfo(
        URL("https://www.twitch.tv"),
        "kd1unb4b3q4t58fwlpcbzcbnm76a8fp",
        [
            (
                "Dalvik/2.1.0 (Linux; U; Android 16; SM-S911B Build/TP1A.220624.014) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
            (
                "Dalvik/2.1.0 (Linux; U; Android 16; SM-S938B Build/BP2A.250605.031) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
            (
                "Dalvik/2.1.0 (Linux; Android 16; SM-X716N Build/UP1A.231005.007) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
            (
                "Dalvik/2.1.0 (Linux; U; Android 15; SM-G990B Build/AP3A.240905.015.A2) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
            (
                "Dalvik/2.1.0 (Linux; U; Android 15; SM-G970F Build/AP3A.241105.008) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
            (
                "Dalvik/2.1.0 (Linux; U; Android 15; SM-A566E Build/AP3A.240905.015.A2) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
            (
                "Dalvik/2.1.0 (Linux; U; Android 14; SM-X306B Build/UP1A.231005.007) "
                "tv.twitch.android.app/25.3.0/2503006"
            ),
        ]
    )
    SMARTBOX = ClientInfo(
        URL("https://android.tv.twitch.tv"),
        "ue6666qo983tsx6so1t0vnawi233wa",
        (
            "Mozilla/5.0 (Linux; Android 7.1; Smart Box C1) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
        )
    )

    @classmethod
    def as_dict(cls) -> dict[str, ClientInfo]:
        return {
            "WEB": cls.WEB,
            "MOBILE_WEB": cls.MOBILE_WEB,
            "ANDROID_APP": cls.ANDROID_APP,
            "SMARTBOX": cls.SMARTBOX,
        }


# Twitch invalidates client IDs from time to time and the app then has to move on to
# another one: the Android app client stopped being accepted by the device code flow in
# September 2026 (`invalid client` on every login attempt). The client is therefore a
# setting, not a constant, and every known client is tried in turn during the login
# process - so a future rotation is a config change instead of a code change.
CLIENT_TYPE_ORDER: tuple[str, ...] = ("SMARTBOX", "ANDROID_APP", "MOBILE_WEB", "WEB")
DEFAULT_CLIENT_TYPE: str = "SMARTBOX"


class State(Enum):
    IDLE = auto()
    INVENTORY_FETCH = auto()
    GAMES_UPDATE = auto()
    CHANNELS_FETCH = auto()
    CHANNELS_CLEANUP = auto()
    CHANNEL_SWITCH = auto()
    RESTART = auto()
    EXIT = auto()


class PriorityMode(Enum):
    PRIORITY_ONLY = 0
    ENDING_SOONEST = 1
    LOW_AVBL_FIRST = 2


class GQLQuery(JsonType):
    def __init__(self, query: str, g64data: str):
        super().__init__(
            query=query,
            variables={
                "input": {
                    "data": g64data,
                    "repository": "twilight",
                    "encoding": "GZIP_B64",
                }
            }
        )


class GQLPersistedQuery(JsonType):
    def __init__(self, name: str, sha256: str, *, variables: JsonType | None = None):
        super().__init__(
            operationName=name,
            extensions={
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": sha256,
                }
            }
        )
        if variables is not None:
            self.__setitem__("variables", variables)

    def with_variables(self, variables: JsonType) -> GQLPersistedQuery:
        modified = deepcopy(self)
        if "variables" in self:
            existing_variables: JsonType = modified["variables"]
            _merge_vars(existing_variables, variables)
        else:
            modified["variables"] = variables
        return modified


GQL_QUERIES: dict[str, GQLPersistedQuery] = {
    # returns stream information for a particular channel
    "GetStreamInfo": GQLPersistedQuery(
        "VideoPlayerStreamInfoOverlayChannel",
        "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
        variables={
            "channel": ...,  # channel login
        },
    ),
    # can be used to claim channel points
    "ClaimCommunityPoints": GQLPersistedQuery(
        "ClaimCommunityPoints",
        "46aaeebe02c99afdf4fc97c7c0cba964124bf6b0af229395f1f6d1feed05b3d0",
        variables={
            "input": {
                "claimID": ...,  # points claim_id
                "channelID": ...,  # channel ID as a str
            },
        },
    ),
    # can be used to claim a drop
    "ClaimDrop": GQLPersistedQuery(
        "DropsPage_ClaimDropRewards",
        "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
        variables={
            "input": {
                "dropInstanceID": ...,  # drop claim_id
            },
        },
    ),
    # returns current state of points (balance, claim available) for a particular channel
    "ChannelPointsContext": GQLPersistedQuery(
        "ChannelPointsContext",
        "374314de591e69925fce3ddc2bcf085796f56ebb8cad67a0daa3165c03adc345",
        variables={
            "channelLogin": ...,  # channel login
        },
    ),
    # returns all in-progress campaigns
    "Inventory": GQLPersistedQuery(
        "Inventory",
        "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
        variables={
            "fetchRewardCampaigns": False,
        }
    ),
    # returns current state of drops (current drop progress)
    "CurrentDrop": GQLPersistedQuery(
        "DropCurrentSessionContext",
        "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        variables={
            "channelID": ...,  # watched channel ID as a str
            "channelLogin": "",  # always empty string
        },
    ),
    # returns all available campaigns
    "Campaigns": GQLPersistedQuery(
        "ViewerDropsDashboard",
        "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb",
        variables={
            "fetchRewardCampaigns": False,
        }
    ),
    # returns extended information about a particular campaign
    "CampaignDetails": GQLPersistedQuery(
        "DropCampaignDetails",
        "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1",
        variables={
            "channelLogin": ...,  # user login
            "dropID": ...,  # campaign ID
        },
    ),
    # returns drops available for a particular channel
    "AvailableDrops": GQLPersistedQuery(
        "DropsHighlightService_AvailableDrops",
        "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
        variables={
            "channelID": ...,  # channel ID as a str
        },
    ),
    # searches games by name, used to grow the scan's games catalogue
    # NOTE: unlike the campaigns queries, this one is not behind an integrity check
    "SearchCategories": GQLPersistedQuery(
        "SearchResultsPage_SearchResults",
        "075d5e21955b5be5580afaf7d4bf5a7b6392379d38a40995a86f0d5794ad3e01",
        variables={
            "platform": "web",
            "query": ...,  # search term
            "options": {"targets": None, "shouldSkipDiscoveryControl": False},
            "requestID": ...,  # random UUID as a str
        },
    ),
    # retuns stream playback access token
    "PlaybackAccessToken": GQLPersistedQuery(
        "PlaybackAccessToken",
        "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
        variables={
            "isLive": True,
            "isVod": False,
            "login": ...,  # channel login
            "platform": "web",
            "playerType": "site",
            "vodID": "",
        },
    ),
    # returns live channels for a particular game
    "GameDirectory": GQLPersistedQuery(
        "DirectoryPage_Game",
        "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd",
        variables={
            "limit": 30,  # limit of channels returned
            "slug": ...,  # game slug
            "imageWidth": 50,
            "includeCostreaming": False,
            "options": {
                "broadcasterLanguages": [],
                "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "RELEVANCE",  # also accepted: "VIEWER_COUNT"
                "systemFilters": [],
                "tags": [],
                "requestID": "JIRA-VXP-2397",
            },
            "sortTypeIsRecency": False,
        },
    ),
    "SlugRedirect": GQLPersistedQuery(  # can be used to turn game name -> game slug
        "DirectoryGameRedirect",
        "1f0300090caceec51f33c5e20647aceff9017f740f223c3c532ba6fa59f6b6cc",
        variables={
            "name": ...,  # game name
        },
    ),
    "NotificationsView": GQLPersistedQuery(  # unused, triggers notifications "update-summary"
        "OnsiteNotifications_View",
        "e8e06193f8df73d04a1260df318585d1bd7a7bb447afa058e52095513f2bfa4f",
        variables={
            "input": {},
        },
    ),
    "NotificationsList": GQLPersistedQuery(  # unused
        "OnsiteNotifications_ListNotifications",
        "11cdb54a2706c2c0b2969769907675680f02a6e77d8afe79a749180ad16bfea6",
        variables={
            "cursor": "",
            "displayType": "VIEWER",
            "language": "en",
            "limit": 10,
            "shouldLoadLastBroadcast": False,
        },
    ),
    "NotificationsDelete": GQLPersistedQuery(
        "OnsiteNotifications_DeleteNotification",
        "13d463c831f28ffe17dccf55b3148ed8b3edbbd0ebadd56352f1ff0160616816",
        variables={
            "input": {
                "id": "",  # ID of the notification to delete
            }
        },
    ),
}


class WebsocketTopic:
    def __init__(
        self,
        category: Literal["User", "Channel"],
        topic_name: str,
        target_id: int,
        process: TopicProcess,
    ):
        assert isinstance(target_id, int)
        self._id: str = self.as_str(category, topic_name, target_id)
        self._target_id = target_id
        self._process: TopicProcess = process

    @classmethod
    def as_str(
        cls, category: Literal["User", "Channel"], topic_name: str, target_id: int
    ) -> str:
        return f"{WEBSOCKET_TOPICS[category][topic_name]}.{target_id}"

    def __call__(self, message: JsonType):
        return self._process(self._target_id, message)

    def __str__(self) -> str:
        return self._id

    def __repr__(self) -> str:
        return f"Topic({self._id})"

    def __eq__(self, other) -> bool:
        if isinstance(other, WebsocketTopic):
            return self._id == other._id
        elif isinstance(other, str):
            return self._id == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.__class__.__name__, self._id))


WEBSOCKET_TOPICS: dict[str, dict[str, str]] = {
    "User": {  # Using user_id
        "Presence": "presence",  # unused
        "Drops": "user-drop-events",
        "Notifications": "onsite-notifications",
        "CommunityPoints": "community-points-user-v1",
    },
    "Channel": {  # Using channel_id
        "Drops": "channel-drop-events",  # unused
        "StreamState": "video-playback-by-id",
        "StreamUpdate": "broadcast-settings-update",
        "CommunityPoints": "community-points-channel-v1",  # unused
    },
}
