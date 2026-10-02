# AGENTS.md

For AI agents (and people) changing this repository. What the parts are, how
to test, deploy and release, and what was learned about Surveillance
Station's API is in [docs/development.md](docs/development.md); read it
before a change. This file holds only the rules every change must keep.

To **install** the integration on a Home Assistant instead of changing it,
follow [.claude/skills/install-surveillance-station/SKILL.md](.claude/skills/install-surveillance-station/SKILL.md).

## Done means

- `scripts/test.sh` passes with no arguments: that form also requires 95%
  coverage of the integration and of the library, each on its own.
- `node --test "tests/js/*.test.mjs"` passes, if a card changed.
- A change to how a card behaves in a browser was driven by a scenario in
  `tests/browser/` (they need a real HA, NAS and GPU: see
  [Tests](docs/development.md#tests)). If you cannot run one, say so in your
  report; a card change is not verified without it.
- No skipped test, stub or TODO stands in for the work.
- The doc that describes the changed behaviour says the new behaviour.

## Rules

1. **English in the repository**: code, comments, commit messages, docs, test
   names, log and error text. Users and contributors read them.
2. **The library knows nothing of Home Assistant.** `synology_ss/` imports no
   `homeassistant` module and has its own tests; the integration reaches
   Surveillance Station only through the library's client. It is published on
   PyPI by itself, and the integration's tests mock exactly that client.
3. **Recordings are never transcoded.** Playback sends SS's own H.265/H.264;
   only time-lapse is re-encoded ([why](docs/timelapse.md#why-it-is-transcoded)).
   A change that decodes or encodes recorded video on HA is a different
   product: ask first.
4. **No secret leaves the config entry.** The DSM password, SS session ids
   and tokens stay out of logs, exception text, URLs handed to browsers and
   diagnostics ([security model](docs/security.md)). A new stored field with a
   secret or an address in it is added to the diagnostics' redaction.
5. **What users' HA has stored keeps working.** Entity unique ids, device
   identifiers, Store formats and config-entry data are in people's
   installations: changing one needs a migration and a test that loads the
   old form.
6. **One version, in two places.** `manifest.json`'s `version` and
   `CARD_VERSION` in `frontend/ss-timeline-card.js` are equal
   (`tests/test_init.py` checks). Browsers cache the cards for a month by that
   version, so a card change that ships without a bump is not seen.
7. **Text in two places.** `translations/en.json` is a copy of `strings.json`;
   change both (`tests/test_translations.py` checks that every translation
   has the same keys and placeholders).
8. **The installer follows the flows.** `scripts/install.py` fills in the
   config and options flows over HA's API: when a flow's fields or error keys
   change, change the installer with them (`tests/test_install_script.py`
   fails until you do). It stays standard-library-only and Python 3.9
   compatible, since people run it as a single downloaded file (the same test
   file checks both). It prints what it picked out of HA's answers, never a
   raw answer or an unforeseen error's text: a flow's form echoes the
   password.
9. **Numbers in the docs are measured**, and say on what
   ([Assumed setup](README.md#assumed-setup)). Do not round a guess into the
   README.
10. **Comments say why**, not what; match the file you are in.

## Git

- `main` is the release line and `dev` the development line. Commit summaries
  are one short line (`README: …`, `Docs: …`, or what changed); a release
  commit is just the version.
- Pushing, tagging, publishing a release and deleting anything remote are the
  maintainer's decisions: ask first. The steps are in
  [Releasing](docs/development.md#releasing).

## Working against a real Home Assistant

- The checkout is the only source: change it and deploy
  ([Deploying a checkout](docs/development.md#deploying-a-checkout)), never
  edit the copy under HA's `custom_components/`.
- Never edit files in HA's `.storage`; go through HA's API.
- A test that changes someone's HA (their mute switches, options, dashboards)
  puts back what it changed before it ends, pass or fail.
