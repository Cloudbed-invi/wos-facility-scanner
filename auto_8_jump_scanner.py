import csv
import os
import cv2
import easyocr
import numpy as np
import subprocess
import json
import time
import re
import gspread
import threading
import queue
import mss
import win32gui
from google.oauth2.service_account import Credentials

with open('config.json', 'r') as f:
    config = json.load(f)

print("Loading EasyOCR (GPU Enabled)...")
reader = easyocr.Reader(['en'], gpu=True)
print("EasyOCR loaded.")

ADB_PATH = config.get("adb_path", "adb")

def setup_adb_connection():
    configured_serial = config.get("adb_serial", "127.0.0.1:7555")
    # Always attempt connecting to common emulator ports if using network ADB
    for port in ['127.0.0.1:7555', '127.0.0.1:16384', '127.0.0.1:5555']:
        try:
            subprocess.run([ADB_PATH, 'connect', port], capture_output=True, timeout=3)
        except subprocess.TimeoutExpired:
            pass
    
    try:
        res = subprocess.run([ADB_PATH, 'devices'], capture_output=True, text=True)
        lines = [l.strip() for l in res.stdout.strip().split('\n')[1:] if '\tdevice' in l]
        if lines:
            # If configured serial is present, prefer it
            serials = [l.split('\t')[0] for l in lines]
            if configured_serial in serials:
                serial = configured_serial
            else:
                serial = serials[0]
            print(f"[+] Connected to ADB device: {serial}")
            return serial
    except Exception as e:
        print(f"[!] ADB detection failed: {e}")
    
    print(f"[!] Falling back to configured serial: {configured_serial}")
    return configured_serial

ADB_SERIAL = setup_adb_connection()

# ─── Dynamic Resolution Scaling ──────────────────────────────────────────────
TARGET_W, TARGET_H = 1080, 1920

def get_device_resolution():
    try:
        res = subprocess.run([ADB_PATH, '-s', ADB_SERIAL, 'shell', 'wm', 'size'], capture_output=True, text=True)
        match = re.search(r'(\d+)x(\d+)', res.stdout)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            return w, h
    except Exception as e:
        print(f"[!] Could not detect wm size: {e}")
    return 1080, 1920

DEVICE_W, DEVICE_H = get_device_resolution()
print(f"[+] Emulator Physical Resolution: {DEVICE_W}x{DEVICE_H}")

SCALE_X = DEVICE_W / float(TARGET_W)
SCALE_Y = DEVICE_H / float(TARGET_H)
print(f"[+] Coordinate Scaling Factors: X={SCALE_X:.4f}, Y={SCALE_Y:.4f}")

def scale_coords(x, y):
    """Converts 1080x1920 base coordinates to actual emulator resolution."""
    return int(round(x * SCALE_X)), int(round(y * SCALE_Y))

