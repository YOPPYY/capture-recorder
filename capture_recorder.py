#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
キャプチャ録画タイマー（Python版・ヘルパー内蔵）

USB接続の外部映像キャプチャデバイスの映像・音声を、終了タイマー付きで録画・録音します。
  - 音声のみ(MP3) ：sounddevice で取り込み、ffmpeg でMP3に変換（無音カット・無音で自動終了に対応）
  - 映像＋音声(MP4)：ffmpeg で直接録画（無音で自動終了は ffmpeg の silencedetect を使用）
  - 終了後にPCをスリープ／シャットダウン（別ヘルパー不要・猶予カウントダウンつき）

必要なもの:  Python 3.9+ / ffmpeg（PATHに通す） / pip install numpy sounddevice
使い方:      python capture_recorder.py            （画面つきで起動）
             python capture_recorder.py --help     （コマンドライン版）
"""
import argparse
import collections
import datetime
import json
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = "キャプチャ録画タイマー"
CONFIG = Path.home() / ".capture_recorder.json"
BLOCK = 4096          # 1ブロックのフレーム数
TAIL_PAD_SEC = 1.0    # 音の前後に残す余白（秒）
PENDING_MAX_SEC = 600 # 無音がこれより長く続いて、その後に音が出たときは途中の無音として残す

POWER_CMDS = {
    "Windows": {
        "sleep": ["powershell", "-NoProfile", "-Command",
                  "Add-Type -AssemblyName System.Windows.Forms; "
                  "[System.Windows.Forms.Application]::SetSuspendState("
                  "[System.Windows.Forms.PowerState]::Suspend, $false, $false)"],
        "shutdown": ["shutdown", "/s", "/t", "0"],
    },
    "Darwin": {
        "sleep": ["pmset", "sleepnow"],
        "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
    },
    "Linux": {
        "sleep": ["systemctl", "suspend"],
        "shutdown": ["systemctl", "poweroff"],
    },
}


def power_action(action, dry_run=False, log=print):
    cmd = POWER_CMDS.get(platform.system(), {}).get(action)
    if not cmd:
        log("このOSでは '%s' に未対応です" % action)
        return False
    if dry_run:
        log("[dry-run] 実行する予定のコマンド: " + " ".join(cmd))
        return True
    subprocess.Popen(cmd)
    return True


def fmt_hms(sec):
    sec = max(0, int(round(sec)))
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def find_ffmpeg():
    return shutil.which("ffmpeg")


# ============================================================ 音声ソース
class SoundDeviceSource:
    """sounddevice(PortAudio)で入力デバイスから音を取り込む。キューが無制限なので取りこぼしにくい。"""

    def __init__(self, device):
        import sounddevice as sd
        self.sd = sd
        info = sd.query_devices(device, "input")
        self.sr = int(info["default_samplerate"])
        self.ch = 2 if info["max_input_channels"] >= 2 else 1
        self.device = device
        self.overflows = 0
        self.stream = None

    def start(self, q):
        def cb(indata, frames, time_info, status):
            if status and status.input_overflow:
                self.overflows += 1
            q.put(indata.copy())
        self.stream = self.sd.InputStream(device=self.device, channels=self.ch, samplerate=self.sr,
                                          dtype="int16", blocksize=BLOCK, callback=cb)
        self.stream.start()

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass


class FakeSource:
    """動作確認用の疑似音源（できるだけ速く生成する）。pattern = [(秒, 振幅), ...]"""

    def __init__(self, pattern, sr=44100):
        self.sr = sr
        self.ch = 2
        self.pattern = pattern
        self.overflows = 0
        self.alive = False

    def start(self, q):
        import numpy as np
        self.alive = True

        def run():
            t = 0
            for sec, amp in self.pattern:
                n = int(sec * self.sr)
                done = 0
                while done < n and self.alive:
                    m = min(BLOCK, n - done)
                    x = (amp * 32767 * np.sin(2 * np.pi * 440 * (t + np.arange(m)) / self.sr)).astype("int16")
                    q.put(np.stack([x, x], axis=1))
                    t += m
                    done += m
                    time.sleep(0.0005)
            while self.alive:  # パターン終了後は無音を出し続ける
                q.put(np.zeros((BLOCK, 2), dtype="int16"))
                time.sleep(0.0005)

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self.alive = False


# ============================================================ エンジン（共通）
class BaseEngine:
    def __init__(self):
        self.stop_flag = threading.Event()
        self.auto_ended = False
        self.elapsed = 0.0
        self.silent = 0.0
        self.heard = False
        self.state = "idle"       # idle / recording / done / error
        self.out_path = None
        self.logs = []
        self.summary = ""

    def log(self, m):
        self.logs.append(m)

    def stop(self):
        self.stop_flag.set()


# ============================================================ MP3（音声のみ）
class Mp3Engine(BaseEngine):
    def __init__(self, source, out_path, kbps=192, gain=1.0, trim=True, thr_db=-50,
                 sil_stop=True, sil_sec=30, max_sec=3600, gain_getter=None):
        super().__init__()
        self.source, self.out_path = source, out_path
        self.kbps, self.gain, self.trim = kbps, gain, trim
        self.thr = 10 ** (thr_db / 20.0)
        self.sil_stop, self.sil_sec, self.max_sec = sil_stop, sil_sec, max_sec
        self.gain_getter = gain_getter  # 録音中に音量を変えられるよう、毎ブロック読み直す

    def run(self):
        import numpy as np
        ff = find_ffmpeg()
        if not ff:
            self.state = "error"
            self.summary = "ffmpeg が見つかりません。ffmpeg をインストールして PATH に通してください。"
            return
        sr = self.source.sr
        pad = max(1, math.ceil(TAIL_PAD_SEC * sr / BLOCK))
        q = queue.Queue()
        proc = None
        started = not self.trim
        pre = collections.deque(maxlen=pad)
        pending = []
        frames_total = last_loud_frames = written = 0
        heard = False
        pending_limit = int(PENDING_MAX_SEC * sr / BLOCK)
        t0 = time.time()

        def open_ff():
            Path(self.out_path).parent.mkdir(parents=True, exist_ok=True)
            return subprocess.Popen(
                [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sr), "-ac", "2",
                 "-i", "-", "-c:a", "libmp3lame", "-b:a", "%dk" % self.kbps, str(self.out_path)],
                stdin=subprocess.PIPE)

        def write(b):
            nonlocal proc, written
            if proc is None:
                proc = open_ff()
            proc.stdin.write(b.tobytes())
            written += len(b)

        self.state = "recording"
        self.source.start(q)
        try:
            while not self.stop_flag.is_set():
                try:
                    d = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                frames_total += len(d)
                peak = float(np.abs(d).max()) / 32768.0
                loud = peak > self.thr
                if loud:
                    heard = True
                    last_loud_frames = frames_total
                g = self.gain_getter() if self.gain_getter else self.gain
                st = d if d.shape[1] == 2 else np.repeat(d, 2, axis=1)
                if g != 1.0:
                    st = np.clip(st.astype("float32") * g, -32768, 32767).astype("int16")
                if self.trim:
                    if not started:
                        if loud:
                            started = True
                            for b in pre:
                                write(b)
                            pre.clear()
                            write(st)
                        else:
                            pre.append(st)
                    elif loud:
                        for b in pending:
                            write(b)
                        pending.clear()
                        write(st)
                    else:
                        pending.append(st)
                        if len(pending) > pending_limit:  # 長い無音は途中の無音として残す
                            for b in pending:
                                write(b)
                            pending.clear()
                else:
                    write(st)
                self.elapsed = frames_total / sr
                self.heard = heard
                self.silent = (frames_total - last_loud_frames) / sr if heard else 0.0
                if self.elapsed >= self.max_sec:
                    self.auto_ended = True
                    self.log("終了タイマーに達しました。")
                    break
                if self.sil_stop and heard and self.silent >= self.sil_sec:
                    self.auto_ended = True
                    self.log("無音が%d秒続いたため自動終了しました。" % self.sil_sec)
                    break
        finally:
            self.source.stop()
        # 末尾の無音は余白(約1秒)だけ残して捨てる
        if self.trim and started:
            for b in pending[:pad]:
                write(b)
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
        real = time.time() - t0
        if proc is None:
            self.state = "done"
            self.out_path = None
            self.summary = "音が検出されなかったため、ファイルは保存しませんでした。"
            return
        size = os.path.getsize(self.out_path) / 1048576.0
        if self.trim:
            self.summary = "保存: %s\n実時間 %s → カット後 %s ／ %.1f MB" % (
                self.out_path, fmt_hms(frames_total / sr), fmt_hms(written / sr), size)
        else:
            self.summary = "保存: %s\n録音時間 %s ／ %.1f MB" % (self.out_path, fmt_hms(written / sr), size)
        if getattr(self.source, "overflows", 0):
            self.summary += "\n⚠ 入力の取りこぼし(オーバーフロー)を %d 回検出しました。" % self.source.overflows
        self.state = "done"


# ============================================================ 映像＋音声（ffmpeg）
def list_dshow_devices():
    """Windows: ffmpeg で DirectShow のデバイス名を取得 → (映像, 音声)"""
    ff = find_ffmpeg()
    if not ff:
        return [], []
    r = subprocess.run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                       capture_output=True, text=True, errors="replace")
    vids, auds = [], []
    for m in re.finditer(r'"([^"]+)"\s+\((video|audio)\)', r.stderr):
        (vids if m.group(2) == "video" else auds).append(m.group(1))
    return vids, auds


class VideoEngine(BaseEngine):
    def __init__(self, out_path, duration, video_dev=None, audio_dev=None, res="1920x1080", fps=30,
                 crf=23, gain=1.0, sil_stop=True, sil_sec=30, thr_db=-50, input_args=None):
        super().__init__()
        self.out_path = out_path
        self.duration, self.video_dev, self.audio_dev = duration, video_dev, audio_dev
        self.res, self.fps, self.crf, self.gain = res, fps, crf, gain
        self.sil_stop, self.sil_sec, self.thr_db = sil_stop, sil_sec, thr_db
        self.input_args = input_args  # テスト用に入力を差し替えられる

    def build_cmd(self, ff):
        cmd = [ff, "-y", "-hide_banner"]
        if self.input_args:
            cmd += self.input_args
        else:
            osn = platform.system()
            if osn == "Windows":
                cmd += ["-f", "dshow", "-rtbufsize", "256M", "-framerate", str(self.fps)]
                if self.res and self.res != "auto":
                    cmd += ["-video_size", self.res]
                spec = "video=%s" % self.video_dev
                if self.audio_dev:
                    spec += ":audio=%s" % self.audio_dev
                cmd += ["-i", spec]
            elif osn == "Darwin":
                cmd += ["-f", "avfoundation", "-framerate", str(self.fps), "-i",
                        "%s:%s" % (self.video_dev or "0", self.audio_dev or "0")]
            else:
                cmd += ["-f", "v4l2", "-framerate", str(self.fps), "-i", self.video_dev or "/dev/video0"]
                if self.audio_dev:
                    cmd += ["-f", "alsa", "-i", self.audio_dev]
        if self.duration:
            cmd += ["-t", str(int(self.duration))]
        af = []
        if self.gain != 1.0:
            af.append("volume=%.3f" % self.gain)
        if self.sil_stop:
            af.append("silencedetect=n=%ddB:d=%d" % (self.thr_db, self.sil_sec))
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.crf), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", str(self.out_path)]
        return cmd

    def run(self):
        ff = find_ffmpeg()
        if not ff:
            self.state = "error"
            self.summary = "ffmpeg が見つかりません。ffmpeg をインストールして PATH に通してください。"
            return
        Path(self.out_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = self.build_cmd(ff)
        self.log("ffmpeg: " + " ".join('"%s"' % c if " " in c else c for c in cmd))
        self.state = "recording"
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        errtail = collections.deque(maxlen=12)
        self._heard = False

        def reader():
            buf = b""
            while True:
                ch = p.stderr.read(1)
                if not ch:
                    break
                if ch in (b"\r", b"\n"):
                    line = buf.decode("utf-8", "replace")
                    buf = b""
                    if not line:
                        continue
                    m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                    if m:
                        self.elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        continue
                    m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
                    if m:
                        if self._heard or float(m.group(1)) > 1.0:  # 一度音が出たあとの無音
                            self._heard = True
                            self.heard = True
                            self.auto_ended = True
                            self.log("無音が%d秒続いたため自動終了しました。" % self.sil_sec)
                            self.stop_flag.set()
                        continue
                    if "silence_end" in line:
                        self._heard = True
                        self.heard = True
                        continue
                    errtail.append(line)
                else:
                    buf += ch

        threading.Thread(target=reader, daemon=True).start()
        while p.poll() is None:
            if self.stop_flag.is_set():
                try:
                    p.stdin.write(b"q\n")
                    p.stdin.flush()
                except Exception:
                    pass
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.terminate()
                break
            time.sleep(0.2)
        p.wait()
        if self.duration and self.elapsed >= self.duration - 1:
            self.auto_ended = True
            self.log("終了タイマーに達しました。")
        if os.path.exists(self.out_path) and os.path.getsize(self.out_path) > 0:
            size = os.path.getsize(self.out_path) / 1048576.0
            self.summary = "保存: %s\n録画時間 %s ／ %.1f MB" % (self.out_path, fmt_hms(self.elapsed), size)
            self.state = "done"
        else:
            self.state = "error"
            self.summary = "録画できませんでした。\n" + "\n".join(errtail)


# ============================================================ 共通ユーティリティ
def new_out_path(folder, ext):
    now = datetime.datetime.now()
    return Path(folder) / ("capture_%s.%s" % (now.strftime("%Y%m%d_%H%M%S"), ext))


def seconds_until(hhmmss):
    h, m, s = (list(map(int, hhmmss.split(":"))) + [0, 0])[:3]
    now = datetime.datetime.now()
    t = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if t <= now:
        t += datetime.timedelta(days=1)
    return int((t - now).total_seconds())


def list_audio_inputs():
    try:
        import sounddevice as sd
        return [(i, "%d: %s" % (i, d["name"])) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception:
        return []


# ============================================================ GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title(APP)
    root.geometry("860x760")

    cfg = {}
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        pass

    def var(kind, key, default):
        v = {"s": tk.StringVar, "i": tk.IntVar, "b": tk.BooleanVar, "d": tk.DoubleVar}[kind](value=cfg.get(key, default))
        v._key = key
        return v

    V = {
        "mode": var("s", "mode", "音声のみ (MP3)"),
        "adev": var("s", "adev", ""), "vdev": var("s", "vdev", ""),
        "gain": var("d", "gain", min(float(cfg.get("gain", 100.0)), 150.0)),
        "end": var("s", "end", "duration"),
        "hh": var("i", "hh", 1), "mm": var("i", "mm", 0), "ss": var("i", "ss", 0), "endat": var("s", "endat", "23:00:00"),
        "sil_stop": var("b", "sil_stop", True), "sil_sec": var("i", "sil_sec", 30),
        "trim": var("b", "trim", True), "thr": var("s", "thr", "-50"),
        "kbps": var("s", "kbps", "192"), "res": var("s", "res", "1920x1080"), "fps": var("s", "fps", "30"),
        "out": var("s", "out", str(Path.home() / "capture_recordings")),
        "pre": var("i", "pre", 3),
        "power": var("s", "power", "なし"), "pwrsec": var("i", "pwrsec", 60),
    }

    def save_cfg():
        try:
            CONFIG.write_text(json.dumps({v._key: v.get() for v in V.values()}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    pad = {"padx": 8, "pady": 3}
    main = ttk.Frame(root, padding=10)
    main.pack(fill="both", expand=True)
    main.columnconfigure(1, weight=1)
    r = 0

    def row(label, widget_builder):
        nonlocal r
        ttk.Label(main, text=label).grid(row=r, column=0, sticky="w", **pad)
        f = ttk.Frame(main)
        f.grid(row=r, column=1, sticky="ew", **pad)
        widget_builder(f)
        r += 1
        return f

    modes = ["音声のみ (MP3)", "映像＋音声 (MP4 / ffmpeg)"]
    row("形式", lambda f: ttk.Combobox(f, textvariable=V["mode"], values=modes, state="readonly", width=32).pack(side="left"))

    adevs = list_audio_inputs()
    acombo = {}

    def build_adev(f):
        acombo["w"] = ttk.Combobox(f, textvariable=V["adev"], values=[x[1] for x in adevs], width=60)
        acombo["w"].pack(side="left")
        if not V["adev"].get() and adevs:
            V["adev"].set(adevs[0][1])
    row("音声デバイス", build_adev)

    vcombo = {}

    def build_vdev(f):
        vcombo["w"] = ttk.Combobox(f, textvariable=V["vdev"], width=45)
        vcombo["w"].pack(side="left")

        def refresh():
            if platform.system() == "Windows":
                vids, auds = list_dshow_devices()
                vcombo["w"]["values"] = vids
                acombo["w"]["values"] = [x[1] for x in adevs] + auds
                if vids and not V["vdev"].get():
                    V["vdev"].set(vids[0])
            else:
                vcombo["w"]["values"] = sorted(str(p) for p in Path("/dev").glob("video*"))
        ttk.Button(f, text="更新", command=refresh).pack(side="left", padx=6)
        root.after(300, refresh)
    row("映像デバイス（映像時）", build_vdev)

    def build_gain(f):
        lab = ttk.Label(f, text="100%", width=6)
        ttk.Scale(f, from_=0, to=150, variable=V["gain"], length=300,
                  command=lambda x: lab.config(text="%d%%" % float(x))).pack(side="left")
        lab.pack(side="left", padx=6)
        lab.config(text="%d%%" % V["gain"].get())
    row("録音音量", build_gain)

    def build_end(f):
        ttk.Radiobutton(f, text="録画時間", variable=V["end"], value="duration").pack(side="left")
        for k, u in (("hh", "時間"), ("mm", "分"), ("ss", "秒")):
            ttk.Spinbox(f, from_=0, to=99, width=4, textvariable=V[k]).pack(side="left", padx=(6, 0))
            ttk.Label(f, text=u).pack(side="left")
        ttk.Radiobutton(f, text="終了時刻", variable=V["end"], value="endat").pack(side="left", padx=(16, 0))
        ttk.Entry(f, textvariable=V["endat"], width=9).pack(side="left", padx=4)
    row("終了条件", build_end)

    def build_sil(f):
        ttk.Checkbutton(f, text="音が出たあと、無音が続いたら自動終了", variable=V["sil_stop"]).pack(side="left")
        ttk.Spinbox(f, from_=1, to=3600, width=5, textvariable=V["sil_sec"]).pack(side="left", padx=6)
        ttk.Label(f, text="秒").pack(side="left")
    row("自動終了", build_sil)

    def build_sset(f):
        ttk.Label(f, text="無音と判定する音量(dB)").pack(side="left")
        ttk.Combobox(f, textvariable=V["thr"], values=["-40", "-50", "-60"], width=5, state="readonly").pack(side="left", padx=6)
        ttk.Checkbutton(f, text="最初と最後の無音を自動カット（MP3のみ）", variable=V["trim"]).pack(side="left", padx=10)
    row("無音の設定", build_sset)

    def build_q(f):
        ttk.Label(f, text="MP3 kbps").pack(side="left")
        ttk.Combobox(f, textvariable=V["kbps"], values=["128", "192", "256", "320"], width=5, state="readonly").pack(side="left", padx=4)
        ttk.Label(f, text="映像 解像度").pack(side="left", padx=(14, 0))
        ttk.Combobox(f, textvariable=V["res"], values=["auto", "3840x2160", "1920x1080", "1280x720", "640x480"], width=10).pack(side="left", padx=4)
        ttk.Label(f, text="FPS").pack(side="left", padx=(8, 0))
        ttk.Combobox(f, textvariable=V["fps"], values=["60", "30", "24"], width=4).pack(side="left", padx=4)
        ttk.Label(f, text="開始前カウントダウン(秒)").pack(side="left", padx=(14, 0))
        ttk.Spinbox(f, from_=0, to=60, width=4, textvariable=V["pre"]).pack(side="left", padx=4)
    row("品質・その他", build_q)

    def build_out(f):
        ttk.Entry(f, textvariable=V["out"], width=56).pack(side="left")
        ttk.Button(f, text="参照", command=lambda: V["out"].set(filedialog.askdirectory() or V["out"].get())).pack(side="left", padx=6)
    row("保存先フォルダ", build_out)

    def build_pwr(f):
        ttk.Combobox(f, textvariable=V["power"], values=["なし", "スリープ", "シャットダウン"], width=14, state="readonly").pack(side="left")
        ttk.Label(f, text="実行までの猶予(秒)").pack(side="left", padx=(12, 0))
        ttk.Spinbox(f, from_=5, to=3600, width=6, textvariable=V["pwrsec"]).pack(side="left", padx=4)
        ttk.Label(f, text="※自動終了したときだけ実行（停止ボタンでは実行しません）").pack(side="left", padx=8)
    row("終了後のPC操作", build_pwr)

    clock = ttk.Label(main, text="00:00:00", font=("", 34, "bold"))
    clock.grid(row=r, column=0, columnspan=2, pady=(10, 0))
    r += 1
    status = ttk.Label(main, text="待機中", foreground="#666")
    status.grid(row=r, column=0, columnspan=2)
    r += 1
    bf = ttk.Frame(main)
    bf.grid(row=r, column=0, columnspan=2, pady=8)
    btn_start = ttk.Button(bf, text="● 開始", width=16)
    btn_stop = ttk.Button(bf, text="■ 停止", width=16, state="disabled")
    btn_cancel = ttk.Button(bf, text="PC操作をキャンセル", width=20)
    btn_start.pack(side="left", padx=6)
    btn_stop.pack(side="left", padx=6)
    r += 1
    logbox = tk.Text(main, height=9, state="disabled", wrap="word")
    logbox.grid(row=r, column=0, columnspan=2, sticky="nsew", padx=8, pady=4)
    main.rowconfigure(r, weight=1)

    def say(m):
        logbox.config(state="normal")
        logbox.insert("end", "[%s] %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), m))
        logbox.see("end")
        logbox.config(state="disabled")

    S = {"engine": None, "thread": None, "cancel_power": False, "shown": 0}

    def start():
        is_video = V["mode"].get() != modes[0]
        out = V["out"].get().strip()
        if V["end"].get() == "duration":
            total = V["hh"].get() * 3600 + V["mm"].get() * 60 + V["ss"].get()
        else:
            try:
                total = seconds_until(V["endat"].get())
            except Exception:
                messagebox.showerror(APP, "終了時刻は HH:MM:SS の形式で入力してください。")
                return
        if total <= 0:
            messagebox.showerror(APP, "終了条件を設定してください。")
            return
        if not find_ffmpeg():
            messagebox.showerror(APP, "ffmpeg が見つかりません。インストールして PATH に通してください。")
            return
        gain = V["gain"].get() / 100.0
        thr = int(V["thr"].get())
        if is_video:
            if not V["vdev"].get():
                messagebox.showerror(APP, "映像デバイスを選んでください。")
                return
            aud = V["adev"].get()
            aud = re.sub(r"^\d+:\s*", "", aud) if aud else None
            eng = VideoEngine(new_out_path(out, "mp4"), total, V["vdev"].get(), aud, V["res"].get(), V["fps"].get(),
                              gain=gain, sil_stop=V["sil_stop"].get(), sil_sec=V["sil_sec"].get(), thr_db=thr)
        else:
            sel = V["adev"].get()
            m = re.match(r"^(\d+):", sel)
            if not m:
                messagebox.showerror(APP, "音声デバイスを選んでください。(pip install numpy sounddevice が必要です)")
                return
            try:
                src = SoundDeviceSource(int(m.group(1)))
            except Exception as e:
                messagebox.showerror(APP, "音声デバイスを開けませんでした:\n%s" % e)
                return
            eng = Mp3Engine(src, new_out_path(out, "mp3"), kbps=int(V["kbps"].get()), trim=V["trim"].get(),
                            thr_db=thr, sil_stop=V["sil_stop"].get(), sil_sec=V["sil_sec"].get(), max_sec=total,
                            gain_getter=lambda: V["gain"].get() / 100.0)
        save_cfg()
        S["engine"] = eng
        S["shown"] = 0
        btn_start.config(state="disabled")
        btn_stop.config(state="normal")

        def go(n):
            if S["engine"] is not eng:
                return
            if n > 0:
                clock.config(text=str(n))
                status.config(text="まもなく開始…")
                root.after(1000, lambda: go(n - 1))
                return
            say("%sを開始します。" % ("録画" if is_video else "録音"))
            S["thread"] = threading.Thread(target=eng.run, daemon=True)
            S["thread"].start()
            root.after(200, poll)
        go(V["pre"].get())

    def poll():
        eng = S["engine"]
        if eng is None:
            return
        while S["shown"] < len(eng.logs):
            say(eng.logs[S["shown"]])
            S["shown"] += 1
        if eng.state == "recording":
            clock.config(text=fmt_hms(eng.elapsed))
            extra = (" ／ 無音 %d/%d秒" % (eng.silent, V["sil_sec"].get())) if V["sil_stop"].get() and eng.heard and eng.silent >= 1 else ""
            status.config(text="● 記録中 経過 %s%s" % (fmt_hms(eng.elapsed), extra))
            root.after(200, poll)
            return
        if S["thread"] and S["thread"].is_alive():
            root.after(200, poll)
            return
        finished(eng)

    def finished(eng):
        btn_stop.config(state="disabled")
        btn_start.config(state="normal")
        status.config(text="完了" if eng.state == "done" else "エラー")
        say(eng.summary or "終了しました。")
        S["engine"] = None
        act = {"スリープ": "sleep", "シャットダウン": "shutdown"}.get(V["power"].get())
        if act and eng.auto_ended and eng.state == "done":
            S["cancel_power"] = False
            btn_cancel.pack(side="left", padx=6)
            btn_cancel.config(command=lambda: S.__setitem__("cancel_power", True))

            def count(left):
                if S["cancel_power"]:
                    btn_cancel.pack_forget()
                    say("PC操作をキャンセルしました。")
                    return
                if left <= 0:
                    btn_cancel.pack_forget()
                    say("PCを%sします…" % V["power"].get())
                    power_action(act, log=say)
                    return
                status.config(text="%d秒後にPCを%sします" % (left, V["power"].get()))
                root.after(1000, lambda: count(left - 1))
            count(max(5, V["pwrsec"].get()))

    def stop():
        eng = S["engine"]
        if eng:
            eng.stop()
            say("停止します…")
            S["engine"] = eng
        btn_stop.config(state="disabled")

    btn_start.config(command=start)
    btn_stop.config(command=stop)

    def on_close():
        save_cfg()
        if S["engine"] and S["engine"].state == "recording":
            if not messagebox.askyesno(APP, "記録中です。停止して終了しますか？"):
                return
            S["engine"].stop()
            if S["thread"]:
                S["thread"].join(timeout=20)
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


# ============================================================ CLI / セルフテスト
def run_cli(a):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if a.list_devices:
        print("音声入力デバイス:")
        for _, name in list_audio_inputs():
            print("  " + name)
        if platform.system() == "Windows":
            v, au = list_dshow_devices()
            print("映像デバイス(dshow):", v)
            print("音声デバイス(dshow):", au)
        return 0
    total = a.duration
    gain = a.gain / 100.0
    if a.mode == "video":
        eng = VideoEngine(new_out_path(a.out, "mp4"), total, a.video_device, a.audio_name, a.res, a.fps, gain=gain,
                          sil_stop=a.silence_stop > 0, sil_sec=a.silence_stop or 30, thr_db=a.thr)
    else:
        src = SoundDeviceSource(a.device)
        eng = Mp3Engine(src, new_out_path(a.out, "mp3"), kbps=a.kbps, gain=gain, trim=not a.no_trim, thr_db=a.thr,
                        sil_stop=a.silence_stop > 0, sil_sec=a.silence_stop or 30, max_sec=total)
    th = threading.Thread(target=eng.run, daemon=True)
    th.start()
    shown = 0
    try:
        while th.is_alive():
            time.sleep(1)
            while shown < len(eng.logs):
                print(eng.logs[shown]); shown += 1
            print("\r経過 %s 無音 %ds " % (fmt_hms(eng.elapsed), eng.silent), end="", flush=True)
    except KeyboardInterrupt:
        print("\n停止します…")
        eng.stop()
        th.join()
    print("\n" + eng.summary)
    if a.power != "none" and eng.auto_ended and eng.state == "done":
        print("%d秒後にPCを%sします（Ctrl+Cでキャンセル）" % (a.power_delay, a.power))
        try:
            time.sleep(a.power_delay)
            power_action(a.power, dry_run=a.dry_run)
        except KeyboardInterrupt:
            print("キャンセルしました。")
    return 0 if eng.state == "done" else 1


def selftest():
    """疑似音源とffmpegで、無音カット・無音終了・音量・動画の自動終了を確認する"""
    import tempfile
    d = tempfile.mkdtemp()
    ok = True

    def probe(path):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        return float(r.stdout.strip() or 0)

    # 1) 3秒無音→5秒音→4秒無音→5秒音→以降無音。無音30秒で自動終了、前後の無音カット
    src = FakeSource([(3, 0.0), (5, 0.3), (4, 0.0), (5, 0.3)])
    e = Mp3Engine(src, Path(d) / "t1.mp3", trim=True, sil_stop=True, sil_sec=30, max_sec=600)
    e.run()
    dur = probe(e.out_path)
    exp = 1 + 5 + 4 + 5 + 1  # 前余白1 + 音5 + 途中無音4 + 音5 + 後余白1
    good = e.auto_ended and abs(dur - exp) < 1.0
    print("[1] 無音カット+無音で自動終了: 長さ %.1fs (期待 約%ds) 自動終了=%s → %s" % (dur, exp, e.auto_ended, "OK" if good else "NG"))
    ok &= good
    # 2) トリムなし・時間上限
    src = FakeSource([(2, 0.3)])
    e = Mp3Engine(src, Path(d) / "t2.mp3", trim=False, sil_stop=False, max_sec=6)
    e.run()
    dur = probe(e.out_path)
    good = abs(dur - 6) < 1.0
    print("[2] トリムなし・6秒で終了: 長さ %.1fs → %s" % (dur, "OK" if good else "NG"))
    ok &= good
    # 3) 音量 50% / 200%
    def peak(path):
        r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
        return float(m.group(1)) if m else None
    levels = {}
    for g in (0.5, 1.0, 2.0):
        e = Mp3Engine(FakeSource([(3, 0.2)]), Path(d) / ("g%d.mp3" % int(g * 100)), trim=False, sil_stop=False, max_sec=3, gain=g)
        e.run()
        levels[g] = peak(e.out_path)
    good = levels[0.5] < levels[1.0] - 4 and levels[2.0] > levels[1.0] + 4
    print("[3] 音量: 50%%=%s dB / 100%%=%s dB / 200%%=%s dB → %s" % (levels[0.5], levels[1.0], levels[2.0], "OK" if good else "NG"))
    ok &= good
    # 4) 全部無音ならファイルを作らない
    e = Mp3Engine(FakeSource([(1, 0.0)]), Path(d) / "t4.mp3", trim=True, sil_stop=False, max_sec=3)
    e.run()
    good = e.out_path is None and not (Path(d) / "t4.mp3").exists()
    print("[4] 全部無音は保存しない → %s" % ("OK" if good else "NG"))
    ok &= good
    # 5) 動画(ffmpeg): 3秒の音のあと無音4秒で自動終了
    inp = ["-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
           "-re", "-f", "lavfi", "-i", "aevalsrc=if(lt(t\\,3)\\,0.3*sin(2*PI*440*t)\\,0):s=44100"]
    v = VideoEngine(Path(d) / "v.mp4", 60, input_args=inp, sil_stop=True, sil_sec=4, thr_db=-50)
    t0 = time.time()
    v.run()
    took = time.time() - t0
    good = v.state == "done" and v.auto_ended and 6 < took < 15
    print("[5] 動画: 無音4秒で自動終了 (%.1f秒で停止, 状態=%s) → %s" % (took, v.state, "OK" if good else "NG"))
    ok &= good
    # 6) 電源操作 dry-run
    print("[6] 電源操作(dry-run):", end=" ")
    power_action("sleep", dry_run=True)
    print("\n結果:", "すべてOK" if ok else "失敗あり")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=APP + "（Python版）")
    ap.add_argument("--cli", action="store_true", help="画面なしで実行する")
    ap.add_argument("--selftest", action="store_true", help="動作確認（疑似音源とffmpegで試す）")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--mode", choices=["mp3", "video"], default="mp3")
    ap.add_argument("--device", type=int, default=None, help="音声入力デバイス番号（--list-devices で確認）")
    ap.add_argument("--video-device", default=None, help="映像デバイス名（Windows: dshowの名前）")
    ap.add_argument("--audio-name", default=None, help="映像録画時の音声デバイス名（dshow）")
    ap.add_argument("--duration", type=int, default=3600, help="録画/録音の最大時間(秒)")
    ap.add_argument("--silence-stop", type=int, default=30, help="無音が続いたら自動終了(秒, 0で無効)")
    ap.add_argument("--no-trim", action="store_true", help="前後の無音カットをしない(MP3)")
    ap.add_argument("--thr", type=int, default=-50, help="無音と判定する音量(dB)")
    ap.add_argument("--gain", type=float, default=100.0, help="録音音量(%%)")
    ap.add_argument("--kbps", type=int, default=192)
    ap.add_argument("--res", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", default=str(Path.home() / "capture_recordings"))
    ap.add_argument("--power", choices=["none", "sleep", "shutdown"], default="none")
    ap.add_argument("--power-delay", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true", help="電源操作を実際には実行しない")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.cli or a.list_devices:
        sys.exit(run_cli(a))
    run_gui()


if __name__ == "__main__":
    main()
