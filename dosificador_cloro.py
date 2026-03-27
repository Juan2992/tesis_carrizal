import time
import logging
from typing import Dict, Optional

import RPi.GPIO as GPIO
from influxdb_client import InfluxDBClient

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

# ---------- GPIO ----------
RELAY_PIN = 17
RELAY_ACTIVE_LOW = True   # True si el relé se activa con LOW, False si se activa con HIGH

# ---------- InfluxDB ----------
INFLUX_URL = "http://192.168.1.25:8086"
INFLUX_TOKEN = "PON_AQUI_TU_TOKEN"
INFLUX_ORG = "ca286a3888bffbba"
INFLUX_BUCKET = "sensores_agua"

MEASUREMENT = "calidad_agua"
NODE_TAG_KEY = "sensor_id"
NODE_TAG_VALUE = "nodo1"

FIELD_TEMPERATURA = "temperatura"
FIELD_PH = "ph"
FIELD_CONDUCTIVIDAD = "conductividad"
FIELD_TURBIDEZ = "turbidez"

# ---------- Control ----------
CLORO_OBJETIVO = 0.50        # mg/L
K_SEGUNDOS_POR_MG_L = 100.0  # ganancia del control
MAX_ON_SECONDS = 30.0        # tiempo máximo de activación por ciclo
MIN_ON_SECONDS = 3.0         # tiempo mínimo útil
DEADBAND = 0.03              # banda muerta
CYCLE_SECONDS = 300          # cada 5 minutos

# ---------- Límites operativos ----------
CLORO_ALTO_CORTE = 1.00
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
# MODELO DE REGRESIÓN
# ============================================================

def calcular_cloro_estimado(
    temperatura: float,
    ph: float,
    turbidez: float,
    conductividad: float
) -> float:
    """
    Ecuación de regresión:
    Cloro residual = 5.524 + 0.008*T - 0.932*pH - 0.279*Turbidez + 0.009*Conductividad
    """
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

def calcular_tiempo_activacion(cloro_estimado: float) -> float:
    if cloro_estimado >= CLORO_ALTO_CORTE:
        return 0.0

    error = calcular_error(cloro_estimado)

    if error <= DEADBAND:
        return 0.0

    segundos = error * K_SEGUNDOS_POR_MG_L

    if segundos < MIN_ON_SECONDS:
        segundos = MIN_ON_SECONDS

    if segundos > MAX_ON_SECONDS:
        segundos = MAX_ON_SECONDS

    return round(segundos, 2)

def activar_bomba(segundos: float) -> None:
    if segundos <= 0:
        logging.info("Bomba apagada")
        relay_off()
        return

    logging.info("Bomba encendida por %.2f segundos", segundos)
    relay_on()
    time.sleep(segundos)
    relay_off()
    logging.info("Bomba apagada")

# ============================================================
# BUCLE PRINCIPAL
# ============================================================

def main() -> None:
    logging.info("==============================================")
    logging.info("Sistema de dosificación iniciado")
    logging.info("GPIO relé: %s", RELAY_PIN)
    logging.info("Cloro objetivo: %.2f mg/L", CLORO_OBJETIVO)
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
            tiempo_bomba = calcular_tiempo_activacion(cloro_estimado)

            logging.info(
                "Nodo1 -> T=%.2f °C | pH=%.2f | Turb=%.2f NTU | Cond=%.2f uS/cm",
                temperatura, ph, turbidez, conductividad
            )
            logging.info("Cloro estimado: %.3f mg/L", cloro_estimado)
            logging.info("Error de control: %.3f mg/L", error)
            logging.info("Tiempo de activación: %.2f s", tiempo_bomba)

            activar_bomba(tiempo_bomba)

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
