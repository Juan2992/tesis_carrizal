import time
import json
import logging
from typing import Dict, Optional

import RPi.GPIO as GPIO
import paho.mqtt.publish as publish
from influxdb_client import InfluxDBClient

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

# ---------- GPIO ----------
RELAY_PIN = 17
RELAY_ACTIVE_LOW = True   # True si el relé activa en LOW

# ---------- InfluxDB ----------
INFLUX_URL = "http://192.168.1.25:8086"
INFLUX_TOKEN = "eVTwanO40S3sbILZ9NfHZZ9n6kDD_khNmc2sf8Oz8qgE9cHmMMrmNG--SQrHPq7er4Wwpde_qz3h87cX-iFBUQ=="
INFLUX_ORG = "ca286a3888bffbba"
INFLUX_BUCKET = "sensores_agua"

MEASUREMENT = "calidad_agua"
NODE_TAG_KEY = "sensor_id"
NODE_TAG_VALUE = "nodo1"

FIELD_TEMPERATURA = "temperatura"
FIELD_PH = "ph"
FIELD_CONDUCTIVIDAD = "conductividad"
FIELD_TURBIDEZ = "turbidez"

# ---------- MQTT ----------
MQTT_BROKER = "192.168.1.25"
MQTT_PORT = 1883
MQTT_TOPIC = "ptap/carrizal/bomba/estado"
MQTT_USERNAME = "tesis"
MQTT_PASSWORD = "tesis123"

# ---------- Control ----------
CLORO_OBJETIVO = 0.50         # mg/L
TIEMPO_BASE = 5.0             # segundos mínimos por ciclo
K_SEGUNDOS_POR_MG_L = 100.0   # ganancia del ajuste
MAX_ON_SECONDS = 60.0         # máximo total por ciclo
MIN_AJUSTE_SECONDS = 3.0      # mínimo ajuste útil cuando sí haga falta
DEADBAND = 0.03               # banda muerta
CYCLE_SECONDS = 300           # nuevo cálculo cada 5 minutos

# ---------- Límites operativos ----------
CLORO_ALTO_CORTE = 1.50       # por norma INEN 1108 el máximo permitido es 1.5 mg/L
PH_MIN = 6.5
PH_MAX = 8.0
TURBIDEZ_MAX_REFERENCIA = 5.0

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ============================================================
# GPIO
# ============================================================

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(RELAY_PIN, GPIO.OUT)

def relay_off() -> None:
    GPIO.output(RELAY_PIN, GPIO.HIGH if RELAY_ACTIVE_LOW else GPIO.LOW)

def relay_on() -> None:
    GPIO.output(RELAY_PIN, GPIO.LOW if RELAY_ACTIVE_LOW else GPIO.HIGH)

relay_off()

# ============================================================
# INFLUXDB
# ============================================================

client = InfluxDBClient(
    url=INFLUX_URL,
    token=INFLUX_TOKEN,
    org=INFLUX_ORG
)
query_api = client.query_api()

