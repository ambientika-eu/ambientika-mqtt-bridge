'use strict';

/**
 * ioBroker adapter for Ambientika ventilation units - powered by NeuraCell-X(R)
 *
 * Reads the topics of the Ambientika MQTT Bridge from an MQTT broker and shows
 * every unit as an ioBroker device:
 *
 *   <prefix>/<serial>/state         JSON status of the unit (retained)
 *   <prefix>/<serial>/availability  online / offline (retained)
 *   <prefix>/<serial>/diagnostics   JSON poll diagnostics (retained)
 *   <prefix>/<serial>/reset_state   status of the last filter reset (retained)
 *   <prefix>/<serial>/reset_info    JSON, what the filter reset does on this unit
 *   <prefix>/bridge/availability    online / offline of the bridge itself
 *   <prefix>/neuracell/state        NeuraCell-X radon and dew-point status
 *   <discovery>/+/+/config          Home Assistant discovery, used for the unit names
 *
 * Commands are sent back as <prefix>/<serial>/set/<attribute> with a plain value.
 *
 * NeuraCell-X(R) and PhaseCell-X(R) are registered trademarks. Patent pending.
 * https://github.com/ambientika-eu/ambientika-mqtt-bridge
 */

const utils = require('@iobroker/adapter-core');
const mqtt = require('mqtt');

const OPERATING_MODES = [
    'Smart', 'Auto', 'ManualHeatRecovery', 'Night', 'AwayHome', 'Surveillance',
    'TimedExpulsion', 'Expulsion', 'Intake', 'MasterSlaveFlow', 'SlaveMasterFlow', 'Off',
];
// Night and Turbo are steps a unit chooses itself. The bridge accepts them as a
// command (a scene restores what it saw) and sends Low / High instead.
const FAN_SPEEDS = ['Low', 'Medium', 'High', 'Night', 'Turbo'];
const HUMIDITY_LEVELS = ['Dry', 'Normal', 'Moist'];
const LIGHT_SENSOR_LEVELS = ['NotAvailable', 'Off', 'Low', 'Medium'];

/** Longest payload that is still looked at; the bridge stays far below this. */
const MAX_PAYLOAD = 64 * 1024;
/** Upper bound for units created from MQTT messages. */
const MAX_DEVICES = 250;
/** Longest text written into a state. */
const MAX_TEXT = 200;

function statesOf(list) {
    const out = {};
    for (const v of list) out[v] = v;
    return out;
}

/**
 * States of one unit, filled from <prefix>/<serial>/state. Key = key in the
 * bridge payload. cmd = attribute for <prefix>/<serial>/set/<cmd>, values = what
 * may be sent (writable states only).
 */
const DEVICE_STATES = {
    operating_mode: { name: 'Operating mode', type: 'string', role: 'level.mode', write: true, cmd: 'operating_mode', values: OPERATING_MODES },
    fan_speed: { name: 'Fan speed', type: 'string', role: 'level.mode.fan', write: true, cmd: 'fan_speed', values: FAN_SPEEDS },
    humidity_level: { name: 'Humidity level', type: 'string', role: 'level.mode', write: true, cmd: 'humidity_level', values: HUMIDITY_LEVELS },
    light_sensor_level: { name: 'Light sensor sensitivity', type: 'string', role: 'level.mode', write: true, cmd: 'light_sensor_level', values: LIGHT_SENSOR_LEVELS },
    temperature: { name: 'Temperature', type: 'number', role: 'value.temperature', unit: '°C' },
    humidity: { name: 'Humidity', type: 'number', role: 'value.humidity', unit: '%' },
    temperature_avg: { name: 'Temperature (averaged)', type: 'number', role: 'value.temperature', unit: '°C' },
    humidity_avg: { name: 'Humidity (averaged)', type: 'number', role: 'value.humidity', unit: '%' },
    humidity_raw: { name: 'Humidity raw', type: 'number', role: 'value.humidity', unit: '%' },
    air_quality: { name: 'Air quality', type: 'string', role: 'text' },
    humidity_alarm: { name: 'Humidity alarm', type: 'boolean', role: 'indicator.alarm' },
    night_alarm: { name: 'Night mode', type: 'boolean', role: 'indicator' },
    filters_status: { name: 'Filter status', type: 'string', role: 'text' },
    filters_status_raw: { name: 'Filter status raw', type: 'string', role: 'text' },
    filter_ack_since: { name: 'Filter acknowledged since', type: 'string', role: 'date' },
    filter_ack_until: { name: 'Filter acknowledgement valid until', type: 'string', role: 'date' },
    operating_mode_raw: { name: 'Mode raw', type: 'string', role: 'text' },
    last_operating_mode: { name: 'Last mode', type: 'string', role: 'text' },
    device_role: { name: 'Device role', type: 'string', role: 'text' },
    zone_index: { name: 'Zone', type: 'number', role: 'value' },
    operating_mode_num: { name: 'Mode (num)', type: 'number', role: 'value' },
    operating_mode_raw_num: { name: 'Mode raw (num)', type: 'number', role: 'value' },
    last_operating_mode_num: { name: 'Last mode (num)', type: 'number', role: 'value' },
    fan_speed_num: { name: 'Fan speed (num)', type: 'number', role: 'value' },
    humidity_level_num: { name: 'Humidity level (num)', type: 'number', role: 'value' },
    light_sensor_level_num: { name: 'Light sensor sensitivity (num)', type: 'number', role: 'value' },
    air_quality_num: { name: 'Air quality (num)', type: 'number', role: 'value' },
    filter_status_num: { name: 'Filter status (num)', type: 'number', role: 'value' },
    filter_status_raw_num: { name: 'Filter status raw (num)', type: 'number', role: 'value' },
};
for (const def of Object.values(DEVICE_STATES)) {
    if (def.values) def.states = statesOf(def.values);
}

