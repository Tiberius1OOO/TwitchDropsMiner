from __future__ import annotations

from collections import abc
from typing import Any, TypedDict, TYPE_CHECKING

from exceptions import MinerException
from utils import json_load, json_save
from constants import IS_PACKAGED, LANG_PATH, DEFAULT_LANG

if TYPE_CHECKING:
    from typing_extensions import NotRequired


class StatusMessages(TypedDict):
    terminated: str
    watching: str
    goes_online: str
    goes_offline: str
    claimed_drop: str
    no_channel: str
    no_campaign: str


class ChromeMessages(TypedDict):
    startup: str
    login_to_complete: str
    no_token: str
    closed_window: str


class LoginMessages(TypedDict):
    chrome: ChromeMessages
    error_code: str
    unexpected_content: str
    email_code_required: str
    twofa_code_required: str
    incorrect_login_pass: str
    incorrect_email_code: str
    incorrect_twofa_code: str


class ErrorMessages(TypedDict):
    captcha: str
    no_connection: str
    site_down: str


class GUIStatus(TypedDict):
    name: str
    idle: str
    exiting: str
    terminated: str
    cleanup: str
    gathering: str
    switching: str
    fetching_inventory: str
    fetching_campaigns: str
    adding_campaigns: str


class GUITabs(TypedDict):
    main: str
    inventory: str
    settings: str
    help: str


class GUITray(TypedDict):
    notification_title: str
    minimize: str
    show: str
    quit: str
    still_running: str


class GUILoginForm(TypedDict):
    name: str
    labels: str
    logging_in: str
    logged_in: str
    logged_out: str
    logged_in_as: str
    request: str
    required: str
    username: str
    password: str
    twofa_code: str
    button: str
    logout: str
    device_code: str


class GUIWebsocket(TypedDict):
    name: str
    websocket: str
    initializing: str
    connected: str
    disconnected: str
    connecting: str
    disconnecting: str
    reconnecting: str


class GUIProgress(TypedDict):
    name: str
    drop: str
    game: str
    campaign: str
    remaining: str
    drop_progress: str
    campaign_progress: str
    campaign_percentage: str


class GUIChannelHeadings(TypedDict):
    channel: str
    status: str
    game: str
    drops: str
    farm: str
    viewers: str


class GUIChannels(TypedDict):
    name: str
    switch: str
    online: str
    pending: str
    offline: str
    headings: GUIChannelHeadings


class GUIInvFilter(TypedDict):
    name: str
    show: str
    linked: str
    not_linked: str
    upcoming: str
    expired: str
    excluded: str
    finished: str
    refresh: str
    help: str


class GUIInvStatus(TypedDict):
    linked: str
    not_linked: str
    active: str
    expired: str
    upcoming: str
    claimed: str
    ready_to_claim: str


class GUIInventory(TypedDict):
    filter: GUIInvFilter
    status: GUIInvStatus
    starts: str
    ends: str
    allowed_channels: str
    all_channels: str
    and_more: str
    percent_progress: str
    minutes_progress: str
    farm: str
    hours_left: str
    hours_needed: str
    days_left: str
    days_needed: str
    starts_in_hours: str
    starts_in_days: str
    farm_hint: str


class GUISettingsGeneral(TypedDict):
    name: str
    autostart: str
    tray: str
    close_to_tray: str
    close_to_tray_note: str
    tray_notifications: str
    dark_mode: str
    priority_mode: str
    proxy: str


class GUISettingsAdvanced(TypedDict):
    name: str
    warning: str
    warning_text: str
    enable_badges_emotes: str
    available_drops_check: str


class GUIThemes(TypedDict):
    classic: str
    dark: str
    graphite: str
    light_accent: str
    compact: str


class GUIPriorityModes(TypedDict):
    priority_only: str
    ending_soonest: str
    low_availability: str


class GUISettings(TypedDict):
    general: GUISettingsGeneral
    advanced: GUISettingsAdvanced
    priority_modes: GUIPriorityModes
    themes: GUIThemes
    game_name: str
    priority: str
    exclude: str
    reload: str
    reload_text: str


class GUIHelpLinks(TypedDict):
    name: str
    inventory: str
    campaigns: str


class GUIHelpInvalidate(TypedDict):
    button: str
    text: str


class GUIHelp(TypedDict):
    links: GUIHelpLinks
    channels: str
    channels_text: str
    how_it_works: str
    how_it_works_text: str
    getting_started: str
    getting_started_text: str
    invalidate: GUIHelpInvalidate


