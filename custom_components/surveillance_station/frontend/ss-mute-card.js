/*
 * ss-mute-card: the notification mutes of Surveillance Station, laid out as a
 * hierarchy: everything, then a kind for every camera, then each camera (all
 * its kinds, and expanded, one kind at a time).
 *
 * Loaded by ss-timeline-card.js (same version), so it needs no resource of
 * its own. It is built from the integration's mute switches (found by their
 * mute_ends attribute, so cameras that come, go or are renamed need no
 * configuring) and turns them with switch.turn_on / turn_off; the switches
 * keep the hierarchy straight (all kinds on shows its kinds on, everything
 * muted leaves the rest unavailable). The buttons call the mute and unmute
 * actions.
 *
 * Card options (all optional):
 *   title:     the heading (default: Notifications)
 *   durations: hours the buttons mute everything for (default: [1, 8])
 */

const MUTE_TAG = "ss-mute-card";
const { esc, muteText, muteEnds, muteGroups } = await import(
  new URL(`./ss-common.js${new URL(import.meta.url).search}`, import.meta.url).href
);
const KIND_ICONS = { person: "mdi:account", car: "mdi:car", animal: "mdi:paw" };

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
  .row.unavailable { opacity: .45; }
  .row ha-icon { color: var(--secondary-text-color); flex: none; }
  .row.on ha-icon { color: var(--primary-color); }
  .name { flex: 1; min-width: 0; }
  .name small { display: block; color: var(--secondary-text-color); }
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
    this._sig = null;
    this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
  }

  setConfig(config) {
    this._config = config ?? {};
    this._sig = null;
    this._render();
  }

  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _language() {
    return this._hass?.locale?.language ?? this._hass?.language;
  }

  _render() {
    if (!this._hass || !this._config) return;
    const groups = muteGroups(this._hass);
    const ids = groups.flatMap((g) => [g.all, ...Object.values(g.kinds), ...g.cameras.flatMap((c) => [c.all, ...Object.values(c.kinds)])]);
    // Only what the card shows: a state or its end changing, not every entity's.
    const sig = JSON.stringify([this._language(), this._config, [...this._open], ids.map((id) => {
      const s = this._hass.states[id];
      return [id, s?.state, s?.attributes.mute_ends, s?.attributes.camera, s?.attributes.locked];
    })]);
    if (sig === this._sig) return;
    this._sig = sig;
    const text = muteText(this._language());
    const hours = Array.isArray(this._config.durations) ? this._config.durations : [1, 8];
    const body = groups.length
      ? groups.map((g) => this._device(g, text)).join("")
      : `<div class="empty">${esc(text.none)}</div>`;
    this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card>
      <h2>${esc(this._config.title ?? text.title)}</h2>
      ${groups.length ? `<div class="buttons">
        ${hours.map((n) => `<button data-mute="${Number(n)}">${esc(text.muteFor.replace("{n}", n))}</button>`).join("")}
        <button data-unmute>${esc(text.unmuteAll)}</button></div>` : ""}
      ${body}</ha-card>`;
  }

  _row(id, label, icon, text, cls = "", lead = "", sub = "") {
    if (!id) return "";
    const s = this._hass.states[id];
    const on = s?.state === "on";
    const gone = !s || s.state === "unavailable" || s.attributes.locked === true; // locked: covered by a wider mute
    const ends = sub || (on ? muteEnds(s, text) : "");
    return `<div class="row ${cls} ${on ? "on" : ""} ${gone ? "unavailable" : ""}">
      ${lead}<ha-icon icon="${icon}"></ha-icon>
      <div class="name">${esc(label)}${ends ? `<small>${esc(ends)}</small>` : ""}</div>
      <ha-switch data-e="${esc(id)}" ${on ? "checked" : ""} ${gone ? "disabled" : ""}></ha-switch></div>`;
  }

  _kindRows(kinds, text, cls) {
    return ["person", "car", "animal", ...Object.keys(kinds).filter((k) => !KIND_ICONS[k])]
      .filter((k) => kinds[k])
      .map((k) => this._row(kinds[k], text[k] ?? k[0].toUpperCase() + k.slice(1), KIND_ICONS[k] ?? "mdi:tag", text, cls))
      .join("");
  }

  _device(g, text) {
    const cameras = g.cameras.map((c) => {
      const open = this._open.has(c.name);
      const lead = `<button class="chevron" data-camera="${esc(c.name)}"><ha-icon icon="mdi:chevron-right"></ha-icon></button>`;
      const partial = Object.entries(c.kinds).filter(([, id]) => this._hass.states[id]?.state === "on").map(([k]) => text[k] ?? k);
      const all = this._hass.states[c.all]?.state === "on";
      const sub = !all && partial.length ? `${partial.join(", ")} · ${text.muted}` : "";
      const row = this._row(c.all, c.name, "mdi:cctv", text, "camera", lead, sub);
      return `<div class="${open ? "open" : ""}">${row}${open ? this._kindRows(c.kinds, text, "kind") : ""}</div>`;
    }).join("");
    return `<div class="device">
      ${this._row(g.all, text.everything, "mdi:bell-off", text)}
      ${Object.keys(g.kinds).length ? `<div class="section">${esc(text.allCameras)}</div>${this._kindRows(g.kinds, text, "kind")}` : ""}
      ${g.cameras.length ? `<div class="section">${esc(text.cameras)}</div>${cameras}` : ""}</div>`;
  }

  _onChange(e) {
    const el = e.target.closest?.("ha-switch[data-e]");
    if (!el) return;
    this._hass.callService("switch", el.checked ? "turn_on" : "turn_off", { entity_id: el.dataset.e });
  }

  _onClick(e) {
    const t = e.target.closest?.("button");
    if (!t) return;
    if (t.dataset.mute) this._hass.callService("surveillance_station", "mute", { duration: { hours: Number(t.dataset.mute) } });
    else if ("unmute" in t.dataset) this._hass.callService("surveillance_station", "unmute", {});
    else if (t.dataset.camera != null) {
      this._open.has(t.dataset.camera) ? this._open.delete(t.dataset.camera) : this._open.add(t.dataset.camera);
      this._render();
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