/** Filled from <prefix>/<serial>/diagnostics, kept in the channel "diagnostics". */
const DIAGNOSTIC_STATES = {
    last_status: { name: 'Last status packet', type: 'string', role: 'date' },
    poll_failures_total: { name: 'Failed polls since the bridge started', type: 'number', role: 'value' },
    poll_failures_consecutive: { name: 'Failed polls in a row', type: 'number', role: 'value' },
    last_failure: { name: 'Last failed poll', type: 'string', role: 'date' },
    last_failure_reason: { name: 'Reason of the last failed poll', type: 'string', role: 'text' },
    last_outage_start: { name: 'Last outage: first failed poll', type: 'string', role: 'date' },
    last_outage_end: { name: 'Last outage: last failed poll', type: 'string', role: 'date' },
    last_outage_polls: { name: 'Last outage: failed polls', type: 'number', role: 'value' },
    bridge_started: { name: 'Bridge started (failed polls are counted from here)', type: 'string', role: 'date' },
};
/** From <prefix>/<serial>/reset_info, also in the channel "diagnostics". */
const RESET_INFO_STATES = {
    reset_note: { from: 'note', name: 'What the filter reset does on this unit', type: 'string', role: 'text', max: 1200 },
};

const EXTRA_DEVICE_STATES = {
    online: { name: 'Online', type: 'boolean', role: 'indicator.reachable' },
    reset_state: { name: 'Filter reset status', type: 'string', role: 'text' },
    reset_filter: { name: 'Reset filter', type: 'boolean', role: 'button', read: false, write: true, cmd: 'reset_filter' },
};

const NEURACELL_STATES = {
    radon_protection: { name: 'Radon protection active', type: 'boolean', role: 'indicator.alarm' },
    radon: { name: 'Radon', type: 'number', role: 'value', unit: 'Bq/m³' },
    radon_threshold: { name: 'Radon threshold', type: 'number', role: 'value', unit: 'Bq/m³' },
    radon_meter_connected: { name: 'Radon meter connected', type: 'boolean', role: 'indicator.reachable' },
    dewpoint_block: { name: 'Dew-point ventilation block', type: 'boolean', role: 'indicator' },
    dewpoint_controller_connected: { name: 'Dew-point controller connected', type: 'boolean', role: 'indicator.reachable' },
    dewpoint_fallback: { name: 'Fallback ventilation active', type: 'boolean', role: 'indicator' },
    indoor_dew_point: { name: 'Dew point indoor', type: 'number', role: 'value.temperature', unit: '°C' },
    outdoor_dew_point: { name: 'Dew point outdoor', type: 'number', role: 'value.temperature', unit: '°C' },
    override_active: { name: 'NeuraCell-X override active', type: 'boolean', role: 'indicator' },
};

const TOP_STATES = Object.assign({}, DEVICE_STATES, EXTRA_DEVICE_STATES);
const CHANNEL_STATES = Object.assign({}, DIAGNOSTIC_STATES, RESET_INFO_STATES);

