#!/usr/bin/env python3
"""
ws_encode_gui.py - Tk-Oberflaeche fuer den Encoder (ws_encode.py)
=================================================================

Bedienung in drei Schritten
  1. Broadcast Channels anlegen ("Neuer BC"): Name (max. 8 Zeichen), BCID und
     eine bis acht Service Components (MP3-Dateien).  Fuer jede Datei kann die
     Zielbitrate/Abtastrate gewaehlt werden; passt die Datei nicht ins
     432-ms-Raster, wird sie vom Encoder per ffmpeg umkodiert.
  2. Ausgabe und (optional) Kanalstoerungen einstellen.
  3. "Pruefen" (Trockenlauf) oder "Encoder starten".

Die GUI ruft ws_encode.py als Unterprozess auf; die genaue Kommandozeile ist
unter "Befehl" sichtbar und kopierbar.  Die Anzahl der PRC je BC zeigt die GUI
vorab an: n = aufgerundet(Summe der SC-Raten / 16 kbit/s)  [PAT Fig.14, ITU].
"""
import json
import os
import re
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import wsconfig as C
import wsgui_common as G

SAMPLE_RATES = ("auto", "48000", "32000", "24000", "16000", "12000", "8000")
KBPS_VALUES = ("Datei uebernehmen",) + tuple(str(k) for k in range(8, 129, 8))
FORMATS = ("cs8", "cs16", "cf32", "cu8")
BYTES_PER_SAMPLE = {"cs8": 2, "cs16": 4, "cf32": 8, "cu8": 2}

_ws = None


def ws_mod():
    """wscore erst bei Bedarf laden (dauert ~1 s) - dient nur der Vorab-Analyse."""
    global _ws
    if _ws is None:
        try:
            import wscore
            _ws = wscore
        except Exception:
            _ws = False
    return _ws or None


_analysis_cache = {}


def analyze(path):
    """Prueft, ob die Datei schon rasterkonform ist.  -> dict(rate, frames, ok, msg)"""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return {"rate": None, "frames": None, "ok": False, "msg": "Datei nicht gefunden"}
    if key in _analysis_cache:
        return _analysis_cache[key]
    res = {"rate": None, "frames": None, "ok": False, "msg": ""}
    ws = ws_mod()
    if not path.lower().endswith(".mp3"):
        res["msg"] = "kein MP3 - wird umkodiert (Bitrate waehlen)"
    elif ws is None:
        res["msg"] = "Analyse nicht moeglich (numpy/scipy fehlen)"
    else:
        try:
            rate, chunks = ws.mp3_bc_chunks(open(path, "rb").read())
            res.update(rate=rate, frames=len(chunks), ok=True,
                       msg=f"rasterkonform: {rate} kbit/s, {len(chunks)} BC-Rahmen "
                           f"({len(chunks) * C.BC_FRAME_SECONDS:.1f} s)")
        except ws.NonConformant as e:
            res["msg"] = f"nicht rasterkonform: {e} - Bitrate waehlen"
        except Exception as e:
            res["msg"] = f"Analysefehler: {e}"
    _analysis_cache[key] = res
    return res


def sc_rate(sc):
    """Effektive Rate eines SC in kbit/s (None = unbekannt)."""
    if sc.get("kbps"):
        return int(sc["kbps"])
    return analyze(sc["path"])["rate"]


def bc_prc(bc):
    rates = [sc_rate(s) for s in bc["scs"]]
    if not rates or any(r is None for r in rates):
        return None
    return -(-sum(rates) // C.PRI_KBPS)


# ---------------------------------------------------------------------------
class SCDialog(tk.Toplevel):
    """Eine Service Component bearbeiten (Datei, Bitrate, Abtastrate)."""
    def __init__(self, parent, sc=None):
        super().__init__(parent)
        self.title("Service Component")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        sc = sc or {"path": "", "kbps": None, "sr": None}
        self.var_path = tk.StringVar(value=sc["path"])
        self.var_kbps = tk.StringVar(value=str(sc["kbps"]) if sc["kbps"] else KBPS_VALUES[0])
        self.var_sr = tk.StringVar(value=str(sc["sr"]) if sc["sr"] else "auto")
        f = ttk.Frame(self, padding=12)
        f.grid()
        ttk.Label(f, text="Audiodatei:").grid(row=0, column=0, sticky="w")
        e = ttk.Entry(f, textvariable=self.var_path, width=52)
        e.grid(row=0, column=1, padx=6)
        ttk.Button(f, text="Durchsuchen ...", command=self.browse).grid(row=0, column=2)
        ttk.Label(f, text="Bitrate (kbit/s):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(f, textvariable=self.var_kbps, values=KBPS_VALUES, width=18,
                     state="readonly").grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(f, text="Abtastrate (Hz):").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(f, textvariable=self.var_sr, values=SAMPLE_RATES, width=18,
                     state="readonly").grid(row=2, column=1, sticky="w", padx=6, pady=(4, 0))
        self.lbl = ttk.Label(f, text="", style="Hint.TLabel", wraplength=520, justify="left")
        self.lbl.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, style="Hint.TLabel", wraplength=520, justify="left", text=(
            "Hinweis: Bitrate = Vielfaches von 8 (max. 128). Bei 'Datei uebernehmen' muss die MP3 bereits "
            "rasterkonform sein (CBR, ohne Padding, 48/32/24/16/12/8 kHz). Sonst eine Bitrate waehlen - "
            "der Encoder kodiert dann mit ffmpeg um.")).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="OK", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left")
        self.var_path.trace_add("write", lambda *_: self.refresh())
        self.refresh()
        self.grab_set()
        G.center(self, parent)
        e.focus_set()

    def browse(self):
        p = filedialog.askopenfilename(parent=self, title="Audiodatei waehlen", filetypes=[
            ("Audio", "*.mp3 *.wav *.flac *.ogg *.m4a *.aac"), ("Alle Dateien", "*.*")])
        if p:
            self.var_path.set(p)

    def refresh(self):
        p = self.var_path.get()
        self.lbl.config(text=analyze(p)["msg"] if p and os.path.exists(p) else "")

    def ok(self):
        p = self.var_path.get().strip()
        if not os.path.isfile(p):
            messagebox.showerror("Datei", "Die Datei existiert nicht.", parent=self)
            return
        if re.search(r"[,@]", os.path.basename(p) + os.path.dirname(p)):
            messagebox.showerror("Dateiname", "Pfad/Dateiname darf kein ',' oder '@' enthalten "
                                 "(Trennzeichen der Kommandozeile).", parent=self)
            return
        kbps = None if self.var_kbps.get() == KBPS_VALUES[0] else int(self.var_kbps.get())
        sr = None if self.var_sr.get() == "auto" else int(self.var_sr.get())
        if kbps is None and sr is None and not analyze(p)["ok"]:
            messagebox.showerror("Nicht rasterkonform", analyze(p)["msg"] + "\n\nBitte eine Bitrate waehlen.",
                                 parent=self)
            return
        self.result = {"path": p, "kbps": kbps, "sr": sr}
        self.destroy()


class BCDialog(tk.Toplevel):
    """Einen Broadcast Channel bearbeiten."""
    def __init__(self, parent, bc, used_ids):
        super().__init__(parent)
        self.title("Broadcast Channel")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.used = used_ids
        self.scs = [dict(s) for s in bc["scs"]]
        self.var_id = tk.IntVar(value=bc["bcid"])
        self.var_label = tk.StringVar(value=bc["label"])
        f = ttk.Frame(self, padding=12)
        f.grid()
        ttk.Label(f, text="BCID-Nummer (1..510):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(f, from_=1, to=510, textvariable=self.var_id, width=8).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(f, text="Sendername (max. 8 Zeichen):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.var_label, width=14).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, text="Service Components (max. 8):").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.lb = tk.Listbox(f, width=78, height=7, exportselection=False)
        self.lb.grid(row=3, column=0, columnspan=2, sticky="ew")
        side = ttk.Frame(f)
        side.grid(row=3, column=2, sticky="n", padx=(8, 0))
        for t, c in (("Hinzufuegen ...", self.add), ("Bearbeiten ...", self.edit),
                     ("Entfernen", self.remove), ("Nach oben", lambda: self.move(-1)),
                     ("Nach unten", lambda: self.move(1))):
            ttk.Button(side, text=t, command=c, width=16).pack(pady=1)
        self.info = ttk.Label(f, text="", style="Hint.TLabel")
        self.info.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(b, text="OK", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left")
        self.fill()
        self.grab_set()
        G.center(self, parent)

    def fill(self):
        self.lb.delete(0, "end")
        for i, s in enumerate(self.scs, 1):
            r = sc_rate(s)
            extra = []
            if s["kbps"]:
                extra.append("umkodieren")
            if s["sr"]:
                extra.append(f"{s['sr']} Hz")
            self.lb.insert("end", f"SC{i}: {os.path.basename(s['path'])}   "
                           f"[{r if r else '?'} kbit/s{', ' + ', '.join(extra) if extra else ''}]")
        n = bc_prc({"scs": self.scs}) if self.scs else None
        total = sum(sc_rate(s) or 0 for s in self.scs)
        self.info.config(text=f"Summe {total} kbit/s -> {n if n else '?'} PRC "
                              f"({(n or 0) * C.PRI_BITS / C.BC_FRAME_SECONDS / 1000:.1f} kbit/s brutto inkl. SCH)")

    def sel(self):
        s = self.lb.curselection()
        return s[0] if s else None

    def add(self):
        if len(self.scs) >= C.MAX_SC:
            messagebox.showinfo("Limit", "Maximal 8 Service Components je BC.", parent=self)
            return
        d = SCDialog(self)
        self.wait_window(d)
        if d.result:
            self.scs.append(d.result)
            self.fill()

    def edit(self):
        i = self.sel()
        if i is None:
            return
        d = SCDialog(self, self.scs[i])
        self.wait_window(d)
        if d.result:
            self.scs[i] = d.result
            self.fill()

    def remove(self):
        i = self.sel()
        if i is not None:
            del self.scs[i]
            self.fill()

    def move(self, d):
        i = self.sel()
        if i is None or not 0 <= i + d < len(self.scs):
            return
        self.scs[i], self.scs[i + d] = self.scs[i + d], self.scs[i]
        self.fill()
        self.lb.selection_set(i + d)

    def ok(self):
        try:
            bcid = int(self.var_id.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("BCID", "Bitte eine ganze Zahl eintragen.", parent=self)
            return
        label = self.var_label.get().strip()
        if not 1 <= bcid <= 510:
            messagebox.showerror("BCID", "BCID muss 1..510 sein (0 = unbenutzt, 511 = Testkanal).", parent=self)
        elif bcid in self.used:
            messagebox.showerror("BCID", f"BCID {bcid} wird schon verwendet.", parent=self)
        elif not label or len(label) > 8 or re.search(r"[:,=@]", label):
            messagebox.showerror("Name", "Name: 1-8 Zeichen, ohne : , = @", parent=self)
        elif not self.scs:
            messagebox.showerror("Service Components", "Mindestens eine SC noetig.", parent=self)
        else:
            self.result = {"bcid": bcid, "label": label, "scs": self.scs}
            self.destroy()


# ---------------------------------------------------------------------------
class EncoderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WorldSpace-Encoder (Basisband)")
        G.setup_style(self)
        self.minsize(900, 720)
        self.bcs = []
        self.runner = G.ProcRunner(self, self.on_line, self.on_done)
        self.dry = False
        self._build()
        self.refresh()

    # -- Aufbau ----------------------------------------------------------------
    def _build(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Neues Projekt", command=self.new_project)
        fm.add_command(label="Projekt oeffnen ...", command=self.load_project)
        fm.add_command(label="Projekt speichern ...", command=self.save_project)
        fm.add_separator()
        fm.add_command(label="Beenden", command=self.destroy)
        m.add_cascade(label="Datei", menu=fm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="Hinweise zum Raster ...", command=self.help_raster)
        m.add_cascade(label="Hilfe", menu=hm)
        self.config(menu=m)

        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=3)
        root.rowconfigure(4, weight=2)

        # Broadcast Channels
        lf = ttk.LabelFrame(root, text=" 1  Broadcast Channels ", padding=6)
        lf.grid(row=0, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        cols = ("label", "rate", "prc", "info")
        self.tree = ttk.Treeview(lf, columns=cols, height=7, selectmode="browse")
        self.tree.heading("#0", text="BCID / Service Component")
        for c, t, w in (("label", "Name", 90), ("rate", "kbit/s", 70), ("prc", "PRC (n)", 70), ("info", "Hinweis", 420)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=300)
        sy = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda e: self.edit_bc())
        bf = ttk.Frame(lf)
        bf.grid(row=0, column=2, sticky="n", padx=(8, 0))
        for t, c in (("Neuer BC ...", self.add_bc), ("Bearbeiten ...", self.edit_bc), ("Loeschen", self.del_bc),
                     ("Nach oben", lambda: self.move_bc(-1)), ("Nach unten", lambda: self.move_bc(1))):
            ttk.Button(bf, text=t, command=c, width=15).pack(pady=1)
        self.lbl_sum = ttk.Label(lf, text="", style="Hint.TLabel")
        self.lbl_sum.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Ausgabe
        of = ttk.LabelFrame(root, text=" 2  Ausgabe ", padding=6)
        of.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        of.columnconfigure(1, weight=1)
        self.v_out = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "mux.cs8"))
        self.v_fmt = tk.StringVar(value="cs8")
        self.v_sps = tk.IntVar(value=4)
        self.v_max = tk.StringVar(value="")
        self.v_loop = tk.BooleanVar(value=False)
        self.v_lead = tk.IntVar(value=1)
        self.v_first = tk.IntVar(value=0)
        ttk.Label(of, text="Zieldatei:").grid(row=0, column=0, sticky="w")
        ttk.Entry(of, textvariable=self.v_out).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(of, text="Durchsuchen ...", command=self.browse_out).grid(row=0, column=2)
        r2 = ttk.Frame(of)
        r2.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(r2, text="Format:").pack(side="left")
        cb = ttk.Combobox(r2, textvariable=self.v_fmt, values=FORMATS, width=6, state="readonly")
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self.fmt_changed())
        ttk.Label(r2, text="Samples/Symbol:").pack(side="left")
        ttk.Spinbox(r2, from_=4, to=16, textvariable=self.v_sps, width=4, command=self.refresh).pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="Max. Sekunden:").pack(side="left")
        ttk.Entry(r2, textvariable=self.v_max, width=6).pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="Vorlauf (BC-Rahmen):").pack(side="left")
        ttk.Spinbox(r2, from_=0, to=10, textvariable=self.v_lead, width=3).pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="Erster PRC:").pack(side="left")
        ttk.Spinbox(r2, from_=0, to=95, textvariable=self.v_first, width=4).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(r2, text="Kuerzere SC wiederholen", variable=self.v_loop).pack(side="left")
        for v in (self.v_out, self.v_max, self.v_loop, self.v_lead, self.v_first, self.v_sps):
            v.trace_add("write", lambda *_: self.refresh())

        # Stoerungen
        sf = ttk.LabelFrame(root, text=" Kanalstoerungen (zum Testen des Decoders) ", padding=6)
        sf.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.v_cn_on = tk.BooleanVar(value=False)
        self.v_cn = tk.StringVar(value="9")
        self.v_cfo = tk.StringVar(value="0")
        self.v_phase = tk.StringVar(value="0")
        self.v_timing = tk.StringVar(value="0")
        self.v_seed = tk.StringVar(value="1")
        ttk.Checkbutton(sf, text="Rauschen, C/N (dB):", variable=self.v_cn_on).pack(side="left")
        ttk.Entry(sf, textvariable=self.v_cn, width=6).pack(side="left", padx=(4, 14))
        for lab, var in (("Frequenzablage (Hz):", self.v_cfo), ("Phase (Grad):", self.v_phase),
                         ("Symboltakt (0..1):", self.v_timing), ("Seed:", self.v_seed)):
            ttk.Label(sf, text=lab).pack(side="left")
            ttk.Entry(sf, textvariable=var, width=8).pack(side="left", padx=(4, 14))
        for v in (self.v_cn_on, self.v_cn, self.v_cfo, self.v_phase, self.v_timing, self.v_seed):
            v.trace_add("write", lambda *_: self.refresh())

        # Befehl + Knoepfe
        cf = ttk.LabelFrame(root, text=" 3  Befehl und Start ", padding=6)
        cf.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        cf.columnconfigure(0, weight=1)
        self.txt_cmd = tk.Text(cf, height=3, wrap="word", font=("Courier", 9), background="#f2f2f2")
        self.txt_cmd.grid(row=0, column=0, sticky="ew")
        ttk.Button(cf, text="Kopieren", command=self.copy_cmd).grid(row=0, column=1, padx=(6, 0), sticky="n")
        row = ttk.Frame(cf)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.btn_check = ttk.Button(row, text="Pruefen (Trockenlauf)", command=lambda: self.start(True))
        self.btn_start = ttk.Button(row, text="Encoder starten", style="Big.TButton", command=lambda: self.start(False))
        self.btn_stop = ttk.Button(row, text="Abbrechen", command=self.runner.stop, state="disabled")
        self.btn_radio = ttk.Button(row, text="Im Radio oeffnen", command=self.open_radio, state="disabled")
        for b in (self.btn_check, self.btn_start, self.btn_stop):
            b.pack(side="left", padx=(0, 6))
        self.btn_radio.pack(side="right")
        self.pb = ttk.Progressbar(cf, mode="determinate", maximum=100)
        self.pb.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.lbl_status = ttk.Label(cf, text="Bereit.", style="Hint.TLabel")
        self.lbl_status.grid(row=3, column=0, columnspan=2, sticky="w")

        self.log = G.LogView(root, height=8)
        self.log.grid(row=4, column=0, sticky="nsew", pady=(6, 0))

    # -- Hilfen ------------------------------------------------------------------
    def help_raster(self):
        messagebox.showinfo("Hinweise zum Raster", (
            "Jeder BC-Rahmen dauert 432 ms. Damit jeder Rahmen mit einem MPEG-Frame-Header beginnt, braucht der "
            "Encoder MP3-Ströme mit\n"
            " - Layer III, konstanter Bitrate (Vielfaches von 8 kbit/s, max. 128), ohne Padding,\n"
            " - Abtastrate 48/32/24/16/12/8 kHz (NICHT 44,1 kHz),\n"
            " - genau Rate x 54 Byte je 432 ms.\n\n"
            "Andere Dateien waehlt man mit Bitrate (und ggf. Abtastrate); dann kodiert der Encoder per ffmpeg um "
            "(ffmpeg muss installiert sein).\n\n"
            "Anzahl PRC je BC: n = aufgerundet(Summe der SC-Raten / 16 kbit/s), maximal 8; insgesamt 96 PRC."), parent=self)

    def browse_out(self):
        p = filedialog.asksaveasfilename(parent=self, title="Basisband-Datei", initialfile=os.path.basename(self.v_out.get()),
                                         defaultextension="." + self.v_fmt.get(),
                                         filetypes=[("Basisband", "*.cs8 *.cs16 *.cf32 *.cu8"), ("Alle", "*.*")])
        if p:
            self.v_out.set(p)

    def fmt_changed(self):
        base, ext = os.path.splitext(self.v_out.get())
        if ext.lstrip(".") in FORMATS:
            self.v_out.set(base + "." + self.v_fmt.get())
        self.refresh()

    def copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(self.txt_cmd.get("1.0", "end").strip())
        self.lbl_status.config(text="Befehl in die Zwischenablage kopiert.")

    def next_free_id(self):
        used = {b["bcid"] for b in self.bcs}
        i = 1
        while i in used:
            i += 1
        return i

    # -- BC-Verwaltung --------------------------------------------------------------
    def add_bc(self):
        d = BCDialog(self, {"bcid": self.next_free_id(), "label": "Radio", "scs": []}, {b["bcid"] for b in self.bcs})
        self.wait_window(d)
        if d.result:
            self.bcs.append(d.result)
            self.refresh()

    def sel_index(self):
        s = self.tree.selection()
        if not s:
            return None
        iid = s[0]
        top = iid if self.tree.parent(iid) == "" else self.tree.parent(iid)
        return int(top)

    def edit_bc(self):
        i = self.sel_index()
        if i is None:
            return
        used = {b["bcid"] for k, b in enumerate(self.bcs) if k != i}
        d = BCDialog(self, self.bcs[i], used)
        self.wait_window(d)
        if d.result:
            self.bcs[i] = d.result
            self.refresh()

    def del_bc(self):
        i = self.sel_index()
        if i is not None and messagebox.askyesno("Loeschen", f"BC '{self.bcs[i]['label']}' loeschen?", parent=self):
            del self.bcs[i]
            self.refresh()

    def move_bc(self, d):
        i = self.sel_index()
        if i is None or not 0 <= i + d < len(self.bcs):
            return
        self.bcs[i], self.bcs[i + d] = self.bcs[i + d], self.bcs[i]
        self.refresh()
        self.tree.selection_set(str(i + d))

    # -- Anzeige ---------------------------------------------------------------------
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        total_prc, unknown, dur = 0, False, None
        for i, bc in enumerate(self.bcs):
            n = bc_prc(bc)
            if n is None:
                unknown = True
            else:
                total_prc += n
            rate = sum(sc_rate(s) or 0 for s in bc["scs"])
            self.tree.insert("", "end", iid=str(i), text=f"BCID {bc['bcid']}", open=True,
                             values=(bc["label"], rate, n if n else "?", ""))
            for k, sc in enumerate(bc["scs"], 1):
                a = analyze(sc["path"])
                note = ("wird mit ffmpeg umkodiert" + (f" auf {sc['sr']} Hz" if sc["sr"] else "")
                        if sc["kbps"] or sc["sr"] else a["msg"])
                if a["frames"] and not (sc["kbps"] or sc["sr"]):
                    dur = a["frames"] if dur is None else min(dur, a["frames"])
                self.tree.insert(str(i), "end", iid=f"{i}.{k}", text=f"  SC{k}  {os.path.basename(sc['path'])}",
                                 values=("", sc_rate(sc) or "?", "", note))
        sps = self.v_sps.get() if self._intval(self.v_sps) else 4
        sec = dur * C.BC_FRAME_SECONDS if dur else None
        try:                                   # "Max. Sekunden" begrenzt die Dauer
            if sec and self.v_max.get().strip():
                sec = min(sec, float(self.v_max.get()))
        except ValueError:
            pass
        size = ""
        if sec:
            size = f", ca. {(sec + self._intval(self.v_lead, 1) * C.BC_FRAME_SECONDS) * sps * C.SYMRATE * BYTES_PER_SAMPLE[self.v_fmt.get()] / 1e6:.0f} MB"
        warn = "  UEBERBELEGT!" if total_prc > C.NPRC else ""
        self.lbl_sum.config(text=f"PRC belegt: {total_prc}{'+?' if unknown else ''}/{C.NPRC}{warn}"
                                 + (f"   |   Dauer ca. {sec:.1f} s{size}" if sec else ""))
        try:
            cmd = self.build_cmd(False, validate=False)
            self.txt_cmd.configure(state="normal")
            self.txt_cmd.delete("1.0", "end")
            self.txt_cmd.insert("1.0", G.cmd_to_text(cmd))
            self.txt_cmd.configure(state="disabled")
        except ValueError:
            pass

    def _intval(self, var, default=0):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    # -- Kommandozeile ---------------------------------------------------------------
    def build_cmd(self, dry, validate=True):
        errs = []
        if validate:
            if not self.bcs:
                errs.append("Es ist noch kein Broadcast Channel angelegt.")
            if not self.v_out.get().strip():
                errs.append("Zieldatei fehlt.")
            tot = sum(bc_prc(b) or 0 for b in self.bcs)
            if tot > C.NPRC:
                errs.append(f"{tot} PRC belegt, erlaubt sind {C.NPRC}.")
            if self._intval(self.v_first) + tot > C.NPRC:
                errs.append("Erster PRC + belegte PRC ueberschreiten 96.")
            for b in self.bcs:
                n = bc_prc(b)
                if n and n > C.MAX_PRI:
                    errs.append(f"BC '{b['label']}': {n} PRC > {C.MAX_PRI} (Summe der Raten > 128 kbit/s).")
            if self.v_max.get().strip():
                try:
                    float(self.v_max.get())
                except ValueError:
                    errs.append("'Max. Sekunden' ist keine Zahl.")
            for lab, var in (("C/N", self.v_cn), ("Frequenzablage", self.v_cfo), ("Phase", self.v_phase),
                             ("Symboltakt", self.v_timing), ("Seed", self.v_seed)):
                try:
                    float(var.get())
                except ValueError:
                    errs.append(f"'{lab}' ist keine Zahl.")
            if errs:
                raise ValueError("\n".join(errs))
        args = ["-o", self.v_out.get().strip(), "--fmt", self.v_fmt.get(), "--sps", self._intval(self.v_sps, 4),
                "--lead-in", self._intval(self.v_lead, 1), "--first-prc", self._intval(self.v_first)]
        if self.v_max.get().strip():
            args += ["--max-seconds", self.v_max.get().strip()]
        if self.v_loop.get():
            args.append("--loop")
        if self.v_cn_on.get():
            args += ["--cn", self.v_cn.get()]
        for flag, var in (("--cfo", self.v_cfo), ("--phase", self.v_phase), ("--timing", self.v_timing)):
            try:
                if float(var.get()) != 0:
                    args += [flag, var.get()]
            except ValueError:
                pass
        args += ["--seed", self.v_seed.get()]
        if dry:
            args.append("--dry-run")
        for bc in self.bcs:
            files = []
            for s in bc["scs"]:
                f = s["path"]
                if s["kbps"] or s["sr"]:
                    f += f"@{s['kbps'] or ''}" + (f"@{s['sr']}" if s["sr"] else "")
                files.append(f)
            args += ["--bc", f"{bc['bcid']}={bc['label']}:{','.join(files)}"]
        return G.py_cmd("ws_encode.py", *args)

    # -- Ausfuehrung -------------------------------------------------------------------
    def set_busy(self, busy):
        st = "disabled" if busy else "normal"
        for b in (self.btn_check, self.btn_start):
            b.config(state=st)
        self.btn_stop.config(state="normal" if busy else "disabled")

    def start(self, dry):
        try:
            cmd = self.build_cmd(dry)
        except ValueError as e:
            messagebox.showerror("Eingabe pruefen", str(e), parent=self)
            return
        out = self.v_out.get().strip()
        if not dry and os.path.exists(out) and not messagebox.askyesno(
                "Ueberschreiben", f"{out} existiert bereits. Ueberschreiben?", parent=self):
            return
        self.dry = dry
        self.log.clear()
        self.log.feed("$ " + G.cmd_to_text(cmd), "\n", "cmd")
        self.pb.config(mode="indeterminate")
        self.pb.start(12)
        self.lbl_status.config(text="Trockenlauf ..." if dry else "Encoder laeuft ...")
        self.btn_radio.config(state="disabled")
        self.set_busy(True)
        self.runner.start(cmd)

    def on_line(self, text, kind):
        m = re.search(r"TDM-Rahmen (\d+)/(\d+)", text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if str(self.pb.cget("mode")) != "determinate":
                self.pb.stop()
                self.pb.config(mode="determinate")
            self.pb.config(value=100.0 * a / b)
            self.lbl_status.config(text=f"Encoder laeuft: TDM-Rahmen {a}/{b} ({100 * a / b:.0f} %)")
        self.log.feed(text, kind)

    def on_done(self, rc):
        self.pb.stop()
        self.pb.config(mode="determinate", value=100 if rc == 0 else 0)
        self.set_busy(False)
        out = self.v_out.get().strip()
        if rc == 0 and self.dry:
            self.lbl_status.config(text="Trockenlauf erfolgreich - alle Quellen sind in Ordnung.")
            self.log.feed("Trockenlauf erfolgreich.", "\n", "ok")
        elif rc == 0:
            size = os.path.getsize(out) / 1e6 if os.path.exists(out) else 0
            self.lbl_status.config(text=f"Fertig: {out} ({size:.0f} MB)")
            self.log.feed(f"Fertig: {out} ({size:.0f} MB)", "\n", "ok")
            self.btn_radio.config(state="normal")
        elif rc == -1:
            self.lbl_status.config(text="Abgebrochen.")
            self.log.feed("Abgebrochen.", "\n", "err")
        else:
            self.lbl_status.config(text=f"Fehler (Rueckgabewert {rc}) - siehe Protokoll.")
            messagebox.showerror("Encoder", f"Der Encoder meldete einen Fehler (Code {rc}).\n"
                                            "Details stehen im Protokoll.", parent=self)

    def open_radio(self):
        subprocess.Popen([sys.executable, G.script_path("ws_radio_gui.py"), self.v_out.get().strip()], cwd=G.HERE)

    # -- Projekt -------------------------------------------------------------------------
    def new_project(self):
        if not self.bcs or messagebox.askyesno("Neues Projekt", "Aktuelle Kanalliste verwerfen?", parent=self):
            self.bcs = []
            self.refresh()

    def save_project(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".wsproj",
                                         filetypes=[("WorldSpace-Projekt", "*.wsproj"), ("JSON", "*.json")])
        if not p:
            return
        opts = {k: getattr(self, "v_" + k).get() for k in
                ("out", "fmt", "sps", "max", "loop", "lead", "first", "cn_on", "cn", "cfo", "phase", "timing", "seed")}
        json.dump({"bcs": self.bcs, "options": opts}, open(p, "w"), indent=1)
        self.lbl_status.config(text=f"Projekt gespeichert: {p}")

    def load_project(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("WorldSpace-Projekt", "*.wsproj *.json"), ("Alle", "*.*")])
        if not p:
            return
        try:
            d = json.load(open(p))
            self.bcs = d["bcs"]
            for k, v in d.get("options", {}).items():
                if hasattr(self, "v_" + k):
                    getattr(self, "v_" + k).set(v)
        except Exception as e:
            messagebox.showerror("Projekt", f"Konnte Projekt nicht laden: {e}", parent=self)
            return
        self.refresh()


def main():
    app = EncoderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
