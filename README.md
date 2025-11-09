# ShadowCastX-Touch

ShadowCastX-Touch is a lightweight PyQt5 application that embeds a [`scrcpy`](https://github.com/Genymobile/scrcpy) window, giving you a frameless, resizable view of a connected Android device. It adds a few quality-of-life enhancements on top of the default scrcpy experience:

- Automatic detection of the first connected device via `adb`, with manual device selection when multiple devices are present.
- Automatic window embedding and resizing that respects the device aspect ratio.
- Quick screenshots with an interactive cropper and PNG export.
- Clean shutdown handling so the scrcpy process never lingers.

## Requirements

- Windows 10/11 with [ADB](https://developer.android.com/tools/adb) available on the `PATH`.
- [`scrcpy` 3.x](https://github.com/Genymobile/scrcpy) installed locally. You can either place the binary on the `PATH` or point `SCRCPY_EXE` at the executable.
- [`sndcpy`](https://github.com/Genymobile/sndcpy) available when you want audio capture. Place it on the `PATH` or reference it with `SNDCPY_EXE`.
- Python 3.9+ with the following packages installed:
  - `PyQt5`
  - `pywin32`

```
pip install PyQt5 pywin32
```

## Configuration

The application can be configured through module-level constants in `script.py` or environment variables:

- `SCRCPY_EXE` – Absolute path to the scrcpy executable. If omitted, the script will fall back to the environment variable of the same name and finally to whatever is on the `PATH`.
- `SNDCPY_EXE` – Optional path to the sndcpy executable or script used for audio capture.
- `DEVICE_SERIAL` – Optional Android device serial to bind to on startup. When omitted the first available device is used.
- `DEFAULT_MAX_FPS` / `DEFAULT_BITRATE` – Default stream quality values.
- `DEFAULT_SCREENSHOT_DIR` – Destination folder for saved screenshots.

## Usage

1. Connect an Android device with USB debugging enabled.
2. Ensure `adb` detects your device via `adb devices`.
3. Run the script:

```
python script.py
```

4. (Optional) Pick a specific device from the **Device** drop-down, then click **Refresh** if you plug in a new device while the app is running.
5. Click **Start Stream** to launch the embedded scrcpy session.
6. (Optional) Enable **Audio** before starting to mirror sound through sndcpy. The first launch may require accepting the capture prompt on your device.
7. Use **Screenshot** to capture the current frame, optionally cropping before saving.

The application provides basic status messaging and automatically shuts down the scrcpy process when you close the window.

## Troubleshooting

- **scrcpy.exe not found** – Update `SCRCPY_EXE` or place the executable in your `PATH`.
- **No devices listed** – Verify that USB debugging is enabled and that the device is authorised. Running `adb devices` manually should show it as `device` rather than `unauthorized`.
- **Screenshot saving fails** – Confirm that the target directory is writable and that the chosen filename ends with `.png`.

## License

This project inherits the licensing of the upstream repository.
