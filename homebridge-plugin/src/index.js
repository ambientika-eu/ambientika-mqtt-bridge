'use strict';

/**
 * Homebridge plugin for Ambientika Ventilation Units  —  powered by NeuraCell-X(R)
 *
 * Exposes each Ambientika unit as a HomeKit Air Purifier (+ humidity, temperature,
 * air-quality and filter sensors), and surfaces the status of NeuraCell-X(R) — the
 * patented AI Neural Control System — as a dedicated accessory with occupancy
 * sensors for "Radon Protection Active" and "Dew-Point Ventilation Block".
 * (Radon alarm -> supply/Intake at Stufe 1; dew point "not ideal" -> Off; radon
 * has priority.)
 *
 * NeuraCell-X(R) and PhaseCell-X(R) are registered trademarks. Patent pending.
 * GitHub: https://github.com/ambientika-eu/ambientika-mqtt-bridge
 */

const mqtt = require('mqtt');

const PLUGIN_NAME = 'homebridge-ambientika';
const PLATFORM_NAME = 'AmbientikaPlugin';
const NEURACELL_KEY = '__neuracell__';

module.exports = (api) => {
  api.registerPlatform(PLUGIN_NAME, PLATFORM_NAME, AmbientikaPlugin);
};

class AmbientikaPlugin {
  constructor(log, config, api) {
    this.log = log;
    this.config = config;
    this.api = api;
    this.accessories = new Map();
    this.mqttClient = null;

    this.mqttHost = config.mqttHost || 'localhost';
    this.mqttPort = config.mqttPort || 1883;
    this.mqttUsername = config.mqttUsername || null;
    this.mqttPassword = config.mqttPassword || null;
    this.topicPrefix = config.topicPrefix || 'ambientika';

    if (api) {
      api.on('didFinishLaunching', () => {
        this.connectMQTT();
      });
    }
  }

  connectMQTT() {
    const options = {
      host: this.mqttHost,
      port: this.mqttPort,
      clientId: 'homebridge-ambientika-' + Math.random().toString(16).slice(2),
    };
    if (this.mqttUsername) options.username = this.mqttUsername;
    if (this.mqttPassword) options.password = this.mqttPassword;

    this.mqttClient = mqtt.connect(options);

    this.mqttClient.on('connect', () => {
      this.log.info('Connected to MQTT broker at ' + this.mqttHost + ':' + this.mqttPort +
                    ' — powered by NeuraCell-X® (patented)');
      // '+/state' also matches 'neuracell/state'.
      this.mqttClient.subscribe(this.topicPrefix + '/+/state', { qos: 1 });
      this.log.info('Subscribed to ' + this.topicPrefix + '/+/state');
    });

    this.mqttClient.on('message', (topic, message) => {
      const parts = topic.split('/');
      if (parts.length >= 3 && parts[2] === 'state') {
        // NeuraCell-X® status: <prefix>/neuracell/state
        if (parts[1] === 'neuracell') {
          try {
            this.updateNeuraCell(JSON.parse(message.toString()));
          } catch (e) {
            this.log.warn('Failed to parse NeuraCell-X state: ' + e.message);
          }
          return;
        }
        const serial = parts[1];
        try {
          const state = JSON.parse(message.toString());
          this.updateAccessory(serial, state);
        } catch (e) {
          this.log.warn('Failed to parse state message for ' + serial + ': ' + e.message);
        }
      }
    });

    this.mqttClient.on('error', (err) => {
      this.log.error('MQTT error: ' + err.message);
    });

    this.mqttClient.on('reconnect', () => {
      this.log.info('Reconnecting to MQTT broker...');
    });
  }

  updateAccessory(serial, state) {
    let accessory = this.accessories.get(serial);

    if (!accessory) {
      this.log.info('Discovered new Ambientika device: ' + serial);
      const uuid = this.api.hap.uuid.generate(serial);
      accessory = new this.api.platformAccessory(
        'Ambientika ' + serial,
        uuid,
        this.api.hap.Categories.AIR_PURIFIER
      );
      accessory.context.serial = serial;
      this.setupServices(accessory, serial);
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [accessory]);
      this.accessories.set(serial, accessory);
    }

