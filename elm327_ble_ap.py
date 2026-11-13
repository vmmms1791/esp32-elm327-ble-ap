"""
ESP32-C3 SuperMini (MicroPython): ELM327 по Bluetooth + точка доступа Wi-Fi + веб-сервер.

  * ESP32 поднимает точку доступа (AP_SSID / AP_PASS). ПК и VNCI подключаются к ней как клиенты.
  * Для Car Scanner (iPhone) плата работает как BLE-адаптер ELM327 (профиль FFF0):
    VIN отдаётся по 0902 и 22F190, остальные PID симулируются.
  * VIN плата читает у VNCI по DoIP (UDS 22 F190, шлюз 0x1010), как в read_vin_doip.py.
    Запасной путь: ввести VIN вручную на веб-странице. VIN запоминается в файле vin.txt.
  * Веб-страница контроля: http://192.168.4.1/  (статус, клиенты точки, VNCI, VIN,
    состояние Bluetooth, журнал команд, кнопки). Для скриптов: /status (JSON), /vin, /log.

Запускается лаунчером main_launcher_ap.py (он включает радио до загрузки этого файла).
Сохраните на плату под именем elm327_ble_ap.py. Wi-Fi у ESP32-C3 только 2.4 ГГц.
Bluetooth запускается ДО Wi-Fi (иначе не хватает памяти IDF). Если BLE не стартует,
плата работает дальше без него (точка доступа + веб + VNCI), причина видна на веб-странице.
"""
import gc
import time
import math
import json
import os
import struct
import socket
import network
import bluetooth
from machine import Pin

# ---------------- Настройки ----------------
AP_SSID = "dlink"              # имя точки доступа (к ней же настроен VNCI)
AP_PASS = "36700013932013"     # пароль точки (WPA2, не короче 8 символов)
AP_CHANNEL = 6
WEB_PORT = 80
WEB_KEY = ""                   # если задан, ко всем адресам нужно добавлять ?key=...
VNCI_IP = ""                   # пусто = искать VNCI среди клиентов точки (192.168.4.x)
AUTO_READ = True               # сама читать VIN у VNCI, пока VIN не известен
TESTER = 0x0E80                # адреса DoIP, как в вашем захвате
GATEWAY = 0x1010
VIN_FILE = "vin.txt"
ELM_VERSION = "ELM327 v1.5"

VIN = None
vin_src = ""
vnci_ip = None
read_err = ""
want_read = False
cmd_count = 0
ap = None
srv = None
LOG = []


def log(*a):
    line = "%d %s" % (time.ticks_ms() // 1000, " ".join(str(x) for x in a))
    print(line)
    LOG.append(line[:120])
    if len(LOG) > 40:
        del LOG[0]


def idf_mem():
    """Память IDF (из неё берут Wi-Fi и контроллер Bluetooth): (свободно, самый большой блок)."""
    try:
        import esp32
        info = esp32.idf_heap_info(esp32.HEAP_DATA)
        return sum(h[1] for h in info), max(h[2] for h in info)
    except Exception:
        return None



# ---------------- Радио: Bluetooth и Wi-Fi ----------------
# Память IDF (~38 КБ после запуска Wi-Fi) не вмещает контроллер Bluetooth, отсюда EIO в ble.active(True).
# Поэтому Bluetooth поднимается ПЕРВЫМ, пока память IDF свободна, и только потом Wi-Fi.
# Если что-то не поднялось, плата не падает, а продолжает работать с тем, что есть.
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21

# Профиль 0xFFF0 (Vgate iCar / популярные BLE ELM327 адаптеры)
SERVICE_UUID = bluetooth.UUID(0xFFF0)
NOTIFY_UUID = bluetooth.UUID(0xFFF1)   # ESP32 -> iPhone
WRITE_UUID = bluetooth.UUID(0xFFF2)    # iPhone -> ESP32

service = (
    SERVICE_UUID,
    (
        (NOTIFY_UUID, bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY),
        (WRITE_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
    ),
)

name = b"OBDII"
adv_data = b"\x02\x01\x06" + bytes((len(name) + 1, 0x09)) + name
# 16-битный UUID 0xFFF0 (little-endian: F0 FF) в Scan Response
resp_data = b"\x03\x03\xF0\xFF"

ble = None
ble_ok = False
ble_err = ""
tx_handle = rx_handle = None
conn_handle = None
mtu = 23
t0 = time.ticks_ms()


def ble_up():
    """Запускает Bluetooth и регистрирует сервис. True, если получилось."""
    global ble, ble_ok, ble_err, tx_handle, rx_handle
    try:
        gc.collect()
        if ble is None:
            ble = bluetooth.BLE()
        ble.active(True)
        ble.config(gap_name="OBDII")
        ((tx_handle, rx_handle),) = ble.gatts_register_services((service,))
        ble_ok, ble_err = True, ""
    except OSError as e:
        ble_ok, ble_err = False, str(e)
        print("BLE не запустился:", e, "| IDF (свободно, макс. блок):", idf_mem())
        try:
            ble.active(False)        # вернуть память IDF
        except Exception:
            pass
        gc.collect()
    return ble_ok


def ap_up():
    """Поднимает точку доступа (до 3 попыток). True, если получилось."""
    global ap
    for attempt in range(3):
        try:
            gc.collect()
            ap = network.WLAN(network.AP_IF)
            ap.active(True)
            ap.config(essid=AP_SSID, password=AP_PASS, authmode=network.AUTH_WPA2_PSK,
                      channel=AP_CHANNEL, max_clients=3)
            return True
        except OSError as e:
            print("AP:", e, "| MP free:", gc.mem_free(), "| IDF:", idf_mem())
            try:
                ap.active(False)
            except Exception:
                pass
            gc.collect()
            time.sleep(1)
    return False


print("IDF при старте скрипта (радио уже включено лаунчером):", idf_mem())
ble_up()                                  # 1) Bluetooth первым
if not ap_up():                           # 2) Wi-Fi
    if ble_ok:                            # Wi-Fi не хватило памяти при включённом BLE:
        ble.active(False)                 # приоритет у Wi-Fi (VNCI, веб-страница)
        ble_ok = False
        ble_err = "отключён: не хватило памяти IDF для Wi-Fi"
        gc.collect()
        if not ap_up():
            raise RuntimeError("Wi-Fi не запустился")
elif not ble_ok:
    ble_up()                              # повторная попытка BLE уже при работающей точке
log("Точка доступа:", AP_SSID, "IP", ap.ifconfig()[0], "| MP free:", gc.mem_free(), "| IDF:", idf_mem())
log("Bluetooth:", "OK" if ble_ok else "НЕТ (%s)" % ble_err)

led = Pin(8, Pin.OUT)


def advertise():
    if ble_ok:
        ble.gap_advertise(100_000, adv_data=adv_data, resp_data=resp_data)


def send_elm(text):
    if not ble_ok or conn_handle is None:
        return
    data = text.encode("ascii")
    log("TX:", data)
    size = mtu - 3
    try:
        if len(data) <= size:
            ble.gatts_notify(conn_handle, tx_handle, data)
        else:
            for i in range(0, len(data), size):
                ble.gatts_notify(conn_handle, tx_handle, data[i:i + size])
                time.sleep_ms(10)
    except OSError as e:
        log("notify error:", e)

# ---------------- Состояние адаптера ELM327 ----------------
echo = False
headers = False
spaces = True
sh = "7DF"       # заголовок запроса (ATSH)
cra = None       # фильтр адреса ответа (ATCRA)


def reset_state():
    global echo, headers, spaces, sh, cra
    echo, headers, spaces, sh, cra = False, False, True, "7DF", None


def rid():
    """CAN-ID, от имени которого 'отвечает' блок."""
    if cra:
        return cra
    if sh == "710":                      # шлюз VW
        return "77A"
    if len(sh) == 3 and sh.startswith("7E"):
        try:
            return "7E%X" % ((int(sh[2], 16) + 8) & 0xF)
        except ValueError:
            pass
    return "7E8"


def hexs(b):
    sep = " " if spaces else ""
    return sep.join("%02X" % x for x in b)


def fmt(data):
    """Однокадровый ответ (до 7 байт)."""
    if headers:
        sep = " " if spaces else ""
        return rid() + sep + "%02X" % len(data) + sep + hexs(data)
    return hexs(data)


def tp_lines(rid_, data):
    """Многокадровый ответ ISO-TP в формате ELM327."""
    sep = " " if spaces else ""
    n = len(data)
    if headers:
        out = [rid_ + sep + "1%X" % (n >> 8) + sep + "%02X" % (n & 0xFF)
               + sep + hexs(data[:6])]
        i, k = 6, 1
        while i < n:
            out.append(rid_ + sep + "%02X" % (0x20 + (k & 0xF)) + sep + hexs(data[i:i + 7]))
            i += 7
            k += 1
        return out
    out = ["%03X" % n, "0: " + hexs(data[:6])]
    i, k = 6, 1
    while i < n:
        out.append("%X: " % (k & 0xF) + hexs(data[i:i + 7]))
        i += 7
        k += 1
    return out


def reply(data):
    """Выбирает одно- или многокадровый формат по длине."""
    return [fmt(data)] if len(data) <= 7 else tp_lines(rid(), data)


# ---------------- Симуляция автомобиля ----------------
def pid_value(pid):
    t = time.ticks_diff(time.ticks_ms(), t0) / 1000
    speed = 50 + 40 * math.sin(t / 15)          # км/ч
    rpm = 900 + speed * 35                      # об/мин
    load = 20 + speed * 0.5                     # %
    throttle = 10 + speed * 0.4                 # %
    mapk = 30 + speed * 0.5                     # кПа
    runtime = int(t)

    if pid == 0x00:   # поддерживаются: 01,04,05,0B,0C,0D,0F,11,1F
        return b"\x98\x3A\x80\x02"
    if pid == 0x01:
        return b"\x00\x07\x65\x00"
    if pid == 0x04:
        return bytes((int(load * 255 / 100),))
    if pid == 0x05:
        return bytes((90 + 40,))
    if pid == 0x0B:
        return bytes((int(mapk),))
    if pid == 0x0C:
        v = int(rpm * 4)
        return bytes((v >> 8, v & 0xFF))
    if pid == 0x0D:
        return bytes((int(speed),))
    if pid == 0x0F:
        return bytes((30 + 40,))
    if pid == 0x11:
        return bytes((int(throttle * 255 / 100),))
    if pid == 0x1F:
        return bytes(((runtime >> 8) & 0xFF, runtime & 0xFF))
    if pid == 0x20:
        return b"\x00\x00\x00\x00"
    return None


def obd01(c):
    """Режим 01, в т.ч. составные запросы вида 010103040A0B0D."""
    p = c[2:]
    if len(p) % 2:                 # последний символ -- число ответов (010D1)
        p = p[:-1]
    out = b""
    found = 0
    for i in range(0, len(p), 2):
        try:
            pid = int(p[i:i + 2], 16)
        except ValueError:
            return ["?"]
        v = pid_value(pid)
        if v is not None:
            out += bytes((pid,)) + v
            found += 1
    if not found:
        return ["NO DATA"]
    return reply(b"\x41" + out)


# ---------------- Обработка команд ----------------
def process(cmd):
    """Возвращает список строк ответа."""
    global echo, headers, spaces, sh, cra
    c = cmd.replace(" ", "").upper()

    if c == "STI":
        return ["STN1170 v3.2.0"]
    if c == "VTI":
        return ["12.4V"]

    # --- AT-команды ---
    if c.startswith("AT"):
        a = c[2:]
        if a == "Z":
            reset_state()
            return [ELM_VERSION]
        if a == "I":
            return [ELM_VERSION]
        if a == "@1":
            return ["OBDII to RS232 Interpreter"]
        if a == "RV":
            return ["12.4V"]
        if a == "DPN":
            return ["A6"]
        if a == "DP":
            return ["AUTO, ISO 15765-4 (CAN 11/500)"]
        if a in ("E0", "E1"):
            echo = a == "E1"
        elif a in ("H0", "H1"):
            headers = a == "H1"
        elif a in ("S0", "S1"):
            spaces = a == "S1"
        elif a.startswith("SH"):
            sh = a[2:]
        elif a.startswith("CRA"):
            cra = a[3:]
        elif a == "AR":
            cra = None
        return ["OK"]

    # --- OBD / UDS ---
    try:
        mode = int(c[0:2], 16)
    except ValueError:
        return ["?"]

    if mode == 0x01 and len(c) >= 4:
        return obd01(c)
    if mode == 0x03:                         # сохранённые ошибки: нет
        return reply(b"\x43\x00")
    if mode == 0x04:
        return reply(b"\x44")
    if mode == 0x07:
        return reply(b"\x47\x00")
    if mode == 0x09 and len(c) >= 4:
        if c[2:4] == "00":                   # поддерживается только PID 02
            return reply(b"\x49\x00\x40\x00\x00\x00")
        if c[2:4] == "02" and VIN:
            return reply(b"\x49\x02\x01" + VIN.encode())
        return ["NO DATA"]
    if mode == 0x10 and len(c) >= 4:         # смена сессии UDS
        return reply(b"\x50" + bytes((int(c[2:4], 16),)) + b"\x00\x32\x01\xF4")
    if mode == 0x3E:                         # TesterPresent
        return reply(b"\x7E\x00")
    if c.startswith("22F190") and VIN:       # VIN по UDS
        return reply(b"\x62\xF1\x90" + VIN.encode())
    return ["NO DATA"]


# ---------------- VIN: проверка, хранение ----------------
def valid_vin(v):
    # в VIN нет букв I, O, Q
    return len(v) == 17 and all(ch in "ABCDEFGHJKLMNPRSTUVWXYZ0123456789" for ch in v)


def set_vin(v, src):
    global VIN, vin_src
    VIN, vin_src = v, src
    try:
        with open(VIN_FILE, "w") as f:
            f.write(v)
    except OSError as e:
        log("не удалось сохранить VIN:", e)
    log("VIN:", v, "(" + src + ")")


def clear_vin():
    global VIN, vin_src
    VIN, vin_src = None, ""
    try:
        os.remove(VIN_FILE)
    except OSError:
        pass
    log("VIN сброшен")


def load_vin():
    global VIN, vin_src
    try:
        with open(VIN_FILE) as f:
            v = f.read().strip().upper()
        if valid_vin(v):
            VIN, vin_src = v, "сохранён в памяти"
    except OSError:
        pass


# ---------- Клиенты точки ----------
def stations():
    try:
        return [":".join("%02x" % b for b in s[0]) for s in ap.status("stations")]
    except Exception:
        return []


# ---------- Чтение VIN у VNCI по DoIP ----------
def port_open(ip, port, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def serve_wait(ms):
    """Ждёт, продолжая обслуживать веб-страницу."""
    t = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t) < ms:
        web_poll()
        time.sleep_ms(20)


def find_vnci():
    global vnci_ip
    if VNCI_IP:
        vnci_ip = VNCI_IP
        return vnci_ip
    if not stations():
        vnci_ip = None
        return None
    me = ap.ifconfig()[0]
    base = me.rsplit(".", 1)[0]
    for i in range(2, 12):
        ip = base + "." + str(i)
        if ip != me and (port_open(ip, 13400, 0.4) or port_open(ip, 80, 0.4)):
            vnci_ip = ip
            return ip
        web_poll()
    vnci_ip = None
    return None


def enable_doip(ip):
    s = socket.socket()
    s.settimeout(15)
    try:
        s.connect((ip, 80))
        s.send(("GET /cgi-bin/formular.fcgi?FORM_ID=AVMOD&REQ_TYPE=N&FORM_TRIGGER=submit"
                "&DOIPset=1 HTTP/1.0\r\nHost: " + ip + "\r\n\r\n").encode())
        s.recv(256)
    except OSError as e:
        log("VNCI HTTP:", e)
    finally:
        s.close()


def doip_pkt(ptype, payload):
    return struct.pack(">BBHI", 0x02, 0xFD, ptype, len(payload)) + payload


def rx_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise OSError("соединение закрыто")
        b += c
    return b


def doip_rx(s):
    _, _, ptype, ln = struct.unpack(">BBHI", rx_exact(s, 8))
    return ptype, (rx_exact(s, ln) if ln else b"")


def read_vin_vnci():
    """Возвращает (VIN, "") или (None, причина)."""
    ip = find_vnci()
    if not ip:
        return None, "VNCI не найден среди клиентов точки"
    if not port_open(ip, 13400, 3):
        log("Включаю DoIP на", ip)
        enable_doip(ip)
        ok = False
        t = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t) < 25000:
            if port_open(ip, 13400, 2):
                ok = True
                break
            serve_wait(1500)
        if not ok:
            return None, "порт 13400 не открылся (VNCI не в DoIP, нет зажигания/автомобиля?)"
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((ip, 13400))
        s.send(doip_pkt(0x0005, struct.pack(">HB4s", TESTER, 0, b"\0\0\0\0")))
        t, p = doip_rx(s)
        if t != 0x0006 or len(p) < 5 or p[4] != 0x10:
            return None, "активация маршрута отклонена (тип %X)" % t
        s.send(doip_pkt(0x8001, struct.pack(">HH", TESTER, GATEWAY) + b"\x22\xF1\x90"))
        for _ in range(6):
            t, p = doip_rx(s)
            if t == 0x8001:
                d = p[4:]
                if d[:3] == b"\x62\xF1\x90":
                    v = bytes(d[3:]).decode("ascii", "ignore").strip().upper()
                    if valid_vin(v):
                        return v, ""
                    return None, "некорректный VIN в ответе"
                if d[:1] == b"\x7f" and len(d) > 2 and d[2] == 0x78:
                    continue          # блок просит подождать
                return None, "отрицательный ответ блока"
        return None, "нет ответа шлюза"
    except OSError as e:
        return None, "DoIP: %s" % e
    finally:
        s.close()


def do_read():
    global read_err, want_read
    want_read = False
    log("Читаю VIN у VNCI...")
    v, err = read_vin_vnci()
    if v:
        read_err = ""
        set_vin(v, "VNCI")
    else:
        read_err = err
        log("VNCI:", err)


# ---------------- Веб-сервер контроля ----------------
def esc(t):
    return str(t).replace("&", "&").replace("<", "<").replace(">", ">")


def u(path, q=""):
    parts = [p for p in (q, ("key=" + WEB_KEY) if WEB_KEY else "") if p]
    return path + ("?" + "&".join(parts) if parts else "")


def page():
    st = stations()
    rows = (
        ("Точка доступа", "%s, IP %s" % (AP_SSID, ap.ifconfig()[0])),
        ("Клиенты точки", "%d: %s" % (len(st), ", ".join(st) if st else "нет")),
        ("VNCI", vnci_ip or "не найден"),
        ("VIN", ("%s (%s)" % (VIN, vin_src)) if VIN else "не задан"),
        ("Последняя ошибка чтения", read_err or "-"),
        ("Bluetooth", ("подключён, MTU %d" % mtu) if conn_handle is not None
         else ("ожидание подключения" if ble_ok else "не запущен: %s" % ble_err)),
        ("ELM", "echo=%s headers=%s spaces=%s SH=%s CRA=%s" % (echo, headers, spaces, sh, cra)),
        ("Команд от Car Scanner", cmd_count),
        ("Работает, с", time.ticks_ms() // 1000),
    )
    h = ['<!DOCTYPE html><html><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<meta http-equiv="refresh" content="10"><title>ESP32 ELM327</title>',
         '<style>body{font-family:sans-serif;margin:16px;max-width:760px}',
         'td{padding:3px 10px 3px 0;vertical-align:top}pre{background:#eee;padding:8px;',
         'font-size:12px;overflow:auto}</style></head><body><h2>ESP32 ELM327 / VNCI</h2><table>']
    for k, v in rows:
        h.append("<tr><td><b>%s</b></td><td>%s</td></tr>" % (esc(k), esc(v)))
    h.append("</table><p><a href=\"%s\">Прочитать VIN у VNCI</a> | " % u("/read"))
    h.append("<a href=\"%s\">Сбросить VIN</a> | " % u("/clearvin"))
    h.append("<a href=\"%s\">JSON</a></p>" % u("/status"))
    h.append('<form action="/setvin" method="get">Задать VIN вручную: ')
    h.append('<input name="vin" maxlength="17" size="20" style="text-transform:uppercase">')
    if WEB_KEY:
        h.append('<input type="hidden" name="key" value="%s">' % esc(WEB_KEY))
    h.append(' <input type="submit" value="Сохранить"></form>')
    h.append("<h3>Журнал</h3><pre>%s</pre></body></html>" % esc("\n".join(LOG[-25:])))
    return "".join(h)


def http_reply(cl, code, ctype, body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    names = {200: "OK", 302: "Found", 403: "Forbidden", 404: "Not Found", 503: "Service Unavailable"}
    head = ("HTTP/1.0 %d %s\r\nContent-Type: %s; charset=utf-8\r\nContent-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (code, names.get(code, "OK"), ctype, len(body)))
    cl.sendall(head.encode() + body)


def http_redirect(cl, where):
    cl.sendall(("HTTP/1.0 302 Found\r\nLocation: %s\r\nContent-Length: 0\r\n"
              "Connection: close\r\n\r\n" % where).encode())


def web_poll():
    """Неблокирующая обработка одного HTTP-запроса (если он есть)."""
    global want_read
    if srv is None:
        return
    try:
        cl, _ = srv.accept()
    except OSError:
        return
    try:
        cl.settimeout(2)
        req = cl.recv(1024).decode("utf-8", "ignore")
        parts = req.split("\r\n", 1)[0].split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        route, _, qs = path.partition("?")
        q = {}
        for kv in qs.split("&"):
            if kv:
                k, _, v = kv.partition("=")
                q[k] = v
        if WEB_KEY and q.get("key") != WEB_KEY:
            http_reply(cl, 403, "text/plain", "forbidden")
        elif route == "/":
            http_reply(cl, 200, "text/html", page())
        elif route == "/vin":
            if VIN:
                http_reply(cl, 200, "text/plain", VIN)
            else:
                http_reply(cl, 503, "text/plain", read_err or "VIN не задан")
        elif route == "/log":
            http_reply(cl, 200, "text/plain", "\n".join(LOG))
        elif route == "/status":
            http_reply(cl, 200, "application/json", json.dumps({
                "vin": VIN, "vin_source": vin_src, "vnci_ip": vnci_ip,
                "stations": stations(), "ble_ok": ble_ok, "ble_connected": conn_handle is not None,
                "mtu": mtu, "commands": cmd_count, "read_error": read_err,
                "uptime_s": time.ticks_ms() // 1000}))
        elif route == "/read":
            want_read = True
            http_redirect(cl, u("/"))
        elif route == "/clearvin":
            clear_vin()
            http_redirect(cl, u("/"))
        elif route == "/setvin":
            v = q.get("vin", "").strip().upper()
            if valid_vin(v):
                set_vin(v, "вручную")
            else:
                log("Некорректный VIN:", v)
            http_redirect(cl, u("/"))
        else:
            http_reply(cl, 404, "text/plain", "not found")
    except Exception as e:
        log("web:", e)
    finally:
        cl.close()


def start_web():
    global srv
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", WEB_PORT))
    srv.listen(3)
    srv.setblocking(False)
    log("Веб-сервер: http://%s:%d/" % (ap.ifconfig()[0], WEB_PORT))


def handle_command(cmd):
    global cmd_count
    cmd_count += 1
    log("RX:", cmd)
    is_atz = cmd.replace(" ", "").upper() == "ATZ"
    prefix = "\r\r" if is_atz else ((cmd + "\r") if echo else "")
    lines = process(cmd)           # ATZ сбрасывает состояние, поэтому префикс считаем до
    send_elm(prefix + "\r".join(lines) + "\r\r>")


def irq(event, data):
    global conn_handle, mtu
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, _, _ = data
        mtu = 23
        log("Connected")
    elif event == _IRQ_CENTRAL_DISCONNECT:
        conn_handle = None
        log("Disconnected")
        advertise()
    elif event == _IRQ_MTU_EXCHANGED:
        mtu = data[1]
        log("MTU:", mtu)
    elif event == _IRQ_GATTS_WRITE:
        _, value_handle = data
        if value_handle == rx_handle:
            raw = ble.gatts_read(rx_handle).decode("utf-8", "ignore")
            # Car Scanner может прислать несколько команд подряд, разделённых \r
            for part in raw.replace("\n", "\r").split("\r"):
                if part.strip():
                    handle_command(part)


if ble_ok:
    ble.irq(irq)

load_vin()
start_web()
advertise()
log("Bluetooth:", "OBDII (FFF0)" if ble_ok else "выключен", "| VIN:", VIN)

last_blink = time.ticks_ms()
last_auto = time.ticks_ms() - 15000
last_gc = time.ticks_ms()
gc.collect()
while True:
    web_poll()
    now = time.ticks_ms()
    if time.ticks_diff(now, last_gc) >= 5000:
        gc.collect()
        last_gc = now
    if time.ticks_diff(now, last_blink) >= 1000:
        led.value(not led.value())
        last_blink = now
    if want_read:
        do_read()
        last_auto = time.ticks_ms()
    elif (AUTO_READ and VIN is None and conn_handle is None
          and time.ticks_diff(now, last_auto) > 20000 and stations()):
        last_auto = now
        do_read()
    time.sleep_ms(20)