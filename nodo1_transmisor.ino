#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>
#include "esp_sleep.h"

// =========================
// WiFi / MQTT
// =========================
const char* ssid = "CNT Normita";
const char* password = "Mercedes@25";

const char* mqtt_server = "192.168.1.25";
const int mqtt_port = 1883;
const char* mqtt_user = "tesis";
const char* mqtt_password = "tesis123";

const char* mqtt_topic = "ptap/carrizal/datos";

WiFiClient espClient;
PubSubClient client(espClient);

// =========================
// Pines
// =========================
#define SDA_PIN        21
#define SCL_PIN        22
#define MOSFET_PIN     23
#define COND_EXC_PIN   25
#define ONE_WIRE_BUS   4

// =========================
// Objetos
// =========================
Adafruit_ADS1115 ads;
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature ds18b20(&oneWire);

// =========================
// Deep sleep
// =========================
#define uS_TO_S_FACTOR 1000000ULL
#define TIEMPO_SLEEP_SEGUNDOS 600   // 10 minutos

// =========================
// Tiempos de medición
// =========================
const uint32_t TIEMPO_ESTABILIZACION_MS = 10000; // 10 s
const uint16_t NUM_MUESTRAS_PH          = 20;
const uint16_t NUM_MUESTRAS_TURB        = 100;
const uint16_t NUM_MUESTRAS_COND        = 20;
const uint16_t DELAY_ENTRE_MUESTRAS_MS  = 20;

// =========================
// Conversión ADS1115
// =========================
float adcToVoltage(int16_t raw) {
  return raw * 0.125f / 1000.0f;
}

float round_to_dp(float in_value, int decimal_place) {
  float multiplier = powf(10.0f, decimal_place);
  in_value = roundf(in_value * multiplier) / multiplier;
  return in_value;
}

float leerVoltajePromediado(uint8_t canal, uint16_t muestras, uint16_t delayMs) {
  long suma = 0;

  for (uint16_t i = 0; i < muestras; i++) {
    int16_t lectura = 0;

    switch (canal) {
      case 0: lectura = ads.readADC_SingleEnded(0); break;
      case 1: lectura = ads.readADC_SingleEnded(1); break;
      case 2: lectura = ads.readADC_SingleEnded(2); break;
      case 3: lectura = ads.readADC_SingleEnded(3); break;
    }

    suma += lectura;
    delay(delayMs);
  }

  float promedioRaw = (float)suma / muestras;
  return adcToVoltage((int16_t)promedioRaw);
}

// =========================
// pH corregido
// =========================
float leer_ph() {
  float voltajePH = leerVoltajePromediado(0, NUM_MUESTRAS_PH, DELAY_ENTRE_MUESTRAS_MS);
  float ph = (-6.0788954624f * voltajePH) + 7.02492754f;
  return ph;
}

// =========================
// Temperatura
// =========================
float leer_temperatura() {
  ds18b20.requestTemperatures();
  return ds18b20.getTempCByIndex(0);
}

// =========================
// Turbidez
// =========================
float leer_turbidez() {
  float voltajeADS = leerVoltajePromediado(1, NUM_MUESTRAS_TURB, DELAY_ENTRE_MUESTRAS_MS);

  // Recuperar voltaje original del sensor antes del divisor
  float voltajeSensor = voltajeADS * 1.37037f;
  voltajeSensor = round_to_dp(voltajeSensor, 1);

  float ntu;
  if (voltajeSensor < 2.5f) {
    ntu = 2.0f;
  } else {
    ntu = ((-1120.4f * pow(voltajeSensor, 2)) + (5742.3f * voltajeSensor) - 4353.8f) / 1000.0f - 1.0f;
  }

  return ntu;
}

// =========================
// Conductividad corregida
// =========================
float leer_conductividad() {
  long suma = 0;

  for (uint16_t i = 0; i < NUM_MUESTRAS_COND; i++) {
    digitalWrite(COND_EXC_PIN, HIGH);
    delay(10);

    int16_t lectura = ads.readADC_SingleEnded(2);
    suma += lectura;

    digitalWrite(COND_EXC_PIN, LOW);
    delay(50);
  }

  float promedioRaw = (float)suma / NUM_MUESTRAS_COND;
  float voltajeCond = adcToVoltage((int16_t)promedioRaw);

  float conductividad = (0.9759f * voltajeCond) - 0.002f;

  if (conductividad < 0) conductividad = 0;

  return conductividad;
}

// =========================
// WiFi
// =========================
void setup_wifi() {
  Serial.println();
  Serial.print("Conectando a WiFi: ");
  Serial.println(ssid);

  WiFi.begin(ssid, password);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi conectado");
    Serial.print("IP ESP32: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nNo se pudo conectar a WiFi");
  }
}

// =========================
// MQTT
// =========================
bool reconnectMQTT() {
  if (client.connected()) return true;

  Serial.print("Conectando a MQTT...");
  String clientId = "ESP32-Nodo1-";
  clientId += String(random(0xffff), HEX);

  if (client.connect(clientId.c_str(), mqtt_user, mqtt_password)) {
    Serial.println(" conectado");
    return true;
  } else {
    Serial.print(" fallo, rc=");
    Serial.println(client.state());
    return false;
  }
}

// =========================
// Setup
// =========================
void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(MOSFET_PIN, OUTPUT);
  pinMode(COND_EXC_PIN, OUTPUT);

  digitalWrite(MOSFET_PIN, LOW);
  digitalWrite(COND_EXC_PIN, LOW);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);

  if (!ads.begin()) {
    Serial.println("ERROR: No se detecta ADS1115.");
    while (1) delay(100);
  }

  ads.setGain(GAIN_ONE);
  ds18b20.begin();

  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);

  // =========================
  // Medición
  // =========================
  Serial.println("\nIniciando medicion nodo 1...");

  digitalWrite(MOSFET_PIN, HIGH);
  Serial.println("MOSFET ON - Sensores energizados");

  delay(TIEMPO_ESTABILIZACION_MS);

  float ph = leer_ph();
  float temperatura = leer_temperatura();
  float turbidez = leer_turbidez();
  float conductividad = leer_conductividad();

  digitalWrite(MOSFET_PIN, LOW);
  Serial.println("MOSFET OFF - Sensores apagados");

  String payload = "{";
  payload += "\"sensor_id\":\"nodo1\",";
  payload += "\"ph\":" + String(ph, 2) + ",";
  payload += "\"turbidez\":" + String(turbidez, 2) + ",";
  payload += "\"temperatura\":" + String(temperatura, 2) + ",";
  payload += "\"conductividad\":" + String(conductividad, 3);
  payload += "}";

  Serial.println("Publicando nodo 1:");
  Serial.println(payload);

  if (WiFi.status() == WL_CONNECTED && reconnectMQTT()) {
    client.publish(mqtt_topic, payload.c_str());
    delay(500);
    client.disconnect();
  }

  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  // =========================
  // Deep sleep real
  // =========================
  Serial.println("Entrando en deep-sleep por 10 segundos...");
  esp_sleep_enable_timer_wakeup(TIEMPO_SLEEP_SEGUNDOS * uS_TO_S_FACTOR);
  esp_deep_sleep_start();
}

void loop() {
  // nunca llega aquí
}