    this.updateServices(accessory, state, serial);
  }

  // -------------------------------------------------------------------------
  // NeuraCell-X(R) status accessory
  // -------------------------------------------------------------------------
  updateNeuraCell(state) {
    const Characteristic = this.api.hap.Characteristic;
    const Service = this.api.hap.Service;

    let accessory = this.accessories.get(NEURACELL_KEY);
    if (!accessory) {
      this.log.info('Creating NeuraCell-X® status accessory');
      const uuid = this.api.hap.uuid.generate('ambientika-neuracell-x');
      accessory = new this.api.platformAccessory(
        'NeuraCell-X',
        uuid,
        this.api.hap.Categories.SENSOR
      );
      accessory.context.serial = NEURACELL_KEY;

      accessory.addService(Service.OccupancySensor, 'Radon Protection', 'radon');
      accessory.addService(Service.OccupancySensor, 'Dew-Point Block', 'dewpoint');

      const info = accessory.getService(Service.AccessoryInformation);
      info
        .setCharacteristic(Characteristic.Manufacturer, 'Ambientika / SUEDWIND')
        .setCharacteristic(Characteristic.Model, 'NeuraCell-X® AI Neural Control System (patented)')
        .setCharacteristic(Characteristic.SerialNumber, 'neuracell-x');

      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [accessory]);
      this.accessories.set(NEURACELL_KEY, accessory);
    }

    const radonSvc = accessory.getServiceById(Service.OccupancySensor, 'radon');
    if (radonSvc && state.radon_protection !== undefined) {
      radonSvc.updateCharacteristic(
        Characteristic.OccupancyDetected, state.radon_protection ? 1 : 0);
    }
    const dewSvc = accessory.getServiceById(Service.OccupancySensor, 'dewpoint');
    if (dewSvc && state.dewpoint_block !== undefined) {
      dewSvc.updateCharacteristic(
        Characteristic.OccupancyDetected, state.dewpoint_block ? 1 : 0);
    }
    this.log.debug('NeuraCell-X update: radon_protection=' + state.radon_protection +
                   ' dewpoint_block=' + state.dewpoint_block +
                   ' override=' + state.override_active);
  }

  setupServices(accessory, serial) {
    const Characteristic = this.api.hap.Characteristic;
    const Service = this.api.hap.Service;

    // Air Purifier Service (main control)
    let purifierService = accessory.getService(Service.AirPurifier);
    if (!purifierService) {
      purifierService = accessory.addService(Service.AirPurifier, 'Ventilation');
    }

    purifierService.getCharacteristic(Characteristic.Active)
      .onSet((value) => {
        const mode = value ? 'Auto' : 'Standby';
        this.publishCommand(serial, 'operating_mode', mode);
      });

    purifierService.getCharacteristic(Characteristic.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep: 33 })
      .onSet((value) => {
        let speed = 'Low';
        if (value >= 66) speed = 'High';
        else if (value >= 33) speed = 'Medium';
        this.publishCommand(serial, 'fan_speed', speed);
      });

    // Humidity Sensor
    let humidityService = accessory.getService(Service.HumiditySensor);
    if (!humidityService) {
      humidityService = accessory.addService(Service.HumiditySensor, 'Humidity');
    }

    // Temperature Sensor (Supply Air)
    let tempService = accessory.getService(Service.TemperatureSensor);
    if (!tempService) {
      tempService = accessory.addService(Service.TemperatureSensor, 'Supply Air Temp');
    }

    // Air Quality Sensor
    let aqService = accessory.getService(Service.AirQualitySensor);
    if (!aqService) {
      aqService = accessory.addService(Service.AirQualitySensor, 'Air Quality');
    }

    // Filter Maintenance
    let filterService = accessory.getService(Service.FilterMaintenance);
    if (!filterService) {
      filterService = accessory.addService(Service.FilterMaintenance, 'Filter');
    }

    // Info Service
    const infoService = accessory.getService(Service.AccessoryInformation);
    infoService
      .setCharacteristic(Characteristic.Manufacturer, 'Ambientika / SUEDWIND')
      .setCharacteristic(Characteristic.Model, 'Ambientika Ventilation Unit')
      .setCharacteristic(Characteristic.SerialNumber, serial);
  }

  updateServices(accessory, state, serial) {
    const Characteristic = this.api.hap.Characteristic;
    const Service = this.api.hap.Service;

    // Air Purifier
    const purifierService = accessory.getService(Service.AirPurifier);
    if (purifierService) {
      const isActive = state.operating_mode && state.operating_mode !== 'Standby';
      purifierService.updateCharacteristic(Characteristic.Active, isActive ? 1 : 0);
      purifierService.updateCharacteristic(
        Characteristic.CurrentAirPurifierState,
        isActive ? 2 : 0
      );
      purifierService.updateCharacteristic(
        Characteristic.TargetAirPurifierState,
        state.operating_mode === 'Auto' ? 1 : 0
      );

      // Fan speed: Low=33, Medium=66, High=100
      const speedMap = { 'Low': 33, 'Medium': 66, 'High': 100 };
      const speedVal = speedMap[state.fan_speed] || 33;
      purifierService.updateCharacteristic(Characteristic.RotationSpeed, speedVal);
    }

    // Humidity
    const humidityService = accessory.getService(Service.HumiditySensor);
    if (humidityService && state.humidity !== undefined) {
      humidityService.updateCharacteristic(
        Characteristic.CurrentRelativeHumidity,
        Math.min(100, Math.max(0, state.humidity))
      );
    }

    // Temperature
    const tempService = accessory.getService(Service.TemperatureSensor);
    if (tempService && state.supply_air_temperature !== undefined) {
      tempService.updateCharacteristic(
        Characteristic.CurrentTemperature,
        state.supply_air_temperature
      );
    }

    // Air Quality
    const aqService = accessory.getService(Service.AirQualitySensor);
    if (aqService && state.air_quality !== undefined) {
      let aqLevel = 1; // Excellent
      if (state.air_quality > 80) aqLevel = 5; // Poor
      else if (state.air_quality > 60) aqLevel = 4; // Inferior
      else if (state.air_quality > 40) aqLevel = 3; // Fair
      else if (state.air_quality > 20) aqLevel = 2; // Good
      aqService.updateCharacteristic(Characteristic.AirQuality, aqLevel);
    }

    // Filter
    const filterService = accessory.getService(Service.FilterMaintenance);
    if (filterService && state.filter_alarm !== undefined) {
      filterService.updateCharacteristic(
        Characteristic.FilterChangeIndication,
        state.filter_alarm ? 1 : 0
      );
    }
  }

  publishCommand(serial, property, value) {
    const topic = this.topicPrefix + '/' + serial + '/set/' + property;
    this.mqttClient.publish(topic, String(value), { qos: 1 }, (err) => {
      if (err) {
        this.log.error('Failed to publish command: ' + err.message);
      } else {
        this.log.debug('Command published: ' + topic + ' = ' + value);
      }
    });
  }

  configureAccessory(accessory) {
    this.accessories.set(accessory.context.serial, accessory);
  }
}
