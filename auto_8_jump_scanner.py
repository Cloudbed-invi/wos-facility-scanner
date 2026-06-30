import cv2
import easyocr
import numpy as np
import subprocess
import json
import time
import re
import os
import csv
import gspread
import threading
import queue
import mss
import win32gui
from google.oauth2.service_account import Credentials

def load_or_create_config():
    if not os.path.exists('config.json'):
        print("[!] config.json not found. Creating default config...")
        default_config = {
            "adb_serial": "emulator-5554",
            "adb_path": "adb",
            "sheet_url": "",
            "service_account_file": "wos-service-account.json"
        }
        with open('config.json', 'w') as f:
            json.dump(default_config, f, indent=4)
        return default_config
    
    with open('config.json', 'r') as f:
        return json.load(f)

config = load_or_create_config()

print("Loading EasyOCR (GPU Enabled)...")
reader = easyocr.Reader(['en'], gpu=True)
print("EasyOCR loaded.")

def detect_adb_device(adb_path):
    print("[*] Detecting ADB devices...")
    try:
        result = subprocess.run([adb_path, 'devices'], capture_output=True, text=True)
    except FileNotFoundError:
        print(f"[!] ADB not found at path '{adb_path}'. Please install Android platform-tools and add to PATH.")
        return None
    except Exception as e:
        print(f"[!] Failed to run ADB: {e}")
        return None
    
    lines = result.stdout.strip().split('\n')[1:]
    devices = [line.split('\t')[0] for line in lines if '\tdevice' in line]
    
    if len(devices) == 1:
        print(f"[+] Found emulator: {devices[0]}")
        return devices[0]
    elif len(devices) == 0:
        print("[!] No devices found. Attempting to restart ADB server...")
        subprocess.run([adb_path, 'kill-server'])
        subprocess.run([adb_path, 'start-server'])
        time.sleep(2)
        
        result = subprocess.run([adb_path, 'devices'], capture_output=True, text=True)
        lines = result.stdout.strip().split('\n')[1:]
        devices = [line.split('\t')[0] for line in lines if '\tdevice' in line]
        
        if len(devices) == 1:
            print(f"[+] Found emulator after restart: {devices[0]}")
            return devices[0]
        else:
            print(f"[!] Still no device found. Please start your emulator.")
            return None
    else:
        print(f"[!] Multiple devices found: {devices}. Falling back to config.json setting.")
        return None

ADB_PATH = config.get("adb_path", "adb")
auto_serial = detect_adb_device(ADB_PATH)
ADB_SERIAL = auto_serial if auto_serial else config.get("adb_serial", "emulator-5554")

# ─── Find and cache BlueStacks window position ───────────────────────────────
_window_rect = None

def find_bluestacks_window():
    global _window_rect
    titles = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            titles.append((hwnd, win32gui.GetWindowText(hwnd)))
    win32gui.EnumWindows(cb, None)
    
    for hwnd, title in titles:
        if any(kw in title.lower() for kw in ['bluestacks', 'bs5', 'nox', 'ldplayer', 'memu']):
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            # GetClientRect gives size, GetWindowRect gives screen position
            x, y, r, b = win32gui.GetWindowRect(hwnd)
            # Client area offset (title bar)
            client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
            w = right - left
            h = bottom - top
            _window_rect = {"left": client_left, "top": client_top, "width": w, "height": h}
            print(f"[+] Found emulator window: '{title}' at {_window_rect}")
            return _window_rect
    print("[!] Emulator window not found, falling back to ADB screenshot")
    return None

