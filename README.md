# ShadowCastX-Touch

ShadowCastX-Touch embeds a [`scrcpy`](https://github.com/Genymobile/scrcpy)
mirror directly inside a PyQt5 application on Windows. The tool focuses on a
clean streaming experience and adds a polished screenshot workflow with an
inline crop dialog.

## Features

- 🪟 **Borderless embed** – runs scrcpy as a child window that resizes with the
  application.
- ⚙️ **Smart defaults** – automatically detects the first connected device and
  tunes scrcpy with configurable frame-rate and bitrate options.
- 🖼️ **Screenshot helper** – captures the live view, lets you crop it and saves
  the result to the `images/` folder.
- 🧠 **Robust error handling** – clearer diagnostics when `adb` or `scrcpy`
  cannot be located.

## Requirements

- Windows 10 or later (scrcpy embed relies on the Win32 windowing API).
- Python 3.9+
- PyQt5, `pywin32`, `scrcpy`, and `adb` available on your `PATH`.

The easiest way to install the GUI dependencies is via pip:

```bash
pip install PyQt5 pywin32
```

For scrcpy itself you can either install the official release or point the
`SCRCPY_EXE` environment variable at the binary shipped with your Android SDK.

## Usage

1. Connect your Android device and ensure `adb devices` reports it as
   `device`.
2. Run the script: `python script.py`
3. Click **Start Stream** to launch scrcpy inside the window.
4. Use **Screenshot** to capture and crop frames. Files are saved into the
   `images/` directory.

> 💡 Set the `SCRCPY_EXE` environment variable if scrcpy is not in your PATH or
> is installed in a non-standard location.