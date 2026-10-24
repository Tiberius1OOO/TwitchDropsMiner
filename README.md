# Twitch Drops Miner

A desktop application that collects timed Twitch drops. The mining loop comes from [DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner). This copy keeps that loop and puts Main, Inventory, Settings and Help in one dark window.

Maintained by [Tiberia](https://github.com/Tiberius1OOO/TwitchDropsMiner).

## Main

![Main: the drop in progress and the channel list](images/main.png)

The top bar is the same on every page. Under it, Main shows the game and the campaign you are collecting, with two progress bars. The channel being watched has a green mark on the left. Switch moves the miner to the channel you click.

## Inventory

![Inventory: one row per campaign](images/inventory.png)

Every campaign is one row of the same height.

- Farm chooses which games are collected first. With no box ticked, the listed games are all eligible.
- The cover, the title and the green status sit next to the time still left and the time needed to collect the rewards.
- Reward tiles share one size. When a row cannot show every reward, the rest is a `+N` chip.
- Show filters include a group. They do not change the order. Running campaigns stay first, then upcoming, then expired.

## Settings

Settings are grouped, and each explanation sits at the end of its group.

- General: language, autostart, tray, notifications.
- Priority: which games come first, which games are excluded, and the priority mode. Reload after those changes.
- Drop scan: Scan now, Update the drops list, and Reload. The list is fetched once when nothing is saved yet. It is not scanned again on a timer.
- Connection: the proxy. A proxy or language change applies on the next start.
- Advanced: options that can make the miner misbehave. Leave them off unless you need them.

## How a drop advances

Every few seconds the application requests stream metadata for the channel it is watching. Twitch counts that as watch time. The video and the audio are not downloaded. A websocket keeps each channel online or offline, and keeps the viewer count current.

Login is stored in `cookies.jar` beside the program. That file is enough to enter your Twitch account, so keep it private.

Watching another stream in a browser with the same account at the same time makes Twitch report progress that does not match this application.

## Run on Windows

Start `Twitch Drops Miner.exe` from the folder that contains `settings.json` and `cookies.jar`.

1. Log in when the window asks.
2. Link the Twitch account to the games you want on the [campaigns page](https://www.twitch.tv/drops/campaigns).
3. Open Inventory and tick Farm for the games you want first.
4. After a change in Settings, press Reload.

Minimize to Tray leaves the miner running with the window hidden.

## Build

Python 3.11 and the packages in `requirements.txt`.

```
python -m PyInstaller --noconfirm build.spec
```

The executable is written to `dist`.

## Credit

The application was created by [DevilXD](https://github.com/DevilXD/TwitchDropsMiner). This repository is maintained by [Tiberia](https://github.com/Tiberius1OOO/TwitchDropsMiner).
