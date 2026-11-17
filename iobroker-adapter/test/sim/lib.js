'use strict';

/**
 * Helpers for the system test: real processes around the adapter.
 *
 *   Cloud   sim/fake_cloud.py, a local stand-in for the Ambientika cloud (HTTP)
 *   Broker  a real Mosquitto
 *   Bridge  the real bridge.py of this repository
 *   Spy     an MQTT client that records everything on the broker
 *   AdapterProc  the adapter inside the js-controller test folder, with its log
 */

const cp = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const mqtt = require('mqtt');

const REPO = path.join(__dirname, '..', '..', '..');
const PYTHON = process.env.SIM_PYTHON || 'python3';
const MOSQUITTO = process.env.SIM_MOSQUITTO || 'mosquitto'; // another path, e.g. an unconfined copy
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'ambientika-iob-sim-'));
fs.chmodSync(WORK, 0o755); // mosquitto drops its privileges and still has to read its files here

const USER = 'kunde@example.com';
const PASSWORD = 'geheim#1 äöü';
const POLL = 2;
const MASTER = 'A0B1C2D3E402';
const SLAVE = 'A0B1C2D3E401';
const SLAVE2 = 'A0B1C2D3E403';
const SLAVE3 = 'A0B1C2D3E405';
const MASTER2 = 'A0B1C2D3E404';
const SERIALS = [MASTER, SLAVE, SLAVE2, SLAVE3, MASTER2];
const NAMES = { [MASTER]: 'Flur', [SLAVE]: 'Wohnzimmer', [SLAVE2]: 'Kinderzimmer', [SLAVE3]: 'Gäste-Bad', [MASTER2]: 'Schlafzimmer Eltern' };

function wait(ms) {
    return new Promise(r => setTimeout(r, ms));
}

/** Polls fn until it returns something truthy; null after the timeout. */
async function until(fn, timeoutMs = 20000, stepMs = 150) {
    const end = Date.now() + timeoutMs;
    for (;;) {
        let v;
        try {
            v = await fn();
        } catch (e) {
            v = null;
        }
        if (v) return v;
        if (Date.now() > end) return null;
        await wait(stepMs);
    }
}

function freePort() {
    return new Promise((resolve, reject) => {
        const s = net.createServer();
        s.on('error', reject);
        s.listen(0, '127.0.0.1', () => {
            const p = s.address().port;
            s.close(() => resolve(p));
        });
    });
}

function portOpen(port) {
    return new Promise(resolve => {
        const s = net.connect(port, '127.0.0.1');
        s.setTimeout(300);
        s.on('connect', () => { s.destroy(); resolve(true); });
        s.on('error', () => resolve(false));
        s.on('timeout', () => { s.destroy(); resolve(false); });
    });
}

let procNo = 0;

class Proc {
    constructor(name, cmd, args, opts = {}) {
        this.name = name;
        this.file = path.join(WORK, `${name}-${++procNo}.log`);
        this.out = fs.openSync(this.file, 'w');
        this.exit = null;
        this.p = cp.spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, ...(opts.env || {}) }, stdio: ['ignore', this.out, this.out] });
        this.done = new Promise(resolve => {
            this.p.on('exit', (code, signal) => {
                this.exit = code !== null ? code : signal;
                resolve(this.exit);
            });
            this.p.on('error', err => {
                this.exit = `spawn failed: ${err.message}`;
                resolve(this.exit);
            });
        });
    }

    get running() {
        return this.exit === null;
    }

    log() {
        try {
            return fs.readFileSync(this.file, 'utf8');
        } catch (e) {
            return '';
        }
    }

    async stop(signal = 'SIGTERM', timeoutMs = 15000) {
        if (this.running) {
            this.p.kill(signal);
            await Promise.race([this.done, wait(timeoutMs)]);
            if (this.running) {
                this.p.kill('SIGKILL');
                await this.done;
            }
        }
        return this.exit;
    }
}

class Cloud {
    static async start() {
        const c = new Cloud();
        c.port = await freePort();
        c.url = `http://127.0.0.1:${c.port}`;
        c.proc = new Proc('cloud', PYTHON, [path.join(REPO, 'sim', 'fake_cloud.py'), String(c.port)]);
        if (!(await until(() => c.state(), 20000))) throw new Error(`cloud stand-in did not start: ${c.proc.log()}`);
        return c;
    }

