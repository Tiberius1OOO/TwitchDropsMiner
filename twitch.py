from __future__ import annotations

import json
import uuid
import asyncio
import logging
from time import time
from copy import deepcopy
from itertools import chain
from functools import partial
from collections import abc, deque, OrderedDict
from datetime import datetime, timedelta, timezone
from contextlib import suppress, asynccontextmanager
from typing import Any, Literal, Final, NoReturn, overload, cast, TYPE_CHECKING

import aiohttp
from yarl import URL

from translate import _
from gui import GUIManager
from channel import Channel
from websocket import WebsocketPool
from inventory import DropsCampaign
from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    LoginException,
    MinerException,
    RequestInvalid,
    CaptchaRequired,
    RequestException,
)
from utils import (
    CHARS_HEX_LOWER,
    isonow,
    chunk,
    timestamp,
    create_nonce,
    task_wrapper,
    RateLimiter,
    AwaitableValue,
    ExponentialBackoff,
    Game,
)
from constants import (
    CALL,
    _resource_path,
    MAX_INT,
    SCAN_PATH,
    GAMES_CATALOGUE_PATH,
    CATALOGUE_KEEP_DAYS,
    CATALOGUE_SEED_QUERIES,
    SCAN_BOOTSTRAP_CATALOGUE,
    SCAN_KNOWN_DROPPER_DAYS,
    SCAN_ESCALATION_GAMES,
    SCAN_FEED_CHANNELS_PER_GAME,
    GAMES_SEED_PATH,
    GAMES_SEED_URL,
    GAMES_SEED_CACHE_PATH,
    CAMPAIGNS_URL,
    CAMPAIGNS_CACHE_PATH,
    CAMPAIGNS_LOOKAHEAD_DAYS,
    DUMP_PATH,
    COOKIES_PATH,
    CHANNEL_DROPS_GAME_ID,
    CHANNEL_DROPS_GAME_NAME,
    MAX_CHANNELS,
    URGENT_BUFFER_MINUTES,
    GQL_QUERIES,
    WATCH_INTERVAL,
    State,
    ClientType,
    PriorityMode,
    WebsocketTopic,
    CLIENT_TYPE_ORDER,
    DEFAULT_CLIENT_TYPE,
)

if TYPE_CHECKING:
    from gui import LoginForm
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, JsonType, GQLOperation


logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")


class SkipExtraJsonDecoder(json.JSONDecoder):
    def decode(self, s: str, *args):
        # skip whitespace check
        obj, end = self.raw_decode(s)
        return obj


SAFE_LOADS = lambda s: json.loads(s, cls=SkipExtraJsonDecoder)


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str
        self.client_version: str

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def invalidate(self, *, delete_cookies: bool = False) -> None:
        self._delattrs("access_token", "user_id", "login_name")
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")
        if delete_cookies:
            session = self._twitch._session
            if session is not None:
                jar = cast(aiohttp.CookieJar, session.cookie_jar)
                jar.clear()
                COOKIES_PATH.unlink(missing_ok=True)

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "login_name",
            "device_id",
            "session_id",
            "access_token",
            "client_version",
        )
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")

    async def _fetch_device_id(self, client_info: ClientInfo, jar: aiohttp.CookieJar) -> None:
        async with self._twitch.request(
            "GET", client_info.CLIENT_URL, headers=self.headers()
        ) as response:
            page_html = await response.text("utf8")
            assert page_html is not None
        # doing the request ends up setting the "unique_id" value in the cookie
        cookie = jar.filter_cookies(client_info.CLIENT_URL)
        self.device_id = cookie["unique_id"].value

    def _login_headers(self, client_info: ClientInfo) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
            "Host": "id.twitch.tv",
            "Origin": str(client_info.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(client_info.CLIENT_URL),
            "User-Agent": client_info.USER_AGENT,
            "X-Device-Id": self.device_id,
        }

    async def _request_device_code(self) -> JsonType:
        """
        Requests the device code that the account gets authorized with.

        Twitch answers with an error JSON (ex. `{"status": 400, "message": "invalid
        client"}`) for client IDs it no longer accepts for the device code flow - which
        is what the Android app client started doing in September 2026. Every known
        client is therefore tried in turn, and the first accepted one is saved into the
        settings, so that a future rotation of client IDs is a config change instead of
        a broken release.
        """
        clients: dict[str, ClientInfo] = ClientType.as_dict()
        current_name: str = self._twitch._client_type_name
        candidate_names: list[str] = [current_name]
        candidate_names.extend(
            name for name in CLIENT_TYPE_ORDER if name in clients and name != current_name
        )
        rejected: list[str] = []
        for name in candidate_names:
            client_info: ClientInfo = clients[name]
            self._twitch.set_client_type(name, save=False)
            try:
                async with self._twitch.request(
                    "POST",
                    "https://id.twitch.tv/oauth2/device",
                    headers=self._login_headers(client_info),
                    data={"client_id": client_info.CLIENT_ID, "scopes": ""},
                ) as response:
                    # {
                    #     "device_code": "40 chars [A-Za-z0-9]",
                    #     "expires_in": 1800,
                    #     "interval": 5,
                    #     "user_code": "8 chars [A-Z]",
                    #     "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH"
                    # }
                    response_json: JsonType = await response.json()
                    status: int = response.status
            except RequestException as exc:
                rejected.append(f"{name}: {exc}")
                logger.info(f"Login: client {name} unusable: {exc}")
                continue
            if status != 200 or "device_code" not in response_json:
                # ex. {"status":400,"message":"invalid client"}
                detail: str = cast(
                    str,
                    response_json.get("message")
                    or response_json.get("error")
                    or f"HTTP {status}",
                )
                rejected.append(f"{name}: {detail}")
                logger.info(f"Login: client {name} rejected by Twitch: {detail}")
                continue
            # this client works - remember it for the next runs
            self._twitch.set_client_type(name, save=True)
            return response_json
        # put the client back the way it was, the app is about to exit anyway
        self._twitch.set_client_type(current_name, save=False)
        raise LoginException(
            "Twitch rejected the device code request for every known client - "
            f"a new login method is probably required ({'; '.join(rejected)})"
        )

    async def _oauth_login(self) -> str:
        login_form: LoginForm = self._twitch.gui.login
        while True:
            try:
                now = datetime.now(timezone.utc)
                response_json: JsonType = await self._request_device_code()
                client_info: ClientInfo = self._twitch._client_type
                headers: dict[str, str] = self._login_headers(client_info)
                device_code: str = response_json["device_code"]
                user_code: str = response_json["user_code"]
                interval: int = response_json["interval"]
                verification_uri: URL = URL(response_json["verification_uri"])
                expires_at = now + timedelta(seconds=response_json["expires_in"])

                # Print the code to the user, open them the activate page so they can type it in
                await login_form.ask_enter_code(verification_uri, user_code)

                payload = {
                    "client_id": client_info.CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    await asyncio.sleep(interval)
                    async with self._twitch.request(
                        "POST",
                        "https://id.twitch.tv/oauth2/token",
                        headers=headers,
                        data=payload,
                        invalidate_after=expires_at,
                    ) as response:
                        # 200 means success, 400 means the user haven't entered the code yet
                        if response.status != 200:
                            continue
                        response_json = await response.json()
                        # {
                        #     "access_token": "40 chars [A-Za-z0-9]",
                        #     "refresh_token": "40 chars [A-Za-z0-9]",
                        #     "scope": [...],
                        #     "token_type": "bearer"
                        # }
                        self.access_token = cast(str, response_json["access_token"])
                        return self.access_token
            except RequestInvalid:
                # the device_code has expired, request a new code
                continue

    async def _login(self) -> str:
        logger.info("Login flow started")
        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type

        token_kind: str = ''
        use_chrome: bool = False
        payload: JsonType = {
            # username and password are added later
            # "username": str,
            # "password": str,
            # client ID to-be associated with the access token
            "client_id": client_info.CLIENT_ID,
            "undelete_user": False,  # purpose unknown
            "remember_me": True,  # persist the session via the cookie
            # "authy_token": str,  # 2FA token
            # "twitchguard_code": str,  # email code
            # "captcha": str,  # self-fed captcha
            # 'force_twitchguard': False,  # force email code confirmation
        }

        while True:
            login_data = await login_form.ask_login()
            payload["username"] = login_data.username
            payload["password"] = login_data.password
            # reinstate the 2FA token, if present
            payload.pop("authy_token", None)
            payload.pop("twitchguard_code", None)
            if login_data.token:
                # if there's no token kind set yet, and the user has entered a token,
                # we can immediately assume it's an authenticator token and not an email one
                if not token_kind:
                    token_kind = "authy"
                if token_kind == "authy":
                    payload["authy_token"] = login_data.token
                elif token_kind == "email":
                    payload["twitchguard_code"] = login_data.token

            # use fancy headers to mimic the twitch android app
            headers = {
                "Accept": "application/vnd.twitchtv.v3+json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US",
                "Client-Id": client_info.CLIENT_ID,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": "passport.twitch.tv",
                "User-Agent": client_info.USER_AGENT,
                "X-Device-Id": self.device_id,
                # "X-Device-Id": ''.join(random.choices('0123456789abcdef', k=32)),
            }
            async with self._twitch.request(
                "POST", "https://passport.twitch.tv/login", headers=headers, json=payload
            ) as response:
                login_response: JsonType = await response.json(loads=SAFE_LOADS)

            # Feed this back in to avoid running into CAPTCHA if possible
            if "captcha_proof" in login_response:
                payload["captcha"] = {"proof": login_response["captcha_proof"]}

            # Error handling
            if "error_code" in login_response:
                error_code: int = login_response["error_code"]
                logger.info(f"Login error code: {error_code}")
                if error_code == 1000:
                    logger.info("1000: CAPTCHA is required")
                    use_chrome = True
                    break
                elif error_code in (2004, 3001):
                    logger.info("3001: Login failed due to incorrect username or password")
                    gui_print(_("login", "incorrect_login_pass"))
                    if error_code == 2004:
                        # invalid username
                        login_form.clear(login=True)
                    login_form.clear(password=True)
                    continue
                elif error_code in (
                    3012,  # Invalid authy token
                    3023,  # Invalid email code
                ):
                    logger.info("3012/23: Login failed due to incorrect 2FA code")
                    if error_code == 3023:
                        token_kind = "email"
                        gui_print(_("login", "incorrect_email_code"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "incorrect_twofa_code"))
                    login_form.clear(token=True)
                    continue
                elif error_code in (
                    3011,  # Authy token needed
                    3022,  # Email code needed
                ):
                    # 2FA handling
                    logger.info("3011/22: 2FA token required")
                    # user didn't provide a token, so ask them for it
                    if error_code == 3022:
                        token_kind = "email"
                        gui_print(_("login", "email_code_required"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "twofa_code_required"))
                    continue
                elif error_code >= 5000:
                    # Special errors, usually from Twitch telling the user to "go away"
                    # We print the code out to inform the user, and just use chrome flow instead
                    # {
                    #     "error_code":5023,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client is not supported for this feature"
                    # }
                    # {
                    #     "error_code":5027,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client blocked from this operation"
                    # }
                    gui_print(_("login", "error_code").format(error_code=error_code))
                    logger.info(str(login_response))
                    use_chrome = True
                    break
                else:
                    ext_msg = str(login_response)
                    logger.info(ext_msg)
                    raise LoginException(ext_msg)
            # Success handling
            if "access_token" in login_response:
                self.access_token = cast(str, login_response["access_token"])
                logger.info("Access token granted")
                login_form.clear()
                break

        if use_chrome:
            # await self._chrome_login()
            raise CaptchaRequired()

        if hasattr(self, "access_token"):
            return self.access_token
        raise LoginException("Login flow finished without setting the access token")

    def headers(self, *, user_agent: str = '', gql: bool = False) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        # if hasattr(self, "client_version"):
            # headers["Client-Version"] = self.client_version
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate(self):
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        if not self._hasattrs("device_id", "access_token", "user_id"):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
            client_info: ClientInfo = self._twitch._client_type
        if not self._hasattrs("device_id"):
            #     match = re.search(r'twilightBuildID="([-a-z0-9]+)"', page_html)
            # if match is None:
            #     raise MinerException("Unable to extract client_version")
            # self.client_version = match.group(1)
            await self._fetch_device_id(client_info, jar)
        if not self._hasattrs("access_token", "user_id"):
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger.info("Checking login")
            login_form.update(_("gui", "login", "logging_in"), None)
            for client_mismatch_attempt in range(2):
                for invalid_token_attempt in range(2):
                    cookie = jar.filter_cookies(client_info.CLIENT_URL)
                    if "auth-token" not in cookie:
                        self.access_token = await self._oauth_login()
                        if client_info.CLIENT_ID != self._twitch._client_type.CLIENT_ID:
                            # the login flow switched us to another Twitch client - the
                            # device ID and the cookies have to come from that one instead
                            old_host = client_info.CLIENT_URL.host
                            client_info = self._twitch._client_type
                            if client_info.CLIENT_URL.host != old_host:
                                self._delattrs("device_id")
                                await self._fetch_device_id(client_info, jar)
                            cookie = jar.filter_cookies(client_info.CLIENT_URL)
                        cookie["auth-token"] = self.access_token
                    elif not hasattr(self, "access_token"):
                        logger.info("Restoring session from cookie")
                        self.access_token = cookie["auth-token"].value
                    # validate the auth token, by obtaining user_id
                    async with self._twitch.request(
                        "GET",
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {self.access_token}"}
                    ) as response:
                        if response.status == 401:
                            # the access token we have is invalid - clear the cookie and reauth
                            logger.info("Restored session is invalid")
                            assert client_info.CLIENT_URL.host is not None
                            jar.clear_domain(client_info.CLIENT_URL.host)
                            continue
                        elif response.status == 200:
                            validate_response = await response.json()
                            break
                else:
                    raise RuntimeError("Login verification failure (step #2)")
                # ensure the cookie's client ID matches the currently selected client
                if validate_response["client_id"] == client_info.CLIENT_ID:
                    break
                # otherwise, we need to delete the entire cookie file and clear the jar
                logger.info("Cookie client ID mismatch")
                jar.clear()
                COOKIES_PATH.unlink(missing_ok=True)
            else:
                raise RuntimeError("Login verification failure (step #1)")
            self.user_id = int(validate_response["user_id"])
            self.login_name = str(validate_response.get("login") or "")
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}")
            login_form.update(_("gui", "login", "logged_in"), self.user_id)
            # update our cookie and save it
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            jar.save(COOKIES_PATH)
        self._twitch.gui.help._invalidate_button.config(state="normal")
        self._logged_in.set()