def adb_shell(cmd, wait=True):
    full_cmd = [ADB_PATH, '-s', ADB_SERIAL, 'shell', cmd]
    if wait:
        subprocess.run(full_cmd, capture_output=True)
    else:
        subprocess.Popen(full_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def adb_tap(x, y, sleep_time=0.1):
    sx, sy = scale_coords(x, y)
    adb_shell(f'input tap {sx} {sy}')
    time.sleep(sleep_time)

# Target resolution — all OCR and crop logic assumes this
TARGET_W, TARGET_H = 1080, 1920

def get_screenshot():
    """Captures screenshot directly from ADB for 100% clean emulator framebuffer."""
    cmd = [ADB_PATH, '-s', ADB_SERIAL, 'exec-out', 'screencap', '-p']
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    arr = np.frombuffer(result.stdout, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    # Normalize to TARGET resolution so all coordinate logic is consistent
    h, w = img.shape[:2]
    if w != TARGET_W or h != TARGET_H:
        img = cv2.resize(img, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LINEAR)
    return img

def extract_tag(text):
    if not text:
        return None
    # Match tags like [FOR], [ZROJ, etc.
    # EasyOCR often turns ']' into 'J', 'j', 'I', 'i', 'L', 'l', '1', '}', ')'
    match = re.search(r'[\[\(\{]([A-Za-z0-9]{3})[\]\)\}JjIiLl1\|]', text)
    if match:
        tag = match.group(1).upper()
        if not re.match(r'^\d+$', tag) and tag not in ['VIP', 'ROW', 'COO', 'MIN', 'UTC', 'ETA']:
            return tag
    return None

def read_popup_tag(full_image, use_crop=False):
    """
    Scans the screen popup zone (excluding top HUD and bottom chat/navigation bar).
    Supports multi-language 'Occupied by' field or directly detects alliance tag [TAG].
    """
    if full_image is None:
        return ""

    img_h, img_w = full_image.shape[:2]
    results = reader.readtext(full_image)

    # Filter out top HUD (< 15% height) and bottom Chat/Nav (> 84% height)
    min_y = img_h * 0.15
    max_y = img_h * 0.84

    mid_results = []
    for (bbox, text, prob) in results:
        cy = (bbox[0][1] + bbox[2][1]) / 2
        if min_y <= cy <= max_y and prob > 0.2:
            mid_results.append((bbox, text, prob, cy))

    # Priority 1: Check for 'Occupied by' (including multi-language variants)
    # e.g., 'Occupied', 'Occup', 'Occupe', 'Besetzt', 'Ocupad', 'Оккуп', '占'
    occ_cy = None
    for (bbox, text, prob, cy) in mid_results:
        if re.search(r'[Oo]ccup|Besetz|[Oo]cupad|Оккуп|占', text, re.I):
            occ_cy = cy
            # Check if this line itself says 'None' or 'Empty'
            if re.search(r'\bNone\b|\bEmpty\b|Aucun|Kein|Никто', text, re.I):
                return ""
            tag = extract_tag(text)
            if tag:
                return tag
            break

    if occ_cy is not None:
        # Check lines close to the 'Occupied' line vertically
        candidates = []
        for (bbox, text, prob, cy) in mid_results:
            dist = abs(cy - occ_cy)
            if dist < 120:
                if re.search(r'\bNone\b|\bEmpty\b|Aucun|Kein|Никто', text, re.I):
                    return ""
                tag = extract_tag(text)
                if tag:
                    candidates.append((dist, tag))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]

    # Priority 2: Any valid [TAG] in the popup zone
    for (bbox, text, prob, cy) in mid_results:
        tag = extract_tag(text)
        if tag:
            return tag

    return ""


# ─── Jump logic ─────────────────────────────────────────────────────────────
def jump_to_coordinates(x, y, prev_x=None, prev_y=None):
    taps = config['taps']
    del_keys = "input keyevent 67 67 67 67 67"

    # Scale the 1080x1920 base tap coordinates to actual emulator resolution
    search_x, search_y = scale_coords(*taps['search_map_icon'])
    x_box_x, x_box_y = scale_coords(*taps['x_input_box'])
    x_ok_x, x_ok_y = scale_coords(*taps['x_ok_button'])
    y_box_x, y_box_y = scale_coords(*taps['y_input_box'])
    y_ok_x, y_ok_y = scale_coords(*taps['y_ok_button'])
    go_x, go_y = scale_coords(*taps['go_button'])

    parts = [
        f"input tap {search_x} {search_y}",
        "sleep 0.7"
    ]

    if str(prev_x) != str(x):
        parts += [
            f"input tap {x_box_x} {x_box_y}",
            "sleep 0.2",
            del_keys,
            f"input text {x}",
            f"input tap {x_ok_x} {x_ok_y}",
            "sleep 0.1"
        ]

    if str(prev_y) != str(y):
        parts += [
            f"input tap {y_box_x} {y_box_y}",
            "sleep 0.2",
            del_keys,
            f"input text {y}",
            f"input tap {y_ok_x} {y_ok_y}",
            "sleep 0.1"
        ]

    parts.append(f"input tap {go_x} {go_y}")
    adb_shell(" && ".join(parts))
    time.sleep(1.6)  # Jump animation

# ─── Background OCR worker ───────────────────────────────────────────────────
def ocr_worker(ocr_queue, results_list):
    while True:
        task = ocr_queue.get()
        if task is None:
            break
        i, img = task
        tag = read_popup_tag(img)
        if tag:
            results_list.append((i, tag))
        ocr_queue.task_done()

# ─── Main scan ───────────────────────────────────────────────────────────────
def scan_facility(base_x, base_y, need_owner=True, need_connected=True, row_num=None):
    start = time.time()
    owner = ""
    connected = set()
    img_path = ""

    os.makedirs('facility_images', exist_ok=True)

    # ── Step 0: Read Facility Owner & Save Facility Image ──
    print(f"  [Jump 0] Center ({base_x}, {base_y}) → reading owner...")
    jump_to_coordinates(base_x, base_y)
    last_img = None
    for attempt in range(3):
        adb_tap(540, 960, sleep_time=1.0)
        img = get_screenshot()
        last_img = img
        owner = read_popup_tag(img, use_crop=False)
        if owner:
            break
        print(f"  [!] Owner empty, retry {attempt+1}/3...")
        time.sleep(0.5)

    # Save screenshot of facility popup
    if last_img is not None:
        tag_str = f"_{owner}" if owner else ""
        row_str = f"Row{row_num}_" if row_num else ""
        img_path = f"facility_images/{row_str}facility_X{base_x}_Y{base_y}{tag_str}.png"
        cv2.imwrite(img_path, last_img)
        print(f"  [+] Saved facility screenshot: {img_path}")

    if not owner and last_img is not None:
        debug_path = f"debug_fail_{base_x}_{base_y}.png"
        cv2.imwrite(debug_path, last_img)
        raw = reader.readtext(last_img)
        print(f"  [DEBUG] Saved screenshot: {debug_path}")
        print(f"  [DEBUG] Raw OCR found: {[(t, round(p,2)) for _,t,p in raw if p > 0.3]}")

    print(f"  [+] Owner: '{owner}'")
    adb_tap(10, 500, sleep_time=0.4)

    if not need_connected:
        print(f"  Done in {time.time()-start:.1f}s (owner-only scan)")
        return owner, [], img_path

    # ── Step 1: Perimeter — deduplicated 12 unique points ──
    sweep = [+9, +3, -2, -7]
    jump_points = []

    # Right side: all 4 including both corners
    for dy in sweep:
        jump_points.append((base_x + 9, base_y + dy, f"Right y{dy:+d}"))

    # Bottom side: skip first point (+9,-7) — already covered by Right
    for dx in sweep[1:]:
        jump_points.append((base_x + dx, base_y - 7, f"Bottom x{dx:+d}"))

    # Left side: skip first point (-7,-7) — already covered by Bottom
    for dy in list(reversed(sweep))[1:]:
        jump_points.append((base_x - 7, base_y + dy, f"Left y{dy:+d}"))

    # Top side: skip first point (-7,+9) — already covered by Left
    #           skip last point  (+9,+9) — already covered by Right
    for dx in list(reversed(sweep))[1:-1]:
        jump_points.append((base_x + dx, base_y + 9, f"Top x{dx:+d}"))

    total_jumps = len(jump_points)  # Should be 4+3+3+2 = 12 unique points

    # ── Producer-Consumer threading ──
    ocr_queue = queue.Queue()
    results_list = []
    t = threading.Thread(target=ocr_worker, args=(ocr_queue, results_list), daemon=True)
    t.start()

    prev_x = base_x if not need_owner else jump_points[0][0]  # avoid re-typing if skipped center
    prev_y = base_y if not need_owner else jump_points[0][1]
    prev_x, prev_y = base_x, base_y

    for i, (jx, jy, desc) in enumerate(jump_points):
        print(f"  [Jump {i+1}/{total_jumps}] {desc} ({jx},{jy})")
        jump_to_coordinates(jx, jy, prev_x, prev_y)
        prev_x, prev_y = jx, jy
        adb_tap(540, 960, sleep_time=0.8)
        img = get_screenshot()
        ocr_queue.put((i + 1, img))
        adb_tap(10, 500, sleep_time=0.3)

    print("  [Waiting] OCR finishing...")
    ocr_queue.join()
    ocr_queue.put(None)
    t.join()

    for (i, tag) in results_list:
        if tag and tag.upper() != owner.upper():
            print(f"    → Point {i} owned by: {tag}")
            connected.add(tag)

    print(f"  Done in {time.time()-start:.1f}s → Owner: {owner}, Connected: {sorted(connected)}")
    return owner, sorted(connected), img_path

# ─── Full scanner ─────────────────────────────────────────────────────────────
def load_data_from_csv(csv_path):
    scanned_file = 'facilities_scanned.csv'
    # If facilities_scanned.csv already exists, use it to resume progress!
    target_file = scanned_file if os.path.exists(scanned_file) else csv_path

    if not os.path.exists(target_file):
        print(f"[*] {target_file} not found. Auto-generating from master facility database...")
        master_file = 'facilities_master.json'
        facs = []
        if os.path.exists(master_file):
            with open(master_file, 'r', encoding='utf-8') as f:
                facs = json.load(f)
        with open(target_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Row', 'Facility Type', 'Bonus', 'Level', 'Coordinates', 'Current Owner', 'Connected', 'Screenshot'])
            for item in facs:
                writer.writerow([item.get('row', ''), item.get('type', ''), item.get('bonus', ''), item.get('level', ''), item.get('coords', ''), '', '', ''])
        print(f"[+] Created {target_file} with {len(facs)} facilities.")
    else:
        print(f"[+] Loaded facility database from: {target_file}")

    with open(target_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        data = list(reader)
    
    if data and len(data[0]) == 7:
        data[0].append('Screenshot')
        
    return data

def save_data_to_csv(csv_path, data):
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(data)

def run_full_scan():
    import sys
    use_sheets = False
    worksheet = None
    data = None
    csv_file = 'facilities.csv'

    force_csv = '--csv' in sys.argv or config.get("mode") == "csv"
    service_acc = config.get("service_account_file", "wos-service-account.json")
    sheet_url = config.get("sheet_url", "")

    if force_csv:
        print("[*] Running in CSV mode (offline / local file).")
        use_sheets = False
    elif os.path.exists(service_acc) and sheet_url:
        try:
            print("[*] Connecting to Google Sheets...")
            scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
            creds = Credentials.from_service_account_file(service_acc, scopes=scopes)
            gc = gspread.authorize(creds)
            worksheet = gc.open_by_url(sheet_url).sheet1
            data = worksheet.get_all_values()
            use_sheets = True
            print("[+] Google Sheets connected!")
        except Exception as e:
            print(f"[!] Google Sheets connection failed: {e}")
            print("[*] Falling back to CSV mode...")
            use_sheets = False
    else:
        print("[*] Google Sheets credentials not configured. Running in CSV mode.")
        use_sheets = False

    if not use_sheets:
        data = load_data_from_csv(csv_file)

    if not data or len(data) < 2:
        print("[!] No facilities found to scan.")
        return

    rows = data[1:]  # Skip header row

    to_scan = []
    already_done = 0
    for i, row in enumerate(rows):
        while len(row) < 7:
            row.append("")

        coords_str = row[4].strip() if (not use_sheets and ';' in row[4]) else (row[3].strip() if ';' in row[3] else '')
        owner_idx = 5 if (not use_sheets and ';' in row[4]) else 4
        conn_idx = 6 if (not use_sheets and ';' in row[4]) else 5

        owner_val = row[owner_idx].strip()
        conn_val = row[conn_idx].strip()

        parts = coords_str.split(';')
        if len(parts) != 2:
            continue
        try:
            x, y = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            continue

        sheet_row = i + 2
        owner_empty = not owner_val or owner_val == "ERROR"
        conn_empty = not conn_val

        if not owner_empty:
            already_done += 1
        else:
            to_scan.append((sheet_row, x, y, owner_empty, conn_empty, i + 1, owner_idx, conn_idx))

    total = len(to_scan)
    print(f"\n[+] Already filled : {already_done} facilities (skipping)")
    print(f"[+] To scan        : {total} facilities")
    print(f"[+] Estimated time : {total * 2:.0f} - {total * 3:.0f} minutes")
    print(f"[+] Starting scan...\n")

    done = 0
    errors = 0
    start_all = time.time()

    for (sheet_row, x, y, need_owner, need_connected, data_idx, owner_idx, conn_idx) in to_scan:
        done += 1
        elapsed = time.time() - start_all
        eta = (elapsed / done) * (total - done) if done > 1 else 0
        mode = "Owner+Connected" if (need_owner and need_connected) else ("Owner only" if need_owner else "Connected only")
        print(f"\n{'='*55}")
        print(f"[{done}/{total}] Row {sheet_row} | Coords: {x},{y} | Mode: {mode} | ETA: {eta/60:.1f} min")

        try:
            auto_owner, auto_connected, img_file = scan_facility(x, y, need_owner=need_owner, need_connected=need_connected, row_num=sheet_row)
            connected_str = ", ".join(auto_connected)

            if use_sheets:
                if need_owner:
                    worksheet.update_cell(sheet_row, owner_idx + 1, auto_owner)
                if need_connected:
                    worksheet.update_cell(sheet_row, conn_idx + 1, connected_str)
                print(f"  [Sheet] Written → Owner: '{auto_owner}', Connected: '{connected_str}'")
                time.sleep(1.5)
            else:
                if need_owner:
                    data[data_idx][owner_idx] = auto_owner
                if need_connected:
                    data[data_idx][conn_idx] = connected_str
                while len(data[data_idx]) < 8:
                    data[data_idx].append("")
                data[data_idx][7] = img_file
                for attempt in range(60): # wait up to 5 minutes
                    try:
                        save_data_to_csv('facilities_scanned.csv', data)
                        break
                    except PermissionError:
                        print("  [!] CSV is open in another program (e.g., Excel). Please close it! Retrying in 5s...")
                        time.sleep(5)
                print(f"  [CSV] Written → Owner: '{auto_owner}', Connected: '{connected_str}' (Image: {img_file})")

        except Exception as e:
            errors += 1
            print(f"  [ERROR] Row {sheet_row}: {e}")
            if use_sheets:
                try:
                    worksheet.update_cell(sheet_row, owner_idx + 1, "ERROR")
                except:
                    pass
            else:
                data[data_idx][owner_idx] = "ERROR"
                for attempt in range(5):
                    try:
                        save_data_to_csv('facilities_scanned.csv', data)
                        break
                    except PermissionError:
                        print("  [!] CSV is open in another program (e.g., Excel). Please close it! Retrying in 5s...")
                        time.sleep(5)

    total_time = time.time() - start_all
    print(f"\n{'='*55}")
    print(f"SCAN COMPLETE!")
    print(f"  Facilities scanned : {done}/{total}")
    print(f"  Errors             : {errors}")
    print(f"  Total time         : {total_time/60:.1f} minutes")

if __name__ == '__main__':
    run_full_scan()