    req(method, p, body) {
        return new Promise((resolve, reject) => {
            const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
            const r = http.request(this.url + p, { method, headers: data ? { 'Content-Type': 'application/json', 'Content-Length': data.length } : {}, timeout: 5000 }, res => {
                const chunks = [];
                res.on('data', d => chunks.push(d));
                res.on('end', () => {
                    try {
                        resolve(JSON.parse(Buffer.concat(chunks).toString() || 'null'));
                    } catch (e) {
                        reject(e);
                    }
                });
            });
            r.on('error', reject);
            r.on('timeout', () => r.destroy(new Error('timeout')));
            if (data) r.write(data);
            r.end();
        });
    }

    state() { return this.req('GET', '/_ctl/state'); }
    async log(kind) { return (await this.req('GET', '/_ctl/log')).filter(r => !kind || r.kind === kind); }
    clear() { return this.req('POST', '/_ctl/clear', {}); }
    unit(serial, set, fault) {
        const body = { serial };
        if (set) body.set = set;
        if (fault !== undefined) body.fault = fault;
        return this.req('POST', '/_ctl/unit', body);
    }
    outage(mode) { return this.req('POST', '/_ctl/outage', { mode }); }
    stop() { return this.proc.stop('SIGKILL'); }
}

class Broker {
    /** opts: { user, password, tls } */
    static async start(opts = {}) {
        const b = new Broker();
        b.port = opts.port || (await freePort());
        b.user = opts.user;
        b.password = opts.password;
        b.tls = !!opts.tls;
        const lines = [`listener ${b.port} 127.0.0.1`, 'persistence false'];
        if (b.user) {
            const pw = path.join(WORK, `passwd-${b.port}`);
            cp.execFileSync('mosquitto_passwd', ['-b', '-c', pw, b.user, b.password], { stdio: 'ignore' });
            fs.chmodSync(pw, 0o644);
            lines.push('allow_anonymous false', `password_file ${pw}`);
        } else {
            lines.push('allow_anonymous true');
        }
        if (b.tls) {
            const key = path.join(WORK, `tls-${b.port}.key`);
            const crt = path.join(WORK, `tls-${b.port}.crt`);
            cp.execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', crt, '-days', '2',
                '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], { stdio: 'ignore' });
            fs.chmodSync(key, 0o644);
            lines.push(`certfile ${crt}`, `keyfile ${key}`);
            b.cert = crt;
        }
        b.conf = path.join(WORK, `mosquitto-${b.port}.conf`);
        fs.writeFileSync(b.conf, `${lines.join('\n')}\n`);
        await b.up();
        return b;
    }

    async up() {
        this.proc = new Proc('mosquitto', MOSQUITTO, ['-c', this.conf]);
        if (!(await until(() => portOpen(this.port), 10000))) throw new Error(`mosquitto did not start: ${this.proc.log()}`);
    }

    down() {
        return this.proc.stop('SIGKILL');
    }
}

class Spy {
    static async start(broker) {
        const s = new Spy();
        s.msgs = [];
        const opts = { clientId: `spy-${Math.random().toString(16).slice(2, 8)}`, reconnectPeriod: 500 };
        if (broker.user) Object.assign(opts, { username: broker.user, password: broker.password });
        if (broker.tls) opts.rejectUnauthorized = false;
        s.c = mqtt.connect(`${broker.tls ? 'mqtts' : 'mqtt'}://127.0.0.1:${broker.port}`, opts);
        s.c.on('connect', () => s.c.subscribe('#'));
        s.c.on('error', () => {});
        s.c.on('message', (topic, payload, packet) => {
            s.msgs.push({ t: Date.now(), topic, payload: payload.toString(), retain: !!packet.retain });
        });
        await until(() => s.c.connected, 10000);
        return s;
    }

    all(topic, since = 0) {
        return this.msgs.filter(m => (!topic || m.topic === topic) && m.t >= since);
    }

    last(topic, since = 0) {
        const rows = this.all(topic, since);
        return rows.length ? rows[rows.length - 1].payload : null;
    }

    json(topic, since = 0) {
        try {
            return JSON.parse(this.last(topic, since));
        } catch (e) {
            return null;
        }
    }

    pub(topic, payload, retain = false) {
        return new Promise(resolve => this.c.publish(topic, payload, { qos: 1, retain }, () => resolve()));
    }

