// Panel of a HottoH stove: the web interface of hottoh_api, relayed by Home Assistant.
// The relay accepts a session cookie instead of the access token, which an iframe cannot send:
// the panel opens the session, then renews it while it is shown.

const RENEW_MS = 5 * 60 * 1000;

class HottohPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        /* ha-panel-custom is a block without a height (padded by the safe areas):
           100% would leave the iframe at its default 150 px */
        :host { display: flex; flex-direction: column;
          height: calc(100vh - var(--safe-area-inset-top, 0px) - var(--safe-area-inset-bottom, 0px));
          height: calc(100dvh - var(--safe-area-inset-top, 0px) - var(--safe-area-inset-bottom, 0px));
          background: var(--primary-background-color); }
        .toolbar { display: flex; align-items: center; height: var(--header-height, 56px);
          padding: 0 12px; box-sizing: border-box; flex-shrink: 0;
          background: var(--app-header-background-color, var(--primary-color));
          color: var(--app-header-text-color, var(--text-primary-color));
          border-bottom: var(--app-header-border-bottom, none);
          font-size: 20px; }
        .title { margin-left: 12px; }
        iframe { flex: 1; min-height: 0; width: 100%; border: 0; display: block; }
        .error { padding: 16px; color: var(--error-color); }
      </style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><span class="title"></span></div>
      <iframe title="hottoh_api" hidden></iframe>
      <div class="error" hidden></div>`;
    this._menu = this.shadowRoot.querySelector('ha-menu-button');
    this._frame = this.shadowRoot.querySelector('iframe');
    this._error = this.shadowRoot.querySelector('.error');
  }

  set hass(hass) {
    this._hass = hass;
    this._menu.hass = hass;
    this._start();
  }

  set narrow(narrow) {
    this._menu.narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
    this.shadowRoot.querySelector('.title').textContent = panel.title || 'HottoH';
    this._start();
  }

  connectedCallback() {
    this._start();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = undefined;
  }

  async _start() {
    if (!this._hass || !this._panel || !this.isConnected || this._timer !== undefined) {
      return;
    }
    this._timer = setInterval(() => this._session(), RENEW_MS);
    await this._session();
  }

  async _session() {
    try {
      await this._hass.callApi('POST', 'hottoh/session');
      this._error.hidden = true;
      const relay = this._panel.config.relay;
      if (this._frame.getAttribute('src') !== relay) {
        this._frame.src = relay;
      }
      this._frame.hidden = false;
    } catch (err) {
      this._error.textContent = `Cannot open the hottoh_api interface: ${err.message || err.body || err}`;
      this._error.hidden = false;
    }
  }
}

customElements.define('hottoh-panel', HottohPanel);