class Twitch:
    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        # State management
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self.wanted_games: list[Game] = []
        # Games and ACL channels that can still be finished with a tight positive buffer.
        self._urgent_games: set[Game] = set()
        self._urgent_channel_ids: set[int] = set()
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._mnt_triggers: deque[datetime] = deque()
        # live numbers for the settings scan panel; the GUI is built next and reads this
        self._scan_progress: dict[str, Any] = {
            "phase": "idle",
            "done": 0,
            "total": 0,
            "game": "",
            "campaigns": 0,
        }
        # bumped whenever a scan finishes, so an inventory fetch already in progress
        # loads the finished list instead of keeping the copy it started with
        self._inventory_epoch: int = 0
        # set when a scan finishes during another state, so that state's final
        # change_state cannot throw the reload away
        self._reload_pending: bool = False
        self._feed_task: asyncio.Task[None] | None = None
        # NOTE: GQL is pretty volatile and breaks everything if one runs into their rate limit.
        # Do not modify the default, safe values.
        self._qgl_limiter = RateLimiter(capacity=5, window=1)
        # Client type, session and auth
        # NOTE: The client ID is a setting, because Twitch keeps disabling the ones it
        # doesn't like anymore (the Android app client in Sep 2026, for example).
        self._client_type_name: str = self._resolve_client_name(settings.client_type)
        self._client_type: ClientInfo = ClientType.as_dict()[self._client_type_name]
        self._session: aiohttp.ClientSession | None = None
        self._auth_state: _AuthState = _AuthState(self)
        # GUI
        self.gui = GUIManager(self)
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None
        # Background drop scan task, and the loop that keeps it on schedule
        self._scan_task: asyncio.Task[None] | None = None
        self._scan_loop_task: asyncio.Task[None] | None = None
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    @staticmethod
    def _resolve_client_name(name: str) -> str:
        if name not in ClientType.as_dict():
            logger.warning(
                f"Unknown Twitch client in settings: {name!r} - "
                f"falling back to {DEFAULT_CLIENT_TYPE!r}"
            )
            return DEFAULT_CLIENT_TYPE
        return name

    def set_client_type(self, name: str, *, save: bool = True) -> None:
        """
        Switches the Twitch client used for authorization and API requests.

        Saved into the settings, so that the next run starts with the client that works.
        """
        clients: dict[str, ClientInfo] = ClientType.as_dict()
        if name not in clients:
            raise ValueError(f"Unknown Twitch client: {name!r}")
        if name == self._client_type_name:
            return
        logger.info(f"Switching Twitch client: {self._client_type_name} -> {name}")
        self._client_type_name = name
        self._client_type = clients[name]
        if save:
            self.settings.client_type = name
            self.settings.save()

    async def get_session(self) -> aiohttp.ClientSession:
        if (session := self._session) is not None:
            if session.closed:
                raise RuntimeError("Session is closed")
            return session
        # load in cookies
        cookie_jar = aiohttp.CookieJar()
        try:
            if COOKIES_PATH.exists():
                cookie_jar.load(COOKIES_PATH)
        except Exception:
            # if loading in the cookies file ends up in an error, just ignore it
            # clear the jar, just in case
            cookie_jar.clear()
        # create timeouts
        # connection quality mulitiplier determines the magnitude of timeouts
        connection_quality = self.settings.connection_quality
        if connection_quality < 1:
            connection_quality = self.settings.connection_quality = 1
        elif connection_quality > 6:
            connection_quality = self.settings.connection_quality = 6
        timeout = aiohttp.ClientTimeout(
            sock_connect=5*connection_quality,
            total=10*connection_quality,
        )
        # create session, limited to 50 connections at maximum
        connector = aiohttp.TCPConnector(limit=50)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=cookie_jar,
            headers={"User-Agent": self._client_type.USER_AGENT},
        )
        return self._session

    async def shutdown(self) -> None:
        start_time = time()
        self.stop_watching()
        if self._watching_task is not None:
            self._watching_task.cancel()
            self._watching_task = None
        if self._mnt_task is not None:
            self._mnt_task.cancel()
            self._mnt_task = None
        if self._scan_task is not None:
            self._scan_task.cancel()
            self._scan_task = None
        if self._scan_loop_task is not None:
            self._scan_loop_task.cancel()
            self._scan_loop_task = None
        # stop websocket, close session and save cookies
        await self.websocket.stop(clear_topics=True)
        if self._session is not None:
            cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
            # clear empty cookie entries off the cookies file before saving
            # NOTE: Unfortunately, aiohttp provides no easy way of clearing empty cookies,
            # so we need to access the private '_cookies' attribute for this.
            for cookie_key, cookie in list(cookie_jar._cookies.items()):
                if not cookie:
                    del cookie_jar._cookies[cookie_key]
            cookie_jar.save(COOKIES_PATH)
            await self._session.close()
            self._session = None
        self._drops.clear()
        self.channels.clear()
        self.inventory.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._mnt_triggers.clear()
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(start_time + 0.5 - time())

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        if self._state is not State.EXIT:
            # prevent state changing once we switch to exit state
            self._state = state
        self._state_change.set()

    def _request_inventory_reload(self) -> None:
        """Asks the main loop to rebuild the inventory from the latest saved drops."""
        self._inventory_epoch += 1
        self._reload_pending = True
        self.change_state(State.INVENTORY_FETCH)

    def state_change(self, state: State) -> abc.Callable[[], None]:
        # this is identical to change_state, but defers the call
        # perfect for GUI usage
        return partial(self.change_state, state)

    def close(self):
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.change_state(State.EXIT)

    def prevent_close(self):
        """
        Called when the application window has to be prevented from closing, even after the user
        closes it with X. Usually used solely to display tracebacks from the closing sequence.
        """
        self.gui.prevent_close()

    def print(self, message: str):
        """
        Can be used to print messages within the GUI.
        """
        self.gui.print(message)

    def save(self, *, force: bool = False) -> None:
        """
        Saves the application state.
        """
        self.gui.save(force=force)
        self.settings.save(force=force)

    def get_priority(self, channel: Channel) -> int:
        """
        Return a priority number for a given channel.

        0 has the highest priority.
        Higher numbers -> lower priority.
        MAX_INT (a really big number) signifies the lowest possible priority.
        """
        if (
            (game := channel.game) is None  # None when OFFLINE or no game set
            or game not in self.wanted_games  # we don't care about the played game
        ):
            return MAX_INT
        return self.wanted_games.index(game)

    def _mode_allows(self, campaign: DropsCampaign) -> bool:
        """Priority-only keeps the priority list. The other modes consider every game."""
        if self.settings.priority_mode is not PriorityMode.PRIORITY_ONLY:
            return True
        priority = self.settings.priority
        return not priority or campaign.game.name in priority

    def _campaign_is_urgent(self, campaign: DropsCampaign, now: datetime | None = None) -> bool:
        """True when every remaining drop still fits, with spare time inside the window."""
        if campaign.game.name in self.settings.exclude or not self._mode_allows(campaign):
            return False
        buffer = campaign.finish_buffer_minutes(now)
        return buffer is not None and 0 < buffer <= URGENT_BUFFER_MINUTES

    def _refresh_urgent(self, now: datetime | None = None) -> None:
        self._urgent_games.clear()
        self._urgent_channel_ids.clear()
        for campaign in self.inventory:
            if not self._campaign_is_urgent(campaign, now):
                continue
            if campaign.allowed_channels:
                self._urgent_channel_ids.update(channel.id for channel in campaign.allowed_channels)
            if not campaign.game.is_channel_drops():
                self._urgent_games.add(campaign.game)

    def campaign_audience(self, campaign: DropsCampaign) -> int:
        """Largest live audience that can actually progress this campaign."""
        allowed = {channel.id for channel in campaign.allowed_channels}
        best = 0
        for channel in self.channels.values():
            if not channel.online:
                continue
            if allowed:
                if channel.id not in allowed:
                    continue
            elif channel.game != campaign.game:
                continue
            best = max(best, channel.viewers or 0)
        # A small viewer change should not jump the row. The channel choice still
        # uses the exact count.
        if best >= 1000:
            return (best // 100) * 100
        return (best // 50) * 50

    def campaign_sort_key(self, campaign: DropsCampaign) -> tuple:
        """Inventory order. The miner walks this same order when it picks a game.

        Ending soonest follows the end time. Low availability follows the tightest
        finishable campaign. Priority only follows the priority list.
        Anything that can still be completed with a short positive buffer stays above
        that, and the larger live audience wins when several campaigns are that tight.
        """
        now = datetime.now(timezone.utc)
        name = campaign.game.name
        title = campaign.name
        ends = campaign.ends_at.timestamp()
        if campaign.expired:
            return (4, -ends, name, title)
        if campaign.upcoming:
            return (3, campaign.starts_at.timestamp(), name, title)
        if self._campaign_is_urgent(campaign, now):
            buffer = campaign.finish_buffer_minutes(now) or 0
            return (0, -self.campaign_audience(campaign), buffer, ends, name, title)
        mode = self.settings.priority_mode
        priority = self.settings.priority
        buffer = campaign.finish_buffer_minutes(now)
        # Campaigns that can still be finished stay above ones that can no longer be completed,
        # except in ending-soonest mode, where the clock itself is the list.
        finishable = 0 if buffer is not None and buffer > 0 else 1
        if mode is PriorityMode.PRIORITY_ONLY:
            listed = name in priority
            index = priority.index(name) if listed else MAX_INT
            return (1, 0 if listed else 1, finishable, index, ends, name, title)
        if mode is PriorityMode.LOW_AVBL_FIRST:
            return (1, finishable, campaign.availability, ends, name, title)
        return (1, ends, name, title)

    def _campaign_is_candidate(self, campaign: DropsCampaign, now: datetime | None = None) -> bool:
        """A game the current mode is willing to farm within the next hour."""
        now = now or datetime.now(timezone.utc)
        game = campaign.game
        if game.is_channel_drops() or game.name in self.settings.exclude:
            return False
        if not self._mode_allows(campaign):
            return False
        buffer = campaign.finish_buffer_minutes(now)
        # No spare time means the remaining drops can no longer all be claimed.
        if buffer is None or buffer <= 0:
            return False
        return campaign.can_earn_within(now + timedelta(hours=1))

    def _next_choice_boundary(self, now: datetime) -> datetime | None:
        """The next moment a campaign enters or leaves the short spare-time window."""
        soonest: datetime | None = None
        for campaign in self.inventory:
            if campaign.game.name in self.settings.exclude or not self._mode_allows(campaign):
                continue
            buffer = campaign.finish_buffer_minutes(now)
            if buffer is None or buffer <= 0:
                continue
            if buffer > URGENT_BUFFER_MINUTES:
                when = now + timedelta(minutes=buffer - URGENT_BUFFER_MINUTES)
            else:
                when = now + timedelta(minutes=buffer)
            if soonest is None or when < soonest:
                soonest = when
        return soonest

    def _is_urgent_channel(self, channel: Channel) -> bool:
        if channel.id in self._urgent_channel_ids:
            return True
        game = channel.game
        return game is not None and game in self._urgent_games

    def _channel_sort_key(self, channel: Channel) -> tuple:
        viewers = channel.viewers if channel.viewers is not None else -1
        acl = 0 if channel.acl_based else 1
        if self._is_urgent_channel(channel):
            # One tier for every tight campaign. The bigger audience is watched.
            return (0, -viewers, acl, channel.id)
        return (1, self.get_priority(channel), acl, -viewers, channel.id)

    async def run(self):
        if self.settings.dump:
            with open(DUMP_PATH, 'w', encoding="utf8"):
                # replace the existing file with an empty one
                pass
        while True:
            try:
                await self._run()
                break
            except ReloadRequest:
                await self.shutdown()
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise RequestException(_("login", "unexpected_content")) from exc

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        auth_state = await self.get_auth()
        await self.websocket.start()
        # NOTE: watch task is explicitly restarted on each new run
        if self._watching_task is not None:
            self._watching_task.cancel()
        self._watching_task = asyncio.create_task(self._watch_loop())
        if self._scan_loop_task is None or self._scan_loop_task.done():
            self._scan_loop_task = asyncio.create_task(self._scan_loop())
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
            ),
        ])
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._reload_pending and self._state is not State.EXIT:
                self._reload_pending = False
                if self._state is not State.INVENTORY_FETCH:
                    self._state = State.INVENTORY_FETCH
            if self._state is State.IDLE:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.tray.change_icon("idle")
                self.gui.status.update(_("gui", "status", "idle"))
                self.stop_watching()
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self._state is State.INVENTORY_FETCH:
                self.gui.tray.change_icon("maint")
                # ensure the websocket is running
                await self.websocket.start()
                while self._state is State.INVENTORY_FETCH:
                    epoch = self._inventory_epoch
                    await self.fetch_inventory()
                    # a scan finished while this fetch was reading the previous file
                    if self._state is State.INVENTORY_FETCH and self._inventory_epoch != epoch:
                        continue
                    break
                if self._state is State.EXIT:
                    continue
                self.gui.set_games(
                    set(
                        campaign.game
                        for campaign in self.inventory
                        if not campaign.game.is_channel_drops()
                    )
                )
                # Save state on every inventory fetch
                self.save()
                self.change_state(State.GAMES_UPDATE)
            elif self._state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim:
                                await drop.claim()
                # figure out which games we want
                self.wanted_games.clear()
                # Same order the inventory shows. A short positive buffer stays first,
                # and the priority list no longer jumps ahead of the selected mode.
                self._refresh_urgent()
                sorted_campaigns: list[DropsCampaign] = sorted(
                    self.inventory, key=self.campaign_sort_key
                )
                if self._urgent_games or self._urgent_channel_ids:
                    urgent_names = ", ".join(sorted(game.name for game in self._urgent_games))
                    logger.info(
                        "Urgent campaigns, buffer under "
                        f"{URGENT_BUFFER_MINUTES} min: {urgent_names or 'channel drops'}"
                    )
                for campaign in sorted_campaigns:
                    game: Game = campaign.game
                    if game not in self.wanted_games and self._campaign_is_candidate(campaign):
                        self.wanted_games.append(game)
                full_cleanup = True
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                self.gui.status.update(_("gui", "status", "cleanup"))
                if not self.wanted_games or full_cleanup:
                    # no games selected or we're doing full cleanup: remove everything
                    to_remove_channels: list[Channel] = list(channels.values())
                else:
                    # remove all channels that:
                    to_remove_channels = [
                        channel
                        for channel in channels.values()
                        if (
                            not channel.acl_based  # aren't ACL-based
                            and (
                                channel.offline  # and are offline
                                # or online but aren't streaming the game we want anymore
                                or (channel.game is None or channel.game not in self.wanted_games)
                            )
                        )
                    ]
                full_cleanup = False
                if to_remove_channels:
                    to_remove_topics: list[str] = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    for channel in to_remove_channels:
                        del channels[channel.id]
                        channel.remove()
                    del to_remove_channels, to_remove_topics
                channel_campaigns_available: bool = any(
                    campaign.game.is_channel_drops()
                    and campaign.can_earn_within(
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    )
                    for campaign in self.inventory
                )
                if self.wanted_games or channel_campaigns_available:
                    # channel campaigns don't need a game to be selected - they are
                    # pinned to their channel by the ACL, so they can be mined on their own
                    self.change_state(State.CHANNELS_FETCH)
                else:
                    # with no games available, we switch to IDLE after cleanup
                    self.print(_("status", "no_campaign"))
                    self.change_state(State.IDLE)
            elif self._state is State.CHANNELS_FETCH:
                self.gui.status.update(_("gui", "status", "gathering"))
                # start with all current channels, clear the memory and GUI
                new_channels: set[Channel] = set(channels.values())
                channels.clear()
                self.gui.channels.clear()
                # gather and add ACL channels from campaigns
                # NOTE: we consider only campaigns that can be progressed
                # NOTE: we use another set so that we can set them online separately
                no_acl: set[Game] = set()
                acl_channels: set[Channel] = set()
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                for campaign in self.inventory:
                    if (
                        (
                            campaign.game in self.wanted_games
                            # channel campaigns are pinned to their channel by the ACL, so
                            # they don't depend on their game being selected
                            or campaign.game.is_channel_drops()
                        )
                        and campaign.can_earn_within(next_hour)
                    ):
                        if campaign.allowed_channels:
                            acl_channels.update(campaign.allowed_channels)
                        else:
                            no_acl.add(campaign.game)
                # remove all ACL channels that already exist from the other set
                acl_channels.difference_update(new_channels)
                # use the other set to set them online if possible
                await self.bulk_check_online(acl_channels)
                # finally, add them as new channels
                new_channels.update(acl_channels)
                for game in no_acl:
                    # for every campaign without an ACL, for it's game,
                    # add a list of live channels with drops enabled
                    new_channels.update(await self.get_live_streams(game, drops_enabled=True))
                # only keep the channels that have drops available for our account
                await self.refresh_available_drops(new_channels)
                # Tight campaigns first, and among those the larger audience.
                # Every other game keeps its wanted-games order, then ACL, then viewers.
                # NOTE: We can drop using the set now, because there's no more channels being added
                ordered_channels: list[Channel] = sorted(new_channels, key=self._channel_sort_key)
                # ensure that we won't end up with more channels than we can handle
                # NOTE: we trim from the end because that's where the non-priority,
                # offline (or online but low viewers) channels end up
                to_remove_channels = ordered_channels[MAX_CHANNELS:]
                ordered_channels = ordered_channels[:MAX_CHANNELS]
                if to_remove_channels:
                    # tracked channels and gui were cleared earlier, so no need to do it here
                    # just make sure to unsubscribe from their topics
                    to_remove_topics = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    del to_remove_channels, to_remove_topics
                # set our new channel list
                for channel in ordered_channels:
                    channels[channel.id] = channel
                    channel.display(add=True)
                # subscribe to these channel's state updates
                to_add_topics: list[WebsocketTopic] = []
                for channel_id in channels:
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamState", channel_id, self.process_stream_state
                        )
                    )
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamUpdate", channel_id, self.process_stream_update
                        )
                    )
                self.websocket.add_topics(to_add_topics)
                # relink watching channel after cleanup,
                # or stop watching it if it no longer qualifies
                # NOTE: this replaces 'self.watching_channel's internal value with the new object
                watching_channel = self.watching_channel.get_with_default(None)
                if watching_channel is not None:
                    new_watching: Channel | None = channels.get(watching_channel.id)
                    if new_watching is not None and self.can_watch(new_watching):
                        self.watch(new_watching, update_status=False)
                    else:
                        # we've removed a channel we were watching
                        self.stop_watching()
                    del new_watching
                # pre-display the active drop with a substracted minute
                for channel in channels.values():
                    # check if there's any channels we can watch first
                    if self.can_watch(channel):
                        if (
                            (active_campaign := self.get_active_campaign(channel)) is not None
                            and (active_drop := active_campaign.first_drop) is not None
                        ):
                            active_drop.display(countdown=False, subone=True)
                        break
                self.change_state(State.CHANNEL_SWITCH)
                del (
                    no_acl,
                    acl_channels,
                    new_channels,
                    to_add_topics,
                    ordered_channels,
                    watching_channel,
                )
            elif self._state is State.CHANNEL_SWITCH:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.status.update(_("gui", "status", "switching"))
                # Change into the selected channel, stay in the watching channel,
                # or select a new channel that meets the required conditions
                new_watching = None
                selected_channel = self.gui.channels.get_selection()
                if selected_channel is not None and self.can_watch(selected_channel, manual=True):
                    # selected channel is checked first, and set as long as we can watch it
                    new_watching = selected_channel
                else:
                    # other channels additionally need to have a good reason
                    # for a switch (including the watching one)
                    # NOTE: we need to sort the channels every time because one channel
                    # can end up streaming any game - channels aren't game-tied
                    for channel in sorted(channels.values(), key=self._channel_sort_key):
                        if self.should_switch(channel):
                            new_watching = channel
                            break
                watching_channel = self.watching_channel.get_with_default(None)
                if new_watching is not None:
                    # if we have a better switch target - do so
                    self.watch(new_watching)
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                elif watching_channel is not None and self.can_watch(watching_channel):
                    # otherwise, continue watching what we had before
                    self.gui.status.update(
                        _("status", "watching").format(channel=watching_channel.name)
                    )
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                else:
                    # not watching anything and there isn't anything to watch either
                    self.print(_("status", "no_channel"))
                    self.change_state(State.IDLE)
                del new_watching, selected_channel, watching_channel
            elif self._state is State.RESTART:
                raise ReloadRequest()
            elif self._state is State.EXIT:
                self.gui.tray.change_icon("pickaxe")
                self.gui.status.update(_("gui", "status", "exiting"))
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    async def _watch_sleep(self, delay: float) -> None:
        # we use wait_for here to allow an asyncio.sleep-like that can be ended prematurely
        self._watching_restart.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)

    @task_wrapper(critical=True)
    async def _watch_loop(self) -> NoReturn:
        interval: float = WATCH_INTERVAL.total_seconds()
        while True:
            channel: Channel = await self.watching_channel.get()
            if not channel.online:
                # if the channel isn't online anymore, we stop watching it
                self.stop_watching()
                continue
            # logger.log(CALL, f"Sending watch payload to: {channel.name}")
            last_sent: float = time()
            succeeded: bool = await channel.send_watch()
            if not succeeded:
                logger.log(CALL, f"Watch requested failed for channel: {channel.name}")
            # wait ~15 seconds for a progress update
            await asyncio.sleep(15 - min(time() - last_sent, 15))
            if self.gui.progress.minute_almost_done():
                # If the previous update was more than ~60s ago, and the progress tracker
                # isn't counting down anymore, that means Twitch has temporarily
                # stopped reporting drop's progress. To ensure the timer keeps at least somewhat
                # accurate time, we can use GQL to query for the current drop,
                # or even "pretend" mining as a last resort option.
                handled: bool = False

                # Solution 1: use GQL to query for the currently mined drop status
                try:
                    context = await self.gql_request(
                        GQL_QUERIES["CurrentDrop"].with_variables(
                            {"channelID": str(channel.id)}
                        )
                    )
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                except GQLException:
                    drop_data = None
                if drop_data is not None:
                    gql_drop: TimedDrop | None = self._drops.get(drop_data["dropID"])
                    if gql_drop is not None and gql_drop.can_earn(channel):
                        gql_drop.update_minutes(drop_data["currentMinutesWatched"])
                        drop_text: str = (
                            f"{gql_drop.name} ({gql_drop.campaign.game}, "
                            f"{gql_drop.current_minutes}/{gql_drop.required_minutes})"
                        )
                        logger.log(CALL, f"Drop progress from GQL: {drop_text}")
                        handled = True

                # Solution 2: If GQL fails, figure out which campaign we're most likely mining
                # right now, and then bump up the minutes on it's drops
                if not handled:
                    if (active_campaign := self.get_active_campaign(channel)) is not None:
                        active_campaign.bump_minutes(channel)
                        # NOTE: This usually gets overwritten below
                        drop_text = f"Unknown drop ({active_campaign.game})"
                        if (active_drop := active_campaign.first_drop) is not None:
                            active_drop.display()
                            drop_text = (
                                f"{active_drop.name} ({active_drop.campaign.game}, "
                                f"{active_drop.current_minutes}/{active_drop.required_minutes})"
                            )
                        logger.log(CALL, f"Drop progress from active search: {drop_text}")
                        handled = True
                    else:
                        logger.log(CALL, "No active drop could be determined")
            await self._watch_sleep(interval - min(time() - last_sent, interval))

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(hours=1)
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            # Wake at the next campaign start or end, not at the last one in the hour.
            # A spare-time boundary that arrives sooner is taken first.
            next_trigger = next_period
            if self._mnt_triggers:
                next_trigger = min(next_trigger, self._mnt_triggers[0])
            boundary = self._next_choice_boundary(now)
            if (
                boundary is not None
                and now + timedelta(seconds=1) < boundary < next_trigger
            ):
                next_trigger = boundary
            while self._mnt_triggers and self._mnt_triggers[0] <= next_trigger:
                self._mnt_triggers.popleft()
            trigger_type: str = "Reload" if next_trigger == next_period else "Retarget"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep(max((next_trigger - now).total_seconds(), 0))
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            logger.log(CALL, "Maintenance task requests a new game choice")
            # Re-rank before fetching channels again. Cleanup alone would keep
            # the previous game order and miss a campaign that just opened.
            if self._state not in (State.INVENTORY_FETCH, State.EXIT):
                self.change_state(State.GAMES_UPDATE)
        # this triggers a restart of this task every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self.change_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel, *, manual: bool = False) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.

        With manual=True a channel picked by hand in the list is also accepted when Twitch
        reports a live campaign for it, even if this account has nothing left to collect there.
        """
        # exit early if stream is offline
        if not channel.online:
            return False
        for campaign in self.inventory:
            if not campaign.can_earn(channel):
                continue
            # the campaign does the "special games" check itself
            if campaign.game.is_special():
                return True
            # a channel the user asks for by name only needs a campaign that Twitch still lists
            if manual and channel.drops_reported:
                return True
            # otherwise limit watching to the games the user wants
            if (
                channel.game is not None
                and channel.drops_enabled
                and channel.game in self.wanted_games
            ):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a switch candidate.
        """
        if not self.can_watch(channel):
            return False
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        channel_urgent = self._is_urgent_channel(channel)
        watching_urgent = self._is_urgent_channel(watching_channel)
        if channel_urgent or watching_urgent:
            if channel_urgent != watching_urgent:
                return channel_urgent
            # Both are inside the short window. A different, more watched game wins.
            # The same game stays put, unless this channel is the one the ACL requires.
            if channel.game != watching_channel.game:
                return (channel.viewers or -1) > (watching_channel.viewers or -1)
            return channel.acl_based > watching_channel.acl_based
        channel_order = self.get_priority(channel)
        watching_order = self.get_priority(watching_channel)
        return (
            # this channel's game is higher order than the watching one's
            channel_order < watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        self.gui.tray.change_icon("active")
        self.gui.channels.set_watching(channel)
        self.watching_channel.set(channel)
        logger.info(f"Watching: {channel.name} (game: {channel.game or 'unknown'})")
        if update_status:
            status_text = _("status", "watching").format(channel=channel.name)
            self.print(status_text)
            self.gui.status.update(status_text)

    def stop_watching(self):
        self.gui.clear_drop()
        self.watching_channel.clear()
        self.gui.channels.clear_watching()

    def restart_watching(self):
        self.gui.progress.stop_timer()
        self._watching_restart.set()

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                viewers = message["viewers"]
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if self.should_switch(channel):
                    # we can watch the channel, and we should
                    self.print(_("status", "goes_online").format(channel=channel.name))
                    self.watch(channel)
                else:
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            watching_channel = self.watching_channel.get_with_default(None)
            # check if the watching channel was the one updated
            if watching_channel is not None and watching_channel == channel:
                # NOTE: In these cases, channel was the watching channel
                if not self.can_watch(channel):
                    # we can't watch it anymore
                    if stream_after is None:
                        # Channel going OFFLINE
                        self.print(_("status", "goes_offline").format(channel=channel.name))
                    else:
                        # Channel stays ONLINE, but we can't watch it anymore
                        logger.info(
                            f"{channel.name} status has been updated, switching... "
                            f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                            f"{stream_after.drops_enabled and '✔' or '❌'})"
                        )
                    self.change_state(State.CHANNEL_SWITCH)
                else:
                    # Channel stays ONLINE, and we can still watch it - no change
                    pass
            # NOTE: In these cases, it wasn't the watching channel
            elif stream_after is None:
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                # Channel stays ONLINE, but has been updated
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                    f"{stream_after.drops_enabled and '✔' or '❌'})"
                )
                if self.should_switch(channel):
                    # ... and we should watch it
                    self.watch(channel)
        channel.display()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        drop_id: str = message["data"]["drop_id"]
        drop: TimedDrop | None = self._drops.get(drop_id)
        watching_channel: Channel | None = self.watching_channel.get_with_default(None)
        if msg_type == "drop-claim":
            if drop is None:
                logger.error(
                    f"Received a drop claim ID for a non-existing drop: {drop_id}\n"
                    f"Drop claim ID: {message['data']['drop_instance_id']}"
                )
                return
            drop.update_claim(message["data"]["drop_instance_id"])
            campaign = drop.campaign
            await drop.claim()
            drop.display()
            # About 4-20s after claiming the drop, next drop can be started
            # by re-sending the watch payload. We can test for it by fetching the current drop
            # via GQL, and then comparing drop IDs.
            await asyncio.sleep(4)
            if watching_channel is not None:
                for attempt in range(8):
                    context = await self.gql_request(
                        GQL_QUERIES["CurrentDrop"].with_variables(
                            {"channelID": str(watching_channel.id)}
                        )
                    )
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                    if drop_data is None or drop_data["dropID"] != drop.id:
                        break
                    await asyncio.sleep(2)
            if campaign.can_earn(watching_channel):
                self.restart_watching()
            else:
                self.change_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        if drop is not None:
            drop_text = (
                f"{drop.name} ({drop.campaign.game}, "
                f"{message['data']['current_progress_min']}/"
                f"{message['data']['required_progress_min']})"
            )
        else:
            drop_text = "<Unknown>"
        logger.log(CALL, f"Drop update from websocket: {drop_text}")
        if drop is not None and drop.can_earn(self.watching_channel.get_with_default(None)):
            # the received payload is for the drop we expected
            drop.update_minutes(message["data"]["current_progress_min"])

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] in (
                "user_drop_reward_reminder_notification",  # drop confirmation
                "quests_viewer_reward_campaign_earned_emote",  # emote confirmation
                # badge confirmation?
            ):
                self.change_state(State.INVENTORY_FETCH)
                await self.gql_request(
                    GQL_QUERIES["NotificationsDelete"].with_variables(
                        {"input": {"id": data["id"]}}
                    )
                )

    async def get_auth(self) -> _AuthState:
        await self._auth_state.validate()
        return self._auth_state

    @asynccontextmanager
    async def request(
        self, method: str, url: URL | str, *, invalidate_after: datetime | None = None, **kwargs
    ) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        method = method.upper()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        logger.debug(f"Request: ({method=}, {url=}, {kwargs=})")
        session_timeout = timedelta(seconds=session.timeout.total or 0)
        backoff = ExponentialBackoff(maximum=3*60)
        for delay in backoff:
            if self.gui.close_requested:
                raise ExitRequest()
            elif (
                invalidate_after is not None
                # account for the expiration landing during the request
                and datetime.now(timezone.utc) >= (invalidate_after - session_timeout)
            ):
                raise RequestInvalid()
            try:
                response: aiohttp.ClientResponse | None = None
                response = await self.gui.coro_unless_closed(
                    session.request(method, url, **kwargs)
                )
                assert response is not None
                logger.debug(f"Response: {response.status}: {response}")
                if response.status < 500:
                    # pre-read the response to avoid getting errors outside of the context manager
                    raw_response = await response.read()  # noqa
                    yield response
                    return
                self.print(_("error", "site_down").format(seconds=round(delay)))
            except aiohttp.ClientConnectorCertificateError:
                # for a case where SSL verification fails
                raise
            except (
                aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError
            ):
                # connection problems, retry
                if backoff.steps > 1:
                    # just so that quick retries that sometimes happen, aren't shown
                    self.print(
                        _("error", "no_connection").format(seconds=round(delay), url=str(url))
                    )
            finally:
                if response is not None:
                    response.release()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.gui.wait_until_closed(), timeout=delay)

    @overload
    async def gql_request(self, ops: GQLOperation) -> JsonType:
        ...

    @overload
    async def gql_request(self, ops: list[GQLOperation]) -> list[JsonType]:
        ...

    async def gql_request(
        self, ops: GQLOperation | list[GQLOperation]
    ) -> JsonType | list[JsonType]:
        gql_logger.debug(f"GQL Request: {ops}")
        backoff = ExponentialBackoff(maximum=60)
        # Use a flag to retry the request a single time, if a specific set of errors is encountered
        single_retry: bool = True
        for delay in backoff:
            async with self._qgl_limiter:
                auth_state = await self.get_auth()
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_state.headers(user_agent=self._client_type.USER_AGENT, gql=True),
                ) as response:
                    response_json: JsonType | list[JsonType] = await response.json()
            gql_logger.debug(f"GQL Response: {response_json}")
            orig_response = response_json
            if isinstance(response_json, list):
                response_list = response_json
            else:
                response_list = [response_json]
            force_retry: bool = False
            for response_json in response_list:
                # GQL error handling
                if "errors" in response_json:
                    for error_dict in response_json["errors"]:
                        if "message" in error_dict:
                            if (
                                single_retry
                                and error_dict["message"] in (
                                    "service error",
                                    "PersistedQueryNotFound",
                                )
                            ):
                                # NOTE: this is a retry, not a failure - Twitch answers
                                # "service error" to a few requests during every scan, and
                                # the retry almost always succeeds. Kept out of the normal
                                # output so it doesn't read like a problem.
                                gql_logger.debug(
                                    f"Retrying a {error_dict['message']} for "
                                    f"{response_json['extensions']['operationName']}"
                                )
                                single_retry = False
                                if delay < 5:
                                    # overwrite the delay if too short
                                    delay = 5
                                force_retry = True
                                break
                            elif error_dict["message"] == "server error":
                                # nullify the key the error path points to
                                data_dict: JsonType = response_json["data"]
                                path: list[str] = error_dict.get("path", [])
                                for key in path[:-1]:
                                    data_dict = data_dict[key]
                                data_dict[path[-1]] = None
                                break
                            elif (
                                error_dict["message"] in (
                                    "service timeout",
                                    "request cancelled",
                                    "service unavailable",
                                    "context deadline exceeded",
                                )
                            ):
                                force_retry = True
                                break
                    else:
                        raise GQLException(response_json['errors'])
                # Other error handling
                elif "error" in response_json:
                    raise GQLException(
                        f"{response_json['error']}: {response_json['message']}"
                    )
                if force_retry:
                    break
            else:
                return orig_response
            await asyncio.sleep(delay)
        raise RuntimeError("Retry loop was broken")

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        merged = {}
        for key in set(chain(primary_data.keys(), secondary_data.keys())):
            in_primary = key in primary_data
            if in_primary and key in secondary_data:
                vp = primary_data[key]
                vs = secondary_data[key]
                if not isinstance(vp, type(vs)) or not isinstance(vs, type(vp)):
                    raise MinerException("Inconsistent merge data")
                if isinstance(vp, dict):  # both are dicts
                    merged[key] = self._merge_data(vp, vs)
                else:
                    # use primary value
                    merged[key] = vp
            elif in_primary:
                merged[key] = primary_data[key]
            else:  # in campaigns only
                merged[key] = secondary_data[key]
        return merged

    @staticmethod
    def _is_watch_based_campaign(campaign_data: JsonType) -> bool:
        """
        True if at least one of the campaign's drops is earned by watching the stream.

        Channel campaigns often ask for gifted subscriptions instead, which no amount of
        watching will ever progress - those must not be picked up, or the miner looks
        like it works while the counter can never move.
        """
        return any(
            (drop.get("requiredMinutesWatched") or 0) > 0 and not drop.get("requiredSubs")
            for drop in campaign_data.get("timeBasedDrops") or []
        )

    def _build_channel_campaign(
        self, campaign_data: JsonType, channel: Channel
    ) -> JsonType | None:
        """
        Converts a campaign offered by a single channel into one the miner can work with.

        Only the drops earnable by watching are kept, and the channel offering them is
        set as the campaign's ACL, so the campaign is only ever mined on that channel.
        """
        timed_drops: list[JsonType] = []
        for drop_data in campaign_data.get("timeBasedDrops") or []:
            required_minutes: int = drop_data.get("requiredMinutesWatched") or 0
            if required_minutes <= 0 or drop_data.get("requiredSubs"):
                continue
            benefit_edges: list[JsonType] = []
            for edge in drop_data.get("benefitEdges") or []:
                benefit: JsonType = dict(edge["benefit"])
                benefit.setdefault("distributionType", "UNKNOWN")
                benefit.setdefault("imageAssetURL", campaign_data.get("imageURL") or "")
                benefit_edges.append({**edge, "benefit": benefit})
            timed_drops.append({
                "id": drop_data["id"],
                "name": drop_data.get("name") or campaign_data.get("name") or channel.name,
                "startAt": drop_data.get("startAt") or campaign_data.get("startAt"),
                "endAt": drop_data.get("endAt") or campaign_data["endAt"],
                "requiredMinutesWatched": required_minutes,
                "preconditionDrops": None,
                "benefitEdges": benefit_edges,
            })
        if not timed_drops:
            return None
        return {
            "id": campaign_data["id"],
            "name": campaign_data.get("name") or f"{channel.name} rewards",
            "game": {
                "id": CHANNEL_DROPS_GAME_ID,
                "name": CHANNEL_DROPS_GAME_NAME,
                "displayName": CHANNEL_DROPS_GAME_NAME,
                "boxArtURL": campaign_data.get("imageURL") or "",
            },
            "self": {"isAccountConnected": True},
            "accountLinkURL": "",
            "startAt": min(drop["startAt"] for drop in timed_drops),
            "endAt": campaign_data["endAt"],
            "status": "ACTIVE",
            "allow": {
                "channels": [
                    {
                        "id": str(channel.id),
                        "name": channel._login,
                        "displayName": channel._display_name,
                    }
                ],
                "isEnabled": True,
            },
            "timeBasedDrops": timed_drops,
        }

    async def helix_request(self, path: str, params: JsonType | None = None) -> JsonType:
        """
        Queries the Helix API with the account's own token.

        Needed because GQL can no longer be asked what campaigns exist: Helix still
        answers the popular games list and the live drops-enabled channels, and those are
        what the drop scan is built from.
        """
        auth_state = await self.get_auth()
        url = URL(f"https://api.twitch.tv/helix/{path}")
        if params:
            url = url.with_query({key: str(value) for key, value in params.items()})
        headers = {
            "Authorization": f"Bearer {auth_state.access_token}",
            "Client-Id": self._client_type.CLIENT_ID,
        }
        async with self.request("GET", url, headers=headers) as response:
            if response.status != 200:
                raise RequestException(f"Helix {path}: HTTP {response.status}")
            return await response.json()

    async def helix_top_games(self, limit: int) -> list[Game]:
        games: list[Game] = []
        cursor: str | None = None
        while len(games) < limit:
            params: JsonType = {"first": min(100, limit - len(games))}
            if cursor is not None:
                params["after"] = cursor
            response_json: JsonType = await self.helix_request("games/top", params)
            for game_data in response_json.get("data") or []:
                games.append(Game({"id": game_data["id"], "name": game_data["name"]}))
            cursor = (response_json.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return games

    async def helix_drops_channels(self, pages: int = 2) -> list[Channel]:
        """
        Live channels Twitch itself tags as drops-enabled, most watched first.
        """
        channels: list[Channel] = []
        seen: set[int] = set()
        cursor: str | None = None
        for _page in range(pages):
            params: JsonType = {"first": 100}
            if cursor is not None:
                params["after"] = cursor
            response_json: JsonType = await self.helix_request("streams", params)
            for stream_data in response_json.get("data") or []:
                if "DropsEnabled" not in (stream_data.get("tags") or []):
                    continue
                channel_id = int(stream_data["user_id"])
                if channel_id in seen:
                    continue
                seen.add(channel_id)
                channels.append(
                    Channel(
                        self,
                        id=channel_id,
                        login=stream_data["user_login"],
                        display_name=stream_data["user_name"],
                    )
                )
            cursor = (response_json.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return channels

    def load_game_catalogue(self) -> dict[str, JsonType]:
        """
        Games the scan has learned about so far, keyed by name.

        Twitch has no way to list the games that currently carry drops, and the popular
        games list is a poor stand-in: the drops are usually on the niche titles. Every
        game the scan has ever seen is remembered here, so that a game found once keeps
        being checked on the later scans.
        """
        try:
            with open(GAMES_CATALOGUE_PATH, encoding="utf8") as catalogue_file:
                catalogue_data: JsonType = json.load(catalogue_file)
        except (OSError, ValueError):
            return {}
        games = catalogue_data.get("games")
        if not isinstance(games, dict):
            return {}
        return {
            name: entry for name, entry in games.items() if isinstance(entry, dict)
        }

    def save_game_catalogue(
        self,
        catalogue: dict[str, JsonType],
        seed_state: JsonType | None = None,
    ) -> None:
        data: JsonType = {"updated": isonow(), "games": catalogue}
        if seed_state is not None:
            data["seed"] = seed_state
        else:
            # keep whatever progress the catalogue search has made so far
            previous = self.load_catalogue_seed_state()
            if previous:
                data["seed"] = previous
        try:
            with open(GAMES_CATALOGUE_PATH, "w", encoding="utf8") as catalogue_file:
                json.dump(data, catalogue_file, ensure_ascii=False, indent=1)
        except OSError as exc:
            logger.warning(f"Drop scan: unable to save the games catalogue: {exc}")

    def load_catalogue_seed_state(self) -> JsonType:
        """
        How far the search for new games got: which query it's on, and its page cursor.
        """
        try:
            with open(GAMES_CATALOGUE_PATH, encoding="utf8") as catalogue_file:
                catalogue_data: JsonType = json.load(catalogue_file)
        except (OSError, ValueError):
            return {}
        seed_state = catalogue_data.get("seed")
        return seed_state if isinstance(seed_state, dict) else {}

    def _load_games_seed(self) -> JsonType | None:
        """
        The newest games seed available: the refreshed copy wins over the bundled one.
        """
        best: JsonType | None = None
        for path in (GAMES_SEED_PATH, GAMES_SEED_CACHE_PATH):
            try:
                with open(path, encoding="utf8") as seed_file:
                    seed: JsonType = json.load(seed_file)
            except (OSError, ValueError):
                continue
            if not isinstance(seed, dict) or not isinstance(seed.get("games"), list):
                continue
            if best is None or str(seed.get("generated") or "") > str(best.get("generated") or ""):
                best = seed
        return best

    async def _load_json_feed(self, cache_path: Path) -> JsonType | None:
        try:
            with open(cache_path, encoding="utf8") as feed_file:
                return json.load(feed_file)
        except (OSError, ValueError):
            return None

    async def _refresh_json_feed(
        self, url: str, cache_path: Path, label: str, force: bool = False
    ) -> bool:
        """
        Fetches a feed published by the maintainer, at most once per seed_update_hours.

        Without network, a non-200 answer or a broken file the current feed stays in place and
        nothing is reported in the GUI - the app has to work offline regardless. `force` skips
        the interval check, for the GUI's feed button.
        """
        if not self.settings.seed_auto_update and not force:
            return False
        if not force:
            try:
                if cache_path.exists():
                    age: float = time() - cache_path.stat().st_mtime
                    if age < int(self.settings.seed_update_hours) * 3600:
                        return False
            except OSError:
                pass
        try:
            session = await self.get_session()
            async with session.get(url) as response:
                if response.status != 200:
                    logger.log(CALL, f"Drop scan: {label} refresh answered {response.status}")
                    return False
                fetched: JsonType = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.log(CALL, f"Drop scan: {label} refresh skipped: {exc}")
            return False
        payload = None
        if isinstance(fetched, dict):
            for key in ("games", "campaigns"):
                if isinstance(fetched.get(key), list):
                    payload = fetched[key]
                    break
        if not payload:
            return False
        current = await self._load_json_feed(cache_path)
        if current is not None and str(fetched.get("generated") or "") <= str(
            current.get("generated") or ""
        ):
            try:
                cache_path.touch(exist_ok=True)
            except OSError:
                pass
            return False
        try:
            temporary = cache_path.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf8") as feed_file:
                json.dump(fetched, feed_file, ensure_ascii=False)
            temporary.replace(cache_path)
        except OSError as exc:
            logger.log(CALL, f"Drop scan: unable to store the refreshed {label}: {exc}")
            return False
        logger.info(
            f"Drop scan: {label} refreshed from the repository, {len(payload)} entries, "
            f"{fetched.get('generated') or 'no date'}"
        )
        return True

    async def refresh_feeds(self, force: bool = False) -> dict[str, bool]:
        """
        Refreshes both feeds published by the maintainer.

        They are what the scan works from - which games to probe deep and which catalogue to
        start from - so a machine that runs for days has to pick up new versions while it runs,
        not only at startup. The interval itself lives in `_refresh_json_feed`.

        Returns which of the two actually changed, so the caller can tell "nothing new" from
        "downloaded".
        """
        seed, campaigns = await asyncio.gather(
            self._update_games_seed(force=force),
            self._update_campaigns_feed(force=force),
        )
        return {"games seed": seed, "campaigns feed": campaigns}

    async def _update_games_seed(self, force: bool = False) -> bool:
        """
        Refreshes the games seed from the fork's repository, at most every N hours.

        Twitch doesn't list the games that carry drops, so the seed is what a new installation
        starts from and it has to keep up with the campaigns: the file in the repository is
        updated by the maintainer, and this only fetches it. Without network the bundled copy
        stays in use, silently - the app has to start and work regardless.
        """
        return await self._refresh_json_feed(
            GAMES_SEED_URL, GAMES_SEED_CACHE_PATH, "games seed", force=force
        )

    async def _update_campaigns_feed(self, force: bool = False) -> bool:
        """
        Refreshes the campaigns feed published by the maintainer.

        The feed is what makes discovery cheap: a game whose campaign is running or about to
        start is probed deep on the next scan, instead of waiting for the slice rotation of a
        catalogue with thousands of games in it.
        """
        return await self._refresh_json_feed(
            CAMPAIGNS_URL, CAMPAIGNS_CACHE_PATH, "campaigns feed", force=force
        )

    def _load_campaigns_feed(self) -> JsonType | None:
        """The freshest campaigns feed: the downloaded copy, else the one bundled with the app."""
        for path in (CAMPAIGNS_CACHE_PATH, _resource_path("campaigns.json")):
            try:
                with open(path, encoding="utf8") as feed_file:
                    feed: JsonType = json.load(feed_file)
            except (OSError, ValueError):
                continue
            if isinstance(feed, dict) and isinstance(feed.get("campaigns"), list):
                return feed
        return None

    def _feed_active_games(self) -> dict[str, str]:
        """
        Games from the campaigns feed whose campaign runs or starts soon.

        Returns a mapping of the casefolded name to the name as written in the feed, so the
        scan can both match games it already knows and probe a game only the feed talks about.
        """
        loaded: JsonType | None = self._load_campaigns_feed()
        if loaded is None:
            return {}
        campaigns = loaded.get("campaigns") if isinstance(loaded, dict) else None
        if not isinstance(campaigns, list):
            return {}
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=CAMPAIGNS_LOOKAHEAD_DAYS)
        names: dict[str, str] = {}
        for entry in campaigns:
            if not isinstance(entry, dict):
                continue
            game_name = entry.get("game")
            if not isinstance(game_name, str) or not game_name:
                continue
            end_at = entry.get("endAt")
            start_at = entry.get("startAt")
            try:
                if end_at and timestamp(str(end_at)) < now:
                    continue
                if start_at and timestamp(str(start_at)) > horizon:
                    continue
            except ValueError:
                pass
            names.setdefault(game_name.casefold(), game_name)
        return names

    def _apply_games_seed(
        self, catalogue: dict[str, JsonType], seed_state: JsonType
    ) -> JsonType:
        """
        Adds the games shipped with the app to the catalogue, once per seed version.

        Twitch doesn't expose the games that carry drops anywhere, so a bundle of known
        drops games is what makes a fresh install useful straight away: the slice rotation
        probes them, and the category search only has to cover what the list doesn't know.
        """
        seed = self._load_games_seed()
        if seed is None:
            return seed_state
        stamp: str = str(seed.get("generated") or "")
        if not stamp or stamp == str(seed_state.get("games_seed") or ""):
            return seed_state
        names = seed.get("games")
        if not isinstance(names, list):
            return seed_state
        excluded: set[str] = {name.casefold() for name in self.settings.exclude}
        known: set[str] = {name.casefold() for name in catalogue}
        now = isonow()
        added = 0
        for game_name in names:
            if not isinstance(game_name, str) or not game_name.strip():
                continue
            if game_name.casefold() in excluded or game_name.casefold() in known:
                continue
            catalogue[game_name] = {
                "name": game_name,
                "first_seen": now,
                "seeded": now,
                "source": "seed",
                "scans": 0,
                "drops_found": 0,
            }
            known.add(game_name.casefold())
            added += 1
        if added:
            logger.info(f"Drop scan: {added} game(s) taken from the bundled seed list")
        updated = dict(seed_state)
        updated["games_seed"] = stamp
        return updated

    async def _seed_game_catalogue(
        self,
        catalogue: dict[str, JsonType],
        seed_state: JsonType,
        requests_budget: int,
    ) -> tuple[list[Game], JsonType]:
        """
        Harvests games from Twitch's category search, a few pages per scan.

        The popular games list never covers the niche titles, and nothing on Twitch lists
        the games that carry drops, so the catalogue has to be grown from the search: it
        answers without an integrity check and returns hundreds of matches per query.
        The games land in the catalogue and get probed on later scans, as the probe slice
        rotates through it.
        """
        seeded: list[Game] = []
        terms: tuple[str, ...] = CATALOGUE_SEED_QUERIES
        if not terms or requests_budget <= 0:
            return seeded, seed_state
        term_index = int(seed_state.get("term_index") or 0) % len(terms)
        failures: int = 0
        for _ in range(requests_budget):
            term = terms[term_index]
            variables: JsonType = {
                "platform": "web",
                "query": term,
                "options": {"targets": None, "shouldSkipDiscoveryControl": False},
                "requestID": str(uuid.uuid4()),
            }
            operation = GQL_QUERIES["SearchCategories"].with_variables(variables)
            try:
                response = await self.gql_request(operation)
            except MinerException as exc:
                # Twitch answers a "service error" now and then: skip that term, the next
                # one is a different query anyway
                logger.log(CALL, f"Drop scan: category search for {term!r} failed: {exc}")
                failures += 1
                term_index = (term_index + 1) % len(terms)
                if failures >= 3:
                    break
                continue
            failures = 0
            search_data: JsonType = (response.get("data") or {}).get("searchFor") or {}
            games_data: JsonType = search_data.get("games") or {}
            now = isonow()
            for edge in games_data.get("edges") or []:
                item: JsonType = edge.get("item") or {}
                game_name: str | None = item.get("displayName") or item.get("name")
                if not game_name or game_name in catalogue:
                    continue
                if game_name.casefold() in {name.casefold() for name in self.settings.exclude}:
                    continue
                catalogue[game_name] = {
                    "name": game_name,
                    "first_seen": now,
                    "seeded": now,
                    "scans": 0,
                    "drops_found": 0,
                    **({"id": int(item["id"])} if item.get("id") else {}),
                }
                seeded.append(Game({"id": int(item.get("id") or 0), "name": game_name}))
            # the search ignores its own cursor (every page comes back identical), so the
            # only way to see other games is to move on to the next query
            term_index = (term_index + 1) % len(terms)
        if seeded:
            logger.log(CALL, f"Drop scan: {len(seeded)} new game(s) harvested from the search")
        updated = dict(seed_state)
        updated["term_index"] = term_index
        return seeded, updated

    def _update_game_catalogue(
        self,
        catalogue: dict[str, JsonType],
        probed_games: abc.Iterable[Game],
        games_with_drops: abc.Iterable[str],
    ) -> dict[str, JsonType]:
        now = isonow()
        for game in probed_games:
            entry = catalogue.setdefault(
                game.name,
                {"name": game.name, "first_seen": now, "scans": 0, "drops_found": 0},
            )
            entry["last_scan"] = now
            entry["scans"] = int(entry.get("scans") or 0) + 1
            if game.id:
                entry["id"] = game.id
        for name in games_with_drops:
            entry = catalogue.setdefault(
                name, {"name": name, "first_seen": now, "scans": 0, "drops_found": 0}
            )
            entry["last_drop"] = now
            entry["drops_found"] = int(entry.get("drops_found") or 0) + 1
        # a game that never had drops and hasn't been popular in a while is not worth
        # checking on every scan anymore
        keep_after = datetime.now(timezone.utc) - timedelta(days=CATALOGUE_KEEP_DAYS)
        return {
            name: entry
            for name, entry in catalogue.items()
            if entry.get("last_drop") is not None
            or timestamp(entry.get("last_scan") or now) > keep_after
        }

    @property
    def scan_progress(self) -> dict[str, Any]:
        return self._scan_progress

    def _set_scan_progress(
        self, phase: str, done: int, total: int, game: str, campaigns: int
    ) -> None:
        self._scan_progress = {
            "phase": phase,
            "done": done,
            "total": total,
            "game": game,
            "campaigns": campaigns,
        }

    def _note_scan_attempt(self, games_probed: int, channels_asked: int) -> JsonType:
        """
        Records that a scan finished, even when it saw no live channels.

        An empty result used to look like "never scanned", so the next inventory
        reload started another scan. Campaigns already on disk stay put.
        """
        previous, _stale = self.load_scan_cache()
        if previous.get("scanned_at"):
            previous["scanned_at"] = isonow()
            previous["games_probed"] = games_probed
            previous["channels_asked"] = channels_asked
            self._write_scan_cache(previous)
            return previous
        scan_data: JsonType = {
            "scanned_at": isonow(),
            "channels_asked": channels_asked,
            "games_probed": games_probed,
            "games_with_drops": [],
            "campaigns": {},
            "channel_campaigns": {},
        }
        self._write_scan_cache(scan_data)
        return scan_data

    def _write_scan_cache(self, scan_data: JsonType) -> None:
        temporary = SCAN_PATH.with_name(f"{SCAN_PATH.name}.new")
        try:
            with open(temporary, "w", encoding="utf8") as scan_file:
                json.dump(scan_data, scan_file, ensure_ascii=False, indent=1)
            temporary.replace(SCAN_PATH)
        except OSError as exc:
            logger.warning(f"Drop scan: unable to save the results: {exc}")

    @property
    def scan_running(self) -> bool:
        # NOTE: the GUI is built before the task fields exist, so this has to be lenient
        task = getattr(self, "_scan_task", None)
        return task is not None and not task.done()

    def scan_coverage(self) -> JsonType:
        """
        How much of the games catalogue has been checked for drops at least once.

        The scan walks the catalogue in slices, so a fresh installation needs a while before
        every known game has been looked at - this is what the GUI shows as a progress bar.
        """
        catalogue: dict[str, JsonType] = self.load_game_catalogue()
        total: int = len(catalogue)
        probed: int = sum(1 for entry in catalogue.values() if entry.get("last_scan"))
        seed_games: int = sum(
            1
            for entry in catalogue.values()
            if entry.get("source") == "seed" or entry.get("seeded")
        )
        scan_data, _stale, _known = self.scan_status()
        campaigns: int = len((scan_data or {}).get("games_with_drops") or [])
        last_scan: str = ""
        if probed:
            last_scan = max(
                (str(entry.get("last_scan") or "") for entry in catalogue.values()), default=""
            )
        return {
            "probed": probed,
            "total": total,
            "seed": seed_games,
            "campaigns": campaigns,
            "last_scan": last_scan,
            "complete": probed >= total > 0,
        }

    def scan_status(self) -> tuple[JsonType, bool, int]:
        """
        The last scan results, whether they are stale, and how many games are known.
        """
        scan_data, stale = self.load_scan_cache()
        return scan_data, stale, len(self.load_game_catalogue())

    def load_scan_cache(self) -> tuple[JsonType, bool]:
        """
        Returns the last drop scan results, and whether they are stale.
        """
        try:
            with open(SCAN_PATH, encoding="utf8") as scan_file:
                scan_data: JsonType = json.load(scan_file)
        except (OSError, ValueError):
            return {}, True
        try:
            scanned_at = timestamp(scan_data["scanned_at"])
        except (KeyError, ValueError):
            return {}, True
        interval = timedelta(minutes=max(1, self.settings.scan_interval_minutes))
        return scan_data, datetime.now(timezone.utc) - scanned_at > interval

    async def scan_drops(self) -> JsonType:
        """
        Builds the list of games with drops available for the account, and the campaigns
        behind them (game campaigns and channel campaigns).

        Twitch answers the campaigns dashboard query with nothing at all since Sep 2026,
        so nothing can be looked up by campaign: instead the popular games are enumerated
        through Helix, every one of them is checked for live drops-enabled channels, and
        each of those channels reports the campaigns available to this account.
        """
        # Only games that already have drops, plus the priority list and the published
        # feed. Walking the whole catalogue is what made the first scan look stuck.
        catalogue: dict[str, JsonType] = self.load_game_catalogue()
        seed_state: JsonType = self.load_catalogue_seed_state()
        excluded_names: set[str] = {name.casefold() for name in self.settings.exclude}
        priority_names: set[str] = {name.casefold() for name in self.settings.priority}
        priority_names.update(game.name.casefold() for game in self.wanted_games)
        feed_games: dict[str, str] = self._feed_active_games()
        feed_names: set[str] = set(feed_games)
        recently_had_drops: str = (
            datetime.now(timezone.utc) - timedelta(days=SCAN_KNOWN_DROPPER_DAYS)
        ).strftime("%Y-%m-%d")
        known_names: set[str] = set(priority_names)
        known_names.update(feed_names)
        previous_scan, _previous_stale = self.load_scan_cache()
        for game_name in previous_scan.get("games_with_drops") or []:
            if isinstance(game_name, str):
                known_names.add(game_name.casefold())
        for name, entry in catalogue.items():
            last_drop = str(entry.get("last_drop") or "")
            if last_drop and last_drop[:10] >= recently_had_drops:
                known_names.add(name.casefold())
        known_names.difference_update(excluded_names)
        games: dict[str, Game] = {}
        for name, entry in catalogue.items():
            if name.casefold() not in known_names:
                continue
            games[name] = Game({"id": int(entry.get("id") or 0), "name": name})
        for campaign in self.inventory:
            folded = campaign.game.name.casefold()
            if campaign.game.is_channel_drops() or folded not in known_names:
                continue
            games[campaign.game.name] = campaign.game
        for game_name in self.settings.priority:
            if game_name.casefold() in known_names:
                games.setdefault(game_name, Game({"id": 0, "name": game_name}))
        for feed_name in feed_games.values():
            if feed_name.casefold() in known_names:
                games.setdefault(feed_name, Game({"id": 0, "name": feed_name}))
        if feed_names:
            logger.log(CALL, f"Drop scan: {len(feed_names)} game(s) taken from the campaigns feed")

        # channels to ask about: live drops-enabled channels of every candidate game
        # NOTE: Twitch reports a campaign only on some of the channels streaming a game
        # (measured: 5 of 60 live channels on a busy game), so a game the user actually
        # wants is probed much deeper than the rest of the catalogue
        shallow_depth: int = max(1, int(self.settings.scan_channels_per_game))
        deep_depth: int = max(shallow_depth, int(self.settings.scan_priority_channels))
        feed_depth: int = max(shallow_depth, SCAN_FEED_CHANNELS_PER_GAME)
        channels: dict[int, Channel] = {}
        channel_games: dict[int, Game] = {}

        def depth_for(game: Game) -> int:
            folded = game.name.casefold()
            if folded in priority_names:
                return deep_depth
            if folded in feed_names:
                return feed_depth
            return shallow_depth

        async def probe_games(game_list: list[Game], phase: str) -> None:
            total = len(game_list)
            done = 0
            self._set_scan_progress(phase, 0, total, "", len(game_campaigns))
            for group in chunk(game_list, 5):
                async def lookup(game: Game) -> tuple[Game, list[Channel]]:
                    try:
                        found = await self.get_live_streams(game, limit=depth_for(game))
                    except MinerException:
                        logger.log(CALL, f"Drop scan: no live drops-enabled channels for: {game.name}")
                        found = []
                    return game, found

                for game, found in await asyncio.gather(*(lookup(game) for game in group)):
                    done += 1
                    for channel in found:
                        channels[channel.id] = channel
                        channel_games[channel.id] = game
                    self._set_scan_progress(phase, done, total, game.name, len(game_campaigns))

        # One pass over the games that actually have a known campaign. The extra
        # catalogue tail used to keep running after the first results and the inventory
        # was never rebuilt from the finished file.
        urgent_games = list(games.values())
        self.print(
            _("gui", "settings", "scan", "started").format(count=len(urgent_games))
        )
        game_campaigns: dict[str, JsonType] = {}
        raw_channel_campaigns: dict[str, JsonType] = {}
        channel_campaigns: dict[str, JsonType] = {}
        games_with_drops: set[str] = set()
        asked: set[int] = set()
        await probe_games(urgent_games, "games")
        if self.settings.channel_campaigns == "all":
            # additionally ask about the most watched drops-enabled channels overall
            try:
                for channel in await self.helix_drops_channels():
                    channels.setdefault(channel.id, channel)
            except (MinerException, RequestException) as exc:
                logger.warning(f"Drop scan: unable to fetch drops-enabled channels: {exc}")
        if not channels:
            logger.warning("Drop scan: no live drops-enabled channels found at all")
            return self._note_scan_attempt(len(urgent_games), 0)

        async def ask_channels(to_ask: dict[int, Channel]) -> None:
            """Asks the channels what campaigns they carry and merges the results."""
            if not to_ask:
                return
            asked.update(to_ask)
            available_drops_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id in to_ask
            ]
            batches = list(chunk(available_drops_ops, 20))
            for index, available_drops_chunk in enumerate(batches, start=1):
                self._set_scan_progress(
                    "channels", index, len(batches), "", len(game_campaigns)
                )
                try:
                    response_list: list[JsonType] = await self.gql_request(available_drops_chunk)
                except MinerException as exc:
                    logger.warning(f"Drop scan: available drops batch failed: {exc}")
                    continue
                for response_json in response_list:
                    channel_info: JsonType | None = (response_json.get("data") or {}).get("channel")
                    if not isinstance(channel_info, dict) or "id" not in channel_info:
                        continue
                    channel = to_ask.get(int(channel_info["id"]))
                    if channel is None:
                        continue
                    for campaign_data in channel_info.get("viewerDropCampaigns") or []:
                        if campaign_data.get("game") is not None:
                            if campaign_data["id"] in game_campaigns:
                                continue
                            if not campaign_data.get("timeBasedDrops"):
                                continue
                            built = self._build_game_campaign(campaign_data)
                            if built is not None:
                                game_campaigns[campaign_data["id"]] = built
                                games_with_drops.add(built["game"]["displayName"])
                            continue
                        # a campaign offered by this channel itself (a badge, an emote, ...)
                        raw_channel_campaigns.setdefault(
                            campaign_data["id"], {"data": campaign_data, "channel": channel}
                        )

        await ask_channels(dict(channels))
        if not asked:
            logger.warning("Drop scan: no live drops-enabled channels found at all")
            return self._note_scan_attempt(len(urgent_games), len(channels))

        # Twitch reports a campaign only on some of the channels streaming it (measured: 5 of
        # 60 on a busy game), so a game with live drops-enabled channels that told us nothing
        # gets asked again, deeper. Doing it here rather than up front keeps the depth
        # affordable: only the games that actually have drops-enabled streams pay for it.
        games_with_campaign: set[str] = {
            campaign["game"]["displayName"] for campaign in game_campaigns.values()
        }
        shallow_games: dict[str, Game] = {}
        for channel_id, channel_game in channel_games.items():
            if channel_id not in asked:
                continue
            if depth_for(channel_game) >= deep_depth:
                # the first pass already asked this game at full depth
                continue
            if channel_game.name in games_with_campaign:
                continue
            shallow_games.setdefault(channel_game.name, channel_game)
        escalated = 0
        for channel_game in list(shallow_games.values())[:SCAN_ESCALATION_GAMES]:
            try:
                deeper = await self.get_live_streams(channel_game, limit=deep_depth)
            except MinerException:
                continue
            extra: dict[int, Channel] = {
                channel.id: channel for channel in deeper if channel.id not in asked
            }
            if not extra:
                continue
            for channel_id in extra:
                channel_games.setdefault(channel_id, channel_game)
            await ask_channels(extra)
            escalated += 1
        if escalated:
            logger.log(
                CALL,
                f"Drop scan: {escalated} game(s) asked deeper, their channels reported nothing",
            )
        for campaign_id, entry in raw_channel_campaigns.items():
            campaign_data: JsonType = entry["data"]
            channel: Channel = entry["channel"]
            channel_game = channel_games.get(channel.id)
            if not self._is_watch_based_campaign(campaign_data):
                # nothing here can be earned by watching, ex. gifted-sub rewards
                continue
            if self.settings.channel_campaigns == "off":
                continue
            if self.settings.channel_campaigns != "all" and (
                channel_game is None
                or (channel_game.name not in self.settings.priority and channel_game not in self.wanted_games)
            ):
                # "relevant" mode: only for the games actually being farmed
                continue
            built = self._build_channel_campaign(campaign_data, channel)
            if built is None:
                continue
            channel_campaigns[campaign_id] = built
            games_with_drops.add(built["game"]["displayName"])
        if len(channel_campaigns) > self.settings.channel_campaigns_limit:
            # keep the ones ending soonest, the rest is likely to expire anyway
            ordered = sorted(
                channel_campaigns.items(), key=lambda item: item[1]["endAt"]
            )[: self.settings.channel_campaigns_limit]
            channel_campaigns = dict(ordered)

        # A campaign Twitch failed to report this time round is not gone: the queries only
        # see the channels that happen to be live right now. Keep every campaign the
        # previous scans found for as long as it hasn't ended, so a single unlucky scan
        # doesn't drop the drops (and their progress) out of the inventory.
        carried = self._carry_over_campaigns(game_campaigns, channel_campaigns)
        for campaign_data in carried:
            games_with_drops.add(campaign_data["game"]["displayName"])

        catalogue = self._update_game_catalogue(catalogue, urgent_games, games_with_drops)
        self.save_game_catalogue(catalogue, seed_state)
        scan_data: JsonType = {
            "scanned_at": isonow(),
            "channels_asked": len(channels),
            "games_probed": len(urgent_games),
            "games_with_drops": sorted(games_with_drops),
            "campaigns": game_campaigns,
            "channel_campaigns": channel_campaigns,
        }
        self._write_scan_cache(scan_data)
        logger.info(
            f"Drop scan: {len(urgent_games)} game(s) checked, {len(channels)} channels asked, "
            f"{len(game_campaigns)} game campaign(s) and "
            f"{len(channel_campaigns)} channel campaign(s) available, "
            f"{len(games_with_drops)} game(s) with drops, "
            f"{len(catalogue)} game(s) remembered for the next scan"
        )
        # NOTE: found games are never added to the priority list on their own - the list is
        # the user's choice of what to farm, so it is only ever edited by hand
        return scan_data

    def _carry_over_campaigns(
        self, game_campaigns: dict[str, JsonType], channel_campaigns: dict[str, JsonType]
    ) -> list[JsonType]:
        """
        Merges the campaigns the previous scan found into the fresh results.

        Twitch answers the per-channel query, and a campaign is only reported by the few
        channels it is assigned to - so whether a scan sees a campaign depends on which
        channels are live at that moment. Without this the inventory loses campaigns (and
        the watch progress made on them) whenever a scan draws blanks.

        Campaigns that have ended are dropped, and so are the channel campaigns when the
        user doesn't want them at all.
        """
        previous_data, _stale = self.load_scan_cache()
        now = datetime.now(timezone.utc)
        carried: list[JsonType] = []
        sources: list[tuple[str, dict[str, JsonType]]] = [("campaigns", game_campaigns)]
        if self.settings.channel_campaigns != "off":
            sources.append(("channel_campaigns", channel_campaigns))
        for scan_key, target in sources:
            for campaign_id, campaign_data in (previous_data.get(scan_key) or {}).items():
                if campaign_id in target or not isinstance(campaign_data, dict):
                    continue
                try:
                    ends_at = timestamp(campaign_data["endAt"])
                    campaign_data["game"]["displayName"]
                except (KeyError, ValueError, TypeError):
                    continue
                if ends_at <= now:
                    # the campaign is over, nothing to keep
                    continue
                target[campaign_id] = campaign_data
                carried.append(campaign_data)
        if carried:
            logger.log(
                CALL,
                f"Drop scan: kept {len(carried)} campaign(s) the scan didn't see this time",
            )
        return carried

    def campaigns_feed_status(self) -> tuple[int, str]:
        """
        How many campaigns the stored feed carries, and when it was published.

        The GUI shows this next to the feed button, so "the list is stale" is visible without
        digging into the console.
        """
        feed = self._load_campaigns_feed()
        if not isinstance(feed, dict):
            return 0, ""
        campaigns = feed.get("campaigns")
        if not isinstance(campaigns, list):
            return 0, ""
        return len(campaigns), str(feed.get("generated") or "")

    def request_feed_refresh(self) -> None:
        """
        Forces a campaigns/games feed refresh and re-runs the scan with what arrived.

        Called from the GUI button; thread-safe, because the click arrives on the Tk thread
        while the refresh has to happen on the event loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            loop.call_soon_threadsafe(self.request_feed_refresh)
            return
        if self._feed_task is not None and not self._feed_task.done():
            return
        self._feed_task = asyncio.create_task(self._feed_refresh_task())

    async def _feed_refresh_task(self) -> None:
        try:
            updated = await self.refresh_feeds(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Drop scan: the requested feed refresh failed: {exc}")
            return
        changed = [name for name, downloaded in updated.items() if downloaded]
        count, generated = self.campaigns_feed_status()
        logger.info(
            f"Drop scan: the feeds were refreshed on request "
            f"({', '.join(changed) if changed else 'nothing new'}), list of campaigns: "
            f"{count}, published {generated or 'unknown'}"
        )
        if changed:
            self.start_scan()
        else:
            self.print(_("gui", "settings", "feed_current"))

    def start_scan(self) -> None:
        """
        Starts a drop scan in the background, unless one is already running.

        Kept out of the mining loop on purpose: a scan takes a minute or so, and the
        mining must keep sending watch payloads while it runs.
        """
        if self.scan_running:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # not called from the event loop (ex. a GUI callback): hop onto it first
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            loop.call_soon_threadsafe(self.start_scan)
            return
        # assigned before the task can print, so a second click cannot start a twin scan
        self._scan_task = asyncio.create_task(self._scan_task_wrapper())

    async def _scan_loop(self) -> None:
        """
        Loads every known drop once, when nothing has been saved yet.

        A timer used to start another scan whenever the file was older than the interval.
        That second pass overlapped the inventory fetch and the list on screen stayed on
        the first handful of campaigns. Further scans happen only from the Scan now button.
        """
        while True:
            try:
                await self.refresh_feeds()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Drop scan: the feeds refresh failed: {exc}")
            try:
                if not self.scan_running:
                    scan_data, _stale = self.load_scan_cache()
                    if not scan_data.get("scanned_at"):
                        logger.info("Drop scan: no saved drops yet, scanning once")
                        self.start_scan()
                        while self.scan_running:
                            await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Drop scan: schedule check failed: {exc}")
            await asyncio.sleep(60)

    async def _scan_task_wrapper(self) -> None:
        self._set_scan_progress("prepare", 0, 0, "", 0)
        try:
            scan_data = await self.scan_drops()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Drop scan failed: {exc}")
            self._set_scan_progress("error", 0, 0, str(exc), 0)
            self.print(f"Drop scan failed: {exc}")
            return
        self._set_scan_progress("idle", 0, 0, "", 0)
        if scan_data:
            # the fetch that may already be running started from the previous file
            self._request_inventory_reload()

    def _build_game_campaign(self, campaign_data: JsonType) -> JsonType | None:
        """
        Turns one campaign as reported by the available drops query into campaign data,
        which is the same shape as what the (dead) campaigns dashboard used to return.
        """
        time_based_drops: list[JsonType] = campaign_data.get("timeBasedDrops") or []
        if not time_based_drops:
            return None
        game_data: JsonType = campaign_data["game"]
        game_name: str = game_data.get("displayName") or game_data["name"]
        # keep the spelling the user typed in the priority list, so that the campaign
        # matches it (the priority comparisons are case-sensitive)
        game_name = next(
            (
                priority_name
                for priority_name in self.settings.priority
                if priority_name.casefold() == game_name.casefold()
            ),
            game_name,
        )
        acl_channels: list[JsonType] = list(
            (campaign_data.get("allow") or {}).get("channels") or []
        )
        timed_drops: list[JsonType] = []
        for drop_data in time_based_drops:
            benefit_edges: list[JsonType] = []
            for edge in drop_data.get("benefitEdges") or []:
                benefit: JsonType = dict(edge["benefit"])
                benefit.setdefault("distributionType", "UNKNOWN")
                benefit.setdefault("imageAssetURL", campaign_data.get("imageURL") or "")
                benefit_edges.append({**edge, "benefit": benefit})
            timed_drops.append({
                "id": drop_data["id"],
                "name": drop_data["name"],
                "startAt": drop_data["startAt"],
                "endAt": drop_data["endAt"],
                "requiredMinutesWatched": drop_data["requiredMinutesWatched"],
                "preconditionDrops": drop_data.get("preconditionDrops"),
                "benefitEdges": benefit_edges,
            })
        return {
            "id": campaign_data["id"],
            "name": campaign_data["name"],
            "game": {
                "id": game_data["id"],
                "name": game_name,
                "displayName": game_name,
                "boxArtURL": (
                    "https://static-cdn.jtvnw.net/ttv-boxart/"
                    f"{game_data['id']}_IGDB-285x380.jpg"
                ),
            },
            "self": {"isAccountConnected": True},
            "accountLinkURL": campaign_data.get("detailsURL") or "",
            "startAt": min(drop["startAt"] for drop in timed_drops),
            "endAt": campaign_data["endAt"],
            "status": "ACTIVE",
            "allow": {"channels": acl_channels, "isEnabled": True},
            "timeBasedDrops": timed_drops,
        }

    async def discover_channel_campaigns(
        self, known_campaigns: dict[str, JsonType]
    ) -> dict[str, JsonType]:
        """
        Discovers campaigns of the account that aren't in its inventory yet.

        Twitch put the campaigns dashboard and the campaign details queries behind an
        integrity check in September 2026: both return nothing for every client the
        miner is able to use, so the campaign list comes back empty and no new campaign
        can be started at all. The per-channel available drops query still works, so we
        use it on live channels of the relevant games to rebuild the missing campaigns.
        """
        games: dict[str, Game] = {}
        for campaign_data in known_campaigns.values():
            if campaign_data.get("game") is not None:
                game = Game(campaign_data["game"])
                games[game.name] = game
        for game_name in self.settings.priority:
            # the game ID isn't known here, but only the name-based slug is needed
            games.setdefault(game_name, Game({"id": 0, "name": game_name}))
        excluded_names: set[str] = {name.casefold() for name in self.settings.exclude}
        games = {
            name: game for name, game in games.items() if name.casefold() not in excluded_names
        }
        if not games:
            return {}
        channels: dict[int, Channel] = {}
        for game in games.values():
            try:
                for channel in await self.get_live_streams(game, limit=30):
                    channels[channel.id] = channel
            except MinerException:
                logger.warning(f"Campaign discovery: unable to fetch streams for: {game.name}")
        if not channels:
            return {}
        available_drops_ops: list[GQLOperation] = [
            GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
            for channel_id in channels
        ]
        discovered: dict[str, JsonType] = {}
        for available_drops_chunk in chunk(available_drops_ops, 20):
            response_list: list[JsonType] = await self.gql_request(available_drops_chunk)
            for response_json in response_list:
                channel_info: JsonType | None = (response_json.get("data") or {}).get("channel")
                if not isinstance(channel_info, dict) or "id" not in channel_info:
                    continue
                channel = channels.get(int(channel_info["id"]))
                if channel is None:
                    continue
                for campaign_data in channel_info.get("viewerDropCampaigns") or []:
                    campaign_id: str = campaign_data["id"]
                    if (
                        campaign_id in known_campaigns
                        or campaign_data["game"] is None
                        or not campaign_data["timeBasedDrops"]
                    ):
                        continue
                    acl_entry: JsonType = {
                        "id": str(channel.id),
                        "name": channel._login,
                        "displayName": channel._display_name,
                    }
                    if campaign_id in discovered:
                        # the same campaign is available on multiple channels
                        discovered[campaign_id]["allow"]["channels"].append(acl_entry)
                        continue
                    game_data: JsonType = campaign_data["game"]
                    game_name: str = game_data.get("displayName") or game_data["name"]
                    # keep the spelling the user typed in the priority list, so that the
                    # campaign matches it (the priority comparisons are case-sensitive)
                    game_name = next(
                        (
                            priority_name
                            for priority_name in self.settings.priority
                            if priority_name.casefold() == game_name.casefold()
                        ),
                        game_name,
                    )
                    # reuse the linked status of known campaigns of the same game
                    linked: bool = next(
                        (
                            known["self"]["isAccountConnected"]
                            for known in known_campaigns.values()
                            if known.get("game") is not None
                            and known.get("self") is not None
                            and str(known["game"]["id"]) == str(game_data["id"])
                        ),
                        True,
                    )
                    timed_drops: list[JsonType] = []
                    for drop_data in campaign_data["timeBasedDrops"]:
                        benefit_edges: list[JsonType] = []
                        for edge in drop_data["benefitEdges"] or []:
                            benefit: JsonType = dict(edge["benefit"])
                            benefit.setdefault("distributionType", "UNKNOWN")
                            benefit_edges.append({**edge, "benefit": benefit})
                        timed_drops.append({
                            "id": drop_data["id"],
                            "name": drop_data["name"],
                            "startAt": drop_data["startAt"],
                            "endAt": drop_data["endAt"],
                            "requiredMinutesWatched": drop_data["requiredMinutesWatched"],
                            "preconditionDrops": drop_data.get("preconditionDrops"),
                            "benefitEdges": benefit_edges,
                        })
                    discovered[campaign_id] = {
                        "id": campaign_id,
                        "name": campaign_data["name"],
                        "game": {
                            "id": game_data["id"],
                            "name": game_name,
                            "displayName": game_name,
                            "boxArtURL": (
                                "https://static-cdn.jtvnw.net/ttv-boxart/"
                                f"{game_data['id']}_IGDB-285x380.jpg"
                            ),
                        },
                        "self": {"isAccountConnected": linked},
                        "accountLinkURL": campaign_data.get("detailsURL") or "",
                        "startAt": min(drop["startAt"] for drop in timed_drops),
                        "endAt": campaign_data["endAt"],
                        "status": "ACTIVE",
                        "allow": {"channels": [acl_entry], "isEnabled": True},
                        "timeBasedDrops": timed_drops,
                    }
        if discovered:
            logger.info(
                f"Campaign discovery: found {len(discovered)} extra campaign(s) "
                f"across {len(channels)} live channel(s)"
            )
        return discovered

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.get_auth()
        try:
            response_list: list[JsonType] = await self.gql_request(
                [
                    GQL_QUERIES["CampaignDetails"].with_variables(
                        {"channelLogin": str(auth_state.user_id), "dropID": cid}
                    )
                    for cid in campaign_ids
                ]
            )
        except GQLException as exc:
            # also gated behind an integrity check since Sep 2026 - the in-progress
            # inventory data stays usable without the campaign details
            logger.warning(f"Unable to fetch campaign details: {exc}")
            return {}
        fetched_data: dict[str, JsonType] = {
            (campaign_data := response_json["data"]["user"]["dropCampaign"])["id"]: campaign_data
            for response_json in response_list
        }
        return self._merge_data(campaign_ids, fetched_data)

    async def fetch_inventory(self) -> None:
        status_update = self.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))
        # fetch in-progress campaigns (inventory)
        response = await self.gql_request(GQL_QUERIES["Inventory"])
        inventory: JsonType = response["data"]["currentUser"]["inventory"]
        ongoing_campaigns: list[JsonType] = inventory["dropCampaignsInProgress"] or []
        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, datetime] = {
            b["id"]: timestamp(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]
        }
        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}
        # fetch general available campaigns data (campaigns)
        try:
            response = await self.gql_request(GQL_QUERIES["Campaigns"])
        except GQLException as exc:
            # Twitch gates this query behind an integrity check since Sep 2026: instead of
            # dying here, fall back to campaign discovery over the live channels
            logger.warning(f"Unable to fetch the campaigns list: {exc}")
            available_list = []
        else:
            available_list = response["data"]["currentUser"]["dropCampaigns"] or []
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {
            c["id"]: c
            for c in available_list
            if c["status"] in applicable_statuses  # that are currently not expired
        }
        # fetch detailed data for each campaign, in chunks
        status_update(_("gui", "status", "fetching_campaigns"))
        fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
            for campaigns_chunk in chunk(available_campaigns.items(), 20)
        ]
        try:
            for coro in asyncio.as_completed(fetch_campaigns_tasks):
                chunk_campaigns_data = await coro
                # merge the inventory and campaigns datas together
                inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in fetch_campaigns_tasks:
                task.cancel()
            raise
        # Twitch's campaign dashboard answers with nothing. The saved scan is the list,
        # whether or not the old "scan every N minutes" switch is on.
        linked_by_game: dict[str, bool] = {}
        for campaign_data in ongoing_campaigns:
            game = campaign_data.get("game") or {}
            self_data = campaign_data.get("self") or {}
            game_id = str(game.get("id") or "")
            if game_id and "isAccountConnected" in self_data:
                linked_by_game[game_id] = bool(self_data["isAccountConnected"])
        scan_data, _scan_is_stale = self.load_scan_cache()
        for scan_key in ("campaigns", "channel_campaigns"):
            if scan_key == "channel_campaigns" and self.settings.channel_campaigns == "off":
                # not wanted: don't resurrect campaigns from an older scan either
                continue
            for campaign_id, campaign_data in (scan_data.get(scan_key) or {}).items():
                if isinstance(campaign_data, dict):
                    game = campaign_data.get("game") or {}
                    game_id = str(game.get("id") or "")
                    self_data = campaign_data.get("self")
                    if game_id in linked_by_game and isinstance(self_data, dict):
                        self_data["isAccountConnected"] = linked_by_game[game_id]
                inventory_data.setdefault(campaign_id, campaign_data)
        if not scan_data.get("scanned_at") and not self.scan_running:
            logger.info("Drop scan: no saved drops yet, scanning once")
            self.start_scan()
        if not scan_data and self.settings.campaign_discovery:
            # fallback for setups with the drop scan turned off
            status_update(_("gui", "status", "fetching_campaigns"))
            try:
                discovered_campaigns = await self.discover_channel_campaigns(inventory_data)
            except MinerException as exc:
                logger.warning(f"Campaign discovery failed: {exc}")
            else:
                for campaign_id, campaign_data in discovered_campaigns.items():
                    inventory_data.setdefault(campaign_id, campaign_data)
        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            campaign_data = inventory_data[campaign_id]
            game = campaign_data.get("game") if isinstance(campaign_data, dict) else None
            if not isinstance(game, dict):
                del inventory_data[campaign_id]

        if self.settings.dump:
            # dump the campaigns data to the dump file
            with open(DUMP_PATH, 'a', encoding="utf8") as file:
                # we need to pre-process the inventory dump a little
                dump_data: JsonType = deepcopy(inventory_data)
                for campaign_data in dump_data.values():
                    # replace ACL lists with a simple text description
                    if (
                        campaign_data["allow"]
                        and campaign_data["allow"].get("isEnabled", True)
                        and campaign_data["allow"]["channels"]
                    ):
                        # simply count the channels included in the ACL
                        campaign_data["allow"]["channels"] = (
                            f"{len(campaign_data['allow']['channels'])} channels"
                        )
                    # replace drop instance IDs, so they don't include user IDs
                    for drop_data in campaign_data["timeBasedDrops"]:
                        if "self" in drop_data and drop_data["self"]["dropInstanceID"]:
                            drop_data["self"]["dropInstanceID"] = "..."
                json.dump(dump_data, file, indent=4, sort_keys=True)
                file.write("\n\n")  # add 2x new line spacer
                json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

        # use the merged data to create campaign objects
        campaigns: list[DropsCampaign] = []
        for campaign_data in inventory_data.values():
            try:
                campaigns.append(DropsCampaign(self, campaign_data, claimed_benefits))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(f"Inventory: skipped a campaign that could not be read: {exc}")
        def inventory_order(campaign: DropsCampaign) -> tuple[int, int, float]:
            # Running first, soonest end at the top, connected accounts above the rest.
            # Upcoming follows by start time, expired by most recently ended.
            # The Show boxes only hide groups; they do not change this order.
            if campaign.active:
                group = 0
                moment = campaign.ends_at.timestamp()
            elif campaign.upcoming:
                group = 1
                moment = campaign.starts_at.timestamp()
            else:
                group = 2
                moment = -campaign.ends_at.timestamp()
            return (group, 0 if campaign.eligible else 1, moment)

        campaigns.sort(key=inventory_order)

        self._drops.clear()
        self.gui.inv.clear()
        self.inventory.clear()
        self._mnt_triggers.clear()
        switch_triggers: set[datetime] = set()
        now = datetime.now(timezone.utc)
        next_hour = now + timedelta(hours=1)
        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            self.inventory.append(campaign)
            self._campaigns[campaign.id] = campaign
        # The list is one snapshot in its own window. Building a Tk card per
        # campaign is what made the inventory slow and the scroll tear.
        status_update(
            _("gui", "status", "adding_campaigns").format(counter=f"({len(campaigns)})")
        )
        self.gui.inv.publish()
        self._mnt_triggers.extend(sorted(switch_triggers))
        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._mnt_triggers and self._mnt_triggers[0] <= now:
            self._mnt_triggers.popleft()
        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._mnt_task is not None and not self._mnt_task.done():
            self._mnt_task.cancel()
        self._mnt_task = asyncio.create_task(self._maintenance_task())

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        if not self.wanted_games:
            return None
        watching_channel = self.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        campaigns: list[DropsCampaign] = []
        for campaign in self.inventory:
            if campaign.can_earn(watching_channel):
                campaigns.append(campaign)
        if campaigns:
            campaigns.sort(key=self.campaign_sort_key)
            return campaigns[0]
        return None

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        filters: list[str] = []
        if drops_enabled:
            filters.append("DROPS_ENABLED")
        try:
            response = await self.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": filters,
                    },
                })
            )
        except GQLException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        game_data: JsonType | None = response["data"].get("game")
        if game_data is None:
            # the slug doesn't resolve to a game (ex. the "Special Events" bucket)
            return []
        return [
            Channel.from_directory(
                self, stream_channel_data["node"], drops_enabled=drops_enabled
            )
            for stream_channel_data in game_data["streams"]["edges"]
            if stream_channel_data["node"]["broadcaster"] is not None
        ]

    async def refresh_available_drops(self, channels: abc.Iterable[Channel]) -> None:
        """
        Marks channels that have drops actually available for the account.

        Twitch lists channels in the game directory that have drops enabled in general,
        but the drops a particular account can progress are reported per channel. Mining
        a channel without them looks like it works, while the drop never moves, so an
        authoritative check is done for the channels we're about to watch.
        """
        if not self.settings.available_drops_check:
            return
        candidates: list[Channel] = [
            channel
            for channel in channels
            # every channel on the list, not only the games being farmed: both the badge and the
            # manual switch depend on this answer
            if channel.online and channel.game is not None
        ]
        if not candidates:
            return
        available_drops_map: dict[int, list[JsonType]] = {}
        available_drops_ops: list[GQLOperation] = [
            GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel.id)})
            for channel in candidates
        ]
        for available_drops_chunk in chunk(available_drops_ops, 20):
            try:
                response_list: list[JsonType] = await self.gql_request(available_drops_chunk)
            except MinerException:
                logger.warning(
                    "Available drops check failed - assuming the channels can progress drops"
                )
                return
            for response_json in response_list:
                available_info: JsonType | None = response_json["data"]["channel"]
                if available_info is None:
                    continue
                available_drops_map[int(available_info["id"])] = (
                    available_info["viewerDropCampaigns"] or []
                )
        if not any(available_drops_map.values()):
            # a channel without drops is normal, all of them at once is not - don't
            # leave the miner without anything to watch because of it
            logger.warning(
                "No channel reports available drops - skipping the available drops check"
            )
            return
        for channel in candidates:
            channel.update_available_drops(available_drops_map.get(channel.id, []))

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """
        Utilize batch GQL requests to check ONLINE status for a lot of channels at once.
        Also handles the drops_enabled check (if enabled).
        """
        acl_streams_map: dict[int, JsonType] = {}
        stream_gql_ops: list[GQLOperation] = [channel.stream_gql for channel in channels]
        if not stream_gql_ops:
            # shortcut for nothing to process
            # NOTE: Have to do this here, becase "channels" can be any iterable
            return
        stream_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.gql_request(stream_gql_chunk))
            for stream_gql_chunk in chunk(stream_gql_ops, 20)
        ]
        try:
            for coro in asyncio.as_completed(stream_gql_tasks):
                response_list: list[JsonType] = await coro
                for response_json in response_list:
                    channel_data: JsonType = response_json["data"]["user"]
                    if channel_data is not None:
                        acl_streams_map[int(channel_data["id"])] = channel_data
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in stream_gql_tasks:
                task.cancel()
            raise
        # for all channels with an active stream, check the available drops as well
        acl_available_drops_map: dict[int, list[JsonType]] = {}
        if self.settings.available_drops_check:
            available_gql_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id, channel_data in acl_streams_map.items()
                if channel_data["stream"] is not None  # only do this for ONLINE channels
            ]
            available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
                asyncio.create_task(self.gql_request(available_gql_chunk))
                for available_gql_chunk in chunk(available_gql_ops, 20)
            ]
            try:
                for coro in asyncio.as_completed(available_gql_tasks):
                    response_list = await coro
                    for response_json in response_list:
                        available_info: JsonType = response_json["data"]["channel"]
                        acl_available_drops_map[int(available_info["id"])] = (
                            available_info["viewerDropCampaigns"] or []
                        )
            except Exception:
                # asyncio.as_completed doesn't cancel tasks on errors
                for task in available_gql_tasks:
                    task.cancel()
                raise
        for channel in channels:
            channel_id = channel.id
            if channel_id not in acl_streams_map:
                continue
            channel_data = acl_streams_map[channel_id]
            if channel_data["stream"] is None:
                continue
            available_drops: list[JsonType] = acl_available_drops_map.get(channel_id, [])
            channel.external_update(channel_data, available_drops)
