class RadioTakCard {
    static ROLE_OPTIONS = [
        'Team Member', 'Team Leader', 'RTO', 'Medic',
        'Observer', 'Scout', 'HQ', 'Vehicle',
    ];

    static COLOR_OPTIONS = [
        'Blue', 'Green', 'Yellow', 'Orange',
        'Red', 'Purple', 'Pink', 'Cyan', 'White',
    ];

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
                <h3 class="r-card__title">TAK Ingestor</h3>
                <span class="r-card__subtitle" id="r-tak-summary">--</span>
            </div>
            <p class="r-card__hint">
                Forward the Pi itself plus observed Meshtastic and MeshCore nodes into TAK. Callsigns
                will update to the node short or long name as Meshpoint learns them.
            </p>
            <div class="config-stack">
                <div class="config-pane">
                    <div class="config-pane__label">Target</div>
                    <div class="config-pane__inputs">
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-enabled">Enabled</label>
                            <select class="r-select" id="r-tak-enabled">
                                <option value="true">On</option>
                                <option value="false">Off</option>
                            </select>
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-host">Host</label>
                            <input class="r-input" id="r-tak-host" placeholder="192.168.1.10" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-port">Port</label>
                            <input type="number" class="r-input r-input--mono r-input--narrow"
                                   id="r-tak-port" min="1" max="65535" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-protocol">Protocol</label>
                            <select class="r-select" id="r-tak-protocol">
                                <option value="udp">UDP</option>
                                <option value="tcp">TCP</option>
                            </select>
                        </div>
                    </div>
                </div>
                <div class="config-pane">
                    <div class="config-pane__label">Identity</div>
                    <div class="config-pane__inputs">
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-cot-type">CoT Type</label>
                            <input class="r-input r-input--mono" id="r-tak-cot-type" placeholder="a-f-G-U-C" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-team">Team</label>
                            <input class="r-input" id="r-tak-team" placeholder="Orange" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-role">Role</label>
                            <select class="r-select" id="r-tak-role"></select>
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-color">Color</label>
                            <select class="r-select" id="r-tak-color"></select>
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-stale">Stale Seconds</label>
                            <input type="number" class="r-input r-input--mono r-input--narrow"
                                   id="r-tak-stale" min="30" max="3600" />
                        </div>
                        <div class="r-field">
                            <label class="r-field__label" for="r-tak-use-names">Use Node Names</label>
                            <select class="r-select" id="r-tak-use-names">
                                <option value="true">Prefer short / long names</option>
                                <option value="false">Use stable IDs only</option>
                            </select>
                        </div>
                    </div>
                </div>
            </div>
            <div class="r-card__actions">
                <button class="r-btn r-btn--secondary" id="r-restart-tak">Restart Meshpoint</button>
                <button class="r-btn r-btn--primary" id="r-save-tak">Save TAK Settings</button>
            </div>
        `;
        this._root.querySelector('#r-tak-role').innerHTML = RadioTakCard.ROLE_OPTIONS
            .map((role) => `<option value="${this._api.escape(role)}">${this._api.escape(role)}</option>`)
            .join('');
        this._root.querySelector('#r-tak-color').innerHTML = RadioTakCard.COLOR_OPTIONS
            .map((color) => `<option value="${this._api.escape(color)}">${this._api.escape(color)}</option>`)
            .join('');
        this._wire();
    }

    render(config) {
        this._current = config.tak || {};
        this._root.querySelector('#r-tak-enabled').value = String(!!this._current.enabled);
        this._root.querySelector('#r-tak-host').value = this._current.host || '';
        this._root.querySelector('#r-tak-port').value = this._current.port || 8088;
        this._root.querySelector('#r-tak-protocol').value = this._current.protocol || 'udp';
        this._root.querySelector('#r-tak-cot-type').value = this._current.cot_type || 'a-f-G-U-C';
        this._root.querySelector('#r-tak-team').value = this._current.team || 'Orange';
        this._root.querySelector('#r-tak-role').value = this._current.role || 'RTO';
        this._root.querySelector('#r-tak-color').value = this._current.color || 'Orange';
        this._root.querySelector('#r-tak-stale').value = this._current.stale_seconds || 120;
        this._root.querySelector('#r-tak-use-names').value = String(
            this._current.use_meshtastic_names !== false
        );
        this._root.querySelector('#r-tak-summary').textContent = this._current.enabled
            ? `${this._current.protocol || 'udp'}://${this._current.host || '--'}:${this._current.port || '--'}`
            : 'disabled';
    }

    _wire() {
        this._root.querySelector('#r-save-tak').addEventListener(
            'click', async () => this._save(),
        );
        this._root.querySelector('#r-restart-tak').addEventListener(
            'click', async () => this._api.restartService('Restarting... reloading in 10 seconds.'),
        );
    }

    async _save() {
        const payload = {
            enabled: this._root.querySelector('#r-tak-enabled').value === 'true',
            host: this._root.querySelector('#r-tak-host').value.trim(),
            port: parseInt(this._root.querySelector('#r-tak-port').value || '8088', 10),
            protocol: this._root.querySelector('#r-tak-protocol').value,
            cot_type: this._root.querySelector('#r-tak-cot-type').value.trim(),
            team: this._root.querySelector('#r-tak-team').value.trim(),
            role: this._root.querySelector('#r-tak-role').value,
            color: this._root.querySelector('#r-tak-color').value,
            stale_seconds: parseInt(this._root.querySelector('#r-tak-stale').value || '120', 10),
            use_meshtastic_names: this._root.querySelector('#r-tak-use-names').value === 'true',
        };

        const result = await this._api.put('/api/config/tak', payload);
        if (result) {
            this._api.toast('TAK settings saved');
            await this._api.refresh();
        }
    }
}

window.RadioTakCard = RadioTakCard;
