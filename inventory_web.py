from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from yarl import URL

from constants import WORKING_DIR, State
from translate import _
from utils import resource_path


logger = logging.getLogger("TwitchDrops")

ROW_HEIGHT = 96
PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Twitch Drops Miner</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #1b1e26;
    --bar: #151820;
    --row: #22262f;
    --line: #343a46;
    --text: #e7e9ee;
    --muted: #9aa3b2;
    --green: #3dd68c;
    --gold: #e0b15a;
    --red: #e06c6c;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font: 13px/1.3 "Segoe UI", sans-serif; }
  header { height: 52px; display: flex; align-items: center; gap: 14px; padding: 0 16px;
    background: var(--bar); border-bottom: 1px solid var(--line); }
  header .show { color: var(--muted); }
  label.opt { display: inline-flex; align-items: center; gap: 6px; color: var(--text); }
  input[type="checkbox"] { width: 15px; height: 15px; margin: 0; accent-color: #4c8dff; }
  #viewport { height: calc(100% - 52px); overflow-y: auto; position: relative; }
  #spacer { width: 1px; }
  #rows { position: absolute; left: 0; right: 0; top: 0; }
  .row { height: 96px; display: grid; grid-template-columns: 78px 84px minmax(220px, 1fr) 88px minmax(180px, 42%);
    align-items: center; column-gap: 8px; padding: 0 12px 0 8px;
    border-bottom: 1px solid var(--line); background: var(--row); }
  .farm { display: flex; align-items: center; gap: 6px; }
  .cover, .tile img, .more { width: 64px; height: 84px; object-fit: cover; background: #12141a;
    border: 1px solid var(--line); }
  .tile img { width: 56px; height: 56px; }
  .title { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .status { font-weight: 650; }
  .status.active, .side.active { color: var(--green); }
  .status.upcoming, .side.upcoming { color: var(--gold); }
  .status.expired, .side.expired { color: var(--red); }
  .time { color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .side { text-align: right; font-weight: 650; }
  .rewards { display: flex; align-items: flex-start; gap: 8px; height: 84px; overflow: hidden; }
  .tile { width: 72px; flex: 0 0 72px; text-align: center; }
  .tile span, .more span { display: block; margin-top: 3px; font-size: 11px; color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile.claimed img { filter: grayscale(0.85); opacity: 0.55; }
  .more { width: 56px; height: 56px; display: flex; align-items: center; justify-content: center;
    font-weight: 650; background: #2a303b; }
  #empty { position: absolute; inset: 0; display: none; align-items: center; justify-content: center;
    color: var(--muted); }
</style>
</head>
<body>
<header>
  <span class="show">Show</span>
  <label class="opt"><input type="checkbox" id="f-linked" checked> Linked</label>
  <label class="opt"><input type="checkbox" id="f-not" checked> Not linked</label>
  <label class="opt"><input type="checkbox" id="f-upcoming" checked> Upcoming</label>
  <label class="opt"><input type="checkbox" id="f-expired"> Expired</label>
  <label class="opt"><input type="checkbox" id="f-excluded"> Excluded</label>
  <label class="opt"><input type="checkbox" id="f-finished"> Finished</label>
</header>
<div id="viewport">
  <div id="spacer"></div>
  <div id="rows"></div>
  <div id="empty">No campaigns match these filters.</div>
</div>
<script>
const TOKEN = "__TOKEN__";
const ROW = 96;
const viewport = document.getElementById("viewport");
const spacer = document.getElementById("spacer");
const rows = document.getElementById("rows");
const empty = document.getElementById("empty");
const filters = {
  linked: document.getElementById("f-linked"),
  not: document.getElementById("f-not"),
  upcoming: document.getElementById("f-upcoming"),
  expired: document.getElementById("f-expired"),
  excluded: document.getElementById("f-excluded"),
  finished: document.getElementById("f-finished"),
};
let campaigns = [];
let shown = [];
let revision = -1;
let painted = "";

function matches(row) {
  if (!row.watchable) return false;
  const linkOk = (filters.linked.checked && row.linked) || (filters.not.checked && !row.linked);
  if (!linkOk) return false;
  const visible = row.status === "active"
    || (filters.upcoming.checked && row.status === "upcoming")
    || (filters.expired.checked && row.status === "expired");
  if (!visible) return false;
  if (row.excluded && !filters.excluded.checked) return false;
  if (row.finished && !filters.finished.checked) return false;
  return true;
}

function tile(reward) {
  const tile = document.createElement("div");
  tile.className = "tile" + (reward.claimed ? " claimed" : "");
  const img = document.createElement("img");
  img.alt = "";
  img.width = 56;
  img.height = 56;
  if (reward.image) img.src = reward.image;
  const name = document.createElement("span");
  name.textContent = reward.name;
  tile.title = reward.detail || reward.name;
  tile.append(img, name);
  return tile;
}

function rowEl(row) {
  const el = document.createElement("div");
  el.className = "row";
  const farm = document.createElement("label");
  farm.className = "farm";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = row.farm;
  box.addEventListener("change", () => {
    fetch("/api/farm?token=" + TOKEN, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({game: row.game, on: box.checked}),
    }).then((response) => {
      if (!response.ok) box.checked = !box.checked;
    }).catch(() => { box.checked = !box.checked; });
  });
  const farmText = document.createElement("span");
  farmText.textContent = "Farm";
  farm.append(box, farmText);

  const cover = document.createElement("img");
  cover.className = "cover";
  cover.alt = "";
  cover.width = 64;
  cover.height = 84;
  if (row.art) cover.src = row.art;

  const text = document.createElement("div");
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = row.game + " - " + row.name;
  const status = document.createElement("div");
  status.className = "status " + row.status;
  status.textContent = row.statusLabel;
  const time = document.createElement("div");
  time.className = "time";
  time.textContent = row.time;
  text.append(title, status, time);

  const side = document.createElement("div");
  side.className = "side " + row.status;
  side.textContent = row.statusLabel;

  const rewards = document.createElement("div");
  rewards.className = "rewards";
  const width = rewardsColumn();
  let slots = Math.max(1, Math.floor(width / 80));
  const extra = Math.max(0, row.rewards.length - slots);
  const count = extra > 0 ? Math.max(1, slots - 1) : row.rewards.length;
  row.rewards.slice(0, count).forEach((reward) => rewards.appendChild(tile(reward)));
  if (row.rewards.length > count) {
    const more = document.createElement("div");
    more.className = "more";
    more.textContent = "+" + (row.rewards.length - count);
    rewards.appendChild(more);
  }
  el.append(farm, cover, text, side, rewards);
  return el;
}

function rewardsColumn() {
  return Math.max(160, Math.floor(viewport.clientWidth * 0.42) - 16);
}

function paint() {
  const top = viewport.scrollTop;
  const height = viewport.clientHeight || 1;
  const start = Math.max(0, Math.floor(top / ROW) - 4);
  const end = Math.min(shown.length, Math.ceil((top + height) / ROW) + 4);
  const key = start + ":" + end + ":" + revision + ":" + viewport.clientWidth;
  if (key === painted) return;
  painted = key;
  rows.style.transform = "translateY(" + (start * ROW) + "px)";
  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i++) frag.appendChild(rowEl(shown[i]));
  rows.replaceChildren(frag);
  empty.style.display = shown.length ? "none" : "flex";
}

function apply() {
  shown = campaigns.filter(matches);
  spacer.style.height = (shown.length * ROW) + "px";
  painted = "";
  paint();
}

async function tick() {
  try {
    const response = await fetch("/api/inventory?token=" + TOKEN);
    if (!response.ok) return;
    const body = await response.json();
    if (body.revision === revision) return;
    revision = body.revision;
    campaigns = body.campaigns || [];
    apply();
  } catch (err) { /* the miner is still starting */ }
}

Object.values(filters).forEach((box) => box.addEventListener("change", () => {
  viewport.scrollTop = 0;
  apply();
}));
viewport.addEventListener("scroll", paint, {passive: true});
window.addEventListener("resize", () => { painted = ""; paint(); });
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


def _pid_alive(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _focus_pid(pid: int) -> None:
    if os.name != "nt" or pid <= 0:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return
    user32.ShowWindow(found[0], 9)
    user32.SetForegroundWindow(found[0])


def _show_pid(pid: int, command: int) -> None:
    if os.name != "nt" or pid <= 0:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    # A hidden window is not visible, so a restore has to look at every window of the process.
    if not found and command != 0:
        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def any_window(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                found.append(hwnd)
                return False
            return True
        user32.EnumWindows(any_window, 0)
    if found:
        user32.ShowWindow(found[0], command)


def _edge_path() -> Path | None:
    roots = [
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        candidate = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if candidate.is_file():
            return candidate
    return None


def _hours(amount: float, *, upcoming: bool, needed: bool) -> str:
    amount = max(0.0, amount)
    if amount >= 48:
        text = f"{amount / 24:.1f}"
        if needed:
            return _("gui", "inventory", "days_needed").format(needed=text)
        key = "starts_in_days" if upcoming else "days_left"
        return _("gui", "inventory", key).format(left=text)
    text = f"{amount:.1f}"
    if needed:
        return _("gui", "inventory", "hours_needed").format(needed=text)
    key = "starts_in_hours" if upcoming else "hours_left"
    return _("gui", "inventory", key).format(left=text)


def _time_line(campaign) -> str:
    now = datetime.now(timezone.utc)
    if campaign.upcoming:
        left = (campaign.starts_at - now).total_seconds() / 3600
        left_text = _hours(left, upcoming=True, needed=False)
    else:
        left = (campaign.ends_at - now).total_seconds() / 3600
        left_text = _hours(left, upcoming=False, needed=False)
    needed = _hours(campaign.remaining_minutes / 60, upcoming=False, needed=True)
    return f"{left_text}  ·  {needed}"


def _status(campaign) -> tuple[str, str]:
    if campaign.active:
        return "active", _("gui", "inventory", "status", "active")
    if campaign.upcoming:
        return "upcoming", _("gui", "inventory", "status", "upcoming")
    return "expired", _("gui", "inventory", "status", "expired")


def _focus_drop_id(campaign, active_id: str | None) -> str | None:
    """The drop this campaign is collecting right now, if there is one."""
    ids = {str(drop.id) for drop in campaign.drops}
    if active_id and active_id in ids:
        return active_id
    for drop in campaign.drops:
        if drop.is_claimed or drop.required_minutes <= 0:
            continue
        if 0 < drop.current_minutes < drop.required_minutes:
            return str(drop.id)
    return None


def _rewards(campaign, active_id: str | None) -> list[dict]:
    focus = _focus_drop_id(campaign, active_id)
    rewards: list[dict] = []
    for drop in campaign.drops:
        detail = ""
        if drop.required_minutes > 0:
            detail = _("gui", "inventory", "percent_progress").format(
                percent=f"{drop.progress * 100:.0f}%",
                current=drop.current_minutes,
                minutes=drop.required_minutes,
            )
        claimed = bool(drop.is_claimed)
        current = not claimed and focus is not None and str(drop.id) == focus
        for benefit in drop.benefits:
            rewards.append({
                "name": benefit.name,
                "image": str(benefit.image_url or ""),
                "claimed": claimed,
                "current": current,
                "detail": detail or benefit.name,
            })
    return rewards


def campaign_payload(
    campaign, priority: set[str], exclude: set[str], active_id: str | None = None
) -> dict | None:
    try:
        kind, label = _status(campaign)
        game_id = int(getattr(campaign.game, "id", 0) or 0)
        art = (
            f"https://static-cdn.jtvnw.net/ttv-boxart/{game_id}-285x380.jpg"
            if game_id > 0
            else str(campaign.image_url or "")
        )
        return {
            "id": campaign.id,
            "game": campaign.game.name,
            "name": campaign.name,
            "art": art,
            "status": kind,
            "statusLabel": label,
            "linked": bool(campaign.linked),
            "excluded": campaign.game.name in exclude,
            "finished": bool(campaign.finished),
            "watchable": campaign.required_minutes > 0,
            "farm": campaign.game.name in priority,
            "time": _time_line(campaign),
            "rewards": _rewards(campaign, active_id),
        }
    except Exception:
        return None


class InventoryOverview:
    """The inventory list lives in its own window so scrolling does not move Tk widgets."""

    def __init__(self, manager, master) -> None:
        self._manager = manager
        self._settings = manager._twitch.settings
        self._token = secrets.token_urlsafe(18)
        self._revision = 0
        self._runner: web.AppRunner | None = None
        self._url = ""
        self._proc: subprocess.Popen | None = None
        self._window_pids: list[int] = []
        self._guard_until = 0.0
        self._capture_gen = 0
        self._launched = False
        self._lookup_pending = False
        self._seen_window = False
        self._closing = False
        self._watcher_started = False
        self._lock = threading.Lock()
        self._starting = False
        self._auto_opened = False
        self._bad_rows: set[str] = set()
        self._task: asyncio.Task | None = None
        frame = master
        import tkinter as tk
        from tkinter import ttk
        ttk.Label(
            frame,
            text="The inventory opens in its own window, one row per campaign.",
            wraplength=640,
        ).grid(column=0, row=0, sticky="w", pady=(8, 6))
        ttk.Button(frame, text="Open inventory", command=self._open_clicked).grid(
            column=0, row=1, sticky="w"
        )
        self._status = ttk.Label(frame, text="")
        self._status.grid(column=0, row=2, sticky="w", pady=(8, 0))
        self._profile = WORKING_DIR / "inventory_window"

    def configure_theme(self, *, bg: str) -> None:
        return None

    def clear(self) -> None:
        return None

    def set_loading(self, done: int, total: int) -> None:
        if total <= 0 or done >= total:
            self._status.config(text="")
            return
        self._status.config(text=f"Preparing the list ({done}/{total})")

    def refresh(self) -> None:
        self._revision += 1

    def update_drop(self, drop) -> None:
        self._revision += 1

    async def add_campaign(self, campaign) -> None:
        self._revision += 1

    def publish(self) -> None:
        self._revision += 1

    def show(self) -> None:
        if not self._launched or not self._window_alive():
            self._schedule(self.ensure_open())
            return
        for pid in list(self._window_pids):
            _show_pid(pid, 9)
            _focus_pid(pid)

    def hide(self) -> None:
        for pid in list(self._window_pids):
            _show_pid(pid, 0)

    def _open_clicked(self) -> None:
        self._schedule(self.ensure_open())

    def _schedule(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._task = task
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.warning(f"Inventory window: {exc}")
        try:
            self._status.config(text="The inventory window could not open.")
        except Exception:
            return

    def _account_name(self) -> str:
        auth = getattr(self._manager._twitch, "_auth_state", None)
        if auth is None or not hasattr(auth, "user_id"):
            return ""
        name = str(getattr(auth, "login_name", "") or "").strip()
        return name or str(auth.user_id)

    def snapshot(self) -> dict:
        twitch = self._manager._twitch
        priority = tuple(self._settings.priority)
        exclude = tuple(sorted(self._settings.exclude))
        inventory = list(getattr(twitch, "inventory", ()))
        try:
            inventory.sort(key=twitch.campaign_sort_key)
        except Exception:
            logger.warning("Inventory: the list could not be sorted, showing the saved order")
        rows = []
        active = self._manager.progress._drop
        active_id = str(active.id) if active is not None else None
        audiences: list[str] = []
        for campaign in inventory:
            row = campaign_payload(campaign, set(priority), set(exclude), active_id)
            if row is None:
                key = str(getattr(campaign, "id", "?"))
                if key not in self._bad_rows:
                    self._bad_rows.add(key)
                    logger.warning(f"Inventory: skipped a row that could not be read: {key}")
                continue
            rows.append(row)
            try:
                if twitch._campaign_is_urgent(campaign):
                    audiences.append(f"{campaign.id}:{twitch.campaign_audience(campaign)}")
            except Exception:
                pass
        stamp = int(time.time() // 60)
        mode = getattr(self._settings.priority_mode, "name", "")
        raw = f"{self._revision}|{priority}|{exclude}|{mode}|{stamp}|{','.join(audiences)}"
        revision = hashlib.sha1(raw.encode()).hexdigest()[:16]
        return {"revision": revision, "campaigns": rows}

    def set_farm(self, game_name: str, farmed: bool) -> None:
        priority = self._settings.priority
        panel = self._manager.settings
        if farmed and game_name not in priority:
            priority.append(game_name)
            panel._priority_list.insert("end", game_name)
            panel._priority_list.see("end")
        elif not farmed and game_name in priority:
            index = priority.index(game_name)
            del priority[index]
            panel._priority_list.delete(index)
        else:
            self._revision += 1
            return
        self._settings.alter()
        self._settings.save()
        panel.update_priority_choices()
        twitch = self._manager._twitch
        if twitch._state is not State.INVENTORY_FETCH:
            twitch.change_state(State.GAMES_UPDATE)
        self._revision += 1

    def _labels(self) -> dict:
        panel = self._manager.settings
        return {
            "tabs": {
                "main": _("gui", "tabs", "main"),
                "inventory": _("gui", "tabs", "inventory"),
                "settings": _("gui", "tabs", "settings"),
                "help": _("gui", "tabs", "help"),
            },
            "tray": _("gui", "tray", "minimize"),
            "username": _("gui", "login", "username"),
            "password": _("gui", "login", "password"),
            "twofa": _("gui", "login", "twofa_code"),
            "loginButton": _("gui", "login", "button"),
            "logoutButton": _("gui", "login", "logout"),
            "accountTitle": _("gui", "login", "name"),
            "loggedInAs": _("gui", "login", "logged_in_as"),
            "loggedOut": _("gui", "login", "logged_out"),
            "logoutHint": _("gui", "help", "invalidate", "text"),
            "deviceCode": _("gui", "login", "device_code"),
            "switchLabel": _("gui", "channels", "switch"),
            "channels": _("gui", "channels", "name"),
            "output": _("gui", "output"),
            "empty": "No campaigns match these filters.",
            "show": _("gui", "inventory", "filter", "show"),
            "farm": _("gui", "inventory", "farm"),
            "filters": {
                "linked": _("gui", "inventory", "filter", "linked"),
                "not": _("gui", "inventory", "filter", "not_linked"),
                "upcoming": _("gui", "inventory", "filter", "upcoming"),
                "expired": _("gui", "inventory", "filter", "expired"),
                "excluded": _("gui", "inventory", "filter", "excluded"),
                "finished": _("gui", "inventory", "filter", "finished"),
            },
            "headChannel": _("gui", "channels", "headings", "channel"),
            "headStatus": _("gui", "channels", "headings", "status"),
            "headGame": _("gui", "channels", "headings", "game"),
            "headDrops": _("gui", "channels", "headings", "drops"),
            "headFarm": _("gui", "channels", "headings", "farm"),
            "headViewers": _("gui", "channels", "headings", "viewers"),
            "progress": _("gui", "progress", "name"),
            "campaignProgress": _("gui", "progress", "campaign_progress"),
            "dropProgress": _("gui", "progress", "drop_progress"),
            "language": "Language",
            "restart": "A language change is applied the next time the application starts.",
            "proxyRestart": "A proxy change is applied the next time the application starts.",
            "generalName": _("gui", "settings", "general", "name"),
            "gamesName": _("gui", "settings", "priority"),
            "gamesNote": _("gui", "settings", "reload_text"),
            "connectionName": "Connection",
            "advancedName": _("gui", "settings", "advanced", "name"),
            "autostart": _("gui", "settings", "general", "autostart"),
            "trayStart": _("gui", "settings", "general", "tray"),
            "notifications": _("gui", "settings", "general", "tray_notifications"),
            "mode": _("gui", "settings", "general", "priority_mode"),
            "proxy": _("gui", "settings", "general", "proxy"),
            "priority": _("gui", "settings", "priority"),
            "exclude": _("gui", "settings", "exclude"),
            "gameName": _("gui", "settings", "game_name"),
            "scan": _("gui", "settings", "scan", "name"),
            "scanNow": _("gui", "settings", "scan", "scan_now"),
            "scanHelp": _("gui", "settings", "scan", "scan_help") + " " + _("gui", "settings", "feed_help"),
            "feed": _("gui", "settings", "feed_reload"),
            "reload": _("gui", "settings", "reload"),
            "badges": _("gui", "settings", "advanced", "enable_badges_emotes"),
            "dropsCheck": _("gui", "settings", "advanced", "available_drops_check"),
            "advanced": _("gui", "settings", "advanced", "warning_text"),
            "about": _("gui", "help", "about", "name"),
            "created": _("gui", "help", "about", "created_by"),
            "modified": _("gui", "help", "about", "modified_by"),
            "links": _("gui", "help", "links", "name"),
            "linkInventory": _("gui", "help", "links", "inventory"),
            "linkCampaigns": _("gui", "help", "links", "campaigns"),
            "how": _("gui", "help", "how_it_works"),
            "howText": _("gui", "help", "how_it_works_text"),
            "start": _("gui", "help", "getting_started"),
            "startText": _("gui", "help", "getting_started_text"),
            "channelHelp": _("gui", "help", "channels"),
            "channelHelpText": _("gui", "help", "channels_text"),
            "invalidate": _("gui", "help", "invalidate", "button"),
            "invalidateText": _("gui", "help", "invalidate", "text"),
        }

    def _app_state(self) -> dict:
        gui = self._manager
        twitch = gui._twitch
        panel = gui.settings
        progress = gui.progress
        campaign = progress._vars["campaign"]
        drop = progress._vars["drop"]
        active = progress._drop
        drop_count = int(active.campaign.total_drops) if active is not None else 0
        table = gui.channels._table
        watching = set(table.tag_has("watching"))
        selected = set(table.selection())
        channels = []
        for iid in table.get_children():
            channels.append({
                "id": iid,
                "name": table.set(iid, "channel"),
                "status": table.set(iid, "status"),
                "game": table.set(iid, "game"),
                "drops": table.set(iid, "drops"),
                "earn": table.set(iid, "earn"),
                "viewers": table.set(iid, "viewers"),
                "watching": iid in watching,
                "selected": iid in selected,
            })
        log = gui.output._text.get("1.0", "end").strip().splitlines()
        sockets = []
        for idx, item in gui.websockets._items.items():
            if item is None:
                continue
            sockets.append({
                "id": idx + 1,
                "status": item["status"],
                "topics": f"{item['topics']}/{item['topics'] and '' or ''}",
            })
            sockets[-1]["topics"] = f"{item['topics']}"
        scan = getattr(twitch, "scan_progress", {}) or {}
        inventory = self.snapshot()
        settings = self._settings
        modes = [{"value": name, "label": name} for name in panel.PRIORITY_MODES.values()]
        return {
            "revision": inventory["revision"],
            "campaigns": inventory["campaigns"],
            "status": gui.status._label.cget("text"),
            "loginReady": str(gui.login._button.cget("state")) != "disabled",
            "logoutReady": str(gui.help._invalidate_button.cget("state")) != "disabled",
            "login": gui.login._var.get(),
            "loginCode": str(getattr(gui.login, "_device_code", "") or ""),
            "account": self._account_name(),
            "drop": {
                "game": campaign["game"].get(),
                "campaign": campaign["name"].get(),
                "campaignProgress": campaign["progress"].get(),
                "campaignLabel": campaign["percentage"].get(),
                "campaignRemaining": campaign["remaining"].get(),
                "dropCount": drop_count,
                "rewards": drop["rewards"].get(),
                "dropProgress": drop["progress"].get(),
                "dropLabel": drop["percentage"].get(),
                "dropRemaining": drop["remaining"].get(),
            },
            "channels": channels,
            "sockets": sockets,
            "log": log[-40:],
            "scan": {
                "running": bool(getattr(twitch, "scan_running", False)),
                "done": int(scan.get("done") or 0),
                "total": int(scan.get("total") or 0),
            },
            "settings": {
                "language": settings.language,
                "languages": list(_.languages),
                "autostart": bool(panel._vars["autostart"].get()),
                "tray": bool(panel._vars["tray"].get()),
                "notifications": bool(settings.tray_notifications),
                "mode": panel._vars["priority_mode"].get(),
                "modes": modes,
                "proxy": str(settings.proxy or ""),
                "priority": list(settings.priority),
                "exclude": sorted(settings.exclude),
                "games": sorted(panel._game_names),
                "badges": bool(settings.enable_badges_emotes),
                "dropsCheck": bool(settings.available_drops_check),
                "scanStatus": panel._scan_status.cget("text"),
                "scanDetail": panel._scan_coverage.cget("text"),
                "feedStatus": panel._feed_status.cget("text"),
            },
        }

    def _select_named(self, names: list[str], game: str) -> int | None:
        try:
            return names.index(game)
        except ValueError:
            return None

    def apply_action(self, body: dict) -> None:
        op = str(body.get("op") or "")
        gui = self._manager
        twitch = gui._twitch
        panel = gui.settings
        if op == "login":
            login = gui.login
            if str(login._button.cget("state")) == "disabled":
                raise web.HTTPConflict()
            login._login_entry.replace(str(body.get("username") or "").strip())
            login._pass_entry.replace(str(body.get("password") or ""))
            login._token_entry.replace(str(body.get("token") or "").strip())
            login._confirm.set()
            return
        if op == "channel":
            iid = str(body.get("id") or "")
            if iid not in gui.channels._channel_map:
                raise web.HTTPBadRequest()
            gui.channels._table.selection_set(iid)
            gui.channels._buttons["switch"].config(state="normal")
            return
        if op == "switch":
            twitch.change_state(State.CHANNEL_SWITCH)
            return
        if op == "tray":
            gui.tray.minimize()
            return
        if op == "scan":
            panel.scan_now()
            return
        if op == "feed":
            twitch.request_feed_refresh()
            return
        if op == "reload":
            twitch.change_state(State.INVENTORY_FETCH)
            return
        if op == "invalidate":
            if str(gui.help._invalidate_button.cget("state")) == "disabled":
                raise web.HTTPConflict()
            gui.help.invalidate_token()
            return
        if op == "setting":
            self._apply_setting(str(body.get("key") or ""), body.get("value"))
            return
        game = str(body.get("game") or "").strip()
        if not game:
            raise web.HTTPBadRequest()
        if op == "priority-add":
            panel._priority_entry.replace(game)
            panel.priority_add()
            self._settings.save()
            self._revision += 1
            return
        if op == "priority-delete":
            idx = self._select_named(list(panel._priority_list.get(0, "end")), game)
            if idx is None:
                return
            panel._priority_list.selection_clear(0, "end")
            panel._priority_list.selection_set(idx)
            panel.priority_delete()
            self._settings.save()
            self._revision += 1
            return
        if op in ("priority-up", "priority-down"):
            idx = self._select_named(list(panel._priority_list.get(0, "end")), game)
            if idx is None:
                return
            panel._priority_list.selection_clear(0, "end")
            panel._priority_list.selection_set(idx)
            panel.priority_move(1 if op == "priority-up" else -1)
            self._settings.save()
            return
        if op == "exclude-add":
            panel._exclude_entry.replace(game)
            panel.exclude_add()
            self._settings.save()
            self._revision += 1
            return
        if op == "exclude-delete":
            names = list(panel._exclude_list.get(0, "end"))
            idx = self._select_named(names, game)
            if idx is None:
                return
            panel._exclude_list.selection_clear(0, "end")
            panel._exclude_list.selection_set(idx)
            panel.exclude_delete()
            self._settings.save()
            self._revision += 1
            return
        raise web.HTTPBadRequest()

    def _apply_setting(self, key: str, value) -> None:
        panel = self._manager.settings
        settings = self._settings
        if key == "autostart":
            panel._vars["autostart"].set(1 if value else 0)
            panel.update_autostart()
            return
        if key == "tray":
            panel._vars["tray"].set(1 if value else 0)
            panel.update_autostart()
            return
        if key == "notifications":
            settings.tray_notifications = bool(value)
            panel._vars["tray_notifications"].set(1 if value else 0)
            settings.save()
            return
        if key == "language":
            settings.language = str(value or "")
            panel._vars["language"].set(settings.language)
            settings.save()
            return
        if key == "mode":
            panel._vars["priority_mode"].set(str(value or ""))
            panel.priority_mode(None)  # type: ignore[arg-type]
            settings.save()
            return
        if key == "proxy":
            raw = str(value or "").strip()
            url = URL(raw)
            if url.host is not None and url.port is not None:
                panel._proxy.replace(raw)
                settings.proxy = url
            else:
                panel._proxy.clear()
                settings.proxy = URL()
            settings.save()
            return
        if key == "badges":
            settings.enable_badges_emotes = bool(value)
            panel._vars["enable_badges_emotes"].set(1 if value else 0)
            settings.save()
            return
        if key == "drops_check":
            settings.available_drops_check = bool(value)
            panel._vars["available_drops_check"].set(1 if value else 0)
            settings.save()
            return
        raise web.HTTPBadRequest()

    def _watch_window(self) -> None:
        while not self._closing:
            time.sleep(1.5)
            if self._closing or not self._seen_window:
                continue
            alive = [pid for pid in self._window_pids if _pid_alive(pid)]
            if alive:
                self._window_pids = alive
                continue
            found = [pid for pid in self._edge_pids() if _pid_alive(pid)]
            if found:
                self._window_pids = found
                continue
            self._closing = True
            loop = getattr(self._manager._twitch, "_loop", None)
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._manager.close)
            return

    async def ensure_open(self) -> None:
        if self._starting:
            return
        self._starting = True
        try:
            await self._ensure_server()
            self._open_window()
        finally:
            self._starting = False

    async def _ensure_server(self) -> None:
        if self._runner is not None and self._url:
            return
        token = self._token
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/inventory", self._inventory)
        app.router.add_get("/api/state", self._state)
        app.router.add_post("/api/farm", self._farm)
        app.router.add_post("/api/action", self._action)
        runner = web.AppRunner(app)
        try:
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            sockets = getattr(site._server, "sockets", None) or []
            port = sockets[0].getsockname()[1]
        except Exception:
            await runner.cleanup()
            self._runner = None
            self._url = ""
            raise
        self._runner = runner
        self._url = f"http://127.0.0.1:{port}/?token={token}"
        logger.info(f"Inventory window is served at 127.0.0.1:{port}")

    def _allowed(self, request: web.Request) -> bool:
        return request.query.get("token") == self._token

    async def _index(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            raise web.HTTPForbidden()
        page = resource_path("shell.html").read_text(encoding="utf-8")
        labels = json.dumps(self._labels()).replace("<", "\\u003c")
        page = page.replace("__TOKEN__", self._token).replace("__LABELS__", labels)
        return web.Response(text=page, content_type="text/html")

    async def _inventory(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            raise web.HTTPForbidden()
        return web.json_response(self.snapshot())

    async def _state(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            raise web.HTTPForbidden()
        try:
            payload = self._app_state()
        except Exception:
            logger.warning("The application window could not read its state", exc_info=True)
            raise web.HTTPInternalServerError()
        return web.json_response(payload)

    async def _action(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except (TypeError, ValueError, UnicodeError):
            raise web.HTTPBadRequest()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        self.apply_action(body)
        return web.json_response({"ok": True})

    async def _farm(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
            game = str(body["game"])
            on = bool(body["on"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise web.HTTPBadRequest()
        self.set_farm(game, on)
        return web.json_response({"ok": True, "revision": self._revision})

    def _window_alive(self) -> bool:
        if time.time() < self._guard_until:
            return True
        if self._proc is not None and self._proc.poll() is None:
            return True
        alive = [pid for pid in self._window_pids if _pid_alive(pid)]
        self._window_pids = alive
        return bool(alive)

    def _capture_window(self, generation: int) -> None:
        for _ in range(4):
            time.sleep(0.7)
            if generation != self._capture_gen:
                return
            found = [pid for pid in self._edge_pids() if _pid_alive(pid)]
            if found:
                self._window_pids = found
                self._seen_window = True
                return

    def _edge_pids(self) -> list[int]:
        """Edge's app-mode launcher exits and leaves the real window behind."""
        if os.name != "nt":
            return []
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -like '*inventory_window*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [int(line) for line in result.stdout.split() if line.isdigit()]

    def _open_window(self) -> None:
        if not self._url:
            return
        if self._window_alive():
            if self._window_pids:
                _focus_pid(self._window_pids[0])
            return
        if self._lookup_pending:
            return
        if self._launched:
            self._lookup_pending = True
            threading.Thread(target=self._reopen_or_focus, daemon=True).start()
            return
        self._launch()

    def _reopen_or_focus(self) -> None:
        found = [pid for pid in self._edge_pids() if _pid_alive(pid)]
        if found:
            self._window_pids = found
            self._lookup_pending = False
            _focus_pid(found[0])
            return
        loop = getattr(self._manager._twitch, "_loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._finish_reopen)
            return
        self._lookup_pending = False

    def _finish_reopen(self) -> None:
        self._lookup_pending = False
        self._launch()

    def _launch(self) -> None:
        with self._lock:
            if self._window_alive():
                return
            edge = _edge_path()
            if edge is None:
                import webbrowser
                webbrowser.open(self._url)
                self._status.config(text="Inventory opened in the system browser.")
                return
            self._profile.mkdir(parents=True, exist_ok=True)
            self._guard_until = time.time() + 4
            self._launched = True
            self._proc = subprocess.Popen(
                [
                    str(edge),
                    f"--app={self._url}",
                    f"--user-data-dir={self._profile}",
                    "--window-size=1280,840",
                    "--no-first-run",
                    "--disable-extensions",
                    "--disable-sync",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._capture_gen += 1
            generation = self._capture_gen
        threading.Thread(
            target=self._capture_window, args=(generation,), daemon=True
        ).start()
        self._status.config(text="")
        if not self._watcher_started:
            self._watcher_started = True
            threading.Thread(target=self._watch_window, daemon=True).start()

    def close_view(self) -> None:
        self._closing = True
        proc = self._proc
        self._proc = None
        self._guard_until = 0.0
        self._launched = False
        self._lookup_pending = False
        self._capture_gen += 1
        pids = list(self._window_pids)
        self._window_pids = []
        if proc is not None and proc.poll() is None:
            pids.append(proc.pid)
        if not pids:
            pids.extend(self._edge_pids())
        for pid in dict.fromkeys(pids):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        runner = self._runner
        self._runner = None
        if runner is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(runner.cleanup())
