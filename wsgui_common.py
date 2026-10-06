"""
wsgui_common.py - gemeinsame Bausteine der beiden Tk-Oberflaechen
=================================================================

Die GUIs enthalten KEINE eigene Signalverarbeitung.  Sie bauen Kommandozeilen
fuer die Konsolenprogramme (ws_encode.py, ws_radio.py), starten sie als
Unterprozess und zeigen deren Ausgabe live an.  Vorteile:
  * Konsole und GUI verhalten sich garantiert identisch,
  * die GUI bleibt bedienbar, waehrend rechenintensive Schritte laufen,
  * jeder Lauf kann ueber "Befehl anzeigen" auch von Hand wiederholt werden.

Bausteine
---------
ProcRunner  startet einen Prozess, liest stdout+stderr zeilenweise in einem
            Hintergrund-Thread und liefert die Zeilen thread-sicher ueber
            eine Queue an den Tk-Hauptthread (Tk ist nicht thread-sicher!).
            Wichtig: Fortschrittsanzeigen der Konsolenprogramme enden mit
            '\\r' (Wagenruecklauf) statt '\\n'; sie werden als "ueberschreibbare
            Zeile" behandelt.
LogView     Textfeld, das '\\r'-Zeilen an Ort und Stelle ueberschreibt.
"""
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
JSON_TAG = "@@JSON@@ "


def script_path(name):
    return os.path.join(HERE, name)


def py_cmd(script, *args):
    """[python, -u, skript, args...]; -u = ungepufferte Ausgabe (Live-Fortschritt)."""
    return [sys.executable, "-u", script_path(script), *[str(a) for a in args]]


def cmd_to_text(cmd):
    """Kommandozeile als kopierbarer Text (plattformgerecht quotiert)."""
    shown = ["python" if i == 0 else c for i, c in enumerate(cmd)]
    shown = [c for c in shown if c != "-u"]
    if os.name == "nt":
        return subprocess.list2cmdline(shown)
    return " ".join(shlex.quote(c) for c in shown)


class ProcRunner:
    """Fuehrt eine Kommandozeile im Hintergrund aus.

    on_line(text, kind)  kind = '\\n' (Zeile fertig) oder '\\r' (Fortschritt)
    on_done(returncode)
    Beide Rueckrufe laufen im Tk-Hauptthread.
    """
    def __init__(self, root, on_line, on_done):
        self.root, self.on_line, self.on_done = root, on_line, on_done
        self.proc = None
        self.q = queue.Queue()
        self._killed = False

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cmd):
        if self.running:
            raise RuntimeError("Es laeuft bereits ein Prozess")
        self._killed = False
        flags = 0x08000000 if os.name == "nt" else 0          # kein Konsolenfenster (Windows)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     bufsize=0, creationflags=flags, cwd=HERE)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.root.after(40, self._poll)

    def stop(self):
        if self.running:
            self._killed = True
            self.proc.terminate()

    def _reader(self, proc):
        fd = proc.stdout.fileno()
        buf = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                text = buf[:m.start()].decode("utf-8", "replace")
                self.q.put(("line", text, buf[m.start():m.end()].decode()))
                buf = buf[m.end():]
        if buf:
            self.q.put(("line", buf.decode("utf-8", "replace"), "\n"))
        rc = proc.wait()
        self.q.put(("done", -1 if self._killed else rc, None))

    def _poll(self):
        finished = False
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "line":
                    self.on_line(a, b)
                else:
                    finished = True
                    self.on_done(a)
        except queue.Empty:
            pass
        if not finished:
            self.root.after(40, self._poll)


class LogView(ttk.Frame):
    """Schreibgeschuetztes Log; '\\r'-Zeilen ueberschreiben die letzte Zeile."""
    def __init__(self, parent, height=10):
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap="none", state="disabled",
                            font=("Courier", 9), background="#fbfbfb")
        sy = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        sx = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.text.tag_configure("err", foreground="#b00020")
        self.text.tag_configure("cmd", foreground="#555555")
        self.text.tag_configure("ok", foreground="#176f2c")
        self._cr = False

    def _tag(self, s):
        if re.search(r"Traceback|Error|Fehler|nicht rasterkonform|nicht gefunden", s):
            return "err"
        return ""

    def feed(self, text, kind="\n", tag=None):
        t = self.text
        t.configure(state="normal")
        if self._cr:                       # laufende Fortschrittszeile ersetzen
            if kind == "\n" and text == "":
                t.insert("end", "\n")
                self._cr = False
                t.configure(state="disabled")
                t.see("end")
                return
            t.delete("end-1c linestart", "end-1c")
        t.insert("end", text + ("\n" if kind == "\n" else ""), tag or self._tag(text))
        self._cr = (kind == "\r")
        t.configure(state="disabled")
        t.see("end")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._cr = False


def center(win, parent=None):
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    if parent is not None:
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


def setup_style(root):
    st = ttk.Style(root)
    for theme in ("clam", "vista", "aqua"):
        if theme in st.theme_names() and os.name != "nt" and theme == "clam":
            st.theme_use(theme)
            break
    st.configure("Title.TLabel", font=("TkDefaultFont", 11, "bold"))
    st.configure("Hint.TLabel", foreground="#555555")
    st.configure("Big.TButton", padding=(10, 4))
