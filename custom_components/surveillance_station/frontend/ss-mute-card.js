/*
 * ss-mute-card: the notification mutes of Surveillance Station, laid out as a
 * hierarchy: everything, then a kind for every camera, then each camera (all
 * its kinds, and expanded, one kind at a time).
 *
 * Loaded by ss-timeline-card.js (same version), so it needs no resource of
 * its own. It is built from the integration's mute switches (its switches
 * with the camera and locked attributes, so cameras that come, go or are
 * renamed need no configuring) and turns them with switch.turn_on / turn_off;
 * the switches keep the hierarchy straight (all kinds on shows its kinds on,
 * everything muted leaves the rest locked). The buttons call the mute and
 * unmute actions.
 *
 * Card options (all optional):
 *   title:     the heading (default: Notifications)
 *   durations: hours the buttons mute everything for (default: [1, 8])
 */

const MUTE_TAG = "ss-mute-card";
const { esc, labelAttrs, fillText, kindLabel, muteText, muteEnds, muteDurations, muteSwitchIds, muteGroups } = await import(
  new URL(`./ss-common.js${new URL(import.meta.url).search}`, import.meta.url).href
);
// Off and on, as icons.json has them for the switches. The kinds are listed in
// this order, then any others; their names are muteText's `kinds`.
const ALL_ICONS = ["mdi:bell-outline", "mdi:bell-off"];
const CAMERA_ICONS = ["mdi:cctv", "mdi:cctv-off"];
const KIND_ICONS = { person: ["mdi:walk", "mdi:account-off"], car: ["mdi:car", "mdi:car-off"], animal: ["mdi:paw", "mdi:paw-off"] };
const OTHER_KIND_ICONS = ["mdi:tag", "mdi:tag"];

const CSS = `
  :host { display: block; }
  ha-card { padding: 8px 0; }
  h2 { margin: 0; padding: 8px 16px; font-size: 1.1em; font-weight: 500; }
  .buttons { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 16px 8px; }
  .buttons button { flex: 1 1 auto; padding: 8px 12px; border: 1px solid var(--divider-color); border-radius: 8px;
    background: none; color: var(--primary-text-color); font: inherit; cursor: pointer; }
  .buttons button:hover { background: var(--secondary-background-color); }
  .device { padding: 4px 0; }
  .device + .device { border-top: 1px solid var(--divider-color); }
  .section { padding: 8px 16px 2px; font-size: .8em; text-transform: uppercase; letter-spacing: .05em; color: var(--secondary-text-color); }
  .row { display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 0 16px; }
  .row.kind { padding-left: 52px; }
  .row.locked, .row.unavailable { opacity: .45; }
  .row ha-icon { color: var(--secondary-text-color); flex: none; }
  .row.on ha-icon { color: var(--primary-color); }
  .name { flex: 1; min-width: 0; }
  .name small { display: block; color: var(--secondary-text-color); }
  .name small:empty { display: none; }
  .chevron { flex: none; width: 24px; height: 24px; padding: 0; border: 0; background: none; color: var(--secondary-text-color); cursor: pointer; }
  .chevron ha-icon { transition: transform .15s; }
  .open .chevron ha-icon { transform: rotate(90deg); }
  .empty { padding: 16px; color: var(--secondary-text-color); }
`;

class SSMuteCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._open = new Set(); // cameras expanded (kept across the redraws a state change makes)
    this._ids = []; // the entities that may be mute switches, from hass.entities
    this._seen = null; // what the rows show was last taken from (null: take it again)
    this._layout = null; // what the markup was built for; the rows are updated in place while it holds
    this._rows = []; // the rows drawn: entity id, elements, icons
    this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
  }

  setConfig(config) {
    this._config = config ?? {};
    this._seen = null;
    this._render();
  }

  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    if (hass?.entities !== this._hass?.entities) this._ids = muteSwitchIds(hass);
    this._hass = hass;
    this._render();
  }

  _language() {
    return this._hass?.locale?.language ?? this._hass?.language;
  }

  _render() {
    if (!this._hass || !this._config) return;
    // HA sets hass on every state change in the house; only the mute switches' states, the language and the time format matter here.
    const seen = [this._hass.locale, this._hass.language, ...this._ids.map((id) => this._hass.states[id])];
    if (this._seen && seen.length === this._seen.length && seen.every((s, i) => s === this._seen[i])) return;
    this._seen = seen;
    const groups = muteGroups(this._hass, this._ids);
    const layout = JSON.stringify([this._language(), this._config, [...this._open], groups]);
    if (layout !== this._layout) {
      this._layout = layout;
      this._build(groups);
    }
    this._update();
  }

  /** The markup: rows, labels, buttons; what each row shows is _update's. */
  _build(groups) {
    const text = (this._text = muteText(this._language()));
    const rows = [];
    const row = (spec) => {
      rows.push(spec);
      return this._rowHtml(spec);
    };
    const body = groups.length
      ? groups.map((g) => this._device(g, text, row)).join("")
      : `<div class="empty">${esc(text.none)}</div>`;
    this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card>
      <h2>${esc(this._config.title ?? text.title)}</h2>
      ${groups.length ? `<div class="buttons">
        ${muteDurations(this._config.durations).map((n) => `<button data-mute="${n}">${esc(fillText(text.muteFor, { n }))}</button>`).join("")}
        <button data-unmute>${esc(text.unmuteAll)}</button></div>` : ""}
      ${body}</ha-card>`;
    const switches = new Map([...this.shadowRoot.querySelectorAll("ha-switch[data-e]")].map((sw) => [sw.dataset.e, sw]));
    this._rows = rows.map((spec) => {
      const sw = switches.get(spec.id);
      const el = sw.closest(".row");
      // :scope > : the row's own icon, not the camera arrow's.
      return { ...spec, el, sw, icon: el.querySelector(":scope > ha-icon"), sub: el.querySelector("small") };
    });
  }

  /** Each row's state: on, locked or unavailable, its icon and how long it lasts. */
  _update() {
    for (const r of this._rows) {
      const s = this._hass.states[r.id];
      const on = s?.state === "on";
      const unavailable = !s || s.state === "unavailable";
      const locked = !unavailable && s.attributes.locked === true; // covered by a wider mute
      // A camera partly muted says which of its kinds are.
      const partial = r.kinds && !on ? Object.entries(r.kinds).filter(([, id]) => this._hass.states[id]?.state === "on").map(([k]) => kindLabel(this._text, k)) : [];
      r.el.classList.toggle("on", on);
      r.el.classList.toggle("locked", locked);
      r.el.classList.toggle("unavailable", unavailable);
      r.icon.setAttribute("icon", r.icons[on ? 1 : 0]);
      r.sub.textContent = partial.length ? `${partial.join(", ")} · ${this._text.muted}` : muteEnds(s, this._text, this._hass.locale);
      r.sw.checked = on; // also puts back a switch whose call was refused
      r.sw.disabled = locked || unavailable;
    }
  }

  _rowHtml({ id, label, aria, cls = "", lead = "" }) {
    return `<div class="row ${cls}">
      ${lead}<ha-icon></ha-icon>
      <div class="name">${esc(label)}<small></small></div>
      <ha-switch data-e="${esc(id)}" aria-label="${esc(aria)}"></ha-switch></div>`;
  }

  _kindRows(kinds, text, row, camera) {
    return [...Object.keys(KIND_ICONS), ...Object.keys(kinds).filter((k) => !KIND_ICONS[k])]
      .filter((k) => kinds[k])
      .map((k) => {
        const label = kindLabel(text, k);
        const aria = camera == null ? fillText(text.muteKind, { kind: label }) : fillText(text.muteCameraKind, { kind: label, camera });
        return row({ id: kinds[k], label, aria, icons: KIND_ICONS[k] ?? OTHER_KIND_ICONS, cls: "kind" });
      })
      .join("");
  }

  _device(g, text, row) {
    const cameras = g.cameras.map((c) => {
      const open = this._open.has(c.name);
      const lead = `<button class="chevron" data-camera="${esc(c.name)}" ${labelAttrs(fillText(text.kindsOf, { camera: c.name }))}
        aria-expanded="${open}"><ha-icon icon="mdi:chevron-right"></ha-icon></button>`;
      const camRow = c.all
        ? row({ id: c.all, label: c.name, aria: fillText(text.muteCamera, { camera: c.name }), icons: CAMERA_ICONS, cls: "camera", lead, kinds: c.kinds })
        : "";
      return `<div class="${open ? "open" : ""}">${camRow}${open ? this._kindRows(c.kinds, text, row, c.name) : ""}</div>`;
    }).join("");
    return `<div class="device">
      ${g.all ? row({ id: g.all, label: text.everything, aria: text.muteAll, icons: ALL_ICONS }) : ""}
      ${Object.keys(g.kinds).length ? `<div class="section">${esc(text.byKind)}</div>${this._kindRows(g.kinds, text, row)}` : ""}
      ${g.cameras.length ? `<div class="section">${esc(text.cameras)}</div>${cameras}` : ""}</div>`;
  }

  _onChange(e) {
    const el = e.target.closest?.("ha-switch[data-e]");
    if (!el) return;
    // Refused (a wider mute came first, the connection dropped): HA says why, the state stays, and so must the switch.
    this._hass.callService("switch", el.checked ? "turn_on" : "turn_off", { entity_id: el.dataset.e }).catch(() => this._update());
  }

  _onClick(e) {
    const button = e.target.closest?.("button");
    if (!button) return;
    const reported = () => {}; // HA shows a refused call's error
    if (button.dataset.mute) this._hass.callService("surveillance_station", "mute", { duration: { hours: Number(button.dataset.mute) } }).catch(reported);
    else if ("unmute" in button.dataset) this._hass.callService("surveillance_station", "unmute", {}).catch(reported);
    else if (button.dataset.camera != null) {
      const camera = button.dataset.camera;
      if (this._open.has(camera)) this._open.delete(camera);
      else this._open.add(camera);
      this._seen = null;
      this._render();
      // The markup was rebuilt: keep the focus on this camera's arrow.
      [...this.shadowRoot.querySelectorAll("button.chevron")].find((b) => b.dataset.camera === camera)?.focus();
    }
  }
}

if (!customElements.get(MUTE_TAG)) {
  customElements.define(MUTE_TAG, SSMuteCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: MUTE_TAG,
    name: "Surveillance Station notifications",
    description: "Mute Surveillance Station notifications: everything, a kind, a camera, or one kind on one camera.",
  });
}
