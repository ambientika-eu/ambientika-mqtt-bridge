const path = require('path');
const net = require('net');
const { tests } = require('@iobroker/testing');

// Starts a real js-controller with this adapter and a local MQTT broker (aedes)
// that plays the part of the Ambientika MQTT Bridge.
const SERIAL = 'A0B1C2D3E401';
const PREFIX = 'ambientika';

const STATE = {
    operating_mode: 'Smart', operating_mode_raw: 'Smart', fan_speed: 'Night',
    humidity_level: 'Normal', light_sensor_level: 'Low', temperature: 21, humidity: 55,
    temperature_avg: 20.8, humidity_avg: 54.2, humidity_raw: 55, air_quality: 'VeryGood',
    humidity_alarm: false, filters_status: 'Good', filters_status_raw: 'Good',
    filter_ack_since: null, filter_ack_until: null, night_alarm: true, device_role: 'Master',
    last_operating_mode: 'Smart', zone_index: 0, operating_mode_num: 0, fan_speed_num: 4,
    air_quality_num: 0, filter_status_num: 0,
};

const DISCOVERY = {
    name: 'Temperature', state_topic: `${PREFIX}/${SERIAL}/state`,
    device: { identifiers: [SERIAL], name: 'Flur', manufacturer: 'Südwind GmbH' },
};

function wait(ms) {
    return new Promise(r => setTimeout(r, ms));
}

async function waitFor(fn, timeoutMs = 20000) {
    const end = Date.now() + timeoutMs;
    for (;;) {
        const v = await fn();
        if (v) return v;
        if (Date.now() > end) throw new Error('timeout');
        await wait(250);
    }
}

tests.integration(path.join(__dirname, '..'), {
    allowedExitCodes: [11],
    defineAdditionalTests({ suite }) {
        suite('Ambientika MQTT Bridge topics', getHarness => {
            let harness;
            let aedes;
            let server;
            let port;
            const received = [];

            before(async function () {
                this.timeout(30000);
                aedes = require('aedes')();
                server = net.createServer(aedes.handle);
                await new Promise(r => server.listen(0, '127.0.0.1', r));
                port = server.address().port;
                aedes.on('publish', (packet, client) => {
                    if (client) received.push({ topic: packet.topic, payload: packet.payload.toString() });
                });
                const pub = (topic, payload) => new Promise(r => aedes.publish({ topic, payload: Buffer.from(payload), retain: true, qos: 0, cmd: 'publish' }, r));
                await pub(`homeassistant/sensor/ambientika_${SERIAL}_temperature/config`, JSON.stringify(DISCOVERY));
                await pub(`${PREFIX}/${SERIAL}/state`, JSON.stringify(STATE));
                await pub(`${PREFIX}/${SERIAL}/availability`, 'online');
                await pub(`${PREFIX}/neuracell/state`, JSON.stringify({ radon_protection: false, radon: 120, radon_threshold: 300, dewpoint_block: false, override_active: false }));
            });

            after(async function () {
                this.timeout(10000);
                await new Promise(r => aedes.close(r));
                server.close();
            });

            it('creates the unit with its name and states and sends commands', async function () {
                this.timeout(90000);
                harness = getHarness();
                await harness.changeAdapterConfig('ambientika', { native: { mqttHost: '127.0.0.1', mqttPort: port, mqttPrefix: PREFIX, discoveryPrefix: 'homeassistant' } });
                await harness.startAdapterAndWait();

                const ns = 'ambientika.0';
                const conn = await waitFor(async () => {
                    const s = await harness.states.getStateAsync(`${ns}.info.connection`);
                    return s && s.val === true ? s : null;
                });
                if (!conn) throw new Error('no connection');

                const temp = await waitFor(() => harness.states.getStateAsync(`${ns}.${SERIAL}.temperature`));
                if (temp.val !== 21 || temp.ack !== true) throw new Error(`temperature ${JSON.stringify(temp)}`);
                const fan = await harness.states.getStateAsync(`${ns}.${SERIAL}.fan_speed`);
                if (fan.val !== 'Night') throw new Error(`fan_speed ${fan && fan.val}`);
                const night = await harness.states.getStateAsync(`${ns}.${SERIAL}.night_alarm`);
                if (night.val !== true) throw new Error('night_alarm');
                const online = await waitFor(() => harness.states.getStateAsync(`${ns}.${SERIAL}.online`));
                if (online.val !== true) throw new Error('online');
                const radon = await waitFor(() => harness.states.getStateAsync(`${ns}.neuracell.radon`));
                if (radon.val !== 120) throw new Error('radon');

                const dev = await waitFor(async () => {
                    const o = await harness.objects.getObjectAsync(`${ns}.${SERIAL}`);
                    return o && o.common.name === 'Flur' ? o : null;
                });
                if (!dev) throw new Error('device name');

                // Commands: valid mode, refused fan speed Night, filter reset.
                await harness.states.setStateAsync(`${ns}.${SERIAL}.operating_mode`, { val: 'Night', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.fan_speed`, { val: 'Turbo', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.fan_speed`, { val: 'High', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.reset_filter`, { val: true, ack: false });
                await waitFor(() => received.find(m => m.topic === `${PREFIX}/${SERIAL}/set/reset_filter`));
                const topics = received.map(m => `${m.topic}=${m.payload}`);
                const expect = [
                    `${PREFIX}/${SERIAL}/set/operating_mode=Night`,
                    `${PREFIX}/${SERIAL}/set/fan_speed=High`,
                    `${PREFIX}/${SERIAL}/set/reset_filter=true`,
                ];
                for (const e of expect) if (!topics.includes(e)) throw new Error(`missing ${e} in ${topics.join(', ')}`);
                if (topics.includes(`${PREFIX}/${SERIAL}/set/fan_speed=Turbo`)) throw new Error('Turbo must not be sent');
                const btn = await waitFor(async () => {
                    const s = await harness.states.getStateAsync(`${ns}.${SERIAL}.reset_filter`);
                    return s && s.ack === true && s.val === false ? s : null;
                });
                if (!btn) throw new Error('reset button not released');
            });
        });
    },
});
