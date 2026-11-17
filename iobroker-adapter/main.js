'use strict';

/**
 * ioBroker adapter for Ambientika ventilation units - powered by NeuraCell-X(R)
 *
 * Reads the topics of the Ambientika MQTT Bridge from an MQTT broker and shows
 * every unit as an ioBroker device:
 *
 *   <prefix>/<serial>/state         JSON status of the unit (retained)
 *   <prefix>/<serial>/availability  online / offline (retained)
 *   <prefix>/neuracell/state        NeuraCell-X radon and dew-point status
 *   <prefix>/bridge/availability    online / offline of the bridge itself
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
const FAN_SPEEDS = ['Low', 'Medium', 'High'];
const HUMIDITY_LEVELS = ['Dry', 'Normal', 'Moist'];
const LIGHT_SENSOR_LEVELS = ['Off', 'Low', 'Medium'];

function statesOf(list) {
    const out = {};
    for (const v of list) out[v] = v;
    return out;
}

/**
 * States of one unit. Key = key in the bridge state payload.
 * cmd = attribute name for <prefix>/<serial>/set/<cmd> (writable states only).
 */
const DEVICE_STATES = {
    operating_mode: { name: 'Operating mode', type: 'string', role: 'level.mode', write: true, cmd: 'operating_mode', states: statesOf(OPERATING_MODES), values: OPERATING_MODES },
    fan_speed: { name: 'Fan speed', type: 'string', role: 'level.mode.fan', write: true, cmd: 'fan_speed', states: Object.assign(statesOf(FAN_SPEEDS), { Night: 'Night', Turbo: 'Turbo' }), values: FAN_SPEEDS },
    humidity_level: { name: 'Humidity level', type: 'string', role: 'level.mode', write: true, cmd: 'humidity_level', states: statesOf(HUMIDITY_LEVELS), values: HUMIDITY_LEVELS },
    light_sensor_level: { name: 'Light sensor sensitivity', type: 'string', role: 'level.mode', write: true, cmd: 'light_sensor_level', states: Object.assign({ NotAvailable: 'NotAvailable' }, statesOf(LIGHT_SENSOR_LEVELS)), values: LIGHT_SENSOR_LEVELS },
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
    operating_mode_num: { name: 'Operating mode (num)', type: 'number', role: 'value' },
    fan_speed_num: { name: 'Fan speed (num)', type: 'number', role: 'value' },
    air_quality_num: { name: 'Air quality (num)', type: 'number', role: 'value' },
    filter_status_num: { name: 'Filter status (num)', type: 'number', role: 'value' },
};

