---
name: install-surveillance-station
description: Install, upgrade, set up or check the Surveillance Station Playback integration on someone's Home Assistant - the integration's files (through HACS or the config directory), the Synology NAS login, a dashboard with the card, and optionally Frigate detections and phone notifications. Use when asked to install, configure, upgrade or troubleshoot this integration on a Home Assistant, not when changing its code.
compatibility: Needs Python 3.9+, network access to the Home Assistant instance, and scripts/install.py from this repository.
---

# Install Surveillance Station Playback

Everything goes through `scripts/install.py`. It talks to Home Assistant's
own API, so it runs from any machine that reaches HA; it looks before each
step and changes only what is missing, so running it again is always safe.
`scripts/install.py --help` lists the flags. Its output is one line per step:
`[ok]` done, `[skip]` already so, `[...]` waiting, `[warn]` works but look at
it, `[todo]` for the person afterwards, `[FAIL]` what to change (exit 1),
`[stop]` what only the person can do (exit 2).

## 1. Collect what is needed

Ask for everything in the first table at once; ask about the optional parts
before you run anything, since they change the command.

| Needed | Why, and where it comes from |
|---|---|
| Home Assistant's address, as this machine reaches it | `--ha-url`. HA 2026.9 or newer. |
| A long-lived access token of an HA **administrator**, in a file | HA: the person's profile → *Security* → *Long-lived access tokens*. Config entries, HACS and restarting are administrator-only. |
| A way to place the files: **HACS** in that HA, or this machine having HA's **config directory** | With HACS nothing else is needed. Without it, pass `--config-dir` (the folder holding `configuration.yaml`) and run as a user who may write there. Neither: have the person install [HACS](https://hacs.xyz) first. |
| Their OK to **restart** Home Assistant | HA loads a new integration's files only at start. Ask each time before passing `--restart`: it interrupts their automations for a minute. |
| The NAS's address **as Home Assistant reaches it**, and the port | `--ss-host`, `--ss-port` (HTTPS 5001 by default; `--ss-http` for 5000). HA in a container may not resolve a name this machine does. Surveillance Station 9 on DSM 7. |
| A **dedicated DSM account** and its password, in a file | The person creates it in DSM ([what it needs](../../../README.md#installation)): Surveillance Station only, may play back and download recordings, no two-step verification. Not their admin account: HA stores the password. |

Optional parts:

| Part | Needs |
|---|---|
| A dashboard with the card (`--dashboard`) | Nothing more. An existing dashboard at that path is never touched. |
| Frigate detections as bookmarks (`--frigate`) | Frigate 0.14+; HA's **MQTT integration** set up on the broker Frigate publishes to (the script stops if it is missing; the person adds it under *Settings → Devices & services*); `record.enabled: true` for each Frigate camera, or Frigate publishes no reviews for it. |
| Frigate's snapshots in notifications, smart search (`--frigate-url`) | Frigate's API **as Home Assistant reaches it, without a login**: its internal port, `http://<frigate>:5000`. Port 8971 asks for a login the integration cannot give, and a URL with a user and password in it is refused. `snapshots.enabled: true` per camera. |
| Cameras named differently in Frigate (`--camera-map`) | Names are matched ignoring case, spaces and punctuation (`drive_way` = "Drive Way"); map only the rest, as `'SS camera=frigate_camera'`. |
| Phone notifications (`--notify-device`) | A phone with the Home Assistant Companion app, logged in to that HA; its device name. |
| Time-lapse (`--timelapse` adds its view) | A time-lapse task set up in Surveillance Station. |

Done when: you know the HA address, where the token file is, how files get
there, the NAS host and account, and which optional parts they want.

## 2. Keep the secrets out of sight

- Have the person put the token and the DSM password in files only they can
  read (`chmod 600`), and pass the paths: `HA_TOKEN_FILE=…`,
  `--ss-password-file …`. If they paste a secret into the chat, write it to
  such a file yourself and use the file from then on. Why: command lines show
  in `ps` and shell history, and chat logs are kept.
- Never read those files back, echo them, or put their contents in a command.
- Never copy secrets into this repository or any file under version control.

## 3. Look first

```sh
export HA_TOKEN_FILE=<token file>
scripts/install.py --ha-url <HA address> --check
```

`--check` changes nothing. Done when it prints `[ok] Home Assistant <version>
…, administrator token`. A `[stop]` saying the integration is not installed is
expected on a first install. If the integration is already there, the lines
that follow say what is set up; install only what is missing.

## 4. Install and set up

One command; leave out what they did not ask for. Add `--config-dir <folder>`
if there is no HACS.

```sh
scripts/install.py --ha-url <HA address> --restart \
    --ss-host <NAS> --ss-user <DSM account> --ss-password-file <file> \
    --dashboard \
    --frigate --frigate-url http://<frigate>:5000 \
    --notify-device "<phone>"
```

- `[FAIL]` names what to change. Change that and run the same command again:
  steps already done are skipped. A NAS that is set up is not logged in to
  again, so a rerun needs no password; to change its login, the person uses
  *Reconfigure* on the integration in HA.
- If this machine cannot reach Frigate at the address HA uses, add
  `--frigate-check-url <address from here>` so the script can still read
  Frigate's settings; without it that check is skipped with a `[warn]`.
- `[warn] Frigate camera X matches no Surveillance Station camera`: ask which
  SS camera it is and rerun with `--camera-map 'SS camera=X'`; or leave it, if
  SS does not record that camera.

Done when the command exits 0 and its last lines include
`[ok] Surveillance Station answers: N cameras (…)` with the cameras the person
expects, and, with Frigate, `[ok] Frigate detections: listening on …`.

## 5. Hand over

Tell the person:

- where the card is (`<HA address>/ss-playback/playback` with `--dashboard`),
  and to reload that page once without the browser's cache;
- that the browser must decode H.265 if the cameras record it: the HA apps,
  Safari and Chrome/Edge do, Firefox does not;
- every `[warn]` line, in their words, and whether it needs them;
- with Frigate: a detection shows as a bookmark within seconds of Frigate's
  next review. None after a real detection means the topic prefix, the broker
  or `record.enabled` ([docs/frigate.md](../../../docs/frigate.md)).

To add the card to a dashboard of their own instead, they add a card of
`type: custom:ss-timeline-card` ([its options](../../../docs/timeline-card.md)).

## Later

- Upgrade: `scripts/install.py --ha-url <HA address> --upgrade --restart`.
  Through HACS that is the latest release. With `--config-dir` it is this
  checkout, so run `git pull` first; the script refuses a checkout older than
  what is installed.
- Change an option: the same script with only that flag
  (`--transcoder cpu`, `--no-frigate`, …); the rest keeps its value.
- Remove: [README, "Removing"](../../../README.md#removing).

## Limits

- Change Home Assistant only through the script or HA's interface. Never edit
  files under HA's `.storage` folder: HA rewrites them from memory, so edits
  are lost or corrupt its registries.
- The script does not create the DSM account, install HACS, add the MQTT
  integration or edit Frigate's configuration. Those are the person's; tell
  them exactly what is missing and wait.
- A problem the script's output does not explain is in HA's log
  (*Settings → System → Logs*) or under *Settings → Repairs*; quote what you
  find there instead of guessing.
