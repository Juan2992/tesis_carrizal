#include <WiFi.h>
#include <PubSubClient.h>

const char* ssid = "CNT Normita";
const char* password = "Mercedes@25";

const char* mqtt_server = "192.168.1.25";
const int mqtt_port = 1883;
const char* mqtt_user = "tesis";
const char* mqtt_password = "tesis123";

const char* mqtt_topic = "ptap/carrizal/datos";

WiFiClient espClient;
PubSubClient client(espClient);

unsigned long lastMsg = 0;

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

void reconnect() {
  while (!client.connected()) {
    Serial.print("Conectando a MQTT...");
    String clientId = "ESP32-Nodo1-";
    clientId += String(random(0xffff), HEX);

    if (client.connect(clientId.c_str(), mqtt_user, mqtt_password)) {
      Serial.println("conectado");
    } else {
      Serial.print("fallo, rc=");
      Serial.print(client.state());
      Serial.println(" intentando de nuevo en 5 segundos");
      delay(5000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  randomSeed(micros());
  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  unsigned long now = millis();
  if (now - lastMsg > 10000) {   // cada 10 segundos para prueba
    lastMsg = now;

    float ph = random(880, 901) / 100.0;             // 8.80 a 9.00
    float turbidez = random(30, 250) / 100.0;        // 0.30 a 2.50
    float temperatura = random(1700, 2901) / 100.0;  // 17.00 a 29.00
    float conductividad = random(250, 900);          // 250 a 900 uS/cm

    String payload = "{";
    payload += "\"sensor_id\":\"nodo1\",";
    payload += "\"ph\":" + String(ph, 2) + ",";
    payload += "\"turbidez\":" + String(turbidez, 2) + ",";
    payload += "\"temperatura\":" + String(temperatura, 2) + ",";
    payload += "\"conductividad\":" + String(conductividad, 2);
    payload += "}";

    Serial.println("Publicando nodo 1:");
    Serial.println(payload);

    client.publish(mqtt_topic, payload.c_str());
  }
}