/** Topic levels and ids that are never a unit. */
const RESERVED = new Set(['bridge', 'neuracell', 'info']);

function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
}

class AmbientikaAdapter extends utils.Adapter {
    constructor(options) {
        super({ ...options, name: 'ambientika' });
        this.client = null;
        this.devices = new Map(); // object id -> serial
        this.names = new Map(); // serial -> name from discovery
        this.created = new Set(); // object ids whose states exist
        this.pending = new Map(); // object id -> creation in progress
        this.unitOnline = new Map(); // object id -> availability the bridge reported
        this.bridgeOnline = null; // null = not reported
        this.connected = false;
        this.stopping = false;
        this.limitLogged = false;
        this.on('ready', this.onReady.bind(this));
        this.on('stateChange', this.onStateChange.bind(this));
        this.on('unload', this.onUnload.bind(this));
    }

    /** A prefix as typed: spaces and slashes around it do not count. */
    static cleanPrefix(value, fallback) {
        const p = String(value === undefined || value === null ? fallback : value).trim().replace(/^\/+|\/+$/g, '').trim();
        return p;
    }

    get prefix() {
        return AmbientikaAdapter.cleanPrefix(this.config.mqttPrefix, 'ambientika') || 'ambientika';
    }

    get discoveryPrefix() {
        return AmbientikaAdapter.cleanPrefix(this.config.discoveryPrefix, 'homeassistant');
    }

    /** Object id for a serial: no dots, no spaces, no characters ioBroker forbids. */
    idOf(serial) {
        return String(serial).replace(this.FORBIDDEN_CHARS, '_').replace(/[.\s]/g, '_');
    }

