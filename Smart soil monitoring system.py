from machine import Pin, I2C, ADC
from npk import read_npk_values, display_npk_oled
import ssd1306
import network
import time
import urequests
from dht import DHT11
from secrets import wifi_ssid, wifi_password, base_url, filename_url, sensor_url, supabase_key

# Constants
SENSOR_READ_INTERVAL = 5
FILENAME_CACHE_DURATION = 300
BATCH_SIZE = 5
BATCH_INTERVAL = 30
MAX_READINGS = 20
DISABLED_POLL_INTERVAL = 5

# Initialize I2C
i2c = I2C(0, scl=Pin(1), sda=Pin(0), freq=400000)

# Initialize OLED
try:
    oled = ssd1306.SSD1306_I2C(128, 64, i2c)
    oled.fill(0)
    oled.text("System Starting...", 0, 0)
    oled.show()
except Exception:
    oled = None
    print("[ERROR] OLED initialization failed.")

# Initialize Sensors
dht_sensor = DHT11(Pin(15)) if Pin(15) else None
moisture_sensor = ADC(Pin(26)) if Pin(26) else None

# Initialize success flag at boot
def initialize_success_flag():
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    try:
        url = f"{base_url}/rest/v1/current_filename?select=id&order=id.desc&limit=1"
        response = urequests.get(url, headers=headers)
        data = response.json()
        response.close()
        if data and isinstance(data, list):
            row_id = data[0].get("id")
            set_success_flag(row_id, False)
            return row_id
    except Exception as e:
        print("[ERROR] Failed to initialize success flag:", e)
    return None

def is_collection_enabled():
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    try:
        url = f"{base_url}/rest/v1/current_filename?select=id,trigger_value&order=id.desc&limit=1"
        response = urequests.get(url, headers=headers)
        data = response.json()
        response.close()
        if data and isinstance(data, list):
            return data[0].get("trigger_value", False), data[0].get("id")
    except Exception as e:
        print("[ERROR] Failed to fetch control flag:", e)
    return False, None

def set_success_flag(row_id, value):
    if row_id is None:
        print("[ERROR] No ID provided to set success flag.")
        return
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json"
    }
    url = f"{base_url}/rest/v1/current_filename?id=eq.{row_id}"
    payload = {"success": value}
    try:
        response = urequests.patch(url, json=payload, headers=headers)
        if response.status_code in (200, 204):
            print(f"[INFO] Success flag set to {value}.")
        else:
            print(f"[WARNING] Failed to set success flag. Status: {response.status_code}")
            print(response.text)
        response.close()
    except Exception as e:
        print("[ERROR] Failed to set success flag:", e)

def reset_trigger_flag(row_id):
    if row_id is None:
        print("[ERROR] No ID provided to reset flag.")
        return
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json"
    }
    url = f"{base_url}/rest/v1/current_filename?id=eq.{row_id}"
    payload = {"trigger_value": False}
    try:
        response = urequests.patch(url, json=payload, headers=headers)
        if response.status_code in (200, 204):
            print("[INFO] Trigger flag reset to False.")
        else:
            print(f"[WARNING] Failed to reset flag. Status: {response.status_code}")
            print(response.text)
        response.close()
    except Exception as e:
        print("[ERROR] Failed to reset trigger flag:", e)

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return True
    wlan.connect(wifi_ssid, wifi_password)
    timeout = 10
    start = time.time()
    while not wlan.isconnected() and (time.time() - start) < timeout:
        time.sleep(0.5)
    if wlan.isconnected():
        print(f"[INFO] WiFi connected. IP: {wlan.ifconfig()[0]}")
        return True
    print("[ERROR] WiFi connection failed.")
    return False

def fetch_filename():
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    try:
        response = urequests.get(filename_url, headers=headers)
        filename = response.json()[0]['filename']
        response.close()
        print(f"[INFO] Retrieved filename: {filename}")
        return filename
    except Exception:
        print("[WARNING] Failed to fetch filename.")
        return None