const EXTRA_DEVICE_STATES = {
    online: { name: 'Online', type: 'boolean', role: 'indicator.reachable' },
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

/** Object IDs may not contain these characters. */
function safeId(s) {
    return String(s).replace(/[\][*,;'"`<>\\?.\s]/g, '_');
}

class AmbientikaAdapter extends utils.Adapter {
    constructor(options) {
        super({ ...options, name: 'ambientika' });
        this.client = null;
        this.devices = new Map(); // id -> serial
        this.names = new Map(); // serial -> name from discovery
        this.created = new Set(); // object ids already created
        this.pending = new Map(); // object id -> creation in progress
        this.on('ready', this.onReady.bind(this));
        this.on('stateChange', this.onStateChange.bind(this));
        this.on('unload', this.onUnload.bind(this));
    }

    get prefix() {
        return (this.config.mqttPrefix || 'ambientika').replace(/\/+$/, '');
    }

    async onReady() {
        await this.setStateAsync('info.connection', { val: false, ack: true });
        const c = this.config;
        const url = `${c.mqttTls ? 'mqtts' : 'mqtt'}://${c.mqttHost || 'localhost'}:${Number(c.mqttPort) || 1883}`;
        const opts = { clientId: `iobroker-ambientika-${this.instance}-${Math.random().toString(16).slice(2, 8)}`, clean: true, reconnectPeriod: 5000 };
        if (c.mqttUser) {
            opts.username = c.mqttUser;
            opts.password = c.mqttPassword;
        }
        if (c.mqttTls && c.mqttTlsInsecure) opts.rejectUnauthorized = false;

        this.log.info(`Connecting to MQTT broker ${url}, topic prefix "${this.prefix}"`);
        this.client = mqtt.connect(url, opts);
        this.client.on('connect', () => {
            this.log.info('Connected to MQTT broker');
            this.setState('info.connection', { val: true, ack: true });
            const topics = [
                `${this.prefix}/+/state`,
                `${this.prefix}/+/availability`,
                `${this.prefix}/neuracell/state`,
            ];
            const disc = (c.discoveryPrefix || 'homeassistant').replace(/\/+$/, '');
            if (disc) topics.push(`${disc}/+/+/config`);
            this.client.subscribe(topics, err => {
                if (err) this.log.error(`Subscribe failed: ${err.message}`);
                else this.log.info(`Subscribed to ${topics.join(', ')}`);
            });
        });
        this.client.on('close', () => this.setState('info.connection', { val: false, ack: true }));
        this.client.on('error', err => this.log.warn(`MQTT error: ${err.message}`));
        this.client.on('message', (topic, msg) => {
            this.onMessage(topic, msg.toString()).catch(e => this.log.warn(`Message on ${topic} not processed: ${e.message}`));
        });
        this.subscribeStates('*');
    }

    async onMessage(topic, text) {
        const disc = (this.config.discoveryPrefix || 'homeassistant').replace(/\/+$/, '');
        if (disc && topic.startsWith(`${disc}/`) && topic.endsWith('/config')) {
            return this.onDiscovery(text);
        }
        if (!topic.startsWith(`${this.prefix}/`)) return;
        const parts = topic.slice(this.prefix.length + 1).split('/');
        if (parts.length !== 2) return;
        const [serial, kind] = parts;

        if (serial === 'neuracell' && kind === 'state') return this.onNeuraCell(text);
        if (serial === 'bridge' || serial === 'neuracell') return;

        if (kind === 'availability') {
            await this.ensureDevice(serial);
            await this.setStateAsync(`${safeId(serial)}.online`, { val: text.trim() === 'online', ack: true });
            return;
        }
        if (kind !== 'state') return;
        let payload;
        try {
            payload = JSON.parse(text);
        } catch (e) {
            this.log.debug(`State of ${serial} is not JSON`);
            return;
        }
        if (!payload || typeof payload !== 'object') return;
        await this.ensureDevice(serial);
        const id = safeId(serial);
        for (const [key, def] of Object.entries(DEVICE_STATES)) {
            if (!(key in payload)) continue;
            const val = this.convert(payload[key], def.type);
            if (val === undefined) continue;
            await this.setStateChangedAsync(`${id}.${key}`, { val, ack: true });
        }
    }

    onDiscovery(text) {
        let cfg;
        try {
            cfg = JSON.parse(text);
        } catch (e) {
            return;
        }
        const dev = cfg && (cfg.device || cfg.dev);
        if (!dev || !Array.isArray(dev.identifiers || dev.ids) || !dev.name) return;
        const stateTopic = String(cfg.state_topic || cfg.stat_t || '');
        if (!stateTopic.startsWith(`${this.prefix}/`)) return;
        const serial = (dev.identifiers || dev.ids)[0];
        if (this.names.get(serial) === dev.name) return;
        this.names.set(serial, dev.name);
        if (this.created.has(safeId(serial))) {
            return this.extendObjectAsync(safeId(serial), { common: { name: dev.name } });
        }
    }

    async onNeuraCell(text) {
        let payload;
        try {
            payload = JSON.parse(text);
        } catch (e) {
            return;
        }
        if (!this.created.has('neuracell')) {
            await this.setObjectNotExistsAsync('neuracell', { type: 'device', common: { name: 'NeuraCell-X® radon and dew-point protection' }, native: {} });
            for (const [key, def] of Object.entries(NEURACELL_STATES)) {
                await this.createState(`neuracell.${key}`, def);
            }
            this.created.add('neuracell');
        }
        for (const [key, def] of Object.entries(NEURACELL_STATES)) {
            if (!(key in payload)) continue;
            const val = this.convert(payload[key], def.type);
            if (val === undefined) continue;
            await this.setStateChangedAsync(`neuracell.${key}`, { val, ack: true });
        }
    }

    convert(v, type) {
        if (v === null) return null;
        if (type === 'number') {
            const n = Number(v);
            return Number.isFinite(n) ? n : undefined;
        }
        if (type === 'boolean') {
            if (typeof v === 'boolean') return v;
            if (typeof v === 'number') return v !== 0;
            const s = String(v).toLowerCase();
            return s === 'true' || s === 'on' || s === '1';
        }
        return typeof v === 'object' ? JSON.stringify(v) : String(v);
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

    ensureDevice(serial) {
        const id = safeId(serial);
        if (this.created.has(id)) return Promise.resolve();
        if (!this.pending.has(id)) {
            this.pending.set(id, this.createDevice(id, serial).finally(() => this.pending.delete(id)));
        }
        return this.pending.get(id);
    }

    async createDevice(id, serial) {
        this.devices.set(id, serial);
        await this.setObjectNotExistsAsync(id, {
            type: 'device',
            common: { name: this.names.get(serial) || `Ambientika ${serial}` },
            native: { serial },
        });
        if (this.names.has(serial)) await this.extendObjectAsync(id, { common: { name: this.names.get(serial) } });
        for (const [key, def] of Object.entries(Object.assign({}, DEVICE_STATES, EXTRA_DEVICE_STATES))) {
            await this.createState(`${id}.${key}`, def);
        }
        this.created.add(id);
        this.log.info(`Unit ${serial} added`);
    }

    async onStateChange(fullId, state) {
        if (!state || state.ack) return;
        const local = fullId.slice(this.namespace.length + 1);
        const [id, key] = local.split('.');
        const def = (DEVICE_STATES[key] || EXTRA_DEVICE_STATES[key]);
        if (!def || !def.cmd || !this.devices.has(id)) return;
        const serial = this.devices.get(id);
        let value = state.val;
        if (key === 'reset_filter') {
            if (!value) return;
            value = 'true';
        } else if (def.values && !def.values.includes(String(value))) {
            this.log.warn(`${key}: "${value}" cannot be set (allowed: ${def.values.join(', ')})`);
            return;
        }
        if (!this.client || !this.client.connected) {
            this.log.warn(`Not connected to the MQTT broker, ${key} not sent`);
            return;
        }
        const topic = `${this.prefix}/${serial}/set/${def.cmd}`;
        this.client.publish(topic, String(value));
        this.log.info(`Sent ${topic} = ${value}`);
        if (key === 'reset_filter') await this.setStateAsync(local, { val: false, ack: true });
    }

    onUnload(callback) {
        try {
            if (this.client) this.client.end(true);
            callback();
        } catch (e) {
            callback();
        }
    }
}

if (require.main !== module) {
    module.exports = options => new AmbientikaAdapter(options);
} else {
    new AmbientikaAdapter();
}
