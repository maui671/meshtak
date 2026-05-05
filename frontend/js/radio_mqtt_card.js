class RadioMqttCard {
    constructor(api) {
        this._api = api;
        this._root = null;
        this._current = null;
    }

    mount(rootEl) {
        this._root = rootEl;
        rootEl.classList.add('r-card');
        rootEl.innerHTML = `
            <div class="r-card__header">
                <h3 class="r-card__title">MQTT Uplink</h3>
                <span class="r-card__subtitle" id="r-mqtt-summary">--</span>
            </div>
            <p class="r-card__hint">
                For Meshradar-style local ingest, <code>API Key</code> mode can work by itself if the key was created in
                Meshradar after broker-sync was enabled. Only enter broker <code>Username / Password</code> here when you
                intentionally want Meshpoint to use a regular MQTT account instead of API-key-only auth.
            </p>
            <div class="config-stack">
                <div class="config-pane">
                    <div class="config-pane__label">Broker</div>
                    <div class="config-pane__inputs">
                        <div class="r-field">
                            <label class="r-field__label" for="r-mqtt-enabled">Enabled</label>
                            <select class="r-select" id="r-mqtt-enabled">
                                <option value="true">On</option>
                                <option value="false">Off</option>
                            </select>
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-mqtt-broker">Host</label>
                            <input class="r-input" id="r-mqtt-broker" placeholder="mqtt.example.com" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-mqtt-port">Port</label>
                            <input type="number" class="r-input r-input--mono r-input--narrow"
                                   id="r-mqtt-port" min="1" max="65535" />
                        </div>
                    </div>
                </div>
                <div class="config-pane">
                    <div class="config-pane__label">Authentication</div>
                    <div class="config-pane__inputs">
                        <div class="r-field">
                            <label class="r-field__label" for="r-mqtt-auth-mode">Mode</label>
                            <select class="r-select" id="r-mqtt-auth-mode">
                                <option value="username_password">Username / Password</option>
                                <option value="api_key">API Key</option>
                                <option value="none">No Auth</option>
                            </select>
                        </div>
                        <div class="r-field" id="r-mqtt-user-wrap">
                            <label class="r-field__label" for="r-mqtt-username">Username</label>
                            <input class="r-input" id="r-mqtt-username" placeholder="meshdev" />
                        </div>
                        <div class="r-field" id="r-mqtt-pass-wrap">
                            <label class="r-field__label" for="r-mqtt-password">Password</label>
                            <input type="password" class="r-input" id="r-mqtt-password" />
                        </div>
                        <div class="r-field" id="r-mqtt-key-wrap">
                            <label class="r-field__label" for="r-mqtt-api-key">API Key</label>
                            <input type="password" class="r-input" id="r-mqtt-api-key" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-mqtt-topic-root">Topic Root</label>
                            <input class="r-input" id="r-mqtt-topic-root" placeholder="msh" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-mqtt-region">MQTT Region</label>
                            <input class="r-input r-input--mono r-input--narrow"
                                   id="r-mqtt-region" placeholder="US" />
                        </div>
                    </div>
                </div>
            </div>
            <div class="r-card__actions">
                <button class="r-btn r-btn--primary" id="r-save-mqtt">Save MQTT</button>
            </div>
        `;
        this._wire();
    }

    render(config) {
        this._current = config.mqtt || {};
        this._root.querySelector('#r-mqtt-enabled').value = String(!!this._current.enabled);
        this._root.querySelector('#r-mqtt-broker').value = this._current.broker || '';
        this._root.querySelector('#r-mqtt-port').value = this._current.port || 1883;
        this._root.querySelector('#r-mqtt-auth-mode').value = this._current.auth_mode || 'username_password';
        this._root.querySelector('#r-mqtt-username').value = this._current.username || '';
        this._root.querySelector('#r-mqtt-password').value = this._current.password || '';
        this._root.querySelector('#r-mqtt-api-key').value = this._current.api_key || '';
        this._root.querySelector('#r-mqtt-topic-root').value = this._current.topic_root || 'msh';
        this._root.querySelector('#r-mqtt-region').value = this._current.region || 'US';
        this._root.querySelector('#r-mqtt-summary').textContent =
            `${this._current.broker || '--'}:${this._current.port || '--'}`;
        this._toggleAuthFields();
    }

    _wire() {
        this._root.querySelector('#r-mqtt-auth-mode').addEventListener(
            'change', () => this._toggleAuthFields(),
        );
        this._root.querySelector('#r-save-mqtt').addEventListener(
            'click', async () => this._save(),
        );
    }

    _toggleAuthFields() {
        const mode = this._root.querySelector('#r-mqtt-auth-mode').value;
        const showUserPass = mode !== 'none';
        const showApiKey = mode === 'api_key';
        this._root.querySelector('#r-mqtt-user-wrap').style.display = showUserPass ? '' : 'none';
        this._root.querySelector('#r-mqtt-pass-wrap').style.display = showUserPass ? '' : 'none';
        this._root.querySelector('#r-mqtt-key-wrap').style.display = showApiKey ? '' : 'none';
    }

    async _save() {
        const payload = {
            enabled: this._root.querySelector('#r-mqtt-enabled').value === 'true',
            broker: this._root.querySelector('#r-mqtt-broker').value.trim(),
            port: parseInt(this._root.querySelector('#r-mqtt-port').value || '1883', 10),
            auth_mode: this._root.querySelector('#r-mqtt-auth-mode').value,
            username: this._root.querySelector('#r-mqtt-auth-mode').value !== 'none'
                ? this._root.querySelector('#r-mqtt-username').value
                : '',
            password: this._root.querySelector('#r-mqtt-auth-mode').value !== 'none'
                ? this._root.querySelector('#r-mqtt-password').value
                : '',
            api_key: this._root.querySelector('#r-mqtt-api-key').value,
            api_key_field: 'password',
            topic_root: this._root.querySelector('#r-mqtt-topic-root').value.trim(),
            region: this._root.querySelector('#r-mqtt-region').value.trim(),
        };

        const result = await this._api.put('/api/config/mqtt', payload);
        if (result) {
            this._api.toast('MQTT settings saved');
            await this._api.refresh();
        }
    }
}

window.RadioMqttCard = RadioMqttCard;
