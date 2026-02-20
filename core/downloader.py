import os
import re
import shutil
import signal
import select
import json
import hashlib
import threading
import urllib.request
import urllib.error
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .utils import parse_progress

_SPEED_RE = re.compile(r"\bat\s+([0-9]+(?:[\.,][0-9]+)?)\s*([KMGTP]?i?B)/s\b", re.IGNORECASE)
_ETA_RE = re.compile(r"\bETA\s+([0-9:]+|Unknown)\b", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PL_ITEM_RE = re.compile(r"Downloading\s+(?:item|video)\s+(\d+)\s*(?:of\s+|/)\s*(\d+)", re.IGNORECASE)


def _speed_to_mbps(value: float, unit: str) -> float:
    # unit: B, KB, KiB, MB, MiB, ...
    u = unit.strip().lower()
    base = 1024 if "i" in u else 1000
    prefix = u[0] if u and u[0] in "kmgpt" else ""
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}[prefix]
    bytes_per_sec = value * (base ** power)
    return (bytes_per_sec * 8.0) / 1_000_000.0  # Mb/sn (decimal)

def _parse_speed_eta(line: str) -> tuple[Optional[float], Optional[str]]:
    speed_mbps: Optional[float] = None
    eta: Optional[str] = None

    m = _SPEED_RE.search(line)
    if m:
        num = m.group(1).replace(",", ".")
        try:
            speed_mbps = _speed_to_mbps(float(num), m.group(2))
        except Exception:
            speed_mbps = None

    m = _ETA_RE.search(line)
    if m:
        eta = m.group(1)

    return speed_mbps, eta


def _cleanup_cancel_artifacts(out_dir: Path, started_ts: float, *, recursive: bool = False) -> None:
    """
    İptal edilen işten kalan geçici/artık dosyaları temizle.
    Yalnızca bu iş başladıktan sonra (mtime) güncellenen dosyaları hedefler.
    """
    patterns = [
        "*.part", "*.part.*", "*.part-*", "*.part*",
        "*.ytdl", "*.aria2",
        "*.__cover_tmp__.m4a",
    ]

    for pat in patterns:
        it = out_dir.rglob(pat) if recursive else out_dir.glob(pat)
        for p in it:
            try:
                if not p.is_file():
                    continue
                if p.stat().st_mtime < (started_ts - 2):
                    continue
                p.unlink(missing_ok=True)
            except Exception:
                pass

    # yt-dlp --write-thumbnail artefaktları (kapak dosyaları) – iptal olunca kalmasın.
    # Güvenlik: yalnızca bu iş başladıktan sonra güncellenmiş ve adında [..] olan thumbnail'ları sil.
    thumb_exts = {".jpg", ".jpeg", ".png", ".webp"}
    try:
        it2 = out_dir.rglob('*') if recursive else out_dir.iterdir()
        for p in it2:
            if not p.is_file():
                continue
            if p.suffix.lower() not in thumb_exts:
                continue
            name = p.name
            if "[" not in name or "]" not in name:
                continue
            if p.stat().st_mtime < (started_ts - 2):
                continue
            p.unlink(missing_ok=True)
    except Exception:
        pass





# Hedefler:
# - Opus ses: en iyi Opus stream -> ffmpeg ile .opus konteynerine remux (re-encode yok).
# - M4A ses: en iyi M4A stream -> olduğu gibi indir (re-encode yok).
# - Video+ses: bestvideo + en iyi Opus audio -> MKV içinde birleştir (ffmpeg gerekli).
# - Sadece video (MKV/MP4): 1080p (<=1080) video-only. MKV için remux; MP4 için mp4 video-only stream şart.

FORMAT_OPTIONS = {
    # UI'da göstermeyeceğiz; içeride kalsa da politika dışına çıkmasın
    "video_best": {
        "name": "Video + Ses (Maksimum)",
        "kind": "video_av",
        "format": "bestvideo[acodec=none][vcodec~='^vp0?9']+bestaudio[vcodec=none][acodec=opus]",
        "merge_output_format": "mkv",
    },

    # 4K/2K/1080p = VP9 video + en iyi Opus ses, çıktı MKV (merge/remux, re-encode yok)
    "video_2160p": {
        "name": "Video + Ses (MKV)",
        "kind": "video_av",
        "format": "bestvideo[acodec=none][vcodec~='^vp0?9'][height=2160]+bestaudio[vcodec=none][acodec=opus]",
        "merge_output_format": "mkv",
        "cap_p": 2160,
    },
    "video_1440p": {
        "name": "Video + Ses (MKV)",
        "kind": "video_av",
        "format": "bestvideo[acodec=none][vcodec~='^vp0?9'][height=1440]+bestaudio[vcodec=none][acodec=opus]",
        "merge_output_format": "mkv",
        "cap_p": 1440,
    },
    "video_1080p": {
        "name": "Video + Ses (MKV)",
        "kind": "video_av",
        "format": "bestvideo[acodec=none][vcodec~='^vp0?9'][height=1080]+bestaudio[vcodec=none][acodec=opus]",
        "merge_output_format": "mkv",
        "cap_p": 1080,
    },

    # Ses: en yüksek bitrate akış (bestaudio seçer)
    "audio_opus": {
        "name": "Ses (En İyi)",
        "kind": "audio_opus",
        "format": "bestaudio[vcodec=none][acodec=opus]",
    },
    "audio_m4a": {
        "name": "Ses (En İyi)",
        "kind": "audio_m4a",
        "format": "bestaudio[vcodec=none][ext=m4a]",
    },

    # Sadece video: 1080p
    "video_only_mkv_1080": {
        "name": "Sadece Video (1080p)",
        "kind": "video_only_remux",
        "format": "bestvideo[acodec=none][vcodec~='^vp0?9'][height=1080]",
        "remux_to": "mkv",
    },
    "video_only_mp4_1080": {
        "name": "Sadece Video (1080p)",
        "kind": "video_only_mp4",
        "format": "bestvideo[acodec=none][ext=mp4][vcodec^=avc1][height=1080]",
    },
}



def _is_network_error_line(s: str) -> bool:
    s = (s or "").lower()
    # yt-dlp / urllib / DNS / TCP tipik ağ hataları
    needles = [
        "name or service not known",
        "temporary failure in name resolution",
        "connection timed out",
        "timed out",
        "network is unreachable",
        "no route to host",
        "connection reset",
        "connection aborted",
        "connection refused",
        "failed to establish a new connection",
        "getaddrinfo failed",
        "unable to download webpage",
        "http error",
        "httpsconnectionpool",
        "proxy error",
        "remote end closed connection",
        "errno -2",
        "errno -3",
        "errno 101",
        "errno 104",
        "errno 110",
        "errno 111",
        "errno 113",
        "[download] got error",
        "giving up after",
    ]
    return any(n in s for n in needles)


def _cleanup_on_network_failure(code: int, last_line: str, out_dir: Path, started_ts: float, *, recursive: bool = False) -> None:
    # Kullanıcı iptali (130) zaten ayrı ele alınıyor; burada ağ kopmasıyla patlayan yarım dosyaları temizliyoruz.
    if code and code != 130 and _is_network_error_line(last_line or ""):
        _cleanup_cancel_artifacts(out_dir, started_ts, recursive=recursive)


# ---------------------------
# yt-dlp runtime self-update
# ---------------------------
# Flatpak içinde /app salt-okunur olduğu için güncellemeyi kullanıcı veri dizinine indirip
# PATH'in başına ekleyerek hem core/formats.py hem de downloader aynı yt-dlp'yi kullanır.

def get_tools_dir() -> str:
    """Kullanıcı veri alanında (Flatpak'te ~/.var/app/.../data) araç dizinini döndürür."""
    data_home = os.environ.get("XDG_DATA_HOME")
    if not data_home:
        data_home = os.path.join(os.path.expanduser("~"), ".local", "share")
    d = os.path.join(data_home, "youtube-downloader", "tools")
    os.makedirs(d, exist_ok=True)
    return d


def get_local_ytdlp_path() -> str:
    return os.path.join(get_tools_dir(), "yt-dlp")


def prepend_tools_dir_to_path() -> str:
    """Tools dizinini PATH'in başına ekler (varsa tekrar eklemez)."""
    tools = get_tools_dir()
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(":") if p]
    if not parts or parts[0] != tools:
        if tools not in parts:
            os.environ["PATH"] = tools + (":" + path if path else "")
    return tools


_YTDLP_UPDATE_STATE = os.path.join(get_tools_dir(), "ytdlp_update_state.json")
_YTDLP_UPDATE_LOCK = threading.Lock()  # thread-safe update


def _read_update_state() -> dict:
    try:
        with open(_YTDLP_UPDATE_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_update_state(state: dict) -> None:
    try:
        tmp = _YTDLP_UPDATE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _YTDLP_UPDATE_STATE)
    except Exception:
        pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _github_latest_tag(timeout: float = 8.0) -> str:
    """GitHub 'latest' redirect'inden tag adını çözer (API rate limit yok)."""
    req = urllib.request.Request(
        "https://github.com/yt-dlp/yt-dlp/releases/latest",
        headers={"User-Agent": "youtube-downloader"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()  # .../tag/<TAG>
    if "/tag/" in final:
        return final.rsplit("/", 1)[-1]
    raise RuntimeError("yt-dlp latest tag çözümlenemedi.")


def _download_url(url: str, dest: str, timeout: float = 20.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "youtube-downloader"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)


def ensure_yt_dlp_updated(force: bool = False, min_check_interval_sec: int = 24 * 3600) -> dict:
    """
    yt-dlp'nin kullanıcı alanındaki kopyasını güncel tutar.
    - force=False iken günlük (min_check_interval_sec) kontrol eder.
    Dönüş: {updated: bool, old: str|None, new: str|None, path: str|None}
    """
    now = time.time()
    state = _read_update_state()
    last_check = float(state.get("last_check", 0.0) or 0.0)
    if (not force) and (now - last_check) < float(min_check_interval_sec):
        return {"updated": False, "old": None, "new": None, "path": None}

    # Aynı anda iki update çalışmasın
    if not _YTDLP_UPDATE_LOCK.acquire(blocking=False):
        return {"updated": False, "old": None, "new": None, "path": None}

    try:
        state["last_check"] = now
        _write_update_state(state)

        try:
            latest = _github_latest_tag()
        except Exception:
            return {"updated": False, "old": None, "new": None, "path": None}

        local_path = get_local_ytdlp_path()
        # Yüklü sürüm (local varsa local; yoksa PATH)
        try:
            current_cmd = local_path if (os.path.isfile(local_path) and os.access(local_path, os.X_OK)) else shutil.which("yt-dlp")
            old_ver = None
            if current_cmd:
                p = subprocess.run([current_cmd, "--version"], capture_output=True, text=True, check=False)
                old_ver = (p.stdout or p.stderr or "").strip().splitlines()[0] if (p.stdout or p.stderr) else None
        except Exception:
            old_ver = None

        if old_ver == latest:
            state["latest"] = latest
            state["last_ok"] = now
            _write_update_state(state)
            return {"updated": False, "old": old_ver, "new": latest, "path": None}

        # İndir + doğrula
        tools = get_tools_dir()
        tmp_bin = os.path.join(tools, "yt-dlp.tmp")
        sums = os.path.join(tools, "SHA2-256SUMS.tmp")

        bin_url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{latest}/yt-dlp"
        sums_url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{latest}/SHA2-256SUMS"

        _download_url(sums_url, sums, timeout=20.0)
        _download_url(bin_url, tmp_bin, timeout=60.0)

        expected = None
        try:
            with open(sums, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # format: <hash>  <filename>
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1] == "yt-dlp":
                        expected = parts[0].lower()
                        break
        except Exception:
            expected = None

        if not expected:
            # Güvenlik: hash bulunamadıysa güncelleme yapma.
            try:
                os.remove(tmp_bin)
            except Exception:
                pass
            return {"updated": False, "old": old_ver, "new": None, "path": None}

        got = _sha256_file(tmp_bin).lower()
        if got != expected:
            try:
                os.remove(tmp_bin)
            except Exception:
                pass
            return {"updated": False, "old": old_ver, "new": None, "path": None}

        os.chmod(tmp_bin, 0o755)
        os.replace(tmp_bin, local_path)

        state["latest"] = latest
        state["last_ok"] = now
        _write_update_state(state)

        return {"updated": True, "old": old_ver, "new": latest, "path": local_path}
    finally:
        try:
            _YTDLP_UPDATE_LOCK.release()
        except Exception:
            pass


def _find_ytdlp() -> str:
    p = shutil.which("yt-dlp")
    if p:
        return p

    project_root = Path(__file__).resolve().parents[1]
    venv_ytdlp = project_root / "venv" / "bin" / "yt-dlp"
    if venv_ytdlp.exists():
        return str(venv_ytdlp)

    raise RuntimeError("yt-dlp bulunamadı (PATH veya venv/bin/yt-dlp).")


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg bulunamadı. Bu seçenek için ffmpeg gerekli.")



def _run_cancelable_process(
    cmd: list[str],
    *,
    cancel_event=None,
    timeout_s: float | None = None,
) -> tuple[int, str]:
    """Run a subprocess with periodic cancellation checks.
    Returns (returncode, stderr_text). If cancelled, returns (130, 'cancelled').
    """
    def cancel_requested() -> bool:
        return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    try:
        while True:
            if cancel_requested():
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                return (130, "cancelled")

            try:
                out, err = proc.communicate(timeout=0.2)
                rc = proc.returncode if proc.returncode is not None else 0
                return (rc, (err or "").strip())
            except subprocess.TimeoutExpired:
                # keep looping
                continue
    finally:
        if timeout_s is not None:
            # best-effort global timeout (rarely used)
            pass


def _extract_video_id_from_name(name: str) -> Optional[str]:
    # Dosya adında [id] varsa yakala (restrict-filenames ile stabil)
    m = re.search(r"\[([A-Za-z0-9_-]{6,})\]", name)
    return m.group(1) if m else None


def _list_cover_images(downloaded_media_path: str) -> list[str]:
    """yt-dlp'nin yazdığı thumbnail dosyalarını topla (write-all-thumbnails destekler)."""
    p = Path(downloaded_media_path)
    d = p.parent
    exts = {".jpg", ".jpeg", ".png", ".webp"}

    vid = _extract_video_id_from_name(p.name)
    stem = p.stem

    candidates: list[str] = []

    try:
        for f in d.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            n = f.name
            if vid and f"[{vid}]" in n:
                candidates.append(str(f))
                continue
            # Fallback: aynı stem (örn. foo [id].jpg veya foo [id].1.jpg)
            if n.startswith(stem) or n.startswith(stem + "."):
                candidates.append(str(f))
    except Exception:
        pass

    # Deterministik sıralama
    candidates = sorted(set(candidates))
    return candidates


def _ffprobe_image_area(path: str) -> Optional[int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        out = (proc.stdout or "").strip()
        if "x" not in out:
            return None
        w_s, h_s = out.split("x", 1)
        w = int(w_s.strip())
        h = int(h_s.strip())
        if w > 0 and h > 0:
            return w * h
    except Exception:
        return None
    return None


def _pick_best_cover_image(candidates: list[str]) -> Optional[str]:
    """En yüksek çözünürlüklü kapağı seçmeye çalış; olmazsa dosya boyutuna göre seç."""
    if not candidates:
        return None

    best = None
    best_metric = -1

    for c in candidates:
        metric = _ffprobe_image_area(c)
        if metric is None:
            try:
                metric = int(os.path.getsize(c))
            except Exception:
                metric = 0
        # tie-break: path name
        if metric > best_metric or (metric == best_metric and best and c < best):
            best_metric = metric
            best = c

    return best


def _find_cover_image(downloaded_media_path: str) -> Optional[str]:
    """En iyi (maksimum çözünürlüklü) thumbnail dosyasını bul."""
    candidates = _list_cover_images(downloaded_media_path)
    return _pick_best_cover_image(candidates)



def _cleanup_any_images_in_dir(dir_path: str, *, recursive: bool = False) -> None:
    """Belirtilen klasördeki tüm thumbnail/kapak görsellerini sil (playlist klasörü için)."""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    root = Path(dir_path)
    try:
        it = root.rglob("*") if recursive else root.iterdir()
        for f in it:
            try:
                if f.is_file() and f.suffix.lower() in exts:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass

def _cleanup_cover_images(downloaded_media_path: str) -> None:
    """Bir medya dosyasına ait tüm thumbnail dosyalarını sil (başarılı postprocess sonrası)."""
    for c in _list_cover_images(downloaded_media_path):
        try:
            Path(c).unlink(missing_ok=True)
        except Exception:
            pass


def _ffmpeg_attach_cover_to_m4a(input_m4a: str, cover_img: str, *, cancel_event=None) -> None:
    """Attach cover art to an .m4a without re-encoding audio (remux only)."""
    _require_ffmpeg()
    src = Path(input_m4a)
    tmp = src.with_name(src.stem + ".__cover_tmp__.m4a")

    cmd = [
        "ffmpeg",
        "-v", "error",
        "-y",
        "-i", str(src),
        "-i", str(cover_img),
        "-map", "0",
        "-map", "1",
        "-c", "copy",
        "-disposition:v:0", "attached_pic",
        str(tmp),
    ]
    rc, err = _run_cancelable_process(cmd, cancel_event=cancel_event)
    if rc == 130:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("İptal edildi")
    if rc != 0:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(err.splitlines()[-1] if err else "ffmpeg kapak ekleme hatası")

    # Replace original atomically
    tmp.replace(src)


def _try_set_cover_opus(opus_path: str, cover_img: str, *, cancel_event=None) -> None:
    """Best-effort: embed cover into .opus using opustags if available."""
    opustags = shutil.which("opustags")
    if not opustags:
        return

    cmd = [opustags, "--in-place", "--set-cover", cover_img, opus_path]
    rc, err = _run_cancelable_process(cmd, cancel_event=cancel_event)
    if rc == 130:
        raise RuntimeError("İptal edildi")
    # opustags returns non-zero on failure; we treat as non-fatal but surface message
    if rc != 0:
        raise RuntimeError(err.splitlines()[-1] if err else "opustags kapak ekleme hatası")


def _run_ytdlp(
    cmd: list[str],
    *,
    progress_cb: Callable[[float, Optional[float], Optional[str]], None],
    status_cb: Callable[[str], None],
    cancel_event=None,
) -> tuple[int, list[str], str]:
    """
    Returns: (returncode, printed_filepaths, last_line)
    printed_filepaths: yt-dlp --print after_move:filepath ile yazdırılan dosya yolları (varsa).
    """

    def cancel_requested() -> bool:
        return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()

    # Sen zaten bunu uygulamışsın: text=False (bytes okuyacağız)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        bufsize=0,
        start_new_session=True,
    )

    printed_paths: list[str] = []
    last_line: str = ""
    pl_item_seen: Optional[tuple[int, int]] = None


    def kill_group(sig_to_send: int):
        try:
            os.killpg(process.pid, sig_to_send)
        except Exception:
            try:
                process.send_signal(sig_to_send)
            except Exception:
                pass

    def handle_line(line_str: str):
        nonlocal printed_paths, last_line
        nonlocal pl_item_seen


        # ANSI renk kodlarını temizle (deterministik regex/parse için)
        plain = _ANSI_RE.sub('', line_str)

        s = plain.strip()
        if s:
            last_line = s


        # Playlist içinde kaçıncı öğe indiriliyor? (UI'da 2/5 gösterebilmek için)
        m_item = _PL_ITEM_RE.search(plain)
        if m_item:
            try:
                raw_i = int(m_item.group(1))
                raw_n = int(m_item.group(2))
            except Exception:
                raw_i, raw_n = 0, 0
            cur = (raw_i, raw_n)
            if cur != pl_item_seen:
                pl_item_seen = cur
                try:
                    status_cb(f"__PL_ITEM__:{raw_i}:{raw_n}")
                except Exception:
                    pass

        p = parse_progress(plain)
        speed_mbps, eta = _parse_speed_eta(plain)
        if p is not None:
            progress_cb(p, speed_mbps, eta)# after_move:filepath çoğunlukla tek satırda yol verir; dosya gerçekten varsa yakala.
        try:
            cand = Path(s)
            if cand.is_absolute() and cand.exists():
                sp = str(cand)
                if sp not in printed_paths:
                    printed_paths.append(sp)
        except Exception:
            pass

    try:
        if process.stdout is None:
            process.wait()
        else:
            buf = b""

            while True:
                if cancel_requested():
                    status_cb("İptal ediliyor...")
                    kill_group(signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except Exception:
                        kill_group(signal.SIGKILL)
                    status_cb("İptal edildi")
                    return (130, printed_paths, last_line)

                # veri bekle
                r, _, _ = select.select([process.stdout], [], [], 0.2)

                if r:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        # EOF
                        if process.poll() is not None:
                            break
                        continue

                    # yt-dlp progress çoğu zaman '\r' ile "aynı satırı" günceller; satır sonu gibi ele al
                    chunk = chunk.replace(b"\r", b"\n")
                    buf += chunk

                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        if not raw_line:
                            continue
                        line_str = raw_line.decode("utf-8", errors="replace")
                        handle_line(line_str)

                else:
                    # timeout oldu: süreç bittiyse buffer'ı da flush et
                    if process.poll() is not None:
                        if buf.strip():
                            line_str = buf.decode("utf-8", errors="replace")
                            handle_line(line_str)
                        break

    finally:
        try:
            if process.poll() is None:
                kill_group(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    kill_group(signal.SIGKILL)
                except Exception:
                    pass
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass

    return (process.returncode or 0, printed_paths, last_line)


def _ffmpeg_remux_audio_to_opus(src_path: str, dst_path: str, *, cancel_event=None) -> None:
    """
    Opus stream -> .opus konteynerine remux (codec copy, re-encode yok).
    İptal destekli.
    """
    _require_ffmpeg()
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src_path, "-vn", "-map_metadata", "0", "-c:a", "copy", dst_path]
    rc, err = _run_cancelable_process(cmd, cancel_event=cancel_event)
    if rc == 130:
        try:
            Path(dst_path).unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("İptal edildi")
    if rc != 0:
        try:
            Path(dst_path).unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(err.splitlines()[-1] if err else "ffmpeg remux hatası")


def download_video(
    url: str,
    output_dir: str,
    format_key: str,
    progress_cb: Callable[[float, Optional[float], Optional[str]], None],
    status_cb: Callable[[str], None],
    cancel_event=None,
    format_override: Optional[str] = None,
    playlist: bool = False,
    playlist_items: Optional[str] = None,
):
    ytdlp = _find_ytdlp()
    opt = FORMAT_OPTIONS.get(format_key)
    if not opt:
        raise RuntimeError(f"Bilinmeyen format_key: {format_key}")

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    job_started_ts = time.time()

    kind = opt["kind"]
    fmt = format_override or opt["format"]

    # ffmpeg gerektiren durumlar
    if kind in ("video_av", "video_only_remux", "audio_opus"):
        _require_ffmpeg()

    out_tmpl = "%(title).200B [%(id)s].%(ext)s"
    if playlist:
        out_tmpl = "%(playlist)s/%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"

    base_cmd = [
        ytdlp,
        "--newline",
        "--progress",
        "--retries", "1000000",
        "--fragment-retries", "1000000",
        "--extractor-retries", "5",
        "--retry-sleep", "5",
        "--socket-timeout", "10",
        # Playlist kontrolü: varsayılan tek video
        ("--yes-playlist" if playlist else "--no-playlist"),
        "--restrict-filenames",
        "-P", str(out_dir),
        "-o", out_tmpl,
        # Temel metadata (title/artist/album) göm.
        # --embed-metadata, --add-metadata ile eşdeğer bir alias'tır.
        "--parse-metadata", "%(title|)s:%(meta_title)s",
        "--parse-metadata", "%(artist,creator,uploader,channel|)s:%(meta_artist)s",
        "--parse-metadata", "%(album,playlist_title,channel,uploader|)s:%(meta_album)s",
        "--embed-metadata",
        "--no-embed-chapters",
        "--no-embed-info-json",
        "--print", "after_move:filepath",
        "-f", fmt,
    ]

    if playlist and playlist_items:
        base_cmd += ["--playlist-items", str(playlist_items)]

    # Video + Ses (mutlaka Opus)
    if kind == "video_av":
        status_cb(opt["name"])
        cmd = base_cmd + ["--merge-output-format", opt["merge_output_format"], url]
        code, paths, last_line = _run_ytdlp(cmd, progress_cb=progress_cb, status_cb=status_cb, cancel_event=cancel_event)
        filepath = paths[-1] if paths else None
        if code == 130:
            _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
            return
        if code != 0:
            _cleanup_on_network_failure(code, last_line, out_dir, job_started_ts, recursive=playlist)
            raise RuntimeError(last_line or "İndirme hatası")
        progress_cb(1.0)
        status_cb("İndirme tamamlandı")
        if playlist and paths:
            try:
                return str(Path(paths[0]).parent)
            except Exception:
                return str(out_dir)
        return filepath

    # Ses (M4A)
    # Ses (M4A) — sadece gerçek M4A
    if kind == "audio_m4a":
        status_cb(opt["name"])
        cmd = base_cmd + ["--write-all-thumbnails", "--convert-thumbnails", "jpg", url]
        code, paths, last_line = _run_ytdlp(cmd, progress_cb=progress_cb, status_cb=status_cb, cancel_event=cancel_event)

        cancelled = (code == 130)
        if cancelled:
            # Kullanıcı iptal etmiş olsa bile, tamamlanmış öğeleri (varsa) post-process ederek
            # seçilen formatın (.m4a + kapak) deterministik kalmasını sağlarız.
            status_cb("İptal edildi (tamamlanan öğeler işleniyor…)")

        if (not cancelled) and code != 0:
            _cleanup_on_network_failure(code, last_line, out_dir, job_started_ts, recursive=playlist)
            raise RuntimeError(last_line or "İndirme hatası")

        if not paths:
            if cancelled:
                _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                status_cb("İptal edildi")
                return
            raise RuntimeError("İndirme tamamlandı ama dosya yolu alınamadı.")

        # Güvenlik: beklenen çıktı .m4a değilse (normalde --print after_move:filepath bunu sağlamalı)
        # iptal modunda yalnızca geçerli .m4a dosyalarını işleyip devam edelim.
        bad = [p for p in paths if Path(p).suffix.lower() != ".m4a"]
        if bad:
            if cancelled:
                paths = [p for p in paths if Path(p).suffix.lower() == ".m4a"]
                if not paths:
                    _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                    status_cb("İptal edildi")
                    return
            else:
                raise RuntimeError("Bu içerik için M4A audio bulunamadı.")

        pp_cancel_event = None if cancelled else cancel_event

        # Kapak (thumbnail) varsa M4A içine göm (remux; re-encode yok).
        for fp in paths:
            if (not cancelled) and pp_cancel_event is not None and getattr(pp_cancel_event, "is_set", lambda: False)():
                _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                return

            cover = _find_cover_image(fp)
            try:
                if cover:
                    status_cb("Kapak ekleniyor…")
                    _ffmpeg_attach_cover_to_m4a(fp, cover, cancel_event=pp_cancel_event)
            except Exception:
                if (not cancelled) and pp_cancel_event is not None and getattr(pp_cancel_event, "is_set", lambda: False)():
                    _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                    return
                raise
            finally:
                # Kullanıcı isteği: çıktı klasöründe thumbnail (jpg/webp/png) kalmasın.
                try:
                    _cleanup_cover_images(fp)
                except Exception:
                    pass

        # Playlist modunda: klasörde thumbnail dosyası kalmasın (tüm jpg/webp/png temizle)
        if playlist:
            try:
                dirs = sorted({str(Path(p).parent) for p in (paths or []) if p})
                if not dirs and out_dir:
                    dirs = [str(Path(out_dir))]
                for d in dirs:
                    _cleanup_any_images_in_dir(d, recursive=False)
            except Exception:
                pass

        if cancelled:
            _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
            status_cb("İptal edildi")
            if playlist and paths:
                try:
                    return str(Path(paths[0]).parent)
                except Exception:
                    return str(out_dir)
            return paths[-1]

        progress_cb(1.0)
        status_cb("İndirme tamamlandı")
        if playlist and paths:
            try:
                return str(Path(paths[0]).parent)
            except Exception:
                return str(out_dir)
        return paths[-1]

    # Ses (Opus)# Ses (Opus) — çıktı .opus olacak
    if kind == "audio_opus":
        status_cb(opt["name"])
        cmd = base_cmd + ["--write-all-thumbnails", "--convert-thumbnails", "jpg", url]
        code, paths, last_line = _run_ytdlp(cmd, progress_cb=progress_cb, status_cb=status_cb, cancel_event=cancel_event)

        cancelled = (code == 130)
        if cancelled:
            # Playlist içinde iptal edildiğinde, yt-dlp tamamlanmış ilk öğeleri .webm olarak bırakabilir.
            # Tamamlananları remux+kapak ile finalize ederek kullanıcı seçimi (.opus) ile uyumlu tutarız.
            status_cb("İptal edildi (tamamlanan öğeler işleniyor…)")

        if (not cancelled) and code != 0:
            _cleanup_on_network_failure(code, last_line, out_dir, job_started_ts, recursive=playlist)
            raise RuntimeError(last_line or "İndirme hatası")

        if not paths:
            if cancelled:
                _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                status_cb("İptal edildi")
                return
            raise RuntimeError("Opus indirildi ama dosya yolu alınamadı.")

        last_dst: Optional[str] = None
        pp_cancel_event = None if cancelled else cancel_event

        for fp in paths:
            if (not cancelled) and pp_cancel_event is not None and getattr(pp_cancel_event, "is_set", lambda: False)():
                _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                return

            src = Path(fp)
            if not src.exists():
                continue

            # Eğer yt-dlp doğrudan .opus verdiyse remux gerekmeyebilir; yine de cover embed yapılabilir.
            if src.suffix.lower() == ".opus":
                dst = src
            else:
                dst = src.with_suffix(".opus")
                try:
                    _ffmpeg_remux_audio_to_opus(str(src), str(dst), cancel_event=pp_cancel_event)
                except Exception:
                    if (not cancelled) and pp_cancel_event is not None and getattr(pp_cancel_event, "is_set", lambda: False)():
                        _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                        return
                    raise

            # Kapak (thumbnail) varsa .opus içine göm (opustags ile; re-encode yok)
            cover = _find_cover_image(str(dst))
            try:
                if cover:
                    status_cb("Kapak ekleniyor…")
                    _try_set_cover_opus(str(dst), cover, cancel_event=pp_cancel_event)
            except Exception:
                # Kapak ekleme hatasında: iptal değilse sessiz geç (indirimi bozmasın).
                if (not cancelled) and pp_cancel_event is not None and getattr(pp_cancel_event, "is_set", lambda: False)():
                    _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
                    return
            finally:
                # Kullanıcı isteği: çıktı klasöründe thumbnail (jpg/webp/png) kalmasın.
                try:
                    _cleanup_cover_images(str(dst))
                except Exception:
                    pass

            # Kaynak .webm'i temizle (dst zaten aynı dosyaysa dokunma)
            if src != dst:
                try:
                    src.unlink(missing_ok=True)
                except Exception:
                    pass

            last_dst = str(dst)

        # Playlist modunda: klasörde thumbnail dosyası kalmasın (tüm jpg/webp/png temizle)
        if playlist:
            try:
                dirs = sorted({str(Path(p).parent) for p in (paths or []) if p})
                if not dirs and out_dir:
                    dirs = [str(Path(out_dir))]
                for d in dirs:
                    _cleanup_any_images_in_dir(d, recursive=False)
            except Exception:
                pass

        if cancelled:
            _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
            status_cb("İptal edildi")
            if playlist and paths:
                try:
                    return str(Path(paths[0]).parent)
                except Exception:
                    return str(out_dir)
            return last_dst or str(out_dir)

        progress_cb(1.0)
        status_cb("İndirme tamamlandı")
        if playlist and paths:
            try:
                return str(Path(paths[0]).parent)
            except Exception:
                return str(out_dir)
        return last_dst or paths[-1]

    if kind == "video_only_remux":
        status_cb(opt["name"])
        cmd = base_cmd + ["--remux-video", opt["remux_to"], url]
        code, paths, last_line = _run_ytdlp(cmd, progress_cb=progress_cb, status_cb=status_cb, cancel_event=cancel_event)
        filepath = paths[-1] if paths else None
        if code == 130:
            _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
            return
        if code != 0:
            _cleanup_on_network_failure(code, last_line, out_dir, job_started_ts, recursive=playlist)
            raise RuntimeError(last_line or "İndirme hatası")
        progress_cb(1.0)
        status_cb("İndirme tamamlandı")
        if playlist and paths:
            try:
                return str(Path(paths[0]).parent)
            except Exception:
                return str(out_dir)
        return filepath
    # Sadece video (MP4) — gerçek MP4 video-only yoksa hata
    if kind == "video_only_mp4":
        status_cb(opt["name"])
        cmd = base_cmd + [url]
        code, paths, last_line = _run_ytdlp(cmd, progress_cb=progress_cb, status_cb=status_cb, cancel_event=cancel_event)
        filepath = paths[-1] if paths else None
        if code == 130:
            _cleanup_cancel_artifacts(out_dir, job_started_ts, recursive=playlist)
            return
        if code != 0:
            _cleanup_on_network_failure(code, last_line, out_dir, job_started_ts, recursive=playlist)
            raise RuntimeError(last_line or "İndirme hatası")
        if filepath and Path(filepath).suffix.lower() != ".mp4":
            raise RuntimeError("Bu içerik için 1080p MP4 video-only formatı bulunamadı.")
        progress_cb(1.0)
        status_cb("İndirme tamamlandı")
        return filepath
    raise RuntimeError("Bilinmeyen seçenek türü.")