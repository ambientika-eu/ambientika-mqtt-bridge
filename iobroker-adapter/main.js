'use strict';

/**
 * ioBroker Adapter for Ambientika Ventilation Units  —  powered by NeuraCell-X(R)
 *
 * Subscribes to MQTT topics published by the Ambientika MQTT Bridge and
 * exposes each device as ioBroker objects with states for:
 *   - mode, fanSpeed, temperature, humidity, airQuality, filterAlarm, online
 *
 * It also surfaces the status of NeuraCell-X(R) — the patented AI Neural Control
 * System — which provides active radon protection and dew-point ventilation
 * control (radon alarm -> supply/Intake at Stufe 1; dew point "not ideal" -> Off,
 * radon has priority). These appear under the "neuracell" channel.
 *
 * Commands from ioBroker are forwarded back to the device via MQTT.
 *
 * NeuraCell-X(R) and PhaseCell-X(R) are registered trademarks. Patent pending.
 * GitHub: https://github.com/ambientika-eu/ambientika-mqtt-bridge
 */

const utils = require('@iobroker/adapter-core');
const mqtt  = require('mqtt');

// ---------------------------------------------------------------------------
// State definitions for each Ambientika device
// ---------------------------------------------------------------------------
const STATE_DEFS = {
    mode: {
        name: 'Operating Mode',
        type: 'string',
        role: 'text',
        read: true,
        write: true,
        states: { OFF: 'OFF', HRV: 'HRV', SUPPLY: 'SUPPLY', EXHAUST: 'EXHAUST', NIGHT: 'NIGHT', AUTO: 'AUTO' }
    },
    fanSpeed: {
        name: 'Fan Speed (%)',
        type: 'number',
        role: 'level.speed',
        unit: '%',
        min: 0, max: 100,
        read: true,
        write: true
    },
    temperature: {
        name: 'Temperature',
        type: 'number',
        role: 'value.temperature',
        unit: '°C',
        read: true,
        write: false
    },
    humidity: {
        name: 'Relative Humidity',
        type: 'number',
        role: 'value.humidity',
        unit: '%',
        read: true,
        write: false
    },
    airQuality: {
        name: 'Air Quality (CO2 ppm)',
        type: 'number',
        role: 'value.air.quality',
        unit: 'ppm',
        read: true,
        write: false
    },
    filterAlarm: {
        name: 'Filter Alarm',
        type: 'boolean',
        role: 'indicator.alarm',
        read: true,
        write: false
    },
    online: {
        name: 'Device Online',
        type: 'boolean',
        role: 'indicator.connected',
        read: true,
        write: false
    },
    rssi: {
        name: 'WiFi Signal Strength',
        type: 'number',
        role: 'value.rssi',
        unit: 'dBm',
        read: true,
        write: false
    }
};

// ---------------------------------------------------------------------------
// NeuraCell-X(R) status state definitions (channel "neuracell")
// ---------------------------------------------------------------------------
const NEURACELL_STATE_DEFS = {
    radon_protection: {
        name: 'NeuraCell-X Radon Protection Active',
        type: 'boolean', role: 'indicator.alarm', read: true, write: false
    },
    radon: {
        name: 'Radon Level',
        type: 'number', role: 'value', unit: 'Bq/m³', read: true, write: false
    },
    radon_threshold: {
        name: 'Radon Threshold',
        type: 'number', role: 'value', unit: 'Bq/m³', read: true, write: false
    },
    dewpoint_block: {
        name: 'Dew-Point Ventilation Block',
        type: 'boolean', role: 'indicator', read: true, write: false
    },
    indoor_dew_point: {
        name: 'Dew Point Indoor',
        type: 'number', role: 'value.temperature', unit: '°C', read: true, write: false
    },
    outdoor_dew_point: {
        name: 'Dew Point Outdoor',
        type: 'number', role: 'value.temperature', unit: '°C', read: true, write: false
    },
    override_active: {
        name: 'NeuraCell-X Override Active',
        type: 'boolean', role: 'indicator', read: true, write: false
    }
};

