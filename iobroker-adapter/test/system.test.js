'use strict';

/**
 * System test of the whole chain with real processes:
 *
 *   cloud stand-in (HTTP)  ->  bridge.py  ->  Mosquitto  ->  this adapter in js-controller
 *
 * and of the adapter alone against whatever can turn up on a broker.
 * Needs python3 with the bridge requirements (SIM_PYTHON) and mosquitto
 * (SIM_MOSQUITTO for another path than the one on the PATH).
 *
 *   SIM_PYTHON=/path/to/python JS_CONTROLLER=7.2.5 npx mocha test/system.test.js --exit
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { tests } = require('@iobroker/testing');
const sim = require('./sim/lib');

const { wait, until, MASTER, SLAVE, SLAVE2, MASTER2, SERIALS, NAMES, POLL } = sim;
const NS = 'ambientika.0';
const TEST_DIR = path.join(os.tmpdir(), 'test-iobroker.ambientika');

const RESULTS = [];
let groupName = '';

function check(name, cond, detail) {
    const ok = !!cond;
    RESULTS.push({ group: groupName, name, ok });
    let extra = '';
    if (!ok && detail !== undefined) {
        extra = `  <<< ${(typeof detail === 'string' ? detail : JSON.stringify(detail)).slice(0, 700)}`;
    }
    console.log(`    ${ok ? 'PASS' : 'FAIL'} ${name}${extra}`);
    return ok;
}

/** Fails the mocha test when a check of this group failed. */
function verdict() {
    const bad = RESULTS.filter(r => r.group === groupName && !r.ok).map(r => r.name);
    if (bad.length) throw new Error(`${bad.length} check(s) failed: ${bad.join(' | ')}`);
}

function db(harness) {
    const get = async id => {
        const s = await harness.states.getStateAsync(`${NS}.${id}`);
        return s || null;
    };
    return {
        get,
        val: async id => {
            const s = await get(id);
            return s ? s.val : undefined;
        },
        obj: id => harness.objects.getObjectAsync(id ? `${NS}.${id}` : NS),
        set: (id, val) => harness.states.setStateAsync(`${NS}.${id}`, { val, ack: false }),
        setAck: (id, val) => harness.states.setStateAsync(`${NS}.${id}`, { val, ack: true }),
        list: async () => {
            const res = await harness.objects.getObjectListAsync({ startkey: `${NS}.`, endkey: `${NS}.香` });
            return res.rows.map(r => r.value).filter(Boolean);
        },
        config: native => harness.changeAdapterConfig('ambientika', { native }),
    };
}

/** Units as the adapter created them: [{id, name, serial}] */
async function devices(d) {
    return (await d.list()).filter(o => o.type === 'device' && o.native && o.native.serial)
        .map(o => ({ id: o._id.slice(NS.length + 1), name: o.common.name, serial: o.native.serial }));
}

const TYPE_OK = {
    number: v => v === null || typeof v === 'number',
    string: v => v === null || typeof v === 'string',
    boolean: v => v === null || typeof v === 'boolean',
};

/** Every state of the adapter holds a value of its declared type. */
async function typeProblems(d, harness) {
    const bad = [];
    for (const o of await d.list()) {
        if (o.type !== 'state') continue;
        const s = await harness.states.getStateAsync(o._id);
        if (!s) continue;
        const fn = TYPE_OK[o.common.type];
        if (!fn || !fn(s.val)) bad.push(`${o._id}=${JSON.stringify(s.val)} (${o.common.type})`);
        if (!o.common.role || typeof o.common.read !== 'boolean' || typeof o.common.write !== 'boolean') bad.push(`${o._id}: common incomplete`);
    }
    return bad;
}

