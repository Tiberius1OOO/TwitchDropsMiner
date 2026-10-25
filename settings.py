from __future__ import annotations

import json
from typing import Any, TypedDict, TYPE_CHECKING

from yarl import URL

from utils import json_load, json_save
from constants import (
    SETTINGS_PATH,
    DEFAULT_LANG,
    DEFAULT_CLIENT_TYPE,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    SCAN_CHANNELS_PER_GAME,
    SCAN_PRIORITY_CHANNELS_PER_GAME,
    SCAN_CATALOGUE_SLICE,
    SCAN_SEED_REQUESTS,
    SCAN_BOOTSTRAP_SLICE,
    SCAN_BOOTSTRAP_SEED_REQUESTS,
    SCAN_INTERVAL_CHOICES,
    PriorityMode,
)

if TYPE_CHECKING:
    from main import ParsedArgs


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    dark_mode: bool
    theme: str
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    close_to_tray: bool
    connection_quality: int
    tray_notifications: bool
    enable_badges_emotes: bool
    available_drops_check: bool
    priority_mode: PriorityMode
    client_type: str
    campaign_discovery: bool
    scan_drops: bool
    scan_interval_minutes: int
    scan_top_games: int
    scan_channels_per_game: int
    scan_priority_channels: int
    scan_catalogue_slice: int
    scan_seed_requests: int
    scan_bootstrap_slice: int
    scan_bootstrap_seed_requests: int
    seed_auto_update: bool
    seed_update_hours: int
    channel_campaigns: str
    channel_campaigns_limit: int


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "dark_mode": False,
    # "" follows dark_mode, otherwise one of the names in gui.THEMES
    "theme": "",
    "autostart_tray": False,
    "close_to_tray": False,
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "tray_notifications": True,
    "enable_badges_emotes": False,
    # NOTE: off by default since 16.2.4 - the extra check costs a request per channel and the
    # options in this section are meant to stay off unless something needs them
    "available_drops_check": False,
    "priority_mode": PriorityMode.PRIORITY_ONLY,
    "client_type": DEFAULT_CLIENT_TYPE,
    "campaign_discovery": True,
    # how the games that have drops are found (Twitch stopped listing campaigns at all)
    "scan_drops": True,
    "scan_interval_minutes": DEFAULT_SCAN_INTERVAL_MINUTES,
    "scan_top_games": 120,
    # channels asked per game; Twitch reports a campaign on only a few of the channels
    # streaming it, so the games being farmed get a deep probe and the rest a shallow one
    "scan_channels_per_game": SCAN_CHANNELS_PER_GAME,
    "scan_priority_channels": SCAN_PRIORITY_CHANNELS_PER_GAME,
    # the catalogue grows from the category search; per scan only this many of its games
    # are probed (the ones checked longest ago first) and this many search pages fetched
    "scan_catalogue_slice": SCAN_CATALOGUE_SLICE,
    "scan_seed_requests": SCAN_SEED_REQUESTS,
    # used instead of the two above while the games catalogue is still being filled
    "scan_bootstrap_slice": SCAN_BOOTSTRAP_SLICE,
    "scan_bootstrap_seed_requests": SCAN_BOOTSTRAP_SEED_REQUESTS,
    # the games seed is refreshed from the repository, this is how often at most
    "seed_auto_update": True,
    "seed_update_hours": 1,
    # "off" | "relevant" (channels of the games in the priority list and of the games
    # already being mined) | "all" (every watch-based channel campaign found)
    # NOTE: off by default - every channel campaign seen so far is a subscriber reward
    # campaign (isRewardCampaign), and Twitch does not credit watch minutes for those
    # unless the account is subscribed to the channel. Turn on to try, if subscribed.
    "channel_campaigns": "off",
    "channel_campaigns_limit": 50,
}


class Settings:
    # from args
    log: bool
    tray: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    dark_mode: bool
    theme: str
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    close_to_tray: bool
    connection_quality: int
    tray_notifications: bool
    enable_badges_emotes: bool
    available_drops_check: bool
    priority_mode: PriorityMode
    client_type: str
    campaign_discovery: bool
    scan_drops: bool
    scan_interval_minutes: int
    scan_top_games: int
    scan_channels_per_game: int
    scan_priority_channels: int
    scan_catalogue_slice: int
    scan_seed_requests: int
    scan_bootstrap_slice: int
    scan_bootstrap_seed_requests: int
    seed_auto_update: bool
    seed_update_hours: int
    channel_campaigns: str
    channel_campaigns_limit: int

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False
        self._migrate()

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(SETTINGS_PATH, self._settings, sort=True)

    def _migrate(self) -> None:
        # pre-16.2 kept the drop scan interval in hours, and offered nothing else.
        # NOTE: the settings loader drops the keys it doesn't know, so the old value has
        # to be read from the file directly.
        try:
            with open(SETTINGS_PATH, encoding="utf8") as settings_file:
                raw_settings: dict[str, Any] = json.load(settings_file)
        except (OSError, ValueError):
            raw_settings = {}
        old_hours = raw_settings.get("scan_interval_hours")
        if old_hours is not None and raw_settings.get("scan_interval_minutes") is None:
            # keep the same rhythm if the new list allows it, the longest one otherwise
            minutes = max(1, int(old_hours)) * 60
            self._settings["scan_interval_minutes"] = max(
                (choice for choice in SCAN_INTERVAL_CHOICES if choice <= minutes),
                default=min(SCAN_INTERVAL_CHOICES),
            )
            self._altered = True
        # the interval has to be one of the offered ones
        if self._settings["scan_interval_minutes"] not in SCAN_INTERVAL_CHOICES:
            self._settings["scan_interval_minutes"] = DEFAULT_SCAN_INTERVAL_MINUTES
            self._altered = True
        # The campaigns list is published hourly since 16.2.4, and 24 hours was the old
        # default for how often to fetch it: keeping it would leave an upgraded install a day
        # behind. Not exposed in the GUI, so a stored 24 is the old default, not a choice.
        if int(self._settings.get("seed_update_hours") or 0) == 24:
            self._settings["seed_update_hours"] = 1
            self._altered = True
