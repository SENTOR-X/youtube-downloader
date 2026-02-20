#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import shutil
import subprocess
import re
from typing import Any, Optional, Tuple, Dict, List


def _find_ytdlp() -> str:
    """yt-dlp binary'sini bul."""
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp bulunamadı. Lütfen yt-dlp kurulu olduğundan emin olun.")
    return exe


def _run_ytdlp_json(cmd: List[str], *, timeout_sec: int = 25) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("yt-dlp zaman aşımına uğradı.") from e

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if not err:
            err = f"yt-dlp hata kodu: {proc.returncode}"
        raise RuntimeError(err)

    try:
        info: Dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError("yt-dlp çıktısı JSON olarak okunamadı.") from e
    return info


def probe_playlist(url: str, *, timeout_sec: int = 20) -> Dict[str, Any]:
    """
    URL playlist mi? (yt-dlp ile probe)
    - yt-dlp --flat-playlist --yes-playlist kullanır.
    Dönen sözlük:
      is_playlist: bool
      playlist_only: bool  (URL 'playlist?list=...' gibi, açık bir tek video hedeflemiyorsa)
      title: str
      count: int
    """
    ytdlp = _find_ytdlp()

    # Basit heuristik: list= yoksa playlist probunu çağırmak gereksiz olabilir;
    # fakat kullanıcı "probe" istediği için, bu heuristik sadece çağıranı hızlandırmak için kullanılmalı.
    cmd = [
        ytdlp,
        "-J",
        "--flat-playlist",
        "--yes-playlist",
        "--skip-download",
        "--no-warnings",
        url,
    ]
    info = _run_ytdlp_json(cmd, timeout_sec=timeout_sec)

    entries = info.get("entries")
    is_playlist = isinstance(entries, list)
    title = (info.get("title") or info.get("playlist_title") or "").strip()
    count = 0
    if is_playlist:
        count = len(entries)
        # Bazı extractor'lar count alanı döndürebilir; varsa daha güvenilir olanı al
        for k in ("playlist_count", "n_entries", "entries_count"):
            v = info.get(k)
            if isinstance(v, int) and v > 0:
                count = v
                break

    # URL playlist-only mi? (heuristik)
    # - playlist?list=... veya list= var ama v= yoksa "playlist-only" kabul ediyoruz.
    playlist_only = False
    try:
        u = url
        has_list = "list=" in u
        has_v = ("v=" in u) or ("/watch" in u and "?" in u and "v=" in u)
        is_playlist_path = "/playlist" in u
        playlist_only = bool(is_playlist and (is_playlist_path or (has_list and not has_v)))
    except Exception:
        playlist_only = False

    return {
        "is_playlist": bool(is_playlist),
        "playlist_only": bool(playlist_only),
        "title": title,
        "count": int(count) if isinstance(count, int) else 0,
    }


def get_formats(url: str, *, timeout_sec: int = 25) -> Tuple[List[Dict[str, Any]], str]:
    """Tek video için formatları getir (playlist kapalı)."""
    ytdlp = _find_ytdlp()
    cmd = [
        ytdlp,
        "-J",
        "--no-playlist",
        "--skip-download",
        "--no-warnings",
        url,
    ]
    info = _run_ytdlp_json(cmd, timeout_sec=timeout_sec)
    title = (info.get("title") or "").strip()
    fmts = info.get("formats", [])
    if not isinstance(fmts, list):
        fmts = []
    return fmts, title


def get_formats_for_playlist_item(url: str, item_index: int, *, timeout_sec: int = 35) -> Tuple[List[Dict[str, Any]], str]:
    """Playlist içindeki tek bir öğe üzerinden format taraması.

    Not: Bazı durumlarda yt-dlp, --playlist-items ile bile "playlist JSON" döndürebilir.
    Bu fonksiyon; önce doğrudan formats alanını dener, yoksa entries[0] üzerinden
    ilgili öğenin webpage_url/url/id bilgisini çözüp tek-video format taraması yapar.
    """
    ytdlp = _find_ytdlp()
    cmd = [
        ytdlp,
        "-J",
        "--yes-playlist",
        "--playlist-items",
        str(item_index),
        "--skip-download",
        "--no-warnings",
        url,
    ]
    info = _run_ytdlp_json(cmd, timeout_sec=timeout_sec)

    title = (info.get("title") or "").strip()
    fmts = info.get("formats", [])
    if isinstance(fmts, list) and fmts:
        return fmts, title

    entries = info.get("entries")
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        e = entries[0]
        etitle = (e.get("title") or title or "").strip()
        efmts = e.get("formats")
        if isinstance(efmts, list) and efmts:
            return efmts, etitle

        eurl = (e.get("webpage_url") or e.get("url") or e.get("id") or "").strip()
        if eurl and (not eurl.startswith("http")):
            # YouTube benzeri video-id ise tam URL oluştur
            if re.fullmatch(r"[A-Za-z0-9_-]{6,}", eurl):
                eurl = f"https://www.youtube.com/watch?v={eurl}"
        if eurl:
            fmts2, title2 = get_formats(eurl, timeout_sec=timeout_sec)
            return fmts2, (etitle or title2 or title)

    return [], title


def first_index_from_playlist_items_spec(spec: str) -> int:
    """'1:10,12,15' gibi spec'ten ilk index'i deterministik olarak çıkar."""
    s = (spec or "").strip()
    if not s:
        return 1
    m = re.search(r"(\d+)", s)
    if not m:
        return 1
    try:
        v = int(m.group(1))
        return v if v > 0 else 1
    except Exception:
        return 1
