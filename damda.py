"""
담다(Damda) — 동영상 다운로더  (THEJOYMAN)

- 영상·재생목록·채널 링크를 붙여넣고 [다운로드] → 큐에 쌓여 순서대로 받음
- 동영상(MP4) / MP3 모드, 화질·음질 선택
- 일시정지 / 이어받기 / 취소, 완료 항목 지우기
- 구독: 채널을 등록해 두고 [새 영상 확인]으로 새 영상만 받기
- 설정: 저장 폴더, 자막, 재생목록 하위 폴더, 동시 다운로드 수

필요한 것: Python 3.11+, PySide6, yt-dlp, imageio-ffmpeg (install.bat 이 설치)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # pragma: no cover
    app = QApplication(sys.argv)
    QMessageBox.critical(None, "yt-dlp 없음", "yt-dlp가 설치되어 있지 않습니다.\n\npip install yt-dlp")
    sys.exit(1)

APP_NAME = "담다"
APP_SUB = "THEJOYMAN"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # PyInstaller 대응
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SETTINGS_FILE = DATA_DIR / "settings.json"
SUBS_FILE = DATA_DIR / "subscriptions.json"

VIDEO_QUALITY = {
    "최고 화질": "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
}
AUDIO_QUALITY = {"320kbps": "320", "256kbps": "256", "192kbps": "192", "128kbps": "128"}
URL_RE = re.compile(r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I)

# ---------------------------------------------------------------- 유틸


def ffmpeg_dir() -> str | None:
    for d in (BASE_DIR, DATA_DIR):
        if (d / "ffmpeg.exe").exists() or (d / "ffmpeg").exists():
            return str(d)
    try:  # pip install imageio-ffmpeg 로 받은 ffmpeg 사용
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def human_size(n) -> str:
    if not n:
        return "-"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 데이터


@dataclass
class Settings:
    save_dir: str = str(Path.home() / "Downloads" / "goodseedown")
    subtitles: bool = False
    sub_langs: str = "ko,en"
    playlist_subfolder: bool = True
    concurrent: int = 2

    @classmethod
    def load(cls) -> "Settings":
        d = load_json(SETTINGS_FILE, {})
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def save(self):
        save_json(SETTINGS_FILE, self.__dict__)


@dataclass
class Item:
    url: str
    mode: str                    # "video" | "audio"
    quality: str                 # 화질 또는 음질 라벨
    title: str = ""
    subfolder: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "대기"         # 대기 | 진행 | 일시정지 | 완료 | 오류 | 취소
    progress: float = 0.0
    detail: str = ""
    filepath: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


# ---------------------------------------------------------------- 워커


class DownloadWorker(QThread):
    progress = Signal(str, float, str)       # id, pct, text
    finished_ok = Signal(str, str)           # id, filepath
    failed = Signal(str, str)                # id, message
    cancelled = Signal(str)
    title_found = Signal(str, str)

    def __init__(self, item: Item, settings: Settings):
        super().__init__()
        self.item = item
        self.settings = settings

    def _opts(self) -> dict:
        it, st = self.item, self.settings
        out_dir = Path(st.save_dir)
        if it.subfolder and st.playlist_subfolder:
            out_dir = out_dir / re.sub(r'[\\/:*?"<>|]', "_", it.subfolder)
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "continuedl": True,
            "windowsfilenames": True,
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp_hook],
        }
        fd = ffmpeg_dir()
        if fd:
            opts["ffmpeg_location"] = fd
        if st.subtitles:
            opts.update({"writesubtitles": True, "writeautomaticsub": True,
                         "subtitleslangs": [s.strip() for s in st.sub_langs.split(",") if s.strip()]})
        if it.mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": AUDIO_QUALITY.get(it.quality, "192")}]
        else:
            opts["format"] = VIDEO_QUALITY.get(it.quality, VIDEO_QUALITY["최고 화질"])
            opts["merge_output_format"] = "mp4"
        return opts

    def _hook(self, d: dict):
        if self.item.cancel.is_set():
            raise DownloadCancelled("일시정지")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = done / total * 100 if total else 0
            self.progress.emit(self.item.id, pct,
                               f"{pct:.1f}%  {human_size(d.get('speed'))}/s  {human_size(done)} / {human_size(total)}")
        elif d.get("status") == "finished":
            self.progress.emit(self.item.id, 100, "받기 완료, 합치는 중… (ffmpeg)")

    def _pp_hook(self, d: dict):
        if d.get("status") == "started":
            self.progress.emit(self.item.id, 100, "변환·합치기 중… (ffmpeg)")
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            fp = info.get("filepath") or info.get("_filename") or ""
            if fp:
                self.item.filepath = fp

    def run(self):
        try:
            with yt_dlp.YoutubeDL(self._opts()) as ydl:
                info = ydl.extract_info(self.item.url, download=False)
                if info and not self.item.title:
                    self.title_found.emit(self.item.id, info.get("title") or self.item.url)
                ydl.process_ie_result(info, download=True)
                if not self.item.filepath:
                    reqs = info.get("requested_downloads") or []
                    if reqs:
                        self.item.filepath = reqs[0].get("filepath", "")
        except DownloadCancelled:
            self.cancelled.emit(self.item.id)
            return
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "ffmpeg" in msg.lower():
                msg += "\nffmpeg.exe를 프로그램과 같은 폴더에 두세요."
            self.failed.emit(self.item.id, msg)
            return
        self.finished_ok.emit(self.item.id, self.item.filepath)


class InfoWorker(QThread):
    """링크가 영상 하나인지 재생목록·채널인지 알아내고 항목 목록을 돌려준다."""
    result = Signal(list, str)   # [(url, title)], playlist_title
    failed = Signal(str)
    channel = Signal(dict)       # 구독용 채널 정보

    def __init__(self, url: str, limit: int = 0):
        super().__init__()
        self.url, self.limit = url, limit

    def run(self):
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
        if self.limit:
            opts["playlistend"] = self.limit
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
            return
        entries = info.get("entries")
        if entries is not None:
            items = []
            for e in entries:
                if not e:
                    continue
                u = e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e.get('id')}"
                items.append((u, e.get("title") or ""))
            self.channel.emit({"name": info.get("channel") or info.get("uploader") or info.get("title") or self.url,
                               "url": info.get("channel_url") or info.get("uploader_url") or self.url,
                               "seen": [e.get("id") for e in entries if e]})
            self.result.emit(items, info.get("title") or "")
        else:
            self.channel.emit({"name": info.get("channel") or info.get("uploader") or "",
                               "url": info.get("channel_url") or info.get("uploader_url") or "",
                               "seen": []})
            self.result.emit([(info.get("webpage_url") or self.url, info.get("title") or "")], "")


# ---------------------------------------------------------------- 스타일

STYLE = """
QMainWindow, QWidget { background: #1a0f10; color: #f3e9e9; font-family: 'Malgun Gothic','Segoe UI',sans-serif; font-size: 13px; }
QLabel { background: transparent; }
QFrame#header { background: #241618; border-bottom: 1px solid #3b2426; }
QLineEdit { background: #2a1b1d; border: 1px solid #4a2d30; border-radius: 6px; padding: 7px 10px; color: #f3e9e9; }
QLineEdit:focus { border-color: #e53935; }
QComboBox, QSpinBox { background: #2a1b1d; border: 1px solid #4a2d30; border-radius: 6px; padding: 5px 8px; color: #f3e9e9; min-height: 20px; }
QComboBox QAbstractItemView { background: #2a1b1d; color: #f3e9e9; selection-background-color: #8e1e1e; }
QPushButton { background: #2a1b1d; border: 1px solid #4a2d30; border-radius: 6px; padding: 6px 12px; color: #f3e9e9; }
QPushButton:hover { background: #3b2426; }
QPushButton:disabled { color: #6f8578; }
QPushButton#primary { background: #c62828; border: none; font-weight: bold; padding: 8px 18px; }
QPushButton#primary:hover { background: #e53935; }
QPushButton#seg { background: transparent; border: none; border-radius: 6px; padding: 6px 16px; font-weight: bold; color: #b89a9d; }
QPushButton#seg:checked { background: #c62828; color: white; }
QFrame#segbox { background: #2a1b1d; border: 1px solid #4a2d30; border-radius: 8px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; padding: 10px 22px; color: #b89a9d; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #f3e9e9; border-bottom: 2px solid #e53935; }
QListWidget { background: #1a0f10; border: none; }
QListWidget::item { border-bottom: 1px solid #332022; }
QListWidget::item:selected { background: #241618; }
QProgressBar { background: #2a1b1d; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }
QProgressBar::chunk { background: #e53935; border-radius: 4px; }
QFrame#card { background: transparent; }
QLabel#muted { color: #b89a9d; }
QLabel#footer { color: #b89a9d; font-size: 12px; }
QFrame#footer { background: #241618; border-top: 1px solid #3b2426; }
QCheckBox { spacing: 8px; }
"""


def make_logo(size: int = 36) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#c62828"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, size, size, 8, 8)
    pen = QPen(QColor("white"), max(2, size // 12))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    c = size / 2
    p.drawLine(c, size * 0.2, c, size * 0.58)                  # 화살표 줄기
    p.drawLine(c - size * 0.16, size * 0.44, c, size * 0.6)
    p.drawLine(c + size * 0.16, size * 0.44, c, size * 0.6)
    p.drawLine(size * 0.22, size * 0.62, size * 0.22, size * 0.8)   # 담는 상자
    p.drawLine(size * 0.78, size * 0.62, size * 0.78, size * 0.8)
    p.drawLine(size * 0.22, size * 0.8, size * 0.78, size * 0.8)
    p.end()
    return pm


# ---------------------------------------------------------------- 큐 항목 위젯


class ItemWidget(QFrame):
    def __init__(self, item: Item, owner: "MainWindow"):
        super().__init__()
        self.setObjectName("card")
        self.item, self.owner = item, owner
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        top = QHBoxLayout()
        self.title = QLabel(item.title or item.url)
        self.title.setStyleSheet("font-weight: bold;")
        self.status = QLabel(item.status)
        self.status.setObjectName("muted")
        top.addWidget(self.title, 1)
        top.addWidget(self.status)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)
        bottom = QHBoxLayout()
        self.detail = QLabel(item.detail)
        self.detail.setObjectName("muted")
        bottom.addWidget(self.detail, 1)
        self.btn_pause = QPushButton("일시정지")
        self.btn_resume = QPushButton("이어받기")
        self.btn_cancel = QPushButton("취소")
        self.btn_open = QPushButton("폴더")
        for b in (self.btn_pause, self.btn_resume, self.btn_cancel, self.btn_open):
            b.setFixedHeight(26)
            bottom.addWidget(b)
        self.btn_pause.clicked.connect(lambda: owner.pause(item.id))
        self.btn_resume.clicked.connect(lambda: owner.resume(item.id))
        self.btn_cancel.clicked.connect(lambda: owner.cancel(item.id))
        self.btn_open.clicked.connect(lambda: owner.open_item_folder(item.id))
        lay.addLayout(bottom)
        self.refresh()

    def refresh(self):
        it = self.item
        self.title.setText(it.title or it.url)
        self.status.setText(it.status)
        self.bar.setValue(int(it.progress))
        self.detail.setText(it.detail)
        self.btn_pause.setVisible(it.status in ("진행", "대기"))
        self.btn_resume.setVisible(it.status in ("일시정지", "오류", "취소"))
        self.btn_cancel.setVisible(it.status not in ("완료",))
        self.btn_open.setVisible(it.status == "완료")
        colors = {"완료": "#66bb6a", "오류": "#ffb74d", "일시정지": "#d8b24a", "진행": "#f0a3a3"}
        self.status.setStyleSheet(f"color: {colors.get(it.status, '#b89a9d')};")


# ---------------------------------------------------------------- 메인 창


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — 동영상 다운로더")
        self.resize(1160, 800)
        ico = BASE_DIR / "damda.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        self.settings = Settings.load()
        self.subs: list[dict] = load_json(SUBS_FILE, [])
        self.items: dict[str, Item] = {}
        self.widgets: dict[str, ItemWidget] = {}
        self.workers: dict[str, DownloadWorker] = {}
        self.order: list[str] = []
        self._info_workers: list[QThread] = []
        self._build()
        self.setStyleSheet(STYLE)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pump)
        self._timer.start(500)

    # ---------- 화면
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._header())
        v.addWidget(self._tabs(), 1)
        v.addWidget(self._footer())

    def _header(self) -> QFrame:
        f = QFrame()
        f.setObjectName("header")
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(14)

        logo = QLabel()
        logo.setPixmap(make_logo(36))
        h.addWidget(logo)
        name_box = QVBoxLayout()
        name_box.setSpacing(0)
        n = QLabel(APP_NAME)
        n.setFont(QFont("Malgun Gothic", 14, QFont.Bold))
        s = QLabel(APP_SUB)
        s.setObjectName("muted")
        name_box.addWidget(n)
        name_box.addWidget(s)
        h.addLayout(name_box)

        seg = QFrame()
        seg.setObjectName("segbox")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(2)
        self.btn_video = QPushButton("동영상")
        self.btn_audio = QPushButton("MP3")
        for b in (self.btn_video, self.btn_audio):
            b.setObjectName("seg")
            b.setCheckable(True)
            b.clicked.connect(self._mode_changed)
            sl.addWidget(b)
        self.btn_video.setChecked(True)
        h.addWidget(seg)

        mid = QVBoxLayout()
        row1 = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("영상·재생목록·채널 링크를 붙여넣으세요")
        self.url.returnPressed.connect(self.add_download)
        row1.addWidget(self.url, 1)
        b_paste = QPushButton("붙여넣기")
        b_paste.clicked.connect(lambda: self.url.setText(QApplication.clipboard().text().strip()))
        row1.addWidget(b_paste)
        row1.addWidget(QLabel("형식"))
        self.fmt = QComboBox()
        row1.addWidget(self.fmt)
        self.q_label = QLabel("화질")
        row1.addWidget(self.q_label)
        self.quality = QComboBox()
        row1.addWidget(self.quality)
        b_dl = QPushButton("다운로드")
        b_dl.setObjectName("primary")
        b_dl.clicked.connect(self.add_download)
        row1.addWidget(b_dl)
        mid.addLayout(row1)
        row2 = QHBoxLayout()
        b_sub = QPushButton("구독하기")
        b_sub.clicked.connect(self.subscribe)
        row2.addWidget(b_sub)
        row2.addStretch(1)
        mid.addLayout(row2)
        h.addLayout(mid, 1)
        self._mode_changed()
        return f

    def _tabs(self) -> QTabWidget:
        t = QTabWidget()
        self.tabs = t
        t.addTab(self._download_tab(), "다운로드")
        t.addTab(self._subs_tab(), "구독")
        t.addTab(self._settings_tab(), "설정")
        return t

    def _download_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 12, 16, 12)
        bar = QHBoxLayout()
        for text, fn in (("모두 일시정지", self.pause_all), ("모두 이어받기", self.resume_all), ("완료 항목 지우기", self.clear_done)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        b = QPushButton("저장 폴더 열기")
        b.clicked.connect(lambda: self._open_dir(Path(self.settings.save_dir)))
        bar.addWidget(b)
        v.addLayout(bar)

        self.stack = QStackedWidget()
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch(1)
        lg = QLabel()
        lg.setPixmap(make_logo(64))
        lg.setAlignment(Qt.AlignCenter)
        ev.addWidget(lg)
        t1 = QLabel("링크를 붙여넣어 시작하세요")
        t1.setAlignment(Qt.AlignCenter)
        t1.setFont(QFont("Malgun Gothic", 13, QFont.Bold))
        ev.addWidget(t1)
        t2 = QLabel("위의 입력창에 영상·재생목록·채널 링크를 붙여넣고 <b>형식</b>(영상 MP4 / MP3 음악)과 <b>화질</b>을 고른 뒤 "
                    "<b>다운로드</b>를 누르면 바로 받습니다.<br>재생목록·채널 링크는 전체를 재생목록 이름의 하위 폴더에 받습니다. "
                    "자막 등 세부 옵션은 <b>설정</b>에서 정할 수 있어요.")
        t2.setAlignment(Qt.AlignCenter)
        t2.setWordWrap(True)
        t2.setFixedWidth(620)
        t2.setMinimumHeight(70)
        ev.addWidget(t2, 0, Qt.AlignHCenter)
        ev.addStretch(2)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.stack.addWidget(empty)
        self.stack.addWidget(self.list)
        v.addWidget(self.stack, 1)
        return w

    def _subs_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 12, 16, 12)
        bar = QHBoxLayout()
        b1 = QPushButton("새 영상 확인")
        b1.clicked.connect(self.check_subs)
        b2 = QPushButton("선택 삭제")
        b2.clicked.connect(self.remove_sub)
        bar.addWidget(b1)
        bar.addWidget(b2)
        bar.addStretch(1)
        hint = QLabel("구독한 채널의 새 영상은 [새 영상 확인]을 누를 때 큐에 추가됩니다. 구독 시점 이전 영상은 받지 않습니다.")
        hint.setObjectName("muted")
        bar.addWidget(hint)
        v.addLayout(bar)
        self.sub_list = QListWidget()
        v.addWidget(self.sub_list, 1)
        self._refresh_subs()
        return w

    def _settings_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)
        st = self.settings

        r = QHBoxLayout()
        r.addWidget(QLabel("저장 폴더"))
        self.s_dir = QLineEdit(st.save_dir)
        r.addWidget(self.s_dir, 1)
        b = QPushButton("찾기")
        b.clicked.connect(self._choose_dir)
        r.addWidget(b)
        v.addLayout(r)

        self.s_sub = QCheckBox("자막 함께 받기 (자동 자막 포함)")
        self.s_sub.setChecked(st.subtitles)
        v.addWidget(self.s_sub)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("자막 언어 (쉼표로 구분)"))
        self.s_langs = QLineEdit(st.sub_langs)
        self.s_langs.setMaximumWidth(200)
        r2.addWidget(self.s_langs)
        r2.addStretch(1)
        v.addLayout(r2)

        self.s_pl = QCheckBox("재생목록·채널은 이름으로 하위 폴더 만들기")
        self.s_pl.setChecked(st.playlist_subfolder)
        v.addWidget(self.s_pl)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("동시 다운로드 수"))
        self.s_conc = QSpinBox()
        self.s_conc.setRange(1, 5)
        self.s_conc.setValue(st.concurrent)
        r3.addWidget(self.s_conc)
        r3.addStretch(1)
        v.addLayout(r3)

        ff = ffmpeg_dir()
        info = QLabel(f"ffmpeg: {'찾음 (' + ff + ')' if ff else '없음 — ffmpeg.exe를 프로그램 폴더에 넣어 주세요'}   ·   yt-dlp {yt_dlp.version.__version__}")
        info.setObjectName("muted")
        v.addWidget(info)

        b_save = QPushButton("설정 저장")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save_settings)
        v.addWidget(b_save, 0, Qt.AlignLeft)
        v.addStretch(1)
        return w

    def _footer(self) -> QFrame:
        f = QFrame()
        f.setObjectName("footer")
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 6, 16, 6)
        lab = QLabel("⚠ 저작권이 있는 콘텐츠는 권리자의 허락을 받았거나 개인적으로 이용할 수 있는 범위에서만 내려받으세요. "
                     "DRM(복제 방지) 보호 콘텐츠는 지원하지 않습니다.")
        lab.setObjectName("footer")
        h.addStretch(1)
        h.addWidget(lab)
        b = QPushButton("정보")
        b.setFlat(True)
        b.clicked.connect(lambda: QMessageBox.information(
            self, "정보", f"{APP_NAME}\n{APP_SUB}\n\nyt-dlp {yt_dlp.version.__version__} · PySide6 · ffmpeg\n강의 실습용 오픈소스 조합입니다."))
        h.addWidget(b)
        return f

    # ---------- 모드/설정
    def _mode_changed(self):
        sender = self.sender()
        if sender is self.btn_video:
            self.btn_audio.setChecked(False); self.btn_video.setChecked(True)
        elif sender is self.btn_audio:
            self.btn_video.setChecked(False); self.btn_audio.setChecked(True)
        audio = self.btn_audio.isChecked()
        self.fmt.clear()
        self.fmt.addItem("MP3 음악" if audio else "영상 MP4")
        self.q_label.setText("음질" if audio else "화질")
        self.quality.clear()
        self.quality.addItems(list(AUDIO_QUALITY if audio else VIDEO_QUALITY))

    def _choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "저장 폴더", self.s_dir.text())
        if d:
            self.s_dir.setText(d)

    def _save_settings(self):
        st = self.settings
        st.save_dir = self.s_dir.text().strip() or st.save_dir
        st.subtitles = self.s_sub.isChecked()
        st.sub_langs = self.s_langs.text().strip() or "ko,en"
        st.playlist_subfolder = self.s_pl.isChecked()
        st.concurrent = self.s_conc.value()
        st.save()
        self.statusBar().showMessage("설정을 저장했습니다.", 3000)

    def _open_dir(self, p: Path):
        p.mkdir(parents=True, exist_ok=True)
        open_folder(p)

    # ---------- 추가
    def _mode(self) -> str:
        return "audio" if self.btn_audio.isChecked() else "video"

    def _valid_url(self) -> str | None:
        u = self.url.text().strip()
        if not u:
            QMessageBox.warning(self, "링크 없음", "링크를 붙여넣어 주세요."); return None
        if not URL_RE.match(u):
            QMessageBox.warning(self, "링크 확인", "유튜브 링크 형식이 아닙니다."); return None
        return u

    def add_download(self):
        u = self._valid_url()
        if not u:
            return
        mode, q = self._mode(), self.quality.currentText()
        self.statusBar().showMessage("링크를 확인하는 중…")
        w = InfoWorker(u)
        w.result.connect(lambda items, pl: self._queue_items(items, pl, mode, q))
        w.failed.connect(lambda m: QMessageBox.critical(self, "링크 오류", m))
        self._keep(w)
        self.url.clear()

    def _queue_items(self, items: list, playlist: str, mode: str, q: str):
        for u, title in items:
            self._add_item(Item(url=u, mode=mode, quality=q, title=title, subfolder=playlist))
        self.statusBar().showMessage(f"{len(items)}개 항목을 큐에 추가했습니다.", 4000)
        self.tabs.setCurrentIndex(0)

    def _add_item(self, it: Item):
        self.items[it.id] = it
        self.order.append(it.id)
        lw = QListWidgetItem()
        widget = ItemWidget(it, self)
        lw.setSizeHint(widget.sizeHint())
        self.list.addItem(lw)
        self.list.setItemWidget(lw, widget)
        self.widgets[it.id] = widget
        lw.setData(Qt.UserRole, it.id)
        self.stack.setCurrentIndex(1)

    def _keep(self, w: QThread):
        self._info_workers.append(w)
        w.finished.connect(lambda: self._info_workers.remove(w) if w in self._info_workers else None)
        w.start()

    # ---------- 큐 실행
    def _pump(self):
        running = [i for i, it in self.items.items() if it.status == "진행"]
        slots = self.settings.concurrent - len(running)
        for iid in self.order:
            if slots <= 0:
                break
            it = self.items.get(iid)
            if it and it.status == "대기" and iid not in self.workers:
                self._start(it)
                slots -= 1

    def _start(self, it: Item):
        it.cancel.clear()
        it.status, it.detail = "진행", "시작 중…"
        w = DownloadWorker(it, self.settings)
        w.progress.connect(self._on_progress)
        w.finished_ok.connect(self._on_done)
        w.failed.connect(self._on_fail)
        w.cancelled.connect(self._on_cancel)
        w.title_found.connect(self._on_title)
        w.finished.connect(lambda: self.workers.pop(it.id, None))
        self.workers[it.id] = w
        w.start()
        self._refresh(it.id)

    def _refresh(self, iid: str):
        if iid in self.widgets:
            self.widgets[iid].refresh()

    def _on_progress(self, iid, pct, text):
        it = self.items.get(iid)
        if it and it.status == "진행":
            it.progress, it.detail = pct, text
            self._refresh(iid)

    def _on_title(self, iid, title):
        if iid in self.items:
            self.items[iid].title = title
            self._refresh(iid)

    def _on_done(self, iid, fp):
        it = self.items[iid]
        it.status, it.progress, it.filepath = "완료", 100, fp
        it.detail = fp or "완료"
        self._refresh(iid)

    def _on_fail(self, iid, msg):
        it = self.items[iid]
        it.status, it.detail = "오류", msg.splitlines()[0][:160]
        self._refresh(iid)

    def _on_cancel(self, iid):
        it = self.items[iid]
        if it.status == "진행":
            it.status = "일시정지"
        it.detail = "일시정지됨 — 이어받기를 누르면 받던 곳부터 계속합니다." if it.status == "일시정지" else "취소됨"
        self._refresh(iid)

    # ---------- 항목 제어
    def pause(self, iid):
        it = self.items[iid]
        if it.status == "진행":
            it.cancel.set()
        elif it.status == "대기":
            it.status, it.detail = "일시정지", "일시정지됨"
            self._refresh(iid)

    def resume(self, iid):
        it = self.items[iid]
        if it.status in ("일시정지", "오류", "취소"):
            it.status, it.progress, it.detail = "대기", 0, "대기 중"
            self._refresh(iid)

    def cancel(self, iid):
        it = self.items[iid]
        if it.status == "진행":
            it.status = "취소"
            it.cancel.set()
        elif it.status != "완료":
            it.status = "취소"
        it.detail = "취소됨"
        self._refresh(iid)

    def pause_all(self):
        for iid in list(self.items):
            self.pause(iid)

    def resume_all(self):
        for iid in list(self.items):
            self.resume(iid)

    def clear_done(self):
        for row in range(self.list.count() - 1, -1, -1):
            iid = self.list.item(row).data(Qt.UserRole)
            it = self.items.get(iid)
            if it and it.status in ("완료", "취소"):
                self.list.takeItem(row)
                self.items.pop(iid, None); self.widgets.pop(iid, None)
                if iid in self.order:
                    self.order.remove(iid)
        if not self.items:
            self.stack.setCurrentIndex(0)

    def open_item_folder(self, iid):
        it = self.items[iid]
        p = Path(it.filepath).parent if it.filepath else Path(self.settings.save_dir)
        self._open_dir(p)

    # ---------- 구독
    def _refresh_subs(self):
        self.sub_list.clear()
        for s in self.subs:
            self.sub_list.addItem(f"{s['name']}   —   {s['url']}   (확인한 영상 {len(s.get('seen', []))}개)")

    def subscribe(self):
        u = self._valid_url()
        if not u:
            return
        self.statusBar().showMessage("채널 정보를 확인하는 중…")
        w = InfoWorker(u, limit=30)
        w.channel.connect(self._on_channel)
        w.failed.connect(lambda m: QMessageBox.critical(self, "구독 오류", m))
        self._keep(w)

    def _on_channel(self, ch: dict):
        if not ch.get("url"):
            QMessageBox.warning(self, "구독", "채널 정보를 찾지 못했습니다."); return
        if any(s["url"] == ch["url"] for s in self.subs):
            QMessageBox.information(self, "구독", "이미 구독 중인 채널입니다."); return
        if not ch.get("seen"):
            # 영상 하나로 구독한 경우: 채널 목록을 한 번 더 읽어 현재 영상들을 '본 것'으로 기록
            w = InfoWorker(ch["url"], limit=30)
            w.channel.connect(self._on_channel)
            self._keep(w)
            return
        self.subs.append({"name": ch["name"], "url": ch["url"], "seen": ch["seen"]})
        save_json(SUBS_FILE, self.subs)
        self._refresh_subs()
        self.statusBar().showMessage(f"'{ch['name']}' 채널을 구독했습니다. 이후 올라오는 새 영상만 받습니다.", 5000)
        self.tabs.setCurrentIndex(1)

    def remove_sub(self):
        row = self.sub_list.currentRow()
        if row < 0:
            return
        self.subs.pop(row)
        save_json(SUBS_FILE, self.subs)
        self._refresh_subs()

    def check_subs(self):
        if not self.subs:
            QMessageBox.information(self, "구독", "구독한 채널이 없습니다."); return
        mode, q = self._mode(), self.quality.currentText()
        self.statusBar().showMessage("새 영상을 확인하는 중…")
        for s in self.subs:
            w = InfoWorker(s["url"], limit=30)
            w.result.connect(lambda items, pl, s=s: self._on_sub_result(s, items, mode, q))
            self._keep(w)

    def _on_sub_result(self, s: dict, items: list, mode: str, q: str):
        seen = set(s.get("seen", []))
        new = []
        for u, title in items:
            vid = re.search(r"(?:v=|/shorts/|youtu\.be/)([\w-]{11})", u)
            key = vid.group(1) if vid else u
            if key not in seen:
                new.append((u, title)); seen.add(key)
        s["seen"] = list(seen)
        save_json(SUBS_FILE, self.subs)
        if new:
            self._queue_items(new, s["name"], mode, q)
        self._refresh_subs()
        self.statusBar().showMessage(f"{s['name']}: 새 영상 {len(new)}개", 4000)

    def closeEvent(self, e):
        for it in self.items.values():
            it.cancel.set()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
