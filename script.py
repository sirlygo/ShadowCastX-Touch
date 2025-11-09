# script.py — Embedded scrcpy window inside PyQt5 (Windows)
# Works with scrcpy 3.x (uses --video-bit-rate and --window-borderless)

import os
import sys
import subprocess
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QMessageBox
)

import win32con, win32gui

# ====== CONFIG ======
SCRCPY_EXE = r"C:\Users\hilli\PycharmProjects\adbhelper\.venv\scrcpy\scrcpy.exe"
DEVICE_SERIAL: Optional[str] = None  # e.g. "R5CT60SV0RX"
SCRCPY_TITLE = "Android (Embedded)"
DEFAULT_MAX_FPS = 60
DEFAULT_BITRATE = "16M"
# ====================


def _resolve_scrcpy() -> Optional[str]:
    if SCRCPY_EXE and os.path.isfile(SCRCPY_EXE):
        return SCRCPY_EXE
    env = os.environ.get("SCRCPY_EXE")
    if env and os.path.isfile(env):
        return env
    from shutil import which
    return which("scrcpy")


def get_first_device() -> Optional[str]:
    try:
        out = subprocess.check_output(["adb", "devices"], stderr=subprocess.STDOUT).decode("utf-8","ignore")
        for line in out.splitlines()[1:]:
            if "\tdevice" in line:
                return line.split("\t")[0]
    except:
        pass
    return None


class ScrcpyController(QObject):
    started = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, serial: Optional[str]):
        super().__init__()
        self.serial = serial
        self.proc = None
        self.hwnd = None
        self.resolution = None
        self.poll = QTimer()
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._find_window)

    def start(self, fps=DEFAULT_MAX_FPS, bitrate=DEFAULT_BITRATE):
        exe = _resolve_scrcpy()
        if not exe:
            self.error.emit("scrcpy.exe not found. Set SCRCPY_EXE to full path.")
            return

        self._update_resolution()

        args = [
            exe,
            f"--window-title={SCRCPY_TITLE}",
            "--window-borderless",         # ✅ updated for scrcpy 3.2
            f"--max-fps={fps}",
            f"--video-bit-rate={bitrate}", # ✅ updated
            "--stay-awake",
            "--no-audio",
        ]
        if self.serial:
            args.insert(1, "-s")
            args.insert(2, self.serial)

        try:
            self.proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        except Exception as e:
            self.error.emit(str(e))
            return

        self.poll.start()

    def stop(self):
        self.poll.stop()
        if self.proc and self.proc.poll() is None:
            try: self.proc.terminate()
            except: pass
        self.proc = None
        self.hwnd = None
        self.stopped.emit()

    def _find_window(self):
        hwnd = win32gui.FindWindow(None, SCRCPY_TITLE)
        if hwnd and win32gui.IsWindow(hwnd):
            self.hwnd = hwnd
            self.poll.stop()
            self.started.emit()

    def _update_resolution(self):
        try:
            cmd = ["adb"]
            if self.serial:
                cmd += ["-s", self.serial]
            cmd += ["shell", "wm", "size"]
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", "ignore")
            for line in out.splitlines():
                if "Physical size:" in line:
                    size = line.split(":", 1)[1].strip()
                    break
                if "Override size:" in line:
                    size = line.split(":", 1)[1].strip()
                    break
            else:
                size = None
            if size:
                size = size.split()[0]
                if "x" in size:
                    w, h = size.split("x", 1)
                    self.resolution = (int(w), int(h))
                    return
        except Exception:
            pass
        self.resolution = None


class AndroidView(QWidget):
    def __init__(self, controller: ScrcpyController):
        super().__init__()
        self.controller = controller
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:#000;")
        controller.started.connect(self._embed)
        self.r_timer = QTimer()
        self.r_timer.setInterval(1000)
        self.r_timer.timeout.connect(self._resize_child)
        self.r_timer.start()

    def _embed(self):
        hwnd = self.controller.hwnd
        if not hwnd: return
        win32gui.SetParent(hwnd, int(self.winId()))
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style &= ~(win32con.WS_CAPTION|win32con.WS_THICKFRAME|win32con.WS_MINIMIZEBOX|win32con.WS_MAXIMIZEBOX|win32con.WS_SYSMENU)
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        self._resize_child()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_child()

    def _resize_child(self):
        hwnd = self.controller.hwnd
        if hwnd:
            r = self.rect()
            width, height = r.width(), r.height()
            x_off = 0
            y_off = 0

            if self.controller.resolution and self.controller.resolution[1] != 0:
                aspect = self.controller.resolution[0] / self.controller.resolution[1]
                available_aspect = width / height if height else aspect

                target_width = width
                target_height = height
                if available_aspect > aspect:
                    # Limit by height
                    target_height = height
                    target_width = max(1, int(round(target_height * aspect)))
                else:
                    # Limit by width
                    target_width = width
                    target_height = max(1, int(round(target_width / aspect)))

                x_off = (width - target_width) // 2
                y_off = (height - target_height) // 2
                width, height = target_width, target_height

            win32gui.MoveWindow(hwnd, x_off, y_off, width, height, True)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Android — Embedded scrcpy")
        self.resize(1400, 840)

        device = DEVICE_SERIAL or get_first_device()

        self.ctrl = ScrcpyController(device)
        self.view = AndroidView(self.ctrl)

        self.btnStart = QPushButton("Start Stream")
        self.btnStop = QPushButton("Stop")
        self.status = QLabel(f"Device: {device or 'Not Found'}")
        self.status.setStyleSheet("color:#bbb;")

        self.btnStart.clicked.connect(lambda: self.ctrl.start())
        self.btnStop.clicked.connect(self.ctrl.stop)
        self.ctrl.started.connect(lambda: self.status.setText(f"Device: {device} — STREAMING"))
        self.ctrl.stopped.connect(lambda: self.status.setText(f"Device: {device} — STOPPED"))
        self.ctrl.error.connect(lambda m: QMessageBox.critical(self, "scrcpy", m))

        top = QHBoxLayout()
        top.addWidget(self.btnStart)
        top.addWidget(self.btnStop)
        top.addStretch()
        top.addWidget(self.status)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#333;")

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(line)
        layout.addWidget(self.view)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