def send_to_supabase(batched_data):
    if not batched_data:
        return
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json"}
    try:
        print("[DEBUG] Sending payload to Supabase:", batched_data)
        response = urequests.post(sensor_url, json=batched_data, headers=headers)
        if response.status_code == 201:
            print(f"[INFO] Successfully sent {len(batched_data)} readings to Supabase.")
        else:
            print(f"[ERROR] Supabase response: {response.status_code} - {response.text}")
        response.close()
    except Exception as e:
        print("[ERROR] Upload to Supabase failed:", e)

def get_dht11_data():
    if dht_sensor:
        try:
            dht_sensor.measure()
            return dht_sensor.temperature(), dht_sensor.humidity()
        except Exception:
            return None, None
    return None, None

def get_moisture():
    if moisture_sensor:
        try:
            raw = moisture_sensor.read_u16()
            return round((65535 - raw) * 100 / 65535, 2)
        except Exception:
            return None
    return None

# Main execution
if not connect_wifi():
    print("[FATAL] WiFi connection failed. Halting system.")
    if oled:
        oled.fill(0)
        oled.text("WiFi Error", 0, 0)
        oled.show()
    while True:
        time.sleep(1)

row_id = initialize_success_flag()

last_sensor_update = time.time()
last_filename_fetch = 0
cached_filename = None
batched_data = []
last_batch_sent = time.time()
reading_count = 0
last_disabled_log = 0

while reading_count < MAX_READINGS:
    trigger_value, current_row_id = is_collection_enabled()
    if current_row_id:
        row_id = current_row_id
    if not trigger_value:
        now = time.time()
        if now - last_disabled_log >= DISABLED_POLL_INTERVAL:
            print("[INFO] Data collection disabled via Supabase.")
            last_disabled_log = now
            if oled:
                oled.fill(0)
                oled.text("Collection Off", 0, 0)
                oled.show()
        time.sleep(DISABLED_POLL_INTERVAL)
        continue

    now = time.time()
    if now - last_filename_fetch >= FILENAME_CACHE_DURATION or cached_filename is None:
        cached_filename = fetch_filename() or "unknown.png"
        last_filename_fetch = now

    if now - last_sensor_update >= SENSOR_READ_INTERVAL:
        temp, humidity = get_dht11_data()
        moisture = get_moisture()
        n, p, k = read_npk_values()
        print(f"[NPK] N: {n}, P: {p}, K: {k}")

        if oled:
            oled.fill(0)
            oled.text(f"Read {reading_count + 1}/{MAX_READINGS}", 0, 0)
            oled.text(f"T: {temp or '--'}C", 0, 16)
            oled.text(f"H: {humidity or '--'}%", 0, 30)
            oled.text(f"M: {moisture or '--'}%", 0, 44)
            oled.text(f"N:{n} P:{p} K:{k}", 0, 56)
            oled.show()

        data_point = {
            "filename": cached_filename or "unknown.png",
            "temperature": temp if temp is not None else 0,
            "humidity": humidity if humidity is not None else 0,
            "moisture": moisture if moisture is not None else 0,
            "nitrogen": n if n is not None else 0,
            "phosphorus": p if p is not None else 0,
            "potassium": k if k is not None else 0
        }

        print("[DEBUG] Collected data:", data_point)
        batched_data.append(data_point)
        reading_count += 1
        last_sensor_update = now

    if len(batched_data) >= BATCH_SIZE or (now - last_batch_sent >= BATCH_INTERVAL and batched_data):
        if connect_wifi():
            send_to_supabase(batched_data)
            batched_data = []
            last_batch_sent = now

    time.sleep(0.05)

# Final upload
if batched_data and connect_wifi():
    send_to_supabase(batched_data)

if reading_count >= MAX_READINGS:
    set_success_flag(row_id, True)

print(f"[INFO] Completed. {MAX_READINGS} readings sent.")
if oled:
    oled.fill(0)
    oled.text("Upload Complete", 0, 0)
    oled.text(f"{MAX_READINGS} sent", 0, 20)
    oled.show()

reset_trigger_flag(row_id)

while True:
    time.sleep(1)
