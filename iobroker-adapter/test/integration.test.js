const path = require('path');
const net = require('net');
const { tests } = require('@iobroker/testing');

// Starts a real js-controller with this adapter and a local MQTT broker (aedes)
// that plays the part of the Ambientika MQTT Bridge. The whole chain with the
// real bridge and a real Mosquitto is in system.test.js.
const SERIAL = 'A0B1C2D3E401';
const PREFIX = 'ambientika';

const STATE = {
    operating_mode: 'Smart', operating_mode_raw: 'Smart', fan_speed: 'Night',
    humidity_level: 'Normal', light_sensor_level: 'Low', temperature: 21, humidity: 55,
    temperature_avg: 20.8, humidity_avg: 54.2, humidity_raw: 55, air_quality: 'VeryGood',
    humidity_alarm: false, filters_status: 'Good', filters_status_raw: 'Good',
    filter_ack_since: null, filter_ack_until: null, night_alarm: true, device_role: 'Master',
    last_operating_mode: 'Smart', zone_index: 0, operating_mode_num: 0, operating_mode_raw_num: 0,
    last_operating_mode_num: 0, fan_speed_num: 4, humidity_level_num: 1, light_sensor_level_num: 2,
    air_quality_num: 4, filter_status_num: 0, filter_status_raw_num: 0,
};

const DIAGNOSTICS = {
    last_status: '2026-11-17T09:08:45+00:00', poll_failures_total: 7, poll_failures_consecutive: 0,
    last_failure: '2026-11-17T08:00:00+00:00', last_failure_reason: 'status() failed',
    last_outage_start: '2026-11-17T07:58:00+00:00', last_outage_end: '2026-11-17T08:00:00+00:00',
    last_outage_polls: 5, bridge_started: '2026-11-17T07:00:00+00:00',
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
            let pub;
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
                pub = (topic, payload) => new Promise(r => aedes.publish({ topic, payload: Buffer.from(payload), retain: true, qos: 0, cmd: 'publish' }, r));
                await pub(`homeassistant/sensor/ambientika_${SERIAL}_temperature/config`, JSON.stringify(DISCOVERY));
                await pub(`${PREFIX}/bridge/availability`, 'online');
                await pub(`${PREFIX}/${SERIAL}/state`, JSON.stringify(STATE));
                await pub(`${PREFIX}/${SERIAL}/availability`, 'online');
                await pub(`${PREFIX}/${SERIAL}/diagnostics`, JSON.stringify(DIAGNOSTICS));
                await pub(`${PREFIX}/${SERIAL}/reset_state`, 'idle');
                await pub(`${PREFIX}/${SERIAL}/reset_info`, JSON.stringify({ device_role: 'Master', note: 'Master: the cloud filter reset clears this unit\'s counter.' }));
                await pub(`${PREFIX}/neuracell/state`, JSON.stringify({ radon_protection: false, radon: 120, radon_threshold: 300, dewpoint_block: false, override_active: false }));
            });

            after(async function () {
                this.timeout(10000);
                await new Promise(r => aedes.close(r));
                server.close();
            });

            it('creates the unit with its name and states, follows the bridge and sends commands', async function () {
                this.timeout(90000);
                harness = getHarness();
                await harness.changeAdapterConfig('ambientika', { native: { mqttHost: '127.0.0.1', mqttPort: port, mqttPrefix: PREFIX, discoveryPrefix: 'homeassistant' } });
                await harness.startAdapterAndWait();

                const ns = 'ambientika.0';
                const val = async id => {
                    const s = await harness.states.getStateAsync(`${ns}.${id}`);
                    return s ? s.val : undefined;
                };
                await waitFor(async () => (await val('info.connection')) === true);
                await waitFor(async () => (await val('info.bridge')) === true);

                const temp = await waitFor(() => harness.states.getStateAsync(`${ns}.${SERIAL}.temperature`));
                if (temp.val !== 21 || temp.ack !== true) throw new Error(`temperature ${JSON.stringify(temp)}`);
                await waitFor(async () => (await val(`${SERIAL}.filter_status_raw_num`)) === 0);
                for (const [key, expected] of Object.entries(STATE)) {
                    const v = await val(`${SERIAL}.${key}`);
                    if (v !== expected) throw new Error(`${key}: ${JSON.stringify(v)} instead of ${JSON.stringify(expected)}`);
                }
                await waitFor(async () => (await val(`${SERIAL}.online`)) === true);
                await waitFor(async () => (await val(`${SERIAL}.diagnostics.last_outage_polls`)) === 5);
                for (const [key, expected] of Object.entries(DIAGNOSTICS)) {
                    const v = await val(`${SERIAL}.diagnostics.${key}`);
                    if (v !== expected) throw new Error(`diagnostics.${key}: ${JSON.stringify(v)} instead of ${JSON.stringify(expected)}`);
                }
                await waitFor(async () => (await val(`${SERIAL}.reset_state`)) === 'idle');
                await waitFor(async () => /Master/.test(String(await val(`${SERIAL}.diagnostics.reset_note`))));
                await waitFor(async () => (await val('neuracell.radon')) === 120);

                const dev = await waitFor(async () => {
                    const o = await harness.objects.getObjectAsync(`${ns}.${SERIAL}`);
                    return o && o.common.name === 'Flur' ? o : null;
                });
                if (dev.native.serial !== SERIAL) throw new Error('native.serial');

                // Commands: a mode, a value that does not exist, the fan steps, the filter reset.
                await harness.states.setStateAsync(`${ns}.${SERIAL}.operating_mode`, { val: 'Night', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.operating_mode`, { val: 'Hyperdrive', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.fan_speed`, { val: 2, ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.fan_speed`, { val: 'Turbo', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.fan_speed`, { val: 'High', ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.temperature`, { val: 30, ack: false });
                await harness.states.setStateAsync(`${ns}.${SERIAL}.reset_filter`, { val: true, ack: false });
                await waitFor(() => received.find(m => m.topic === `${PREFIX}/${SERIAL}/set/reset_filter`));
                const sent = received.filter(m => m.topic.includes('/set')).map(m => `${m.topic}=${m.payload}`);
                const expected = [
                    `${PREFIX}/${SERIAL}/set/operating_mode=Night`,
                    `${PREFIX}/${SERIAL}/set/fan_speed=Turbo`,
                    `${PREFIX}/${SERIAL}/set/fan_speed=High`,
                    `${PREFIX}/${SERIAL}/set/reset_filter=true`,
                ];
                if (JSON.stringify(sent) !== JSON.stringify(expected)) throw new Error(`sent ${sent.join(', ')}`);
                await waitFor(async () => {
                    const s = await harness.states.getStateAsync(`${ns}.${SERIAL}.reset_filter`);
                    return s && s.ack === true && s.val === false ? s : null;
                });

                // The unit drops out, then the bridge itself.
                await pub(`${PREFIX}/${SERIAL}/availability`, 'offline');
                await waitFor(async () => (await val(`${SERIAL}.online`)) === false);
                await pub(`${PREFIX}/${SERIAL}/availability`, 'online');
                await waitFor(async () => (await val(`${SERIAL}.online`)) === true);
                await pub(`${PREFIX}/bridge/availability`, 'offline');
                await waitFor(async () => (await val('info.bridge')) === false && (await val(`${SERIAL}.online`)) === false);
                await pub(`${PREFIX}/bridge/availability`, 'online');
                await waitFor(async () => (await val('info.bridge')) === true && (await val(`${SERIAL}.online`)) === true);
            });
        });
    },
});