tests.integration(path.join(__dirname, '..'), {
    allowedExitCodes: [11],
    controllerVersion: process.env.JS_CONTROLLER || undefined,
    loglevel: 'info',
    defineAdditionalTests({ suite }) {
        // -------------------------------------------------------------------
        suite('Chain: cloud stand-in, bridge, Mosquitto, adapter', getHarness => {
            let cloud, broker, bridge, spy, adapter, d, harness;

            before(async function () {
                this.timeout(90000);
                harness = getHarness();
                d = db(harness);
                cloud = await sim.Cloud.start();
                broker = await sim.Broker.start();
                spy = await sim.Spy.start(broker);
                bridge = sim.Bridge.start(cloud, broker);
                await d.config({ mqttHost: '127.0.0.1', mqttPort: broker.port, mqttPrefix: 'ambientika', discoveryPrefix: 'homeassistant' });
                adapter = new sim.AdapterProc(TEST_DIR, harness);
                await adapter.start();
            });

            after(async function () {
                this.timeout(60000);
                if (adapter) await adapter.stop();
                if (bridge) await bridge.stop();
                if (spy) await spy.stop();
                if (broker) await broker.down();
                if (cloud) await cloud.stop();
            });

            it('start: units, names, states, values', async function () {
                this.timeout(120000);
                groupName = 'chain/start';
                check('adapter connects to the broker', await until(async () => (await d.val('info.connection')) === true, 20000));
                const ready = await until(async () => {
                    for (const s of SERIALS) {
                        // filter_status_num is among the last fields of a state message
                        if ((await d.val(`${s}.online`)) !== true || (await d.val(`${s}.temperature`)) !== 21 || (await d.val(`${s}.filter_status_num`)) !== 0) return false;
                    }
                    return true;
                }, 40000);
                await wait(1500);
                check('all five units appear with values and online', ready, adapter.proc.log().slice(-1500));
                const devs = await devices(d);
                check('exactly five units, no duplicates', devs.length === 5 && new Set(devs.map(x => x.serial)).size === 5, devs);
                const names = await until(async () => {
                    const list = await devices(d);
                    return SERIALS.every(s => (list.find(x => x.serial === s) || {}).name === NAMES[s]) ? list : null;
                }, 15000);
                check('names as in the app (umlauts, hyphen, space)', names, await devices(d));

                // Parity with what the bridge announces to Home Assistant.
                const payload = spy.json(`ambientika/${MASTER}/state`) || {};
                const diag = spy.json(`ambientika/${MASTER}/diagnostics`) || {};
                const cfgs = spy.msgs.filter(m => m.topic.startsWith('homeassistant/') && m.topic.includes(`/${MASTER}_`) && m.payload).map(m => {
                    try { return JSON.parse(m.payload); } catch (e) { return null; }
                }).filter(Boolean);
                const missing = [];
                const wrong = [];
                let selects = 0;
                for (const c of cfgs) {
                    const key = (/value_json\.([a-z_]+)/.exec(c.value_template || '') || [])[1];
                    if (c.command_topic && /\/set\/reset_filter$/.test(c.command_topic)) {
                        const o = await d.obj(`${MASTER}.reset_filter`);
                        if (!o || o.common.write !== true || o.common.role !== 'button') missing.push('reset_filter');
                    } else if (c.state_topic === `ambientika/${MASTER}/state` && key) {
                        const o = await d.obj(`${MASTER}.${key}`);
                        if (!o) { missing.push(key); continue; }
                        const v = await d.val(`${MASTER}.${key}`);
                        if (v !== payload[key]) wrong.push(`${key}: ${JSON.stringify(v)} != ${JSON.stringify(payload[key])}`);
                        if (c.command_topic) {
                            selects++;
                            if (o.common.write !== true) wrong.push(`${key} not writable`);
                            const opts = Object.keys(o.common.states || {});
                            if (JSON.stringify(opts.slice().sort()) !== JSON.stringify(c.options.slice().sort())) wrong.push(`${key} options ${opts} != ${c.options}`);
                        }
                    } else if (c.state_topic === `ambientika/${MASTER}/diagnostics` && key) {
                        const o = await d.obj(`${MASTER}.diagnostics.${key}`);
                        if (!o) { missing.push(`diagnostics.${key}`); continue; }
                        const v = await d.val(`${MASTER}.diagnostics.${key}`);
                        if (key !== 'last_status' && v !== diag[key]) wrong.push(`diagnostics.${key}: ${v} != ${diag[key]}`);
                    } else if (c.state_topic === `ambientika/${MASTER}/reset_state`) {
                        const v = await d.val(`${MASTER}.reset_state`);
                        if (v !== spy.last(`ambientika/${MASTER}/reset_state`)) missing.push(`reset_state (${v})`);
                    }
                }
                check(`every entity the bridge announces to Home Assistant has its state here (${cfgs.length} entities)`, cfgs.length >= 30 && !missing.length, missing);
                check('values equal the bridge payload, selects are writable with the same options', !wrong.length && selects === 4, wrong);
                const extraKeys = Object.keys(payload).filter(k => !cfgs.some(c => (c.value_template || '').includes(`value_json.${k} `) || (c.value_template || '').includes(`value_json.${k}}`)));
                const extraMissing = [];
                for (const k of extraKeys) if (!(await d.obj(`${MASTER}.${k}`))) extraMissing.push(k);
                check('every other field of the state payload is a state too', !extraMissing.length, extraMissing);

                check('the bridge itself is shown online (info.bridge)', (await d.val('info.bridge')) === true, await d.get('info.bridge'));
                check('NeuraCell-X states', (await d.val('neuracell.radon_threshold')) === 300 && (await d.val('neuracell.radon_protection')) === false,
                    await d.get('neuracell.radon_threshold'));
                check('a Slave shows role and zone', (await d.val(`${SLAVE}.device_role`)) === 'SlaveEqualMaster' && (await d.val(`${MASTER2}.zone_index`)) === 1);
                const tp = await typeProblems(d, harness);
                check('every state holds a value of its declared type and has role/read/write', !tp.length, tp);
                check('no warning or error in the adapter log', !adapter.problems().length, adapter.problems());
                verdict();
            });

            it('values follow the cloud', async function () {
                this.timeout(60000);
                groupName = 'chain/values';
                await cloud.unit(MASTER, { temperature: 23.5, humidity: 61, airQuality: 'Poor', filtersStatus: 'Bad', humidityAlarm: true, nightAlarm: true, fanSpeed: 'Night' });
                check('temperature', await until(async () => (await d.val(`${MASTER}.temperature`)) === 23.5, 15000), await d.get(`${MASTER}.temperature`));
                check('humidity, air quality, filter', await until(async () => (await d.val(`${MASTER}.humidity`)) === 61 && (await d.val(`${MASTER}.air_quality`)) === 'Poor'
                    && (await d.val(`${MASTER}.filters_status`)) === 'Bad' && (await d.val(`${MASTER}.air_quality_num`)) === 1 && (await d.val(`${MASTER}.filter_status_num`)) !== 0, 8000),
                [await d.val(`${MASTER}.humidity`), await d.val(`${MASTER}.air_quality`), await d.val(`${MASTER}.air_quality_num`)]);
                check('alarms as booleans', (await d.val(`${MASTER}.humidity_alarm`)) === true && (await d.val(`${MASTER}.night_alarm`)) === true);
                check('fan step Night chosen by the unit is shown', (await d.val(`${MASTER}.fan_speed`)) === 'Night');
                const st = await d.get(`${MASTER}.temperature`);
                check('values are acknowledged (ack) and stamped by the adapter', st.ack === true && st.from === `system.adapter.${NS}`, st);
                await cloud.unit(SLAVE2, { humidity: 118 });
                check('saturated sensor: 100 % with the raw value beside it',
                    await until(async () => (await d.val(`${SLAVE2}.humidity_raw`)) === 118 && (await d.val(`${SLAVE2}.humidity`)) === 100, 15000),
                    [await d.val(`${SLAVE2}.humidity`), await d.val(`${SLAVE2}.humidity_raw`)]);
                await cloud.unit(MASTER, { fanSpeed: 'Medium', nightAlarm: false, humidityAlarm: false });
                await cloud.unit(SLAVE2, { humidity: 55 });
                check('back to normal', await until(async () => (await d.val(`${MASTER}.fan_speed`)) === 'Medium', 15000));
                check('no warning or error in the adapter log', !adapter.problems().length, adapter.problems());
                verdict();
            });

            it('commands reach the cloud', async function () {
                this.timeout(150000);
                groupName = 'chain/commands';
                await cloud.clear();
                await d.set(`${MASTER}.operating_mode`, 'Night');
                const call = await until(async () => (await cloud.log('change-mode'))[0], 10000);
                check('mode Night -> one accepted change-mode call with mode 3', call && call.ok === true && String(call.body.operatingMode) === '3', call);
                check('fan, humidity level and light sensor stay as they were', call && call.body.fanSpeed === 1 && call.body.humidityLevel === 1 && call.body.lightSensorLevel === 2, call);
                const acked = await until(async () => {
                    const s = await d.get(`${MASTER}.operating_mode`);
                    return s && s.val === 'Night' && s.ack === true ? s : null;
                }, 10000);
                check('the state comes back acknowledged', acked, await d.get(`${MASTER}.operating_mode`));

                await cloud.clear();
                await d.set(`${MASTER}.fan_speed`, 'High');
                await d.set(`${MASTER}.humidity_level`, 'Moist');
                await d.set(`${MASTER}.light_sensor_level`, 'Off');
                await until(async () => (await cloud.log('change-mode')).length, 10000);
                await wait(2000);
                const calls = await cloud.log('change-mode');
                check('three changes in a row become one call', calls.length === 1, calls);
                const body = calls.length ? calls[0].body : {};
                check('with High / Moist / Off', body.fanSpeed === 2 && body.humidityLevel === 2 && body.lightSensorLevel === 1 && String(body.operatingMode) === '3', body);
                check('states follow', await until(async () => (await d.val(`${MASTER}.fan_speed`)) === 'High' && (await d.val(`${MASTER}.humidity_level`)) === 'Moist'
                    && (await d.val(`${MASTER}.light_sensor_level`)) === 'Off', 10000));

                // what must not be sent
                await cloud.clear();
                const before = spy.msgs.length;
                await d.set(`${MASTER}.operating_mode`, 'Hyperdrive');
                await d.set(`${MASTER}.fan_speed`, 3);
                await d.set(`${MASTER}.humidity_level`, '');
                await d.set(`${MASTER}.light_sensor_level`, null);
                await d.set(`${MASTER}.temperature`, 30);
                await d.set(`${MASTER}.online`, false);
                await d.setAck(`${MASTER}.operating_mode`, 'Off');
                await wait(2500);
                const sent = spy.msgs.slice(before).filter(m => m.topic.includes('/set'));
                check('invalid values, read-only states and acknowledged writes send nothing', !sent.length && !(await cloud.log('change-mode')).length, sent);
                const warns = adapter.lines().filter(l => l.level === 'warn' && /cannot be set/.test(l.text));
                check('each refused value is explained in the log', warns.length === 4, adapter.problems());
                check('the unit is still what the cloud says', await until(async () => (await d.val(`${MASTER}.operating_mode`)) === 'Night' && (await d.val(`${MASTER}.temperature`)) === 23.5, 10000),
                    [await d.val(`${MASTER}.operating_mode`), await d.val(`${MASTER}.temperature`)]);

                // steps the unit chooses itself: forwarded, the bridge sends the nearest
                await cloud.clear();
                await d.set(`${MASTER2}.fan_speed`, 'Turbo');
                const turbo = await until(async () => (await cloud.log('change-mode'))[0], 10000);
                check('fan step Turbo (as a scene restores it) is passed on, the bridge sends High', turbo && turbo.body.fanSpeed === 2, turbo || adapter.problems().slice(-2));

                // a Slave
                await cloud.clear();
                await d.set(`${SLAVE}.humidity_level`, 'Dry');
                const sl = await until(async () => (await cloud.log('change-mode'))[0], 10000);
                check('a command for a Slave is sent for that Slave', sl && sl.serial === SLAVE && sl.body.humidityLevel === 0, sl);

                // filter reset
                await cloud.unit(MASTER, { filtersStatus: 'Bad' });
                await until(async () => (await d.val(`${MASTER}.filters_status_raw`)) === 'Bad', 10000);
                await cloud.clear();
                const t0 = Date.now();
                await d.set(`${MASTER}.reset_filter`, true);
                const rs = await until(async () => (await cloud.log('reset-filter'))[0], 10000);
                check('filter reset button -> reset call for the unit', rs && rs.serial === MASTER, rs);
                check('the button releases itself', await until(async () => {
                    const s = await d.get(`${MASTER}.reset_filter`);
                    return s && s.val === false && s.ack === true;
                }, 5000));
                check('reset status: running, then confirmed', await until(async () => (await d.val(`${MASTER}.reset_state`)) === 'confirmed', 40000)
                    && spy.all(`ambientika/${MASTER}/reset_state`, t0)[0].payload === 'running', await d.get(`${MASTER}.reset_state`));
                check('filter Good again', await until(async () => (await d.val(`${MASTER}.filters_status`)) === 'Good', 10000));
                check('a note says what the reset does on a Slave', /Slave/.test(String(await d.val(`${SLAVE}.diagnostics.reset_note`))), await d.get(`${SLAVE}.diagnostics.reset_note`));
                check('nothing but the documented calls reached the cloud', !(await cloud.log('UNEXPECTED')).length);
                verdict();
            });

            it('a unit drops out and comes back', async function () {
                this.timeout(90000);
                groupName = 'chain/unit-outage';
                await cloud.unit(SLAVE2, null, '404');
                check('unit without status packet -> online false after three polls', await until(async () => (await d.val(`${SLAVE2}.online`)) === false, 30000));
                check('the others stay online', (await d.val(`${MASTER}.online`)) === true && (await d.val(`${SLAVE}.online`)) === true);
                check('failed polls are counted', await until(async () => (await d.val(`${SLAVE2}.diagnostics.poll_failures_total`)) >= 3
                    && (await d.val(`${SLAVE2}.diagnostics.poll_failures_consecutive`)) >= 3, 10000), await d.get(`${SLAVE2}.diagnostics.poll_failures_total`));
                check('with start of the outage and reason', !!(await d.val(`${SLAVE2}.diagnostics.last_outage_start`)) && !!(await d.val(`${SLAVE2}.diagnostics.last_failure_reason`)));
                check('the last values stay readable', (await d.val(`${SLAVE2}.temperature`)) === 21);
                const stamp = await d.val(`${MASTER}.diagnostics.last_status`);
                await cloud.unit(SLAVE2, null, null);
                check('unit back -> online true', await until(async () => (await d.val(`${SLAVE2}.online`)) === true, 20000));
                check('run of failures closed', await until(async () => (await d.val(`${SLAVE2}.diagnostics.poll_failures_consecutive`)) === 0, 10000));
                check('time of the last status packet moves on', await until(async () => {
                    const v = await d.val(`${MASTER}.diagnostics.last_status`);
                    return v && v !== stamp;
                }, 10000), stamp);
                check('no warning or error in the adapter log over a unit outage', !adapter.problems().filter(l => !/cannot be set/.test(l.text)).length, adapter.problems());
                verdict();
            });

            it('the bridge stops, is killed, starts again', async function () {
                this.timeout(120000);
                groupName = 'chain/bridge-outage';
                check('bridge stops cleanly', (await bridge.stop()) === 0, bridge.proc.exit);
                check('info.bridge false', await until(async () => (await d.val('info.bridge')) === false, 10000), await d.get('info.bridge'));
                const off = await until(async () => {
                    for (const s of SERIALS) if ((await d.val(`${s}.online`)) !== false) return false;
                    return true;
                }, 10000);
                check('all units online false (their values are no longer updated)', off, await d.val(`${MASTER}.online`));
                check('the adapter itself stays connected to the broker', (await d.val('info.connection')) === true);
                check('one warning says that the bridge is offline', adapter.lines().filter(l => l.level === 'warn' && /Bridge reports offline/.test(l.text)).length === 1, adapter.problems());

                bridge = sim.Bridge.start(cloud, broker);
                check('bridge starts -> info.bridge true', await until(async () => (await d.val('info.bridge')) === true, 40000), bridge.log().slice(-600));
                const on = await until(async () => {
                    for (const s of SERIALS) if ((await d.val(`${s}.online`)) !== true) return false;
                    return true;
                }, 20000);
                check('all units online again', on);
                check('still five units after the bridge restart', (await devices(d)).length === 5, await devices(d));

                await wait(POLL * 1000);
                await bridge.stop('SIGKILL');
                check('killed bridge (Last Will) -> info.bridge false, units online false',
                    await until(async () => (await d.val('info.bridge')) === false && (await d.val(`${MASTER}.online`)) === false, 15000),
                    [await d.val('info.bridge'), await d.val(`${MASTER}.online`)]);
                await d.set(`${MASTER}.fan_speed`, 'Low');
                await wait(500);
                check('adapter keeps running without the bridge', adapter.running && (await d.val(`system.adapter.${NS}.alive`).catch(() => true)) !== false);
                bridge = sim.Bridge.start(cloud, broker);
                check('bridge back -> everything online', await until(async () => (await d.val('info.bridge')) === true && (await d.val(`${MASTER}.online`)) === true, 40000));
                verdict();
            });

            it('the broker restarts', async function () {
                this.timeout(120000);
                groupName = 'chain/broker-restart';
                await spy.stop();
                await broker.down();
                check('broker gone -> info.connection false', await until(async () => (await d.val('info.connection')) === false, 15000));
                check('units online false while nothing can arrive', await until(async () => (await d.val(`${MASTER}.online`)) === false && (await d.val('info.bridge')) === false, 5000),
                    [await d.val(`${MASTER}.online`), await d.val('info.bridge')]);
                const n = adapter.lines().length;
                await d.set(`${MASTER}.fan_speed`, 'Medium');
                await wait(1500);
                check('a command without broker is refused with a clear message', adapter.lines().slice(n).some(l => l.level === 'warn' && /not connected to the MQTT broker/.test(l.text)),
                    adapter.lines().slice(n));
                await wait(6000);
                const flood = adapter.lines().slice(n).filter(l => l.level === 'warn' || l.level === 'error');
                check('no flood of messages while the broker is away', flood.length <= 3, flood);
                check('adapter and bridge keep running', adapter.running && bridge.running);

                await broker.up();
                spy = await sim.Spy.start(broker);
                check('broker back (empty) -> info.connection true', await until(async () => (await d.val('info.connection')) === true, 30000));
                const on = await until(async () => {
                    for (const s of SERIALS) if ((await d.val(`${s}.online`)) !== true) return false;
                    return (await d.val('info.bridge')) === true;
                }, 40000);
                check('bridge and all units online again', on, [await d.val('info.bridge'), await d.val(`${MASTER}.online`)]);
                await cloud.clear();
                await d.set(`${MASTER}.fan_speed`, 'Low');
                check('commands work again', await until(async () => (await cloud.log('change-mode')).length, 10000));
                check('still five units', (await devices(d)).length === 5);
                verdict();
            });

            it('the adapter restarts', async function () {
                this.timeout(120000);
                groupName = 'chain/adapter-restart';
                const exit = await adapter.stop();
                check('adapter stops when js-controller asks (exit code 11, within 8 s)', exit === 11, exit);
                const objectsBefore = (await d.list()).length;
                await cloud.unit(MASTER, { temperature: 19 });
                await wait(2 * POLL * 1000);
                adapter = new sim.AdapterProc(TEST_DIR, harness);
                await adapter.start();
                check('after the restart the value that changed meanwhile arrives', await until(async () => (await d.val(`${MASTER}.temperature`)) === 19, 20000));
                check('no object was added or lost over the restart', (await d.list()).length === objectsBefore, [(await d.list()).length, objectsBefore]);
                check('names kept', (await devices(d)).every(x => x.name === NAMES[x.serial]), await devices(d));
                check('online and info.bridge true again', await until(async () => (await d.val(`${MASTER}.online`)) === true && (await d.val('info.bridge')) === true, 15000));
                check('no warning or error at the restart', !adapter.problems().length, adapter.problems());

                // start while the broker is away
                await adapter.stop();
                await spy.stop();
                await broker.down();
                adapter = new sim.AdapterProc(TEST_DIR, harness);
                await adapter.start();
                await wait(7000);
                check('start without broker: adapter runs, connection false', adapter.running && (await d.val('info.connection')) === false);
                check('units are not shown online from the last run', (await d.val(`${MASTER}.online`)) === false && (await d.val('info.bridge')) === false,
                    [await d.val(`${MASTER}.online`), await d.val('info.bridge')]);
                const probs = adapter.problems();
                check('one warning about the missing broker, not one per attempt', probs.length >= 1 && probs.length <= 2 && /ECONNREFUSED|connect/i.test(probs[0].text), probs);
                await cloud.clear();
                await d.set(`${MASTER}.fan_speed`, 'High');
                await wait(500);
                await broker.up();
                spy = await sim.Spy.start(broker);
                check('broker appears -> connected, units online', await until(async () => (await d.val('info.connection')) === true && (await d.val(`${MASTER}.online`)) === true, 40000));
                await d.set(`${MASTER}.humidity_level`, 'Normal');
                check('a command right after works (unit known from the earlier run)', await until(async () => (await cloud.log('change-mode')).length, 10000));
                verdict();
            });
        });

        // -------------------------------------------------------------------
        suite('Adapter alone: anything on the broker', getHarness => {
            let broker, spy, adapter, d, harness;
            const P = 'ambientika';
            const GOOD = {
                operating_mode: 'Smart', fan_speed: 'Medium', humidity_level: 'Normal', light_sensor_level: 'Low',
                temperature: 21, humidity: 55, air_quality: 'Good', humidity_alarm: false, night_alarm: false,
                filters_status: 'Good', device_role: 'Master', zone_index: 0, operating_mode_num: 0,
            };
            const disco = (serial, name, extra = {}) => JSON.stringify({ name: 'Temperature', state_topic: `${P}/${serial}/state`, device: { identifiers: [serial], name }, ...extra });
            const alive = async () => adapter.running && (await harness.states.getStateAsync(`system.adapter.${NS}.alive`)).val === true;

            before(async function () {
                this.timeout(60000);
                harness = getHarness();
                d = db(harness);
                broker = await sim.Broker.start();
                spy = await sim.Spy.start(broker);
                await d.config({ mqttHost: '127.0.0.1', mqttPort: broker.port, mqttPrefix: P, discoveryPrefix: 'homeassistant' });
                adapter = new sim.AdapterProc(TEST_DIR, harness);
                await adapter.start();
                await until(async () => (await d.val('info.connection')) === true, 20000);
            });

            after(async function () {
                this.timeout(30000);
                if (adapter) await adapter.stop();
                if (spy) await spy.stop();
                if (broker) await broker.down();
            });

            it('hand-made messages: wrong types, odd names, foreign topics', async function () {
                this.timeout(120000);
                groupName = 'alone/crafted';
                await spy.pub(`homeassistant/sensor/U1_temperature/config`, disco('U1', 'Büro'), true);
                await spy.pub(`${P}/U1/state`, JSON.stringify(GOOD), true);
                await spy.pub(`${P}/U1/availability`, 'online', true);
                check('a normal unit appears', await until(async () => (await d.val('U1.temperature')) === 21 && (await d.val('U1.online')) === true, 10000));
                check('with its name', await until(async () => ((await d.obj('U1')) || { common: {} }).common.name === 'Büro', 5000));

                // wrong types in a state payload: each field alone decides
                await spy.pub(`${P}/U1/state`, JSON.stringify({
                    temperature: 'warm', humidity: '48.5', zone_index: true, air_quality: 7, humidity_alarm: 'yes', night_alarm: 'ON',
                    filters_status: { a: 1 }, operating_mode: ['x'], fan_speed: null, operating_mode_num: 1e400, device_role: 'x'.repeat(5000),
                    __proto__: { polluted: true }, constructor: 'c', toString: 1,
                }));
                await until(async () => (await d.val('U1.humidity')) === 48.5, 5000);
                check('number as text is taken, text that is no number is not', (await d.val('U1.humidity')) === 48.5 && (await d.val('U1.temperature')) === 21,
                    [await d.val('U1.humidity'), await d.val('U1.temperature')]);
                check('boolean: ON is true, an unknown word leaves the state alone', (await d.val('U1.night_alarm')) === true && (await d.val('U1.humidity_alarm')) === false,
                    [await d.val('U1.night_alarm'), await d.val('U1.humidity_alarm')]);
                check('a boolean is not turned into a number', (await d.val('U1.zone_index')) === 0, await d.val('U1.zone_index'));
                check('null clears a value', (await d.val('U1.fan_speed')) === null, await d.val('U1.fan_speed'));
                check('objects and lists are not written into text states', (await d.val('U1.filters_status')) === 'Good' && (await d.val('U1.operating_mode')) === 'Smart',
                    [await d.val('U1.filters_status'), await d.val('U1.operating_mode')]);
                check('an overlong text is cut', String(await d.val('U1.device_role')).length <= 200, String(await d.val('U1.device_role')).length);
                check('still every state of its declared type', !(await typeProblems(d, harness)).length, await typeProblems(d, harness));

                // payloads that are not objects
                for (const p of ['', 'null', '42', '"text"', '[1,2,3]', '{broken', 'true', '\u0000\u0001\u0002', '{"temperature": NaN}']) {
                    await spy.pub(`${P}/U1/state`, p);
                    await spy.pub(`${P}/U1/diagnostics`, p);
                    await spy.pub(`${P}/neuracell/state`, p);
                    await spy.pub(`homeassistant/sensor/U1_x/config`, p);
                }
                for (const p of ['', 'ONLINE', 'Offline ', 'maybe', '1', '{"state":"online"}']) await spy.pub(`${P}/U1/availability`, p);
                await wait(800);
                check('availability: only online/offline count (case and spaces forgiven)', (await d.val('U1.online')) === false, await d.val('U1.online'));
                await spy.pub(`${P}/U1/availability`, 'online');
                await spy.pub(`${P}/bridge/availability`, 'nonsense');
                await spy.pub(`${P}/bridge/availability`, 'online');

                // names that must not become objects, or must be made safe
                await spy.pub(`${P}/U1/state`, '{"__proto__": {"polluted": true}, "constructor": {"prototype": {"polluted": true}}, "humidity": 48.5}');
                const odd = ['info', 'bridge', 'neuracell', 'a.b', 'a_b', 'c d', 'ä*ö', '..', '   ', '__proto__', 'x'.repeat(300), 'admin.0', 'system.adapter'];
                for (const s of odd) {
                    await spy.pub(`${P}/${s}/state`, JSON.stringify(GOOD));
                    await spy.pub(`${P}/${s}/availability`, 'online');
                }
                await spy.pub(`${P}//state`, JSON.stringify(GOOD));
                await spy.pub(`${P}/U1/state/extra`, JSON.stringify(GOOD));
                await spy.pub(`${P}/U1`, JSON.stringify(GOOD));
                await spy.pub(`${P}`, JSON.stringify(GOOD));
                await spy.pub(`other/U9/state`, JSON.stringify(GOOD));
                await spy.pub(`${P}x/U9/state`, JSON.stringify(GOOD));
                await wait(1500);
                const all = await d.list();
                const ids = all.map(o => o._id);
                check('no object outside the adapter namespace, none with a dot or space from a serial',
                    ids.every(id => id.startsWith(`${NS}.`)) && !ids.some(id => /^ambientika\.0\.(a\.b|a b|\.\.|admin\.0|system\.adapter)(\.|$)/.test(id)), ids.filter(id => !/U1|info|neuracell/.test(id)).slice(0, 20));
                const infoKids = ids.filter(id => id.startsWith(`${NS}.info.`)).sort();
                check('the info channel keeps only its own states', JSON.stringify(infoKids) === JSON.stringify([`${NS}.info.bridge`, `${NS}.info.connection`]), infoKids);
                check('reserved names bridge and neuracell did not become units', !(await devices(d)).some(x => ['bridge', 'neuracell', 'info'].includes(x.serial)), await devices(d));
                check('a serial with a dot or space becomes one safe id', (await devices(d)).some(x => x.serial === 'a.b' && x.id === 'a_b') && (await devices(d)).some(x => x.serial === 'c d' && x.id === 'c_d'), await devices(d));
                check('two serials that would share an id do not overwrite each other, an empty one is ignored',
                    (await devices(d)).filter(x => x.id === 'a_b').length === 1 && !(await devices(d)).some(x => !x.serial.trim()), await devices(d));
                check('an absurdly long serial is ignored', !(await devices(d)).some(x => x.serial.length > 64), (await devices(d)).map(x => x.serial.length));
                check('topics with other prefixes or extra levels are ignored', !(await devices(d)).some(x => x.serial === 'U9'));
                check('nothing landed on Object.prototype', ({}).polluted === undefined);

                // discovery of other things on the same broker
                for (let i = 0; i < 300; i++) {
                    await spy.pub(`homeassistant/sensor/zigbee_${i}/config`, JSON.stringify({ name: 'T', state_topic: `zigbee2mqtt/dev${i}`, device: { identifiers: [`zb${i}`], name: `Lamp ${i}` } }));
                }
                await spy.pub(`homeassistant/sensor/evil/config`, JSON.stringify({ name: 'T', state_topic: `zigbee2mqtt/U1`, device: { identifiers: ['U1'], name: 'Hijacked' } }));
                await spy.pub(`homeassistant/sensor/U1_b/config`, disco('U1', '   '));
                await spy.pub(`homeassistant/sensor/U1_c/config`, disco('U1', 12345));
                await spy.pub(`homeassistant/sensor/U1_d/config`, JSON.stringify({ state_topic: `${P}/U1/state`, device: { identifiers: 'U1', name: 'NoList' } }));
                await wait(1000);
                check('discovery of other devices does not rename a unit', ((await d.obj('U1')) || { common: {} }).common.name === 'Büro', (await d.obj('U1') || {}).common);
                await spy.pub(`homeassistant/sensor/U1_temperature/config`, disco('U1', 'Büro Süd'));
                check('a new name in the app arrives', await until(async () => ((await d.obj('U1')) || { common: {} }).common.name === 'Büro Süd', 5000));

                check('adapter alive after all of this', await alive());
                await spy.pub(`${P}/U1/state`, JSON.stringify({ ...GOOD, temperature: 22.5 }));
                check('and still processes a normal message', await until(async () => (await d.val('U1.temperature')) === 22.5, 5000));
                const errs = adapter.lines().filter(l => l.level === 'error' || l.level === 'raw');
                check('no error and no stack trace in the log', !errs.length, errs.slice(0, 5));
                const warns = adapter.lines().filter(l => l.level === 'warn');
                check('no warning either (bad input is not the user\'s fault)', !warns.length, warns.slice(0, 5));
                verdict();
            });

            it('random input: 4000 messages', async function () {
                this.timeout(180000);
                groupName = 'alone/fuzz';
                let seed = 20261117;
                const rnd = () => {
                    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
                    return seed / 0x7fffffff;
                };
                const pick = a => a[Math.floor(rnd() * a.length)];
                const junk = () => pick([null, true, false, 0, -1, 3.14, 1e308, -1e308, '', 'x', 'online', 'Smart', '\u0000', '💨', [], {}, [[]], { a: { b: { c: 1 } } }, 'x'.repeat(2000), 12345678901234567890]);
                const keys = ['operating_mode', 'fan_speed', 'humidity_level', 'light_sensor_level', 'temperature', 'humidity', 'temperature_avg', 'humidity_avg', 'humidity_raw',
                    'air_quality', 'humidity_alarm', 'night_alarm', 'filters_status', 'filters_status_raw', 'filter_ack_since', 'filter_ack_until', 'operating_mode_raw',
                    'last_operating_mode', 'device_role', 'zone_index', 'operating_mode_num', 'fan_speed_num', 'air_quality_num', 'filter_status_num', 'unknown_key', '__proto__',
                    'last_status', 'poll_failures_total', 'poll_failures_consecutive', 'radon', 'radon_protection', 'dewpoint_block'];
                const serials = ['U1', 'U2', 'U3', 'F0F0F0F0F0F0', 'ü', 'a/b', '+', '#x', ' ', 'bridge', 'neuracell', 'info'];
                const kinds = ['state', 'availability', 'diagnostics', 'reset_state', 'reset_info', 'set', 'set/fan_speed', 'state/x', ''];
                const rawPayloads = ['', 'online', 'offline', '{', '[]', 'null', '{"a":', '��', '0', '-', '{"temperature":{"$gt":1}}'];
                const t0 = Date.now();
                for (let i = 0; i < 4000; i++) {
                    const r = rnd();
                    let topic, payload;
                    if (r < 0.75) {
                        topic = `${P}/${pick(serials)}/${pick(kinds)}`;
                    } else if (r < 0.9) {
                        topic = `homeassistant/${pick(['sensor', 'select', 'x'])}/${pick(serials)}_${Math.floor(rnd() * 5)}/config`;
                    } else {
                        topic = `${pick([P, 'other', `${P}/${P}`])}/${pick(serials)}`;
                    }
                    topic = topic.replace(/[+#]/g, '_').replace(/\/+$/, '') || P;
                    if (rnd() < 0.2) {
                        payload = pick(rawPayloads);
                    } else {
                        const o = {};
                        const n = Math.floor(rnd() * 12);
                        for (let k = 0; k < n; k++) o[pick(keys)] = junk();
                        if (topic.endsWith('/config')) {
                            o.state_topic = pick([`${P}/U1/state`, 5, null, 'x']);
                            o.device = pick([{ identifiers: [pick(serials)], name: pick(['A', '', 7, null, 'Wohnzimmer']) }, null, 'dev', { identifiers: pick([[], [1], 'x', null]) }]);
                        }
                        payload = JSON.stringify(o);
                    }
                    spy.c.publish(topic, payload, { qos: 0 });
                    if (i % 200 === 199) await wait(60);
                }
                await spy.pub(`${P}/U1/state`, JSON.stringify({ ...GOOD, temperature: 17.25 }));
                check(`all processed, adapter alive (${Date.now() - t0} ms for 4000 messages)`, await until(async () => (await d.val('U1.temperature')) === 17.25, 30000) && (await alive()));
                const errs = adapter.lines().filter(l => l.level === 'error' || l.level === 'raw');
                check('no error and no stack trace', !errs.length, errs.slice(0, 5));
                check('no warning', !adapter.lines().filter(l => l.level === 'warn').length, adapter.lines().filter(l => l.level === 'warn').slice(0, 5));
                const tp = await typeProblems(d, harness);
                check('every state still holds a value of its declared type', !tp.length, tp.slice(0, 10));
                const ids = (await d.list()).map(o => o._id);
                check('no object outside the namespace, info channel untouched', ids.every(id => id.startsWith(`${NS}.`))
                    && ids.filter(id => id.startsWith(`${NS}.info.`)).length === 2, ids.filter(id => id.startsWith(`${NS}.info.`)));
                const mem = process.memoryUsage().rss;
                check('test driver sane', mem > 0);
                verdict();
            });

            it('flood: far more units than a house has, and oversized payloads', async function () {
                this.timeout(240000);
                groupName = 'alone/flood';
                const big = JSON.stringify({ ...GOOD, air_quality: 'x'.repeat(200000) });
                await spy.pub(`${P}/U1/state`, big);
                await spy.pub(`${P}/U1/state`, JSON.stringify({ ...GOOD, temperature: 18 }));
                check('a 200 kB payload is skipped, the next one is read', await until(async () => (await d.val('U1.temperature')) === 18, 10000) && (await d.val('U1.air_quality')) === 'Good',
                    String(await d.val('U1.air_quality')).length);
                for (let i = 0; i < 600; i++) {
                    spy.c.publish(`${P}/FLOOD${String(i).padStart(4, '0')}/state`, JSON.stringify(GOOD), { qos: 0 });
                    if (i % 50 === 49) await wait(100);
                }
                await spy.pub(`${P}/U1/state`, JSON.stringify({ ...GOOD, temperature: 18.5 }));
                check('adapter stays responsive during a flood of 600 new serials', await until(async () => (await d.val('U1.temperature')) === 18.5, 120000) && (await alive()));
                const n = (await devices(d)).length;
                check(`the number of units is capped (${n})`, n <= 250, n);
                const warns = adapter.lines().filter(l => l.level === 'warn');
                check('the cap is reported once', warns.length === 1 && /units/.test(warns[0].text), warns.slice(0, 3));
                check('no error', !adapter.lines().filter(l => l.level === 'error' || l.level === 'raw').length, adapter.lines().filter(l => l.level === 'error').slice(0, 3));
                verdict();
            });
        });

        // -------------------------------------------------------------------
        suite('Adapter settings: password, TLS, prefixes', getHarness => {
            let cloud, adapter, d, harness;
            const started = [];

            before(async function () {
                this.timeout(60000);
                harness = getHarness();
                d = db(harness);
                cloud = await sim.Cloud.start();
            });

            after(async function () {
                this.timeout(60000);
                if (adapter) await adapter.stop();
                for (const x of started.reverse()) {
                    try {
                        if (x.stop) await x.stop();
                        else if (x.down) await x.down();
                    } catch (e) {
                        // already gone
                    }
                }
                if (cloud) await cloud.stop();
            });

            async function restartAdapter(native) {
                if (adapter) await adapter.stop();
                await d.config(native);
                adapter = new sim.AdapterProc(TEST_DIR, harness);
                await adapter.start();
            }

            it('broker with user and password, stored encrypted', async function () {
                this.timeout(180000);
                groupName = 'settings/password';
                const password = 'p#w "x\' $1 ä€';
                const broker = await sim.Broker.start({ user: 'lüftung', password });
                const spy = await sim.Spy.start(broker);
                const bridge = sim.Bridge.start(cloud, broker);
                started.push(broker, spy, bridge);
                const secret = (await harness.objects.getObjectAsync('system.config')).native.secret;
                check('js-controller has a secret for encrypted settings', typeof secret === 'string' && secret.length > 10);
                const common = require(require.resolve('@iobroker/js-controller-common', { paths: [TEST_DIR] }));
                const enc = common.tools.encrypt(secret, password);
                check('password is stored encrypted, not readable', enc !== password && !enc.includes('p#w'), enc);
                await restartAdapter({ mqttHost: '127.0.0.1', mqttPort: broker.port, mqttUser: 'lüftung', mqttPassword: enc, mqttTls: false, mqttPrefix: 'ambientika' });
                check('adapter logs in with the decrypted password (special characters, umlauts)', await until(async () => (await d.val('info.connection')) === true, 20000), adapter.problems());
                check('units arrive', await until(async () => (await d.val(`${MASTER}.online`)) === true, 40000));
                const inst = await harness.objects.getObjectAsync(`system.adapter.${NS}`);
                check('instance declares the password as protected and encrypted', (inst.protectedNative || []).includes('mqttPassword') && (inst.encryptedNative || []).includes('mqttPassword'),
                    [inst.protectedNative, inst.encryptedNative]);
                check('the password appears nowhere in the adapter log', !adapter.proc.log().includes(password) && !adapter.proc.log().includes(enc));

                // legacy encryption of older js-controller versions
                let legacy = '';
                for (let i = 0; i < password.length; i++) legacy += String.fromCharCode(secret[i % secret.length].charCodeAt(0) ^ password.charCodeAt(i));
                await restartAdapter({ mqttPassword: legacy });
                check('a password encrypted the old way (before js-controller 6) works too', await until(async () => (await d.val('info.connection')) === true, 20000), adapter.problems());

                // wrong password
                await restartAdapter({ mqttPassword: common.tools.encrypt(secret, 'falsch') });
                await wait(13000);
                const probs = adapter.problems();
                check('wrong password: no connection, adapter keeps running', (await d.val('info.connection')) === false && adapter.running);
                check('one clear warning, not one per attempt', probs.length === 1 && /authoriz|user ?name or password/i.test(probs[0].text), probs);

                // no user at a broker that wants one
                await restartAdapter({ mqttUser: '', mqttPassword: '' });
                await wait(7000);
                check('no user at a protected broker: one warning, no crash', adapter.running && adapter.problems().length === 1, adapter.problems());
                await bridge.stop();
                await spy.stop();
                await broker.down();
                verdict();
            });

            it('TLS with a self-signed certificate', async function () {
                this.timeout(120000);
                groupName = 'settings/tls';
                const broker = await sim.Broker.start({ tls: true });
                const spy = await sim.Spy.start(broker);
                started.push(broker, spy);
                await spy.pub('ambientika/T1/state', JSON.stringify({ temperature: 20.5 }), true);
                await spy.pub('ambientika/T1/availability', 'online', true);
                await restartAdapter({ mqttHost: '127.0.0.1', mqttPort: broker.port, mqttUser: '', mqttPassword: '', mqttTls: true, mqttTlsInsecure: true, mqttPrefix: 'ambientika' });
                check('TLS, self-signed certificates accepted: connected and reading', await until(async () => (await d.val('T1.temperature')) === 20.5, 20000), adapter.problems());
                await restartAdapter({ mqttTlsInsecure: false });
                await wait(7000);
                const probs = adapter.problems();
                check('TLS, self-signed not accepted: no connection', (await d.val('info.connection')) === false && adapter.running);
                check('the warning names the certificate', probs.length >= 1 && probs.length <= 2 && /self[- ]signed|certificate/i.test(probs[0].text), probs);
                await restartAdapter({ mqttTls: false });
                await wait(7000);
                check('plain connection to a TLS port: no connection, no crash, no flood', (await d.val('info.connection')) === false && adapter.running && adapter.problems().length <= 2, adapter.problems());
                await spy.stop();
                await broker.down();
                verdict();
            });

            it('topic prefixes', async function () {
                this.timeout(180000);
                groupName = 'settings/prefix';
                const broker = await sim.Broker.start();
                const spy = await sim.Spy.start(broker);
                const bridge = sim.Bridge.start(cloud, broker, { MQTT_TOPIC_PREFIX: 'haus/lueftung', DISCOVERY_PREFIX: 'ha/disc' });
                started.push(broker, spy, bridge);
                await spy.pub('ambientika/WRONG/state', JSON.stringify({ temperature: 1 }), true);
                await restartAdapter({ mqttHost: ' mqtt://127.0.0.1/ ', mqttPort: String(broker.port), mqttTls: false, mqttTlsInsecure: false, mqttPrefix: ' /haus/lueftung/ ', discoveryPrefix: 'ha/disc/' });
                check('host pasted as mqtt://…/, port as text, prefix with several levels typed with spaces and slashes around it: units online', await until(async () => (await d.val(`${MASTER}.online`)) === true, 40000), adapter.problems());
                check('names from the own discovery prefix', await until(async () => ((await d.obj(MASTER)) || { common: {} }).common.name === 'Flur', 10000), (await d.obj(MASTER) || {}).common);
                check('nothing is read from another prefix', !(await d.obj('WRONG')));
                await cloud.clear();
                await d.set(`${MASTER2}.operating_mode`, 'Off');
                check('command goes out under the own prefix and reaches the cloud', await until(async () => (await cloud.log('change-mode')).length, 10000),
                    spy.msgs.filter(m => m.topic.includes('/set')).slice(-2));
                check('info.bridge follows the bridge under the own prefix', (await d.val('info.bridge')) === true);

                // no discovery
                await restartAdapter({ discoveryPrefix: '' });
                await until(async () => (await d.val('info.connection')) === true, 20000);
                await spy.pub('haus/lueftung/NEU1/state', JSON.stringify({ temperature: 20 }), true);
                check('without discovery prefix a new unit gets a default name', await until(async () => ((await d.obj('NEU1')) || { common: {} }).common.name === 'Ambientika NEU1', 10000),
                    (await d.obj('NEU1') || {}).common);
                check('known names are kept', ((await d.obj(MASTER)) || { common: {} }).common.name === 'Flur');

                // wildcards are not a prefix
                await restartAdapter({ mqttPrefix: 'haus/+', discoveryPrefix: 'homeassistant' });
                await wait(4000);
                const probs = adapter.problems();
                check('a prefix with + or # is refused with a clear message, adapter keeps running', adapter.running && probs.length === 1 && /prefix/i.test(probs[0].text), probs);
                check('and nothing is subscribed', (await d.val('info.connection')) === false);

                // empty settings fall back to the defaults
                await restartAdapter({ mqttHost: '', mqttPort: 'abc', mqttPrefix: '', discoveryPrefix: 'homeassistant' });
                await wait(3000);
                check('empty host / port / prefix fall back to localhost:1883 and ambientika without a crash', adapter.running
                    && adapter.lines().some(l => /mqtt:\/\/localhost:1883/.test(l.text) && /"ambientika"/.test(l.text)), adapter.lines().slice(0, 4));
                await bridge.stop();
                await spy.stop();
                await broker.down();
                verdict();
            });
        });
    },
});

after(function () {
    const failed = RESULTS.filter(r => !r.ok);
    console.log(`\nSYSTEM TEST: ${RESULTS.length} checks, ${failed.length ? `FAILED ${failed.length}` : 'ALL PASS'}`);
    for (const f of failed) console.log(`  FAIL [${f.group}] ${f.name}`);
    if (process.env.SIM_RESULT) fs.writeFileSync(process.env.SIM_RESULT, JSON.stringify(RESULTS, null, 1));
    console.log(`logs: ${sim.WORK}`);
});
