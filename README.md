# Whiteout Survival - Auto Facility Scanner

An automated computer vision and OCR tool designed to scan Whiteout Survival (WoS) state maps via an Android Emulator. It automatically reads Facility owners and connected alliances, saving hours of manual data entry. 

## Features
- **High-Speed Map Jumping**: Automatically jumps to facility coordinates using precise ADB taps.
- **Smart Perimeter Scanning**: Intelligently scans the 12 tiles surrounding a facility to identify connected alliances.
- **Computer Vision & OCR**: Uses OpenCV and EasyOCR to accurately read popup owner tags, even dynamically detecting where the popup appears on-screen.
- **Google Sheets & CSV Modes**: Directly read from/write to a live Google Sheet, or work entirely offline using a local `facilities.csv` file.
- **Auto-Resume Capability**: Skips facilities that have already been scanned. If the tool crashes or you stop it halfway, just run it again and it picks up exactly where it left off.
- **Auto-Config**: Automatically detects connected emulators (BlueStacks, Nox, LDPlayer, MEmu) and creates config files on the fly.

## Prerequisites

1. **Python 3.8+**
2. **Android Platform Tools (ADB)** - Required for sending click/swipe commands to the emulator. Ensure `adb` is added to your system PATH.
3. **Android Emulator** - Set to **1080x1920** resolution (Portrait mode). BlueStacks 5 is highly recommended. 
4. **Git** (optional, for cloning)

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/wos-facility-scanner.git
   cd wos-facility-scanner
   ```

2. **Install Python dependencies:**
   ```bash
   pip install opencv-python easyocr numpy gspread mss pywin32
   ```

3. **Emulator Setup:**
   - Launch your emulator.
   - Set the display resolution to exactly `1080x1920`.
   - Launch Whiteout Survival and stay on the World Map.
   - Ensure the search coordinates button (the magnifying glass) is accessible and not blocked by chat or menus.

## Usage

### Option A: Offline Mode (CSV)
This is the easiest way to use the scanner without configuring Google Cloud APIs.

1. Simply run the script:
   ```bash
   python auto_8_jump_scanner.py
   ```
2. The script will realize you don't have Google Sheets configured and will auto-generate a `facilities.csv` file.
3. Open `facilities.csv` in Excel or Notepad. Fill out the `ID`, `Name`, `Level`, and `Coordinates` (in `X;Y` format, e.g., `138;666`).
4. Run the script again. It will scan the map and save the results into a new file called `facilities_scanned.csv`.

### Option B: Online Mode (Google Sheets)
This mode allows multiple players to see the results update live on a Google Sheet.

1. **Create a Google Service Account:**
   - Go to Google Cloud Console.
   - Enable the **Google Sheets API** and **Google Drive API**.
   - Create a Service Account and download the JSON key.
   - Rename the downloaded JSON file to `wos-service-account.json` and place it in the same folder as the script.
2. **Share your Sheet:**
   - Open your Google Sheet.
   - Click "Share" and share it with the email address found inside the `wos-service-account.json` file (give it "Editor" permissions).
3. **Configure the Script:**
   - Run the script once to generate `config.json`.
   - Open `config.json` and paste your Google Sheet URL into the `"sheet_url"` field.
4. **Run:**
   ```bash
   python auto_8_jump_scanner.py
   ```
   The script will now read and write directly to your Google Sheet!

## Discord Integration Guide
Since facility scanning is typically a team effort, sharing this data in Discord is highly recommended. 

1. **CSV Mode Users**: You can copy-paste the contents of `facilities_scanned.csv` directly into a Discord spreadsheet parsing bot, or upload the CSV to a private officer channel.
2. **Google Sheets Users (Recommended)**: 
   - Connect your Google Sheet to Discord using a tool like **Zapier** or **Make.com**.
   - Set up a trigger: *When a row in the sheet is updated, send a Discord message.*
   - Since the bot updates the Google Sheet live, your Discord channel will automatically receive a real-time feed of which alliances control which facilities!

## Troubleshooting

- **"ADB not found"**: Ensure `adb.exe` is in your system environment PATH, or specify the exact path to `adb.exe` in `config.json`.
- **"Window capture failed"**: The script uses ultra-fast Windows API screenshots (`mss`). The emulator window must not be minimized (though it can be behind other windows).
- **"Failed to run ADB" or Emulator not responding**: The script will automatically try to `adb kill-server` and restart it. If it still fails, manually restart your emulator.
- **Coordinates wrong format**: Make sure your coordinates are formatted as `X;Y` (with a semicolon). E.g., `553;412`. 

## License
MIT License. Feel free to modify and use it for your alliance!
