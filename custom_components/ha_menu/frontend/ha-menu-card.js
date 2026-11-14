// Carte « Menu du jour » de l'intégration HA - Menu (web component sans dépendance).
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

class HaMenuCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true,
          selector: { entity: { filter: { integration: "ha_menu", domain: "sensor" } } } },
        { name: "title", selector: { text: {} } },
      ],
      computeLabel: (s) => ({ entity: "Entité « Menu affiché »", title: "Titre (facultatif)" }[s.name]),
    };
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass.states).find(
      (e) => e.startsWith("sensor.") && hass.states[e].attributes.lignes !== undefined);
    return { entity: id || "" };
  }

  setConfig(config) {
    if (!config.entity) throw new Error("Choisir l'entité « Menu affiché »");
    this._config = config;
    this._key = null;
  }

  set hass(hass) {
    const st = hass.states[this._config.entity];
    const key = st ? `${st.state}|${st.last_updated}` : "absent";
    if (key === this._key) return;
    this._key = key;
    this._render(st);
  }

  getCardSize() { return 4; }

  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  _render(st) {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    let body;
    if (!st) {
      body = `<div class="empty">Entité introuvable</div>`;
    } else {
      const a = st.attributes;
      const demain = a.jour === "demain";
      const title = this._config.title || (demain ? "Menu de demain" : "Menu de midi");
      const head = `<div class="head"><span class="title">${esc(title)}</span>
        <span class="date">${esc(st.state)}</span></div>`;
      if (st.state === "unavailable" || st.state === "unknown") {
        body = head + `<div class="empty">Menu indisponible</div>`;
      } else if (a.cantine === false) {
        body = head + `<div class="empty">Pas de cantine ${demain ? "demain" : "aujourd'hui"}</div>`;
      } else {
        const lines = (a.lignes || []).map((l) => `<div class="row">
          <span class="label">${esc(l.label)}</span><span class="value">${esc(l.valeur)}</span></div>`).join("");
        const warn = a.cantine === null ? `<div class="warn">inscription non vérifiée</div>` : "";
        body = head + lines + warn;
      }
    }
    this.shadowRoot.innerHTML = `<style>
      ha-card { padding: 16px; }
      .head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
      .title { font-size: 1.2em; font-weight: 500; color: var(--primary-text-color); }
      .date { color: var(--secondary-text-color); }
      .row { display: flex; gap: 12px; padding: 4px 0; border-top: 1px solid var(--divider-color); }
      .label { flex: 0 0 6.5em; color: var(--primary-color); font-weight: 500; }
      .value { color: var(--primary-text-color); }
      .empty { color: var(--secondary-text-color); padding: 8px 0; }
      .warn { margin-top: 8px; font-size: 0.85em; color: var(--warning-color, #e6a23c); }
    </style><ha-card>${body}</ha-card>`;
  }
}

customElements.define("ha-menu-card", HaMenuCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "ha-menu-card", name: "Menu du jour", description: "Menu de cantine HA - Menu", preview: true,
});