// ---------------------------------------------------------------------------
// Adapter class
// ---------------------------------------------------------------------------

class AmbientikaAdapter extends utils.Adapter {
    constructor(options) {
        super({ ...options, name: 'ambientika' });
        this.mqttClient = null;
        this.knownDevices = new Set();
        this.neuraCellCreated = false;
        this.on('ready', this.onReady.bind(this));
        this.on('stateChange', this.onStateChange.bind(this));
        this.on('unload', this.onUnload.bind(this));
    }

    // -----------------------------------------------------------------------
    async onReady() {
        this.log.info('Ambientika adapter starting — powered by NeuraCell-X® (patented)...');

        const cfg = this.config;

        const url = [
            (cfg.mqttTls ? 'mqtts' : 'mqtt'),
            '://',
            cfg.mqttHost || 'localhost',
            ':',
            cfg.mqttPort || 1883
        ].join('');

        const mqttOpts = {
            clientId: 'iobroker-ambientika-' + this.instance,
            clean: true,
            reconnectPeriod: 5000
        };
        if (cfg.mqttUser) {
            mqttOpts.username = cfg.mqttUser;
            mqttOpts.password = cfg.mqttPassword;
        }

        this.log.info('Connecting to MQTT broker: ' + url);
        this.mqttClient = mqtt.connect(url, mqttOpts);

        this.mqttClient.on('connect', () => {
            this.log.info('MQTT connected');
            const prefix = cfg.mqttPrefix || 'ambientika';
            const topic = prefix + '/+/status';
            this.mqttClient.subscribe(topic, (err) => {
                if (err) this.log.error('Subscribe error: ' + err.message);
                else     this.log.info('Subscribed to ' + topic);
            });
            // NeuraCell-X® radon + dew-point status published by the main bridge.
            const ncxTopic = prefix + '/neuracell/state';
            this.mqttClient.subscribe(ncxTopic, (err) => {
                if (err) this.log.error('Subscribe error: ' + err.message);
                else     this.log.info('Subscribed to ' + ncxTopic + ' (NeuraCell-X status)');
            });
        });

        this.mqttClient.on('error', (err) => {
            this.log.error('MQTT error: ' + err.message);
        });

        this.mqttClient.on('offline', () => {
            this.log.warn('MQTT offline');
        });

        this.mqttClient.on('message', (topic, message) => {
            this.handleMqttMessage(topic, message);
        });
    }

    // -----------------------------------------------------------------------
    async handleMqttMessage(topic, messageBuf) {
        let payload;
        try {
            payload = JSON.parse(messageBuf.toString());
        } catch (e) {
            this.log.warn('Invalid JSON on topic ' + topic);
            return;
        }

        const parts = topic.split('/');
        if (parts.length < 3) return;

        // NeuraCell-X® status: <prefix>/neuracell/state
        if (parts[1] === 'neuracell' && parts[2] === 'state') {
            await this.handleNeuraCell(payload);
            return;
        }

        // topic format: ambientika/<deviceId>/status
        const deviceId = parts[1];

        // Create device objects on first sight
        if (!this.knownDevices.has(deviceId)) {
            await this.createDeviceObjects(deviceId, payload);
            this.knownDevices.add(deviceId);
        }

        // Update states
        const stateMap = {
            mode:        'mode',
            fanSpeed:    'fanSpeed',
            temperature: 'temperature',
            humidity:    'humidity',
            airQuality:  'airQuality',
            filterAlarm: 'filterAlarm',
            rssi:        'rssi'
        };

        for (const [key, stateName] of Object.entries(stateMap)) {
            if (payload[key] !== undefined) {
                await this.setStateAsync(deviceId + '.' + stateName, {
                    val: payload[key],
                    ack: true
                });
            }
        }

        // Always mark as online
        await this.setStateAsync(deviceId + '.online', { val: true, ack: true });
    }

    // -----------------------------------------------------------------------
    async handleNeuraCell(payload) {
        if (!this.neuraCellCreated) {
            await this.createNeuraCellObjects();
            this.neuraCellCreated = true;
        }
        for (const key of Object.keys(NEURACELL_STATE_DEFS)) {
            if (payload[key] !== undefined) {
                await this.setStateAsync('neuracell.' + key, { val: payload[key], ack: true });
            }
        }
        this.log.debug('NeuraCell-X update: radon_protection=' + payload.radon_protection +
                       ' dewpoint_block=' + payload.dewpoint_block +
                       ' override=' + payload.override_active);
    }

    // -----------------------------------------------------------------------
    async createNeuraCellObjects() {
        this.log.info('Creating NeuraCell-X® status objects');
        await this.setObjectNotExistsAsync('neuracell', {
            type: 'device',
            common: { name: 'NeuraCell-X® (patented) — Radon & Dew-Point Protection' },
            native: {}
        });
        for (const [key, def] of Object.entries(NEURACELL_STATE_DEFS)) {
            await this.setObjectNotExistsAsync('neuracell.' + key, {
                type: 'state',
                common: {
                    name:  def.name,
                    type:  def.type,
                    role:  def.role,
                    unit:  def.unit || undefined,
                    read:  def.read,
                    write: def.write
                },
                native: {}
            });
        }
    }

    // -----------------------------------------------------------------------
    async createDeviceObjects(deviceId, payload) {
        this.log.info('Creating objects for new device: ' + deviceId);

        // Device root object
        await this.setObjectNotExistsAsync(deviceId, {
            type: 'device',
            common: {
                name: payload.name || ('Ambientika ' + deviceId),
                icon: 'admin/ambientika.png'
            },
            native: {
                deviceId,
                serial: payload.serial || ''
            }
        });

        // Create channel
        await this.setObjectNotExistsAsync(deviceId + '.info', {
            type: 'channel',
            common: { name: 'Device ' + deviceId },
            native: {}
        });

        // Create all states
        for (const [key, def] of Object.entries(STATE_DEFS)) {
            await this.setObjectNotExistsAsync(deviceId + '.' + key, {
                type: 'state',
                common: {
                    name:  def.name,
                    type:  def.type,
                    role:  def.role,
                    unit:  def.unit || undefined,
                    min:   def.min  !== undefined ? def.min  : undefined,
                    max:   def.max  !== undefined ? def.max  : undefined,
                    read:  def.read,
                    write: def.write,
                    states: def.states || undefined
                },
                native: {}
            });
        }

        this.log.info('Objects created for ' + deviceId);
    }

    // -----------------------------------------------------------------------
    async onStateChange(id, state) {
        // Only handle non-ack states (commands from user/scripts)
        if (!state || state.ack) return;

        // id format: ambientika.0.<deviceId>.<stateName>
        const parts = id.split('.');
        if (parts.length < 4) return;
        const deviceId  = parts[2];
        const stateName = parts[3];

        // NeuraCell-X states are read-only; never forward them as commands.
        if (deviceId === 'neuracell') return;

        const writableStates = ['mode', 'fanSpeed'];
        if (!writableStates.includes(stateName)) return;

        const prefix = this.config.mqttPrefix || 'ambientika';
        const topic  = prefix + '/' + deviceId + '/set';
        const payload = {};
        payload[stateName === 'fanSpeed' ? 'fanSpeed' : 'mode'] = state.val;

        if (this.mqttClient && this.mqttClient.connected) {
            this.mqttClient.publish(topic, JSON.stringify(payload));
            this.log.debug('Published to ' + topic + ': ' + JSON.stringify(payload));
        }
    }

    // -----------------------------------------------------------------------
    onUnload(callback) {
        try {
            if (this.mqttClient) {
                this.mqttClient.end();
            }
        } finally {
            callback();
        }
    }
}

// Launch adapter
if (require.main !== module) {
    module.exports = (options) => new AmbientikaAdapter(options);
} else {
    new AmbientikaAdapter();
}