def get_latest_fields() -> Optional[Dict[str, float]]:
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -7d)
      |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
      |> filter(fn: (r) => r["{NODE_TAG_KEY}"] == "{NODE_TAG_VALUE}")
      |> filter(fn: (r) =>
          r._field == "{FIELD_TEMPERATURA}" or
          r._field == "{FIELD_PH}" or
          r._field == "{FIELD_CONDUCTIVIDAD}" or
          r._field == "{FIELD_TURBIDEZ}"
      )
      |> group(columns: ["_field"])
      |> last()
    '''

    result = query_api.query(org=INFLUX_ORG, query=flux)

    values: Dict[str, float] = {}

    for table in result:
        for record in table.records:
            field = record.get_field()
            value = record.get_value()
            if value is not None:
                values[field] = float(value)

    required = [
        FIELD_TEMPERATURA,
        FIELD_PH,
        FIELD_CONDUCTIVIDAD,
        FIELD_TURBIDEZ,
    ]

    if not all(field in values for field in required):
        logging.warning("No se encontraron todos los campos requeridos.")
        logging.warning("Valores obtenidos: %s", values)
        return None

    return values

# ============================================================
# MQTT
# ============================================================

def publicar_estado(
    estado_bomba: str,
    tiempo_total: float,
    tiempo_restante: float,
    cloro_estimado: float,
    error: float,
    temperatura: float,
    ph: float,
    turbidez: float,
    conductividad: float,
    tiempo_base: float,
    tiempo_ajuste: float
) -> None:
    payload = {
        "estado_bomba": estado_bomba,
        "tiempo_total": round(tiempo_total, 2),
        "tiempo_restante": round(tiempo_restante, 2),
        "tiempo_base": round(tiempo_base, 2),
        "tiempo_ajuste": round(tiempo_ajuste, 2),
        "cloro_estimado": round(cloro_estimado, 3),
        "error_control": round(error, 3),
        "temperatura": round(temperatura, 2),
        "ph": round(ph, 2),
        "turbidez": round(turbidez, 2),
        "conductividad": round(conductividad, 2)
    }

    publish.single(
        topic=MQTT_TOPIC,
        payload=json.dumps(payload),
        hostname=MQTT_BROKER,
        port=MQTT_PORT,
        auth={
            "username": MQTT_USERNAME,
            "password": MQTT_PASSWORD
        }
    )

# ============================================================
# MODELO DE REGRESIÓN
# ============================================================

def calcular_cloro_estimado(
    temperatura: float,
    ph: float,
    turbidez: float,
    conductividad: float
) -> float:
    return (
        5.524
        + 0.008 * temperatura
        - 0.932 * ph
        - 0.279 * turbidez
        + 0.009 * conductividad
    )

# ============================================================
# CONTROL
# ============================================================

def calcular_error(cloro_estimado: float) -> float:
    return CLORO_OBJETIVO - cloro_estimado

def calcular_tiempo_ajuste(cloro_estimado: float) -> float:
    """
    Calcula solo el tiempo extra de corrección.
    Si el cloro estimado ya está en o por encima del objetivo,
    el ajuste es cero.
    """
    if cloro_estimado >= CLORO_ALTO_CORTE:
        return 0.0

    error = calcular_error(cloro_estimado)

    if error <= DEADBAND:
        return 0.0

    segundos = error * K_SEGUNDOS_POR_MG_L

    if segundos < MIN_AJUSTE_SECONDS:
        segundos = MIN_AJUSTE_SECONDS

    return round(segundos, 2)

def calcular_tiempo_total(cloro_estimado: float, hay_flujo: bool = True) -> tuple[float, float]:
    """
    Devuelve (tiempo_base, tiempo_total).
    Siempre aplica tiempo base si hay flujo.
    """
    tiempo_base = TIEMPO_BASE if hay_flujo else 0.0
    tiempo_ajuste = calcular_tiempo_ajuste(cloro_estimado)

    tiempo_total = tiempo_base + tiempo_ajuste

    if tiempo_total > MAX_ON_SECONDS:
        tiempo_total = MAX_ON_SECONDS

    return round(tiempo_base, 2), round(tiempo_total, 2)

def activar_bomba(
    segundos_totales: float,
    tiempo_base: float,
    cloro_estimado: float,
    error: float,
    temperatura: float,
    ph: float,
    turbidez: float,
    conductividad: float
) -> None:
    tiempo_ajuste = max(0.0, segundos_totales - tiempo_base)

    if segundos_totales <= 0:
        logging.info("Bomba apagada")
        relay_off()
        publicar_estado(
            estado_bomba="apagada",
            tiempo_total=0,
            tiempo_restante=0,
            cloro_estimado=cloro_estimado,
            error=error,
            temperatura=temperatura,
            ph=ph,
            turbidez=turbidez,
            conductividad=conductividad,
            tiempo_base=tiempo_base,
            tiempo_ajuste=tiempo_ajuste
        )
        return

    logging.info("Bomba encendida por %.2f segundos", segundos_totales)
    relay_on()

    tiempo_entero = int(round(segundos_totales))

    for restante in range(tiempo_entero, -1, -1):
        publicar_estado(
            estado_bomba="encendida",
            tiempo_total=segundos_totales,
            tiempo_restante=restante,
            cloro_estimado=cloro_estimado,
            error=error,
            temperatura=temperatura,
            ph=ph,
            turbidez=turbidez,
            conductividad=conductividad,
            tiempo_base=tiempo_base,
            tiempo_ajuste=tiempo_ajuste
        )
        time.sleep(1)

    relay_off()

    publicar_estado(
        estado_bomba="apagada",
        tiempo_total=0,
        tiempo_restante=0,
        cloro_estimado=cloro_estimado,
        error=error,
        temperatura=temperatura,
        ph=ph,
        turbidez=turbidez,
        conductividad=conductividad,
        tiempo_base=tiempo_base,
        tiempo_ajuste=tiempo_ajuste
    )

    logging.info("Bomba apagada")

# ============================================================
# BUCLE PRINCIPAL
# ============================================================

def main() -> None:
    logging.info("==============================================")
    logging.info("Sistema de dosificación iniciado")
    logging.info("GPIO relé: %s", RELAY_PIN)
    logging.info("Cloro objetivo: %.2f mg/L", CLORO_OBJETIVO)
    logging.info("Tiempo base por ciclo: %.2f s", TIEMPO_BASE)
    logging.info("MQTT topic: %s", MQTT_TOPIC)
    logging.info("==============================================")

    while True:
        try:
            data = get_latest_fields()

            if data is None:
                logging.warning("No hay datos completos, se omite este ciclo.")
                time.sleep(CYCLE_SECONDS)
                continue

            temperatura = data[FIELD_TEMPERATURA]
            ph = data[FIELD_PH]
            conductividad = data[FIELD_CONDUCTIVIDAD]
            turbidez = data[FIELD_TURBIDEZ]

            if not (PH_MIN <= ph <= PH_MAX):
                logging.warning("pH fuera de rango de referencia: %.2f", ph)

            if turbidez > TURBIDEZ_MAX_REFERENCIA:
                logging.warning("Turbidez alta respecto a referencia: %.2f NTU", turbidez)

            cloro_estimado = calcular_cloro_estimado(
                temperatura=temperatura,
                ph=ph,
                turbidez=turbidez,
                conductividad=conductividad
            )

            error = calcular_error(cloro_estimado)

            # Como tu caudal es continuo, asumimos que siempre hay flujo
            tiempo_base, tiempo_total = calcular_tiempo_total(
                cloro_estimado=cloro_estimado,
                hay_flujo=True
            )

            tiempo_ajuste = max(0.0, tiempo_total - tiempo_base)

            logging.info(
                "Nodo1 -> T=%.2f °C | pH=%.2f | Turb=%.2f NTU | Cond=%.2f uS/cm",
                temperatura, ph, turbidez, conductividad
            )
            logging.info("Cloro estimado: %.3f mg/L", cloro_estimado)
            logging.info("Error de control: %.3f mg/L", error)
            logging.info("Tiempo base: %.2f s", tiempo_base)
            logging.info("Tiempo de ajuste: %.2f s", tiempo_ajuste)
            logging.info("Tiempo total de activación: %.2f s", tiempo_total)

            activar_bomba(
                segundos_totales=tiempo_total,
                tiempo_base=tiempo_base,
                cloro_estimado=cloro_estimado,
                error=error,
                temperatura=temperatura,
                ph=ph,
                turbidez=turbidez,
                conductividad=conductividad
            )

            logging.info("Esperando %d segundos para el siguiente ciclo...\n", CYCLE_SECONDS)
            time.sleep(CYCLE_SECONDS)

        except KeyboardInterrupt:
            logging.info("Programa detenido por el usuario.")
            break
        except Exception as exc:
            logging.exception("Error en ejecución: %s", exc)
            relay_off()
            time.sleep(10)

    relay_off()
    GPIO.cleanup()
    client.close()

if __name__ == "__main__":
    main()
