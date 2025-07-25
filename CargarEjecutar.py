#!/usr/bin/env python3

import asyncio
import csv
import json
import time
import sys
import os

from mavsdk import System

# Variables globales para almacenar los datos de GPS y usarlos en el bucle de odometría
global current_lat, current_lon, current_alt, last_lat, last_lon, last_alt, inic_alt
current_lat = None
current_lon = None
current_alt = None
last_lat = None
last_lon = None
last_alt = None
inic_alt = None

current_dir = os.path.dirname(os.path.abspath(__file__))

# Definir la función principal para cargar y ejecutar la misión
async def run(mission_name):  # Recibir el mission_name como argumento
    try:
        global last_lat, last_lon, last_alt, inic_alt
        # Conectar al dron
        drone = System()
        await drone.connect(system_address="udp://:14540")

        print("Esperando a que el dron se conecte...")
        async for state in drone.core.connection_state():
            if state.is_connected:
                print(f"-- Conectado al dron!")
                break

        # Leer el archivo JSON de la misión
        mission_path = f"{current_dir}/Planes/{mission_name}.plan"  # Usar mission_name del argumento
        with open(mission_path, 'r') as f:
            mission_json = json.load(f)

        # Extraer los waypoints y la posición de inicio
        mission_items = mission_json["mission"]["items"]
        planned_home_position = mission_json["mission"].get("plannedHomePosition", None)

        last_wp = mission_items[-1] if mission_items else None
        if last_wp and planned_home_position:
            if last_wp["command"] == 20:
                last_lat = planned_home_position[0]
                last_lon = planned_home_position[1]
                last_alt = planned_home_position[2]
                inic_alt = 0
            else:
                last_lat = last_wp["params"][4]  # Latitud del último waypoint
                last_lon = last_wp["params"][5]  # Longitud del último waypoint
                last_alt = last_wp["params"][6]  # Altitud del último waypoint
                inic_alt = planned_home_position[2]

        # Subir la misión al dron usando MAVSDK
        mission = await drone.mission_raw.import_qgroundcontrol_mission(mission_path)
        await drone.mission_raw.upload_mission(mission.mission_items)

        if len(mission.rally_items) > 0:
            await drone.mission_raw.upload_rally_points(mission.rally_items)

        print(f"Misión {mission_name} cargada.")

        # Esperar a que el dron esté listo para volar
        print("Esperando la estimación de posición global...")
        async for health in drone.telemetry.health():
            if health.is_global_position_ok and health.is_home_position_ok:
                print("-- Estimación de posición global OK")
                break

        await attempt_takeoff(drone)

        # Crear el archivo CSV para registrar los datos
        trayectorias_dir = f"{current_dir}/Trayectorias"
        if not os.path.exists(trayectorias_dir):
            os.makedirs(trayectorias_dir)
        with open(f'{current_dir}/Trayectorias/' + mission_name + '_log.csv', mode='w') as csv_file:
            fieldnames = ['SimTime', 'Lat', 'Lon', 'Alt', 'qw', 'qx', 'qy', 'qz', 'Vx', 'Vy', 'Vz']
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            tasks = [
                asyncio.create_task(log_gps(drone)),
                asyncio.create_task(log_odometry(drone, writer))
            ]

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in pending:
                task.cancel()

    except asyncio.CancelledError:
        print("Todas las tareas han sido canceladas.")

# Definir funciones auxiliares para el despegue
async def attempt_takeoff(drone):
    """Intentar armar el dron y comenzar la misión, reintentando si es necesario."""
    max_attempts = 5
    attempt = 0
    while attempt < max_attempts:
        try:
            print(f"-- Intento {attempt + 1} de armar y despegar el dron")
            
            # Armar el dron
            await drone.action.arm()

            # Iniciar la misión
            await drone.mission_raw.start_mission()

            # Esperar a que el dron esté en el aire
            start_time = time.time()
            async for in_air in drone.telemetry.in_air():
                if in_air:
                    print("-- El dron ha despegado!")
                    return
                elif time.time() - start_time > 1:  # Si han pasado más de 1 segundo
                    print("-- El dron no ha despegado. Reintentando...")
                    break

        except Exception as e:
            print(f"Error en intento de despegue: {e}")

        attempt += 1
        await asyncio.sleep(1)  # Esperar un poco antes de reintentar

    raise RuntimeError("No se pudo armar y despegar el dron después de varios intentos.")

# Definir funciones auxiliares para el registro de datos
async def log_odometry(drone, writer):
    a = 1
    b = 0
    c = 1
    last_sim_time = None
    # Grabando datos de los sensores durante el vuelo
    print("-- Grabando datos de los sensores")

    # Definir la frecuencia de datos esperada (ajustar si cambia el filtrado)
    datos_por_segundo = 1  # Cambiar este valor si el filtrado de datos cambia
    umbral_espera = 20 * datos_por_segundo  # Esperar 20 segundos tras despegue para empezar a comprobar si ha aterrizado.

    # Crear un bucle para leer datos de odometría
    async for odom in drone.telemetry.odometry():
        sim_time_us = odom.time_usec
        sim_time_s = sim_time_us / 1e6  # Convertir a segundos

        if sim_time_s is not None and last_sim_time is not None:
            if round(sim_time_s, 0) == round(last_sim_time, 0):
                continue
        last_sim_time = sim_time_s

        vx, vy, vz = odom.velocity_body.x_m_s, odom.velocity_body.y_m_s, odom.velocity_body.z_m_s
        qw, qx, qy, qz = odom.q.w, odom.q.x, odom.q.y, odom.q.z  # Usamos cuaternión

        # Guardar los datos en el archivo CSV junto con la información GPS actual
        writer.writerow({
            'SimTime': round(sim_time_s, 2),
            'Lat': round(current_lat,7),
            'Lon': round(current_lon,7),
            'Alt': round(current_alt, 3) if current_alt else None,
            'qw': round(odom.q.w, 0),
            'qx': round(odom.q.x, 0),
            'qy': round(odom.q.y, 0),
            'qz': round(odom.q.z, 0),
            'Vx': round(odom.velocity_body.x_m_s, 3),
            'Vy': round(odom.velocity_body.y_m_s, 3),
            'Vz': round(odom.velocity_body.z_m_s, 3),
        })

        # Comprobar si el dron ha aterrizado
        if current_lat is not None and current_lon is not None and current_alt is not None and inic_alt is not None:
            a += 1
            if (b == 0 and abs(current_lat - last_lat) < 0.01 and abs(current_lon - last_lon) < 0.01 and abs(current_alt - last_alt - inic_alt) < 0.5 and a > umbral_espera):
                b = 1
                c = a
            if b == 1 and (a - c) > umbral_espera:
                print("-- El plan de vuelo ha terminado.")
                return  # Finalizar la función y el script cuando se cumplan las condiciones

# Definir función auxiliar para el registro de datos de GPS
async def log_gps(drone):
    global current_lat, current_lon, current_alt
    # Leer información de GPS y actualizar las variables globales
    async for gps_info in drone.telemetry.position():
        current_lat = gps_info.latitude_deg
        current_lon = gps_info.longitude_deg
        current_alt = gps_info.absolute_altitude_m

# Definir la función principal para ejecutar el script
async def main():
    # Capturar el argumento de línea de comandos para el mission_name
    mission_name = sys.argv[1]
    await run(mission_name)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        print("Script finalizado.")