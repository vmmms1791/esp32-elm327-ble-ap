# Полный анализ исходного кода `elm327_ble_ap.py`

## Обзор проекта

**Назначение:** ESP32-C3 SuperMini на MicroPython работает как:
1. **BLE ELM327-адаптер** (профиль 0xFFF0, совместим с Vgate iCar) для Car Scanner на iPhone
2. **Точка доступа Wi-Fi** (AP) — к ней подключаются ПК и VNCI 6154A
3. **Веб-сервер управления** (порт 80) — мониторинг, ручной ввод VIN, JSON API
4. **DoIP-клиент** — читает VIN у VNCI через UDS 0x22 F190 на шлюзе 0x1010

**Архитектура:** Single-threaded event loop с неблокирующими сокетами + BLE IRQ callbacks.

---

## 1. Управление памятью и порядок инициализации радио (КРИТИЧЕСКИЙ)

### Проблема
ESP32-C3 имеет ограниченную **IDF heap** (~156 КБ на старте). Bluetooth контроллер и Wi-Fi MAC берут память из **одного пула IDF heap**. Загрузка большого Python-скрипта потребляет ~65 КБ этого же пула.

### Решение (launcher → main)
```python
# main_launcher_ap.py запускает РАДИО ПЕРВЫМ:
1. ble.active(True)      # Bluetooth первым — когда IDF heap максимально свободен
2. ap.active(True)       # Wi-Fi вторым
3. import elm327_ble_ap  # загрузка скрипта ПОСЛЕ включения радио
```

### Fallback в основном скрипте
```python
ble_up()                          # 1) Пробуем BLE
if not ap_up():                   # 2) Пробуем Wi-Fi
    if ble_ok:                    # Если Wi-Fi не влезло при включённом BLE:
        ble.active(False)         # Выключаем BLE → приоритет Wi-Fi (VNCI, веб)
        ble_ok = False
        if not ap_up(): raise RuntimeError
elif not ble_ok:
    ble_up()                      # Повторная попытка BLE уже при работающем Wi-Fi
```

**Мониторинг:** `idf_mem()` возвращает `(free_total, max_block)` — ключевой диагностический показатель.

---

## 2. BLE стек (ELM327 профиль 0xFFF0)

### GATT сервис
| UUID | Характеристика | Flags | Направление |
|------|----------------|-------|-------------|
| 0xFFF0 | Service | — | — |
| 0xFFF1 | Notify | READ \| NOTIFY | ESP32 → iPhone (ответы ELM) |
| 0xFFF2 | Write | WRITE \| WRITE_NO_RESPONSE | iPhone → ESP32 (команды AT/OBD) |

### Advertising
- **adv_data**: `02 01 06` (Flags LE General Discoverable) + `09` Complete Local Name "OBDII"
- **resp_data**: `03 03 F0 FF` — 16-bit Service UUID 0xFFF0 в Scan Response

### MTU и фрагментация
```python
mtu = 23  # дефолт, обновляется через _IRQ_MTU_EXCHANGED
size = mtu - 3  # 3 байта overhead (opcode + handle)
# Уведомления режутся на куски по size с паузой 10 мс
```

### IRQ обработка
| Event | Константа | Действие |
|-------|-----------|----------|
| Central Connect | 1 | `conn_handle` сохраняется, `mtu=23`, реклама останавливается |
| Central Disconnect | 2 | `conn_handle=None`, возобновляется `advertise()` |
| MTU Exchanged | 21 | `mtu = data[1]` |
| GATTS Write | 3 | Данные читаются из `rx_handle`, парсятся по `\r`, каждая команда → `handle_command()` |

---

## 3. ELM327 протокол — парсер команд

### Состояние адаптера (глобальные переменные)
```python
echo = False      # ATE0/ATE1 — эхо команд
headers = False   # ATH0/ATH1 — заголовки в ответах
spaces = True     # ATS0/ATS1 — пробелы между байтами
sh = "7DF"        # ATSH — заголовок запроса (CAN ID)
cra = None        # ATCRA — фильтр адреса ответа
```

### Поддерживаемые AT-команды
| Команда | Действие | Ответ |
|---------|----------|-------|
| `ATZ` | Полный сброс (`reset_state()`) | `ELM327 v1.5` |
| `ATI` | Информация | `ELM327 v1.5` |
| `AT@1` | Описание устройства | `OBDII to RS232 Interpreter` |
| `ATRV` | Напряжение | `12.4V` |
| `ATDPN` | Протокол | `A6` |
| `ATDP` | Описание протокола | `AUTO, ISO 15765-4 (CAN 11/500)` |
| `ATE0/ATE1` | Эхо выкл/вкл | `OK` |
| `ATH0/ATH1` | Заголовки выкл/вкл | `OK` |
| `ATS0/ATS1` | Пробелы выкл/вкл | `OK` |
| `ATSHxxx` | Установка CAN ID запроса | `OK` |
| `ATCRAxxx` | Фильтр адреса ответа | `OK` |
| `ATAR` | Сброс фильтра (cra=None) | `OK` |

### Форматирование ответов
- **Single-frame** (≤7 байт): `[RID] [LEN] [DATA...]` или `[DATA...]` (если headers=0)
- **Multi-frame ISO-TP** (>7 байт): First Frame `10 LL HH` + 6 байт, затем Consecutive Frames `21..2F` + 7 байт
- `rid()` вычисляет ответный CAN ID:
  - Если `cra` задан → `cra`
  - Если `sh=="710"` (VW gateway) → `"77A"`
  - Если `sh` в диапазоне `7E0-7EF` → `sh + 8` (логика функциональной адресации)
  - Иначе → `"7E8"` (default ECU response)

---

## 4. OBD-II / UDS симуляция (Mode 01, 03, 04, 07, 09, 10, 3E, 22)

### Mode 01 — Live Data (поддерживаемые PID)
| PID | Описание | Формула / Значение |
|-----|----------|-------------------|
| 0x00 | Supported PIDs 01-20 | `98 3A 80 02` (биты: 01,04,05,0B,0C,0D,0F,11,1F) |
| 0x01 | Monitor status | `00 07 65 00` |
| 0x04 | Engine Load | `load * 2.55` (0-255) |
| 0x05 | Coolant Temp | `90 + 40 = 130°C` (константа) |
| 0x0B | Intake MAP | `map_kPa` (30 + speed*0.5) |
| 0x0C | RPM | `rpm * 4` (2 байта big-endian) |
| 0x0D | Vehicle Speed | `speed` км/ч |
| 0x0F | Intake Air Temp | `70°C` |
| 0x11 | Throttle Position | `throttle * 2.55` |
| 0x1F | Run Time | `uptime_sec` (2 байта) |
| 0x20 | PIDs 21-40 | `00 00 00 00` (не поддерживается) |

**Динамика:** `speed = 50 + 40*sin(t/15)` — синусоида 0-90 км/ч, период 94 сек. Остальные PID зависят от speed.

### Составные запросы
`010103040A0B0D` → парсится попарно, каждый PID → `41 PID DATA...` в одном ответе.
**Баг/особенность:** `010D1` — последний символ `1` отбрасывается как "количество ответов" (legacy ELM quirk).

### Mode 03/04/07 — DTC
Все возвращают "нет ошибок": `43 00`, `44`, `47 00`.

### Mode 09 — Vehicle Info
- `0900` → поддерживается только PID 02: `49 00 40 00 00 00`
- `0902` → VIN (если известен): `49 02 01 + VIN(17 байт)`

### UDS (Mode 10, 3E, 22)
- `10 xx` (Session Control) → `50 xx 00 32 01 F4` (positive response + timing)
- `3E` (TesterPresent) → `7E 00`
- `22 F1 90` (Read VIN by UDS) → `62 F1 90 + VIN` (только если VIN известен)

---

## 5. VIN Management

### Хранилище
- Файл `vin.txt` в ФС MicroPython (persistent)
- При старте: `load_vin()` → читает файл, валидирует
- `valid_vin(v)`: 17 символов, алфавит без I,O,Q

### Источники VIN (приоритет)
1. **VNCI DoIP** — авто-чтение каждые 20 сек (`AUTO_READ=True`)
2. **Ручной ввод** — веб-форма `/setvin`
3. **Сохранённый** — из `vin.txt` при загрузке

### DoIP чтение VIN (`read_vin_vnci()`)
```
1. find_vnci() — скан 192.168.4.2..11, порты 13400 (DoIP) и 80 (HTTP)
2. Если порт 13400 закрыт → enable_doip() через HTTP CGI на VNCI
   GET /cgi-bin/formular.fcgi?FORM_ID=AVMOD&REQ_TYPE=N&FORM_TRIGGER=submit&DOIPset=1
   Ждёт до 25 сек открытия порта
3. DoIP Route Activation (0x0005): TESTER=0x0E80 → ожидает 0x0006 + 0x10
4. UDS 0x22 F1 90 через DoIP 0x8001: TESTER=0x0E80, GATEWAY=0x1010
5. Парсит ответ 0x62 F1 90 + VIN, валидирует, сохраняет
6. Обрабатывает 0x7F 0x78 (RequestCorrectlyReceived-ResponsePending) — ретраи до 6 раз
```

---

## 6. Веб-сервер (порт 80, неблокирующий)

### Эндпоинты
| Путь | Метод | Описание |
|------|-------|----------|
| `/` | GET | HTML дашборд (авто-refresh 10 сек) |
| `/status` | GET | JSON: vin, vnci_ip, stations[], ble_ok, mtu, commands, uptime, read_error |
| `/vin` | GET | Plain text VIN или 503 + ошибка |
| `/log` | GET | Plain text журнал (последние 40 строк) |
| `/read` | GET | Триггер `want_read=True` → редирект на `/` |
| `/clearvin` | GET | Удаляет VIN → редирект на `/` |
| `/setvin` | GET | Параметр `vin=XXX` → валидация → сохранение → редирект |

### Безопасность
- Опциональный `WEB_KEY` — все URL требуют `?key=...`
- Нет HTTPS, базовая защита только ключом

### Неблокирующая модель
```python
srv.setblocking(False)
# В главном цикле:
cl, _ = srv.accept()  # OSError если нет соединений
cl.settimeout(2)
req = cl.recv(1024)   # читает только первый пакет запроса
```

---

## 7. Главный цикл (Event Loop)

```python
while True:
    web_poll()                    # обслужить 1 HTTP запрос
    
    now = time.ticks_ms()
    
    if now - last_gc >= 5000:     # GC каждые 5 сек
        gc.collect()
    
    if now - last_blink >= 1000:  # LED мигает 1 Гц
        led.toggle()
    
    if want_read:                 # ручной триггер чтения VIN
        do_read()
    
    elif (AUTO_READ and VIN is None and conn_handle is None
          and now - last_auto > 20000 and stations()):
        do_read()                 # авто-чтение каждые 20 сек, если нет BLE-клиента
    
    time.sleep_ms(20)             # 50 итераций/сек
```

**Приоритеты:**
1. Веб-сервер (неблокирующий)
2. BLE IRQ — асинхронно, прерывает цикл
3. Авто-чтение VIN — только когда нет BLE-соединения (не мешает Car Scanner)

---

## 8. Архитектурные риски и ограничения

| Риск | Описание | Митигация |
|------|----------|-----------|
| **IDF heap exhaustion** | BLE + Wi-Fi + скрипт > доступной памяти | Launcher включает радио первым; fallback отключает BLE |
| **Single-threaded** | Длительные DoIP операции блокируют веб и BLE | `serve_wait()` вызывает `web_poll()` внутри ожидания; DoIP таймауты 5-15 сек |
| **No TLS/HTTPS** | Открытый Wi-Fi AP, пароль WPA2 только | Локальная сеть, опциональный `WEB_KEY` |
| **VIN race condition** | Авто-чтение каждые 20 сек может конфликтовать с ручным | `want_read` флаг, `AUTO_READ` отключается когда VIN получен |
| **BLE MTU фиксировано** | Начальное MTU=23, больше не запрашивается | Работает для ELM327 (малые пакеты) |
| **ISO-TP только TX** | Нет reassembly входящих multiframe | ELM327 клиенты (Car Scanner) шлют однокадровые команды |
| **Car Scanner quirks** | `010D1` парсится с отбрасыванием последнего символа | Заведомо поддерживаемый паттерн |

---

## 9. Файловая структура на плате

```
/flash/
├── main.py              ← переименованный main_launcher_ap.py
├── elm327_ble_ap.py     ← основной скрипт (этот файл)
├── elm327_ble_ap.mpy    ← скомпилированный bytecode (опционально, для экономии RAM)
├── vin.txt              ← сохранённый VIN (создаётся автоматически)
```

---

## 10. Запуск и отладка

### Загрузка на плату
```bash
# 1. main_launcher_ap.py → main.py (rename на плате или загрузить как main.py)
# 2. elm327_ble_ap.py загрузить как есть
# 3. Перезагрузка платы
```

### Веб-интерфейс
```
http://192.168.4.1/           # дашборд
http://192.168.4.1/status     # JSON для скриптов
http://192.168.4.1/vin        # только VIN
http://192.168.4.1/log        # журнал
```

### Логи (Serial REPL)
```
IDF при старте скрипта: (156000, 42000)
Точка доступа: dlink IP 192.168.4.1 | MP free: 120000 | IDF: (42000, 18000)
Bluetooth: OK
Веб-сервер: http://192.168.4.1:80/
Bluetooth: OBDII (FFF0) | VIN: None
0 RX: ATZ
0 TX: b'ATZ\r\rELM327 v1.5\r\r>'
...
```

---

## 11. Соответствие стандартам

| Стандарт | Реализация |
|----------|------------|
| ISO 15765-4 (ISO-TP) | TX: First Frame + Consecutive Frames; RX: только single-frame |
| SAE J1979 (OBD-II) | Mode 01, 03, 04, 07, 09 частично |
| ISO 14229-1 (UDS) | 0x10, 0x22, 0x3E базовые |
| DoIP (ISO 13400) | Route Activation (0x0005/0x0006), Diagnostic Message (0x8001) |
| BLE GATT | Custom 0xFFF0 profile (Vgate iCar compatible) |

---

## 12. Рекомендации по развитию

1. **Добавить watchdog** — `machine.WDT` для защиты от зависаний
2. **Разделить DoIP в отдельную задачу** — `uasyncio` или `_thread` для неблокирующего чтения VIN
3. **Кэширование VIN в RTC memory** — выживание после deep sleep
4. **Поддержка CAN FD / 29-bit** — расширение `rid()` и `sh` парсинга
5. **HTTPS + mDNS** — `http://elm327.local/` вместо IP
6. **Логирование на SD** — кольцевой буфер для пост-мартма
7. **OTA обновления** — через веб-интерфейс или BLE DFU