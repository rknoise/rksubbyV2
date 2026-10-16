# -*- coding: utf-8 -*-
"""Átlátszó felirat-réteg a képernyő alján: mindig legfelül, átkattintható (OBS és lejátszó nélkül, pl.
PC-s játékhoz vagy más lejátszó fölé). PyQt5 kell hozzá (a telepítő felteszi).

Külön folyamatban fut (python -m subby.overlay_qt --port 8765): a PyQt5 és a CUDA-könyvtárak egy
folyamatban összeakadnak. A feliratot ugyanúgy kapja, mint az OBS-oldal (/events); ha a program leáll,
ez is kilép. A főprogram az OverlayProcess-szel indítja/állítja le (cfg.screen_overlay)."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request


class OverlayProcess:
    """A főprogramban: elindítja vagy leállítja a réteget, ahogy a cfg.screen_overlay változik."""

    def __init__(self, cfg, port, log=print):
        self.cfg, self.port, self.log = cfg, port, log
        self.proc = None
        self.screen = None
        self._stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True, name="felirat-reteg").start()

    def _watch(self):
        while not self._stop.wait(0.5):
            want = bool(self.cfg.screen_overlay)
            alive = self.proc is not None and self.proc.poll() is None
            if want and alive and self.screen != self.cfg.overlay_screen:
                self._kill()               # másik monitorra kell: újraindítjuk
                alive = False
            if want and not alive:
                self._spawn()
            elif not want and alive:
                self._kill()

    def _spawn(self):
        self.screen = self.cfg.overlay_screen
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "subby.overlay_qt", "--port", str(self.port), "--screen", str(self.screen)],
                cwd=root, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.log("[felirat-réteg] elindult")
        except OSError as e:
            self.log(f"[felirat-réteg] nem indítható: {e}")
            self.proc = None
            self._stop.wait(10.0)

    def _kill(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            self.log("[felirat-réteg] leállt")

    def stop(self):
        self._stop.set()
        self._kill()


def qt_available():
    import importlib.util

    return importlib.util.find_spec("PyQt5") is not None


# -----------------------------------------------------------------------------
# A réteg folyamata
# -----------------------------------------------------------------------------

def _listen(port, on_state, alive):
    """A felirat állapota a /events-ről; ha a program 8 mp-ig nem érhető el, a réteg kilép."""
    last_ok = time.monotonic()
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=30) as r:
                for raw in r:
                    last_ok = time.monotonic()
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data: ") and line[6:] != "1":
                        try:
                            on_state(json.loads(line[6:]))
                        except ValueError:
                            pass
        except Exception:
            pass
        on_state({"lines": [], "partial": ""})
        if time.monotonic() - last_ok > 8:
            alive.clear()
            return
        time.sleep(1.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="rksubby átlátszó felirat-réteg")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--screen", type=int, default=0)
    args = ap.parse_args(argv)

    from PyQt5 import QtCore, QtGui, QtWidgets

    Qt = QtCore.Qt
    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    screens = QtWidgets.QApplication.screens()
    screen_idx = max(0, min(args.screen, len(screens) - 1))
    alive = threading.Event()
    alive.set()

    class Bridge(QtCore.QObject):
        state = QtCore.pyqtSignal(dict)

    class Overlay(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                             | Qt.WindowTransparentForInput)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            geo = screens[screen_idx].geometry()
            w, h = int(geo.width() * 0.9), int(geo.height() * 0.45)
            self.setGeometry(geo.x() + (geo.width() - w) // 2,
                             geo.y() + geo.height() - h - int(geo.height() * 0.07), w, h)
            self.st = {"lines": [], "partial": "", "style": {}}
            self.show()

        def set_state(self, st):
            self.st.update(st)
            self.update()

        def _wrap(self, text, fm, max_w):
            out, cur = [], ""
            for word in text.split():
                t = (cur + " " + word).strip()
                if not cur or fm.horizontalAdvance(t) <= max_w:
                    cur = t
                else:
                    out.append(cur)
                    cur = word
            if cur:
                out.append(cur)
            return out

        def paintEvent(self, _ev):
            st = self.st
            style = st.get("style") or {}
            size = int(style.get("font_size", 32))
            font_hu = QtGui.QFont(style.get("font") or "Segoe UI")
            font_hu.setPixelSize(int(size * 1.05))
            font_hu.setWeight(QtGui.QFont.DemiBold)
            font_small = QtGui.QFont(style.get("font") or "Segoe UI")
            font_small.setPixelSize(int(size * 0.7))
            font_small.setItalic(True)
            color = QtGui.QColor(style.get("color", "#ffff00"))
            en_color = QtGui.QColor(style.get("en_color", "#dddddd"))
            max_w = min(self.width() - 60, int(style.get("max_width", 1300)))
            blocks = []   # sorok listája [(szöveg, font, szín)]
            for ln in st.get("lines") or []:
                rows = [(r, font_hu, color) for r in self._wrap(ln["hu"], QtGui.QFontMetrics(font_hu), max_w)]
                if ln.get("en"):
                    rows += [(r, font_small, en_color) for r in
                             self._wrap(ln["en"], QtGui.QFontMetrics(font_small), max_w)]
                blocks.append(rows)
            if st.get("partial"):
                blocks.append([(r, font_small, QtGui.QColor(190, 190, 190)) for r in
                               self._wrap(st["partial"], QtGui.QFontMetrics(font_small), max_w)])
            blocks = [b for b in blocks if b]
            if not blocks:
                return
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            pad_x, pad_y, gap = 18, 8, 8
            heights = [sum(QtGui.QFontMetrics(f).height() for _, f, _ in rows) + 2 * pad_y for rows in blocks]
            while len(blocks) > 1 and sum(heights) + gap * (len(blocks) - 1) > self.height():
                blocks.pop(0)      # ha nem fér ki, a legrégebbi mondat tűnik el
                heights.pop(0)
            y = self.height() - sum(heights) - gap * (len(blocks) - 1)
            outline = QtGui.QPen(QtGui.QColor(0, 0, 0, 230), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            box_alpha = int(255 * float(style.get("box_opacity", 0.5))) if style.get("box") else 0
            for rows, bh in zip(blocks, heights):
                bw = max(QtGui.QFontMetrics(f).horizontalAdvance(t) for t, f, _ in rows) + 2 * pad_x
                bx = (self.width() - bw) / 2
                if box_alpha:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QtGui.QColor(0, 0, 0, box_alpha))
                    p.drawRoundedRect(QtCore.QRectF(bx, y, bw, bh), 10, 10)
                ty = y + pad_y
                for text, font, col in rows:
                    fm = QtGui.QFontMetrics(font)
                    path = QtGui.QPainterPath()
                    path.addText((self.width() - fm.horizontalAdvance(text)) / 2, ty + fm.ascent(), font, text)
                    p.strokePath(path, outline)
                    p.fillPath(path, col)
                    ty += fm.height()
                y += bh + gap
            p.end()

    bridge = Bridge()
    overlay = Overlay()
    bridge.state.connect(overlay.set_state)
    threading.Thread(target=_listen, args=(args.port, bridge.state.emit, alive), daemon=True).start()
    watcher = QtCore.QTimer()
    watcher.timeout.connect(lambda: app.quit() if not alive.is_set() else None)
    watcher.start(500)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
