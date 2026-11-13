"""
Лаунчер для схемы "ESP32 = точка доступа + BLE": включает радио ДО загрузки большого скрипта.

Замеры на чистой плате: свободно ~156 КБ памяти IDF; Bluetooth + точка доступа вместе
помещаются в любом порядке (остаётся ~42 КБ). Но загрузка большого скрипта занимает ~65 КБ
этой же памяти, поэтому радио должно стартовать первым.

Файлы на плате:  main_launcher_ap.py (потом переименовать в main.py)  и  elm327_ble_ap.py
"""
import gc

AP_SSID = "dlink"
AP_PASS = "36700013932013"
AP_CHANNEL = 6


def mem(tag):
    gc.collect()
    try:
        import esp32
        info = esp32.idf_heap_info(esp32.HEAP_DATA)
        print(tag, "| IDF свободно:", sum(h[1] for h in info),
              "| макс. блок:", max(h[2] for h in info), "| MP:", gc.mem_free())
    except Exception:
        print(tag, "| MP:", gc.mem_free())


import bluetooth
import network

mem("launcher: старт")
try:
    ble = bluetooth.BLE()
    ble.active(True)
    print("launcher: Bluetooth включён")
except OSError as e:
    print("launcher: Bluetooth не запустился:", e)
try:
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password=AP_PASS, authmode=network.AUTH_WPA2_PSK,
              channel=AP_CHANNEL, max_clients=3)
    print("launcher: точка доступа включена:", AP_SSID, ap.ifconfig()[0])
except OSError as e:
    print("launcher: точка доступа не запустилась:", e)
mem("launcher: радио включено")

import elm327_ble_ap   # основной скрипт (работает до остановки)