    stop() {
        return new Promise(resolve => this.c.end(true, {}, () => resolve()));
    }
}

class Bridge {
    static start(cloud, broker, env = {}) {
        const b = new Bridge();
        b.dir = fs.mkdtempSync(path.join(WORK, 'bridge-'));
        const full = {
            AMBIENTIKA_USERNAME: USER, AMBIENTIKA_PASSWORD: PASSWORD, AMBIENTIKA_HOST: cloud.url,
            MQTT_HOST: '127.0.0.1', MQTT_PORT: String(broker.port), POLL_INTERVAL: String(POLL),
            NEURACELL_STATE_FILE: path.join(b.dir, 'nc.json'), PENDING_RESET_FILE: path.join(b.dir, 'pr.json'),
            FILTER_ACK_PATH: path.join(b.dir, 'ack.json'), CONFIG_YAML: path.join(b.dir, 'none.yaml'),
            BRIDGE_VERSION: 'sim', ...env,
        };
        if (broker.user) Object.assign(full, { MQTT_USERNAME: broker.user, MQTT_PASSWORD: broker.password });
        b.proc = new Proc('bridge', PYTHON, [path.join(REPO, 'bridge.py')], { env: full, cwd: b.dir });
        return b;
    }

    log() { return this.proc.log(); }
    get running() { return this.proc.running; }
    stop(signal) { return this.proc.stop(signal); }
}

/** The adapter as js-controller starts it, with its console log captured. */
class AdapterProc {
    constructor(testDir, harness) {
        this.dir = path.join(testDir, 'node_modules', 'iobroker.ambientika');
        this.harness = harness;
        this.proc = null;
    }

    async start(waitAlive = true) {
        const alive = 'system.adapter.ambientika.0.alive';
        try {
            // js-controller waits before it restarts an instance; here the flag of the last run is cleared instead.
            const s = await this.harness.states.getStateAsync(alive);
            if (s && s.val) await this.harness.states.setStateAsync(alive, { val: false, ack: true, from: 'system.host.testing' });
        } catch (e) {
            // first start
        }
        const t0 = Date.now();
        this.proc = new Proc('adapter', process.execPath, ['main.js', '--console'], { cwd: this.dir });
        if (waitAlive) {
            const ok = await until(async () => {
                if (!this.proc.running) throw new Error('exited');
                const s = await this.harness.states.getStateAsync(alive);
                return s && s.val === true && s.ts >= t0 - 1000;
            }, 30000);
            if (!ok) throw new Error(`adapter did not start (exit ${this.proc.exit}):\n${this.proc.log().slice(-2000)}`);
        }
        return this;
    }

    get running() {
        return !!this.proc && this.proc.running;
    }

    /** Log lines of the adapter: [{level, text}] */
    lines() {
        if (!this.proc) return [];
        const out = [];
        // eslint-disable-next-line no-control-regex
        for (const raw of this.proc.log().replace(/\u001b\[[0-9;]*m/g, '').split('\n')) {
            const m = /- (silly|debug|info|warn|error): ambientika\.0 \(\d+\) (.*)$/.exec(raw);
            if (m) out.push({ level: m[1], text: m[2] });
            else if (raw.trim() && out.length && !/^\d{4}-\d\d-\d\d /.test(raw)) out[out.length - 1].text += `\n${raw}`;
            else if (raw.trim() && !/^\d{4}-\d\d-\d\d /.test(raw)) out.push({ level: 'raw', text: raw });
        }
        return out;
    }

    problems() {
        return this.lines().filter(l => l.level === 'warn' || l.level === 'error' || l.level === 'raw');
    }

    /** Stops the adapter the way js-controller does. */
    async stop() {
        if (!this.running) return this.proc ? this.proc.exit : null;
        try {
            await this.harness.states.setStateAsync('system.adapter.ambientika.0.sigKill', { val: -1, from: 'system.host.testing' });
        } catch (e) {
            // database already closed
        }
        await Promise.race([this.proc.done, wait(8000)]);
        if (this.proc.running) await this.proc.stop('SIGKILL');
        return this.proc.exit;
    }
}

module.exports = {
    REPO, WORK, USER, PASSWORD, POLL, MASTER, SLAVE, SLAVE2, SLAVE3, MASTER2, SERIALS, NAMES,
    wait, until, freePort, portOpen, Proc, Cloud, Broker, Spy, Bridge, AdapterProc,
};