    async onReady() {
        await this.setStateAsync('info.connection', { val: false, ack: true });
        await this.setObjectNotExistsAsync('info.bridge', {
            type: 'state',
            common: { name: 'Ambientika MQTT Bridge online', type: 'boolean', role: 'indicator.reachable', read: true, write: false, def: false },
            native: {},
        });
        await this.setStateAsync('info.bridge', { val: false, ack: true });

        // Units of an earlier run: commands work before their first message, and
        // nothing is shown online before the broker has said so.
        try {
            for (const obj of await this.getDevicesAsync()) {
                const serial = obj && obj.native && obj.native.serial;
                if (!serial) continue;
                const id = obj._id.slice(this.namespace.length + 1);
                this.devices.set(id, String(serial));
                await this.setStateChangedAsync(`${id}.online`, { val: false, ack: true });
            }
        } catch (e) {
            this.log.debug(`Units of the earlier run not read: ${e.message}`);
        }
        this.subscribeStates('*');

        const c = this.config;
        if (/[+#]/.test(this.prefix) || /[+#]/.test(this.discoveryPrefix)) {
            this.log.error('The topic prefix and the discovery prefix must not contain + or # - please correct them in the adapter settings. Nothing is read until then.');
            return;
        }
        // Host as typed: a pasted "mqtt://" in front, a slash behind or brackets around an IPv6 address do not count.
        let host = String(c.mqttHost || '').trim().replace(/^[a-z]+:\/\//i, '').replace(/\/+$/, '');
        if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
        host = host || 'localhost';
        const typedPort = Number(c.mqttPort);
        const port = Number.isInteger(typedPort) && typedPort > 0 && typedPort < 65536 ? typedPort : 1883;
        const protocol = c.mqttTls ? 'mqtts' : 'mqtt';
        const url = `${protocol}://${host.includes(':') ? `[${host}]` : host}:${port}`;
        const opts = {
            protocol,
            host,
            port,
            clientId: `iobroker-ambientika-${this.instance}-${Math.random().toString(16).slice(2, 8)}`,
            clean: true,
            reconnectPeriod: 5000,
            connectTimeout: 15000,
        };
        if (c.mqttUser) {
            opts.username = String(c.mqttUser);
            opts.password = c.mqttPassword === undefined || c.mqttPassword === null ? '' : String(c.mqttPassword);
        }
        if (c.mqttTls && c.mqttTlsInsecure) opts.rejectUnauthorized = false;

        this.log.info(`Connecting to MQTT broker ${url}, topic prefix "${this.prefix}"`);
        let lastError = '';
        this.client = mqtt.connect(opts);
        this.client.on('connect', () => {
            lastError = '';
            this.connected = true;
            this.log.info('Connected to MQTT broker');
            this.setStateSafe('info.connection', true);
            const topics = [
                `${this.prefix}/+/state`,
                `${this.prefix}/+/availability`,
                `${this.prefix}/+/diagnostics`,
                `${this.prefix}/+/reset_state`,
                `${this.prefix}/+/reset_info`,
            ];
            if (this.discoveryPrefix) topics.push(`${this.discoveryPrefix}/+/+/config`);
            this.client.subscribe(topics, err => {
                if (err) this.log.error(`Subscribe failed: ${err.message}`);
                else this.log.debug(`Subscribed to ${topics.join(', ')}`);
            });
        });
        this.client.on('close', () => {
            if (!this.connected) return;
            this.connected = false;
            this.log.info('Connection to the MQTT broker lost - trying again every 5 seconds');
            this.onDisconnected().catch(() => {});
        });
        this.client.on('error', err => {
            // The client retries on its own; the same error is reported once.
            const text = String((err && err.message) || err);
            if (text !== lastError) this.log.warn(`MQTT broker ${url}: ${text}`);
            lastError = text;
        });
        this.client.on('message', (topic, msg) => {
            this.onMessage(topic, msg).catch(e => this.log.warn(`Message on ${topic} not processed: ${e.message}`));
        });
    }

    /** setState that never throws after the adapter was stopped. */
    setStateSafe(id, val) {
        if (this.stopping) return;
        try {
            const p = this.setStateChangedAsync(id, { val, ack: true });
            if (p && p.catch) p.catch(() => {});
        } catch (e) {
            // the database is gone during shutdown
        }
    }

    /** Without the broker nothing is known about bridge and units. */
    async onDisconnected() {
        if (this.stopping) return;
        this.bridgeOnline = null;
        this.unitOnline.clear();
        await this.setStateChangedAsync('info.connection', { val: false, ack: true });
        await this.setStateChangedAsync('info.bridge', { val: false, ack: true });
        for (const id of this.created) {
            if (this.devices.has(id)) await this.setStateChangedAsync(`${id}.online`, { val: false, ack: true });
        }
    }

    async onMessage(topic, msg) {
        if (this.stopping || !msg || msg.length > MAX_PAYLOAD) return;
        const text = msg.toString();
        const disc = this.discoveryPrefix;
        if (disc && topic.startsWith(`${disc}/`) && topic.endsWith('/config')) {
            return this.onDiscovery(text);
        }
        if (!topic.startsWith(`${this.prefix}/`)) return;
        const parts = topic.slice(this.prefix.length + 1).split('/');
        if (parts.length !== 2) return;
        const [serial, kind] = parts;
        if (!serial) return;

        if (serial === 'neuracell') return kind === 'state' ? this.onNeuraCell(text) : undefined;
        if (serial === 'bridge') return kind === 'availability' ? this.onBridgeAvailability(text) : undefined;

        if (kind === 'availability') {
            const word = text.trim().toLowerCase();
            if (word !== 'online' && word !== 'offline') return;
            if (!(await this.ensureDevice(serial))) return;
            const id = this.idOf(serial);
            this.unitOnline.set(id, word === 'online');
            await this.publishOnline(id);
            return;
        }
        if (kind === 'reset_state') {
            const word = text.trim();
            if (!word || word.length > 40) return;
            if (!(await this.ensureDevice(serial))) return;
            await this.setStateChangedAsync(`${this.idOf(serial)}.reset_state`, { val: word, ack: true });
            return;
        }
        if (kind !== 'state' && kind !== 'diagnostics' && kind !== 'reset_info') return;

        let payload;
        try {
            payload = JSON.parse(text);
        } catch (e) {
            this.log.debug(`${topic} is not JSON`);
            return;
        }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
        if (!(await this.ensureDevice(serial))) return;
        const id = this.idOf(serial);
        if (kind === 'state') {
            await this.writeStates(id, DEVICE_STATES, payload);
        } else if (kind === 'diagnostics') {
            await this.writeStates(`${id}.diagnostics`, DIAGNOSTIC_STATES, payload);
        } else {
            await this.writeStates(`${id}.diagnostics`, RESET_INFO_STATES, payload);
        }
    }

    /** Writes every field of the payload that has a state; the others are left alone. */
    async writeStates(base, defs, payload) {
        for (const [key, def] of Object.entries(defs)) {
            const from = def.from || key;
            if (!has(payload, from)) continue;
            const val = this.convert(payload[from], def.type, def.max);
            if (val === undefined) continue;
            await this.setStateChangedAsync(`${base}.${key}`, { val, ack: true });
        }
    }

    async onBridgeAvailability(text) {
        const word = text.trim().toLowerCase();
        if (word !== 'online' && word !== 'offline') return;
        const online = word === 'online';
        if (this.bridgeOnline === online) return;
        const first = this.bridgeOnline === null;
        this.bridgeOnline = online;
        if (!online) this.log.warn('The Ambientika MQTT Bridge reports offline - the values of the units are not updated until it is back');
        else if (!first) this.log.info('The Ambientika MQTT Bridge is online again');
        await this.setStateChangedAsync('info.bridge', { val: online, ack: true });
        for (const id of this.unitOnline.keys()) await this.publishOnline(id);
    }

    /** A unit is online when the bridge says so and the bridge itself is running. */
    async publishOnline(id) {
        if (!this.unitOnline.has(id) || !this.created.has(id)) return;
        const online = this.connected && this.unitOnline.get(id) === true && this.bridgeOnline !== false;
        await this.setStateChangedAsync(`${id}.online`, { val: online, ack: true });
    }

    async onDiscovery(text) {
        let cfg;
        try {
            cfg = JSON.parse(text);
        } catch (e) {
            return;
        }
        if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) return;
        const dev = cfg.device || cfg.dev;
        if (!dev || typeof dev !== 'object') return;
        const ids = dev.identifiers || dev.ids;
        const name = typeof dev.name === 'string' ? dev.name.trim().slice(0, 120) : '';
        if (!Array.isArray(ids) || typeof ids[0] !== 'string' || !ids[0] || !name) return;
        // Only entities of this bridge: their topics start with its prefix.
        const refs = [cfg.state_topic, cfg.stat_t, cfg.command_topic, cfg.cmd_t, cfg.availability_topic, cfg.avty_t];
        if (!refs.some(t => typeof t === 'string' && t.startsWith(`${this.prefix}/`))) return;
        const serial = ids[0];
        if (this.names.get(serial) === name) return;
        if (!this.names.has(serial) && this.names.size >= 4 * MAX_DEVICES) return;
        this.names.set(serial, name);
        const id = this.idOf(serial);
        if (this.pending.has(id)) await this.pending.get(id).catch(() => {});
        if (this.created.has(id) && this.devices.get(id) === serial) await this.extendObjectAsync(id, { common: { name } });
    }

    async onNeuraCell(text) {
        let payload;
        try {
            payload = JSON.parse(text);
        } catch (e) {
            return;
        }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
        if (!this.created.has('neuracell')) {
            if (!this.pending.has('neuracell')) {
                this.pending.set('neuracell', (async () => {
                    await this.setObjectNotExistsAsync('neuracell', { type: 'device', common: { name: 'NeuraCell-X® radon and dew-point protection' }, native: {} });
                    for (const [key, def] of Object.entries(NEURACELL_STATES)) {
                        await this.createState(`neuracell.${key}`, def);
                    }
                    this.created.add('neuracell');
                })().finally(() => this.pending.delete('neuracell')));
            }
            await this.pending.get('neuracell');
        }
        await this.writeStates('neuracell', NEURACELL_STATES, payload);
    }

    /** Value in the type of the state; undefined = leave the state as it is. */
    convert(v, type, max) {
        if (v === null) return null;
        if (type === 'number') {
            if (typeof v === 'number') return Number.isFinite(v) ? v : undefined;
            if (typeof v !== 'string' || !v.trim()) return undefined;
            const n = Number(v);
            return Number.isFinite(n) ? n : undefined;
        }
        if (type === 'boolean') {
            if (typeof v === 'boolean') return v;
            if (typeof v === 'number') return v !== 0;
            if (typeof v !== 'string') return undefined;
            const s = v.trim().toLowerCase();
            if (s === 'true' || s === 'on' || s === '1') return true;
            if (s === 'false' || s === 'off' || s === '0') return false;
            return undefined;
        }
        if (typeof v === 'object') return undefined;
        return String(v).slice(0, max || MAX_TEXT);
    }

    async createState(id, def) {
        await this.setObjectNotExistsAsync(id, {
            type: 'state',
            common: {
                name: def.name,
                type: def.type,
                role: def.role,
                unit: def.unit,
                read: def.read !== false,
                write: !!def.write,
                states: def.states,
            },
            native: def.cmd ? { command: def.cmd } : {},
        });
    }

    /** Creates the unit and its states once; false when the unit is not accepted. */
    ensureDevice(serial) {
        const id = this.idOf(serial);
        if (this.created.has(id)) return Promise.resolve(this.devices.get(id) === String(serial));
        if (!String(serial).trim() || String(serial).length > 64 || RESERVED.has(id)) return Promise.resolve(false);
        if (!this.pending.has(id)) {
            if (this.devices.has(id) && this.devices.get(id) !== String(serial)) return Promise.resolve(false);
            if (!this.devices.has(id) && this.devices.size >= MAX_DEVICES) {
                if (!this.limitLogged) this.log.warn(`More than ${MAX_DEVICES} units on the MQTT broker - further units are ignored`);
                this.limitLogged = true;
                return Promise.resolve(false);
            }
            this.pending.set(id, this.createDevice(id, String(serial)).then(() => true).finally(() => this.pending.delete(id)));
        }
        return this.pending.get(id).then(() => this.devices.get(id) === String(serial));
    }

    async createDevice(id, serial) {
        const known = this.devices.has(id);
        this.devices.set(id, serial);
        const name = this.names.get(serial);
        await this.setObjectNotExistsAsync(id, {
            type: 'device',
            common: { name: name || `Ambientika ${serial}`, statusStates: { onlineId: `${this.namespace}.${id}.online` } },
            native: { serial },
        });
        for (const [key, def] of Object.entries(TOP_STATES)) {
            await this.createState(`${id}.${key}`, def);
        }
        await this.setObjectNotExistsAsync(`${id}.diagnostics`, { type: 'channel', common: { name: 'Diagnostics' }, native: {} });
        for (const [key, def] of Object.entries(CHANNEL_STATES)) {
            await this.createState(`${id}.diagnostics.${key}`, def);
        }
        if (known) {
            // Unit of an earlier version: bring the selectable values up to date.
            for (const [key, def] of Object.entries(DEVICE_STATES)) {
                if (def.states) await this.extendObjectAsync(`${id}.${key}`, { common: { states: def.states } });
            }
            await this.extendObjectAsync(id, { common: { statusStates: { onlineId: `${this.namespace}.${id}.online` } } });
        }
        if (name) await this.extendObjectAsync(id, { common: { name } });
        this.created.add(id);
        if (!known) this.log.info(`Unit ${serial}${name ? ` (${name})` : ''} added`);
    }

    async onStateChange(fullId, state) {
        if (!state || state.ack || this.stopping) return;
        const local = fullId.slice(this.namespace.length + 1);
        const dot = local.indexOf('.');
        if (dot < 0) return;
        const id = local.slice(0, dot);
        const key = local.slice(dot + 1);
        const def = has(TOP_STATES, key) ? TOP_STATES[key] : null;
        if (!def || !def.cmd || RESERVED.has(id) || !this.devices.has(id)) return;
        const serial = this.devices.get(id);
        let value = state.val;
        if (key === 'reset_filter') {
            if (value !== true && value !== 1 && value !== 'true') return;
            value = 'true';
        } else if (typeof value !== 'string' || !def.values.includes(value)) {
            this.log.warn(`${local}: ${JSON.stringify(value)} cannot be set (allowed: ${def.values.join(', ')})`);
            return;
        }
        if (!this.client || !this.connected) {
            this.log.warn(`${local}: not connected to the MQTT broker, nothing sent`);
            return;
        }
        const topic = `${this.prefix}/${serial}/set/${def.cmd}`;
        this.client.publish(topic, value, { qos: 1 }, err => {
            if (err && !this.stopping) this.log.warn(`${topic} not sent: ${err.message}`);
        });
        this.log.debug(`Sent ${topic} = ${value}`);
        if (key === 'reset_filter') await this.setStateAsync(local, { val: false, ack: true });
    }

    onUnload(callback) {
        this.stopping = true;
        try {
            if (this.client) {
                this.client.removeAllListeners('message');
                this.client.end(true);
            }
        } catch (e) {
            // nothing to do
        }
        callback();
    }
}

if (require.main !== module) {
    module.exports = options => new AmbientikaAdapter(options);
} else {
    new AmbientikaAdapter();
}