def adb_shell(cmd, wait=True):
    full_cmd = [ADB_PATH, '-s', ADB_SERIAL, 'shell', cmd]
    if wait:
        subprocess.run(full_cmd, capture_output=True)
    else:
        subprocess.Popen(full_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def adb_tap(x, y, sleep_time=0.1):
    adb_shell(f'input tap {x} {y}')
    time.sleep(sleep_time)

# Target resolution — all OCR and crop logic assumes this
TARGET_W, TARGET_H = 1080, 1920

def get_screenshot():
    """Fast Windows window capture, normalized to 1080x1920. Falls back to ADB."""
    global _window_rect
    if _window_rect is None:
        _window_rect = find_bluestacks_window()

    img = None
    if _window_rect is not None:
        try:
            # Find the window handle and restore it if minimized
            titles = []
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    titles.append((hwnd, win32gui.GetWindowText(hwnd)))
            win32gui.EnumWindows(cb, None)

            for hwnd, title in titles:
                if any(kw in title.lower() for kw in ['bluestacks', 'bs5', 'nox', 'ldplayer', 'memu']):
                    import win32con
                    placement = win32gui.GetWindowPlacement(hwnd)
                    if placement[1] == win32con.SW_SHOWMINIMIZED:
                        # Window is minimized — restore it
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        time.sleep(0.5)  # Wait for restore animation
                    break

            with mss.mss() as sct:
                raw = sct.grab(_window_rect)
                img = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        except Exception as e:
            print(f"[!] Window capture failed: {e}, falling back to ADB")

    if img is None:
        # ADB fallback
        cmd = [ADB_PATH, '-s', ADB_SERIAL, 'exec-out', 'screencap', '-p']
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0 or not result.stdout:
            return None
        arr = np.frombuffer(result.stdout, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    if img is None:
        return None

    # Normalize to TARGET resolution so all crop/color logic is consistent
    h, w = img.shape[:2]
    if w != TARGET_W or h != TARGET_H:
        img = cv2.resize(img, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LINEAR)
    return img

def extract_tag(text):
    # EasyOCR can misread [ as ( or { and ] as ) or J or ]
    # So we match any combination of opening/closing bracket-like chars
    match = re.search(r'[\[(\{]([A-Za-z0-9]{2,5})[\]\)JjIi\}|]', text)
    if match:
        return match.group(1).upper()
    # Also try: just 2-5 uppercase letters surrounded by common OCR noise
    match = re.search(r'(?<![A-Za-z])([A-Z]{2,5})(?![A-Za-z])(?=.*(?:Beer|Occupied|by))', text)
    if match:
        return match.group(1).upper()
    return None

# ─── OCR-based owner read (used ONLY for center facility tap) ────────────────
def find_popup_crop(full_image):
    """
    Auto-detect where the popup panel is on screen using color thresholding.
    WoS popups have a dark background with a distinctive golden/amber border.
    Returns a cropped image of just the popup, or the full image as fallback.
    """
    # Convert to HSV for robust color detection
    hsv = cv2.cvtColor(full_image, cv2.COLOR_BGR2HSV)

    # The WoS popup panel background is very dark (near black)
    # Mask for dark region: low saturation, low value
    lower_dark = np.array([0, 0, 10])
    upper_dark = np.array([180, 80, 80])
    dark_mask = cv2.inRange(hsv, lower_dark, upper_dark)

    # The popup has a golden/amber border
    lower_gold = np.array([15, 100, 150])
    upper_gold = np.array([35, 255, 255])
    gold_mask = cv2.inRange(hsv, lower_gold, upper_gold)

    # Dilate gold border to connect it to the dark panel
    kernel = np.ones((10, 10), np.uint8)
    gold_dilated = cv2.dilate(gold_mask, kernel, iterations=2)

    # Combine: popup = dark panel touching a gold border
    popup_mask = cv2.bitwise_and(dark_mask, gold_dilated)
    popup_mask = cv2.dilate(popup_mask, kernel, iterations=3)

    contours, _ = cv2.findContours(popup_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return full_image  # fallback: use full image

    # Pick the largest contour (the popup panel)
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    h, w = full_image.shape[:2]

    # Sanity check: popup should be at least 3% and less than 60% of screen
    if area < 0.03 * h * w or area > 0.60 * h * w:
        return full_image  # fallback

    x, y, bw, bh = cv2.boundingRect(largest)
    # Add a small padding
    pad = 20
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(w, x + bw + pad)
    y2 = min(h, y + bh + pad)
    return full_image[y1:y2, x1:x2]

def read_popup_tag(full_image, use_crop=True):
    """
    Find the popup anywhere on screen, then run OCR on just that region.
    Pass use_crop=False to skip popup detection and OCR the full image directly.
    """
    if full_image is None:
        return ""
    cropped = find_popup_crop(full_image) if use_crop else full_image
    results = reader.readtext(cropped)

    # Strategy 1: find tag near "Occupied by" text
    occupied_y = None
    for (bbox, text, _) in results:
        if re.search(r'[Oo]ccup', text):
            occupied_y = (bbox[0][1] + bbox[2][1]) / 2
            break

    if occupied_y is not None:
        best_tag, min_dist = "", 9999
        for (bbox, text, _) in results:
            tag = extract_tag(text)
            if tag:
                by = (bbox[0][1] + bbox[2][1]) / 2
                dist = abs(by - occupied_y)
                if dist < min_dist and dist < 150:
                    min_dist, best_tag = dist, tag
        if best_tag:
            return best_tag

    # Strategy 2: return first tag we find anywhere in the popup
    for (_, text, prob) in results:
        if prob > 0.35:
            tag = extract_tag(text)
            if tag:
                return tag
    return ""


# ─── Jump logic ─────────────────────────────────────────────────────────────
def jump_to_coordinates(x, y, prev_x=None, prev_y=None):
    taps = config['taps']
    del_keys = "input keyevent 67 67 67 67 67"

    parts = [
        f"input tap {taps['search_map_icon'][0]} {taps['search_map_icon'][1]}",
        "sleep 0.7"
    ]

    if str(prev_x) != str(x):
        parts += [
            f"input tap {taps['x_input_box'][0]} {taps['x_input_box'][1]}",
            "sleep 0.2",
            del_keys,
            f"input text {x}",
            f"input tap {taps['x_ok_button'][0]} {taps['x_ok_button'][1]}",
            "sleep 0.1"
        ]

    if str(prev_y) != str(y):
        parts += [
            f"input tap {taps['y_input_box'][0]} {taps['y_input_box'][1]}",
            "sleep 0.2",
            del_keys,
            f"input text {y}",
            f"input tap {taps['y_ok_button'][0]} {taps['y_ok_button'][1]}",
            "sleep 0.1"
        ]

    parts.append(f"input tap {taps['go_button'][0]} {taps['go_button'][1]}")
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
def scan_facility(base_x, base_y, need_owner=True, need_connected=True):
    start = time.time()
    owner = ""
    connected = set()

    # ── Step 0: Read Facility Owner (only if needed) ──
    if need_owner:
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

        if not owner and last_img is not None:
            # Save debug screenshot so we can inspect what the bot saw
            debug_path = f"debug_fail_{base_x}_{base_y}.png"
            cv2.imwrite(debug_path, last_img)
            # Print raw OCR text so we know what it found
            raw = reader.readtext(last_img)
            print(f"  [DEBUG] Saved screenshot: {debug_path}")
            print(f"  [DEBUG] Raw OCR found: {[(t, round(p,2)) for _,t,p in raw if p > 0.3]}")

        print(f"  [+] Owner: '{owner}'")
        adb_tap(10, 500, sleep_time=0.4)

    if not need_connected:
        print(f"  Done in {time.time()-start:.1f}s (owner-only scan)")
        return owner, []

    # ── Step 1: Perimeter — deduplicated 12 unique points ──
    # sweep covers +9 to -7 (the full facility width)
    # Corners are counted once in the Right side, not repeated in other sides.
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
    return owner, sorted(connected)

# ─── Full scanner ─────────────────────────────────────────────────────────────
def load_data_from_csv(csv_path):
    rows = []
    if not os.path.exists(csv_path):
        print(f"[!] {csv_path} not found. Creating a template...")
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['ID', 'Name', 'Level', 'Coordinates', 'Owner', 'Connected'])
            writer.writerow(['1', 'Construction Facility', 'Lv. 1', '138;666', '', ''])
        print(f"[+] Created {csv_path}. Please fill it out and run again.")
        return None
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        data = list(reader)
    return data

def save_data_to_csv(csv_path, data):
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(data)

def run_full_scan():
    use_sheets = False
    worksheet = None
    data = None
    csv_file = 'facilities.csv'

    # Try setting up Google Sheets
    service_acc = config.get("service_account_file", "wos-service-account.json")
    sheet_url = config.get("sheet_url", "")
    
    if os.path.exists(service_acc) and sheet_url:
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
            print(f"[!] Failed to connect to Google Sheets: {e}")
            print("[*] Falling back to CSV mode...")
            use_sheets = False
    else:
        print("[*] Google Sheets config missing or incomplete. Using CSV mode.")
        use_sheets = False

    if not use_sheets:
        data = load_data_from_csv(csv_file)
        if data is None:
            return  # Template was just created, exit

    if not data or len(data) < 2:
        print("[!] No data found to scan.")
        return

    rows = data[1:]  # Skip header row

    # Collect rows with coordinates where Owner is still empty
    to_scan = []
    already_done = 0
    for i, row in enumerate(rows):
        # Pad row if necessary (CSV might have missing columns)
        while len(row) < 6:
            row.append("")
            
        coords_str = row[3].strip()
        owner_val  = row[4].strip()
        conn_val   = row[5].strip()

        parts = coords_str.split(';')
        if len(parts) != 2:
            continue
        try:
            x, y = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            continue

        sheet_row = i + 2  # 1-indexed for Sheets, or +1 for header
        owner_empty = not owner_val or owner_val == "ERROR"
        conn_empty  = not conn_val

        if not owner_empty:
            already_done += 1
        else:
            to_scan.append((sheet_row, x, y, owner_empty, conn_empty, i+1)) # i+1 is the index in data array

    total = len(to_scan)
    print(f"\n[+] Already filled : {already_done} facilities (skipping)")
    print(f"[+] To scan        : {total} facilities")
    print(f"[+] Estimated time : {total * 2:.0f} - {total * 3:.0f} minutes")
    print(f"[+] Starting scan...\n")

    done = 0
    errors = 0
    start_all = time.time()

    for (sheet_row, x, y, need_owner, need_connected, data_idx) in to_scan:
        done += 1
        elapsed = time.time() - start_all
        eta = (elapsed / done) * (total - done) if done > 1 else 0
        mode = "Owner+Connected" if (need_owner and need_connected) else ("Owner only" if need_owner else "Connected only")
        print(f"\n{'='*55}")
        print(f"[{done}/{total}] Row {sheet_row} | Coords: {x},{y} | Mode: {mode} | ETA: {eta/60:.1f} min")

        try:
            auto_owner, auto_connected = scan_facility(x, y, need_owner=need_owner, need_connected=need_connected)
            connected_str = ", ".join(auto_connected)

            if use_sheets:
                if need_owner:
                    worksheet.update_cell(sheet_row, 5, auto_owner)
                if need_connected:
                    worksheet.update_cell(sheet_row, 6, connected_str)
                print(f"  [Sheet] Written → Owner: '{auto_owner}', Connected: '{connected_str}'")
                time.sleep(1.5)  # Avoid rate limits
            else:
                if need_owner:
                    data[data_idx][4] = auto_owner
                if need_connected:
                    data[data_idx][5] = connected_str
                # Save locally immediately
                save_data_to_csv('facilities_scanned.csv', data)
                print(f"  [CSV] Written → Owner: '{auto_owner}', Connected: '{connected_str}'")

        except Exception as e:
            errors += 1
            print(f"  [ERROR] Row {sheet_row}: {e}")
            if use_sheets:
                try:
                    worksheet.update_cell(sheet_row, 5, "ERROR")
                except:
                    pass
            else:
                data[data_idx][4] = "ERROR"
                save_data_to_csv('facilities_scanned.csv', data)

    total_time = time.time() - start_all
    print(f"\n{'='*55}")
    print(f"SCAN COMPLETE!")
    print(f"  Facilities scanned : {done}/{total}")
    print(f"  Errors             : {errors}")
    print(f"  Total time         : {total_time/60:.1f} minutes")

if __name__ == '__main__':
    run_full_scan()