class GUIMessages(TypedDict):
    output: str
    status: GUIStatus
    tabs: GUITabs
    tray: GUITray
    login: GUILoginForm
    websocket: GUIWebsocket
    progress: GUIProgress
    channels: GUIChannels
    inventory: GUIInventory
    settings: GUISettings
    help: GUIHelp


class Translation(TypedDict):
    language_name: NotRequired[str]
    english_name: str
    status: StatusMessages
    login: LoginMessages
    error: ErrorMessages
    gui: GUIMessages


default_translation: Translation = {
    "english_name": "English",
    "status": {
        "terminated": "\nApplication Terminated.\nClose the window to exit the application.",
        "watching": "Watching: {channel}",
        "goes_online": "{channel} goes ONLINE, switching...",
        "goes_offline": "{channel} goes OFFLINE, switching...",
        "claimed_drop": "Claimed drop: {drop}",
        "no_channel": "No available channels to watch. Waiting for an ONLINE channel...",
        "no_campaign": "No active campaigns to mine drops for. Waiting for an active campaign...",
    },
    "login": {
        "unexpected_content": (
            "Unexpected content type returned, usually due to being redirected. "
            "Do you need to login for internet access?"
        ),
        "chrome": {
            "startup": "Opening Chrome...",
            "login_to_complete": (
                "Complete the login procedure manually by pressing the Login button again."
            ),
            "no_token": "No authorization token could be found.",
            "closed_window": (
                "The Chrome window was closed before the login procedure could be completed."
            ),
        },
        "error_code": "Login error code: {error_code}",
        "incorrect_login_pass": "Incorrect username or password.",
        "incorrect_email_code": "Incorrect email code.",
        "incorrect_twofa_code": "Incorrect 2FA code.",
        "email_code_required": "Email code required. Check your email.",
        "twofa_code_required": "2FA token required.",
    },
    "error": {
        "captcha": "Your login attempt was denied by CAPTCHA.\nPlease try again in 12+ hours.",
        "site_down": "Twitch is down, retrying in {seconds} seconds...",
        "no_connection": "Cannot connect to Twitch, retrying in {seconds} seconds... ({url})",
    },
    "gui": {
        "output": "Output",
        "status": {
            "name": "Status",
            "idle": "Idle",
            "exiting": "Exiting...",
            "terminated": "Terminated",
            "cleanup": "Cleaning up channels...",
            "gathering": "Gathering channels...",
            "switching": "Switching the channel...",
            "fetching_inventory": "Fetching inventory...",
            "fetching_campaigns": "Fetching campaigns...",
            "adding_campaigns": "Adding campaigns to inventory... {counter}",
        },
        "tabs": {
            "main": "Main",
            "inventory": "Inventory",
            "settings": "Settings",
            "help": "Help",
        },
        "tray": {
            "notification_title": "Mined Drop",
            "minimize": "Minimize to Tray",
            "show": "Show",
            "quit": "Quit",
            "still_running": "Still running in the tray.",
        },
        "login": {
            "name": "Login Form",
            "labels": "Status:\nUser ID:",
            "logged_in": "Logged in",
            "logged_out": "Logged out",
            "logged_in_as": "Logged in as {name}",
            "logging_in": "Logging in...",
            "required": "Login required",
            "request": "Please log in to continue.",
            "username": "Username",
            "password": "Password",
            "twofa_code": "2FA code (optional)",
            "button": "Login",
            "logout": "Log out",
            "device_code": "Enter this code on the Twitch page: {code}",
        },
        "websocket": {
            "name": "Websocket Status",
            "websocket": "Websocket #{id}:",
            "initializing": "Initializing...",
            "connected": "Connected",
            "disconnected": "Disconnected",
            "connecting": "Connecting...",
            "disconnecting": "Disconnecting...",
            "reconnecting": "Reconnecting...",
        },
        "progress": {
            "name": "Campaign Progress",
            "drop": "Drop:",
            "game": "Game:",
            "campaign": "Campaign:",
            "remaining": "{time} remaining",
            "drop_progress": "Progress:",
            "campaign_progress": "Progress:",
            "campaign_percentage": "{claimed}/{total} · average {percent}",
        },
        "channels": {
            "name": "Channels",
            "switch": "Switch",
            "online": "ONLINE  ✔",
            "pending": "OFFLINE ⏳",
            "offline": "OFFLINE ❌",
            "headings": {
                "channel": "Channel",
                "status": "Status",
                "game": "Game",
                "drops": "Drops",
                "farm": "Farm",
                "viewers": "Viewers",
            },
        },
        "inventory": {
            "filter": {
                "name": "Filter",
                "show": "Show:",
                "linked": "Linked",
                "not_linked": "Not linked",
                "upcoming": "Upcoming",
                "expired": "Expired",
                "excluded": "Excluded",
                "finished": "Finished",
                "refresh": "Refresh",
                "help": (
                    "Show includes a group. Linked and Not linked together list every campaign. "
                    "Upcoming, Expired, Excluded and Finished add those groups. "
                    "Order stays: running first, ending soonest at the top, then upcoming, then expired."
                ),
            },
            "status": {
                "linked": "Linked ✔",
                "not_linked": "Not Linked ❌",
                "active": "Active ✔",
                "upcoming": "Upcoming ⏳",
                "expired": "Expired ❌",
                "claimed": "Claimed ✔",
                "ready_to_claim": "Ready to claim ⏳",
            },
            "starts": "Starts: {time}",
            "ends": "Ends: {time}",
            "allowed_channels": "Allowed Channels:",
            "all_channels": "All",
            "and_more": "and {amount} more...",
            "percent_progress": "{percent}, {current}/{minutes} min",
            "minutes_progress": "{minutes} minutes",
            "farm": "Farm",
            "hours_left": "{left} h left",
            "hours_needed": "{needed} h to collect all",
            "days_left": "{left} d left",
            "days_needed": "{needed} d to collect all",
            "starts_in_hours": "starts in {left} h",
            "starts_in_days": "starts in {left} d",
            "farm_hint": "No Farm tick farms every listed game. A tick farms only those games.",
        },
        "settings": {
            "general": {
                "name": "General",
                "autostart": "Autostart: ",
                "tray": "Autostart into tray: ",
                "close_to_tray": "Close to the tray: ",
                "close_to_tray_note": (
                    "Closing the window leaves the miner running. "
                    "Open it again from the tray icon, or start the program again."
                ),
                "tray_notifications": "Tray notifications: ",
                "dark_mode": "Dark mode: ",
                "theme": "Look: ",
                "priority_mode": "Priority mode: ",
                "proxy": "Proxy (requires restart):",
            },
            "advanced": {
                "name": "Advanced",
                "warning": "Warning!",
                "warning_text": (
                    "These options will cause the miner to misbehave.\n"
                    "If you're experiencing any issues, "
                    "make sure all of these options are disabled."
                ),
                "enable_badges_emotes": "Enable partial support for badges and emotes: ",
                "available_drops_check": "Enable extra available drops check: ",
            },
            "scan": {
                "name": "Drop scan",
                "interval": "Scan for drops every: ",
                "once": (
                    "The drop list is loaded once, when nothing is saved yet. "
                    "It is not scanned again on a timer. Scan now fetches every known drop "
                    "and shows the full list."
                ),
                "scan_help": (
                    "Asks the live channels of games that already have drops. "
                    "This runs by itself only when nothing is saved yet."
                ),
                "minutes": "min",
                "off": "Off",
                "scan_now": "Scan now",
                "never": "Not scanned yet",
                "running": "Scanning for drops…",
                "preparing": "Preparing the scan…",
                "channels_phase": "channels",
                "running_progress": "Scanning {done}/{total} · {game} · {campaigns} campaigns",
                "started": "Drop scan started. Fetching every known campaign ({count} games).",
                "partial": "Drop scan: {campaigns} campaign(s) found, still checking more games.",
                "stale": "(stale)",
                "status": "Last scan: {when} UTC · {drops} games with drops",
                "coverage_label": "Games checked: ",
                "saved": "Inventory is showing these {campaigns} games.",
                "coverage": (
                    "Checked {probed} of {total} games ({percent}) · from the published list: "
                    "{seed} · campaigns: {campaigns}"
                ),
            },
            "priority_modes": {
                "priority_only": "Priority list only",
                "ending_soonest": "Ending soonest",
                "low_availability": "Low availability first",
            },
            "themes": {
                "classic": "Classic light",
                "dark": "Classic dark",
                "graphite": "Graphite",
                "light_accent": "Light with accents",
                "compact": "Compact contrast",
            },
            "game_name": "Game name",
            "priority": "Priority",
            "exclude": "Exclude",
            "reload": "Reload",
            "reload_text": "Mode and Farm take effect immediately. Language and proxy apply on the next start.",
            "feed_reload": "Update the drops list",
            "feed_help": (
                "Downloads the published list of which games currently have drops, "
                "then scans those games if the list changed. "
                "Reload below only redraws the inventory from what is already saved."
            ),
            "feed_current": "The drops list is already current. Nothing new to scan.",
            "feed_status": "{count} campaigns in the published list · {generated}",
            "feed_unknown": "Published drops list: not loaded yet",
        },
        "help": {
            "about": {
                "name": "About",
                "created_by": "Application created by: ",
                "modified_by": "Modified by: ",
            },
            "links": {
                "name": "Useful Links",
                "inventory": "See Twitch inventory",
                "campaigns": "See all campaigns and manage account links",
            },
            "how_it_works": "How It Works",
            "how_it_works_text": (
                "Every several seconds, the application pretends to watch a particular stream "
                "by fetching stream metadata - this is enough to advance the drops. "
                "Note that this completely bypasses the need to download "
                "any actual stream of video and sound. "
                "To keep the status (ONLINE or OFFLINE) of the channels up-to-date, "
                "there's a websocket connection established that receives events about streams "
                "going up or down, or updates regarding the current number of viewers."
            ),
            "getting_started": "Getting Started",
            "getting_started_text": (
                "1. Open Settings and press Login. The Twitch code is shown on that page. "
                "Enter it on the page that opens.\n"
                "2. A campaign can be mined before the game account is linked. "
                "The drop stays in your Twitch inventory, and the game can be connected later.\n"
                "3. Ending soonest watches the campaign that ends first and can still be finished. "
                "One with under 30 minutes of spare time is taken first. "
                "If several are that tight, the game with more viewers is watched.\n"
                "4. Priority list only watches games on the priority list. "
                "Ending soonest and Low availability do not let that list jump ahead.\n"
                "5. Exclude is the list of games that are never mined.\n"
                "6. A change to the mode or the lists takes effect immediately."
            ),
            "channels": "Drops on a channel",
            "channels_text": (
                "The channel list has two separate marks: \"Drops\" - Twitch lists a campaign "
                "on this channel (the badge the site shows), and \"Farm\" - there is something "
                "left for you to collect there. A channel can carry a running campaign while you "
                "have nothing left in it: \"Drops\" ✔, \"Farm\" ❌, and the channel can still be "
                "picked with the \"Switch\" button."
            ),
            "invalidate": {
                "button": "Invalidate",
                "text": "Invalidate the authentication token (log out):",
            },
        },
    },
}


