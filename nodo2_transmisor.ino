#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>

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
#define MOSFET_PIN     23   // PWM+ del XY-MOS
#define COND_EXC_PIN   25   // excitación conductividad
#define ONE_WIRE_BUS   4    // DS18B20

// =========================
// Objetos
// =========================
Adafruit_ADS1115 ads;
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature ds18b20(&oneWire);

// =========================
// Tiempos
// =========================
unsigned long lastMsg = 0;
const unsigned long intervaloEnvio = 10000;      // 10 s para prueba
const uint32_t TIEMPO_ESTABILIZACION_MS = 10000; // puedes subirlo a 15000 o 20000

// =========================
// Muestreo
// =========================
const uint16_t NUM_MUESTRAS_PH          = 20;
const uint16_t NUM_MUESTRAS_TURB        = 100;
const uint16_t NUM_MUESTRAS_COND        = 20;
const uint16_t DELAY_ENTRE_MUESTRAS_MS  = 20;

// =========================
// Conversión ADS1115
// GAIN_ONE => ±4.096V => 0.125mV/bit
// =========================
float adcToVoltage(int16_t raw) {
  return raw * 0.125f / 1000.0f;
}

// =========================
// Función para redondear
// =========================
float round_to_dp(float in_value, int decimal_place) {
  float multiplier = powf(10.0f, decimal_place);
  in_value = roundf(in_value * multiplier) / multiplier;
  return in_value;
}

// =========================
// Lectura promediada canal ADS
// =========================
float leerVoltajePromediado(uint8_t canal, uint16_t muestras, uint16_t delayMs) {
  long suma = 0;

  for (uint16_t i = 0; i < muestras; i++) {
    int16_t lectura = 0;

    switch (canal) {
      case 0: lectura = ads.readADC_SingleEnded(0); break;
      case 1: lectura = ads.readADC_SingleEnded(1); break;
      case 2: lectura = ads.readADC_SingleEnded(2); break;
      case 3: lectura = ads.readADC_SingleEnded(3); break;
      default: lectura = 0; break;
    }

    suma += lectura;
    delay(delayMs);
  }

  float promedioRaw = (float)suma / muestras;
  return adcToVoltage((int16_t)promedioRaw);
}

// =========================
// pH adaptado de tu código viejo
// Arduino Nano:
// ph = (-0.029710144 * ADC) + 21.63492754
//
// ADC = (V/5)*1023
// => pH = -6.0788954624 * V + 21.63492754
// =========================
float leer_ph() {
  float voltajePH = leerVoltajePromediado(0, NUM_MUESTRAS_PH, DELAY_ENTRE_MUESTRAS_MS);
  float ph = (-6.0788954624f * voltajePH) + 21.63492754f;
  return ph;
}

// =========================
// Temperatura DS18B20
// =========================
float leer_temperatura() {
  ds18b20.requestTemperatures();
  float temperatura = ds18b20.getTempCByIndex(0);
  return temperatura;
}

// =========================
// Turbidez adaptada de tu código viejo
// Antes el Arduino leía directamente 0-5V
// Ahora el ADS lee un voltaje reducido por divisor:
//
// V_A1 = V_sensor * (270 / 370)
// V_sensor = V_A1 * (370 / 270) = V_A1 * 1.37037
// =========================
float leer_turbidez() {
  float voltajeADS = leerVoltajePromediado(1, NUM_MUESTRAS_TURB, DELAY_ENTRE_MUESTRAS_MS);

  // Recuperar voltaje original del sensor antes del divisor
  float voltajeSensor = voltajeADS * 1.37037f;

  // Igual que tu código anterior
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
// Conductividad adaptada de tu código viejo
// Antes:
// cond = analogRead(A2)*(5.0/1023.0)
// conducti = ((11467/420)*cond) - (50573/525)
//
// Ahora:
// GPIO25 excita con 3.3V, no 5V
// hacemos equivalencia aproximada a 5V:
// V_eq = V_ads * (5.0 / 3.3)
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
  float voltajeADS = adcToVoltage((int16_t)promedioRaw);

  // Adaptación a sistema anterior de 5V
  float cond = voltajeADS * (5.0f / 3.3f);

  float conducti;
  if (cond <= 0) {
    conducti = cond;
  } else {
    conducti = ((11467.0f / 420.0f) * cond) - (50573.0f / 525.0f);
  }

  return conducti;
}

// =========================
// WiFi
// =========================
void setup_wifi() {
  delay(10);
  Serial.println();
  Serial.print("Conectando a WiFi: ");
  Serial.println(ssid);

  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi conectado");
  Serial.print("IP ESP32: ");
  Serial.println(WiFi.localIP());
}

// =========================
// MQTT
// =========================
void reconnect() {
  while (!client.connected()) {
    Serial.print("Conectando a MQTT...");
    String clientId = "ESP32-Nodo2-";
    clientId += String(random(0xffff), HEX);

    if (client.connect(clientId.c_str(), mqtt_user, mqtt_password)) {
      Serial.println(" conectado");
    } else {
      Serial.print(" fallo, rc=");
      Serial.print(client.state());
      Serial.println(" intentando de nuevo en 5 segundos");
      delay(5000);
    }
  }
}

// =========================
// Setup
// =========================
void setup() {
  Serial.begin(115200);
  delay(1000);

  randomSeed(micros());

  pinMode(MOSFET_PIN, OUTPUT);
  pinMode(COND_EXC_PIN, OUTPUT);

  digitalWrite(MOSFET_PIN, LOW);
  digitalWrite(COND_EXC_PIN, LOW);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);

  if (!ads.begin()) {
    Serial.println("ERROR: No se detecta ADS1115. Revisa conexiones.");
    while (1) delay(100);
  }

  ads.setGain(GAIN_ONE);
  ds18b20.begin();

  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);

  Serial.println("Sistema listo.");
}

// =========================
// Loop
// =========================
void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  unsigned long now = millis();
  if (now - lastMsg > intervaloEnvio) {
    lastMsg = now;

    Serial.println("\n====================================");
    Serial.println("Iniciando medicion nodo 2...");

    // Encender sensores controlados por MOSFET
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
    payload += "\"sensor_id\":\"nodo2\",";
    payload += "\"ph\":" + String(ph, 2) + ",";
    payload += "\"turbidez\":" + String(turbidez, 2) + ",";
    payload += "\"temperatura\":" + String(temperatura, 2) + ",";
    payload += "\"conductividad\":" + String(conductividad, 2);
    payload += "}";

    Serial.println("Publicando nodo 2:");
    Serial.println(payload);

    client.publish(mqtt_topic, payload.c_str());
  }
}