class Translator:
    def __init__(self) -> None:
        self._langs: list[str] = []
        # start with (and always copy) the default translation
        self._translation: Translation = default_translation.copy()
        # if we're in dev, update the template English.json file
        if not IS_PACKAGED:
            default_langpath = LANG_PATH.joinpath(f"{DEFAULT_LANG}.json")
            json_save(default_langpath, default_translation)
        self._translation["language_name"] = DEFAULT_LANG
        # load available translation names
        for filepath in LANG_PATH.glob("*.json"):
            self._langs.append(filepath.stem)
        self._langs.sort()
        if DEFAULT_LANG in self._langs:
            self._langs.remove(DEFAULT_LANG)
        self._langs.insert(0, DEFAULT_LANG)

    @property
    def languages(self) -> abc.Iterable[str]:
        return iter(self._langs)

    @property
    def current(self) -> str:
        return self._translation["language_name"]

    def set_language(self, language: str):
        if language not in self._langs:
            raise ValueError("Unrecognized language")
        elif self._translation["language_name"] == language:
            # same language as loaded selected
            return
        elif language == DEFAULT_LANG:
            # default language selected - use the memory value
            self._translation = default_translation.copy()
        else:
            self._translation = json_load(
                LANG_PATH.joinpath(f"{language}.json"), default_translation
            )
            if "language_name" in self._translation:
                raise ValueError("Translations cannot define 'language_name'")
        self._translation["language_name"] = language

    def __call__(self, *path: str) -> str:
        if not path:
            raise ValueError("Language path expected")
        v: Any = self._translation
        try:
            for key in path:
                v = v[key]
        except KeyError:
            # this can only really happen for the default translation
            raise MinerException(
                f"{self.current} translation is missing the '{' -> '.join(path)}' translation key"
            )
        return v


_ = Translator()
