"""Local media library.

Only files below one configured folder are ever listed or served, only with an
allow-listed extension, and clients refer to them by an opaque id derived from
the relative path (stable across restarts and when other files are added).
Paths never come from a client; the id is looked up in a server-built table.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska'}
AUDIO_TYPES = {'.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.opus': 'audio/ogg',
               '.wav': 'audio/wav', '.flac': 'audio/flac'}
MEDIA_TYPES = {**VIDEO_TYPES, **AUDIO_TYPES}
SUB_EXT = ('.vtt', '.srt')
MAX_ENTRIES = 2000
MAX_DEPTH = 3
MAX_SUB_BYTES = 2_000_000
SAFE_VIDEO = {'h264', 'vp8', 'vp9', 'av1'}
SAFE_AUDIO = {'aac', 'mp3', 'opus', 'vorbis', 'flac', 'pcm_s16le', 'pcm_s24le'}


@dataclass
class Entry:
    id: str
    rel: str
    name: str
    folder: str
    size: int
    mtime: float
    mime: str
    kind: str
    subs: list = field(default_factory=list)


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', text)]


def parse_range(header: str | None, size: int):
    """Return None for 'serve everything', (start, end) for a satisfiable range,
    or False when the range is unsatisfiable/malformed (answer 416)."""
    if not header:
        return None
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not m:
        return None if header.strip().lower().startswith('bytes=') and ',' in header else False
    first, last = m.groups()
    if not first and not last:
        return False
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    else:
        length = int(last)
        if length == 0:
            return False
        start, end = max(0, size - length), size - 1
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)


def srt_to_vtt(raw: bytes) -> str:
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if text.lstrip().startswith('WEBVTT'):
        return text
    text = re.sub(r'\{\\[^}]*\}', '', text)
    text = re.sub(r'(\d{1,2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', text)
    return 'WEBVTT\n\n' + text.strip() + '\n'


class Library:
    def __init__(self, root: Path, ttl: float = 4.0, probe: bool = True):
        self.root = Path(root).resolve()
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[str, Entry] = {}
        self._order: list[str] = []
        self._scanned = 0.0
        self._probe_cache: dict[tuple, dict] = {}
        self._probe_queue: list[Entry] = []
        self.ffprobe = shutil.which('ffprobe') if probe else None
        self._probe_thread = None

    # -- scanning -----------------------------------------------------------
    def scan(self, force: bool = False):
        with self._lock:
            if not force and time.monotonic() - self._scanned < self.ttl:
                return
            found: list[Entry] = []
            self._walk(self.root, '', 0, found)
            found.sort(key=lambda e: (natural_key(e.folder), natural_key(e.name)))
            self._entries = {e.id: e for e in found}
            self._order = [e.id for e in found]
            self._scanned = time.monotonic()
            if self.ffprobe:
                for e in found:
                    if e.kind == 'video' and self._key(e) not in self._probe_cache and e not in self._probe_queue:
                        self._probe_queue.append(e)
                self._start_prober()

    def _walk(self, path: Path, rel: str, depth: int, out: list):
        try:
            with os.scandir(path) as it:
                children = sorted(it, key=lambda d: natural_key(d.name))
        except OSError:
            return
        names = {c.name for c in children}
        for c in children:
            if c.name.startswith('.') or len(out) >= MAX_ENTRIES:
                continue
            try:
                if c.is_symlink():
                    continue
                if c.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        self._walk(path / c.name, f'{rel}/{c.name}' if rel else c.name, depth + 1, out)
                    continue
                if not c.is_file(follow_symlinks=False):
                    continue
                suffix = Path(c.name).suffix.lower()
                if suffix not in MEDIA_TYPES:
                    continue
                st = c.stat(follow_symlinks=False)
            except OSError:
                continue
            relpath = f'{rel}/{c.name}' if rel else c.name
            stem = Path(c.name).stem
            subs = []
            for n in sorted(names, key=natural_key):
                p = Path(n)
                if p.suffix.lower() in SUB_EXT and (p.stem == stem or p.stem.startswith(stem + '.')):
                    lang = p.stem[len(stem) + 1:] or 'sub'
                    subs.append({'rel': f'{rel}/{n}' if rel else n, 'lang': lang[:12], 'label': lang[:12].upper() if lang != 'sub' else 'Subtitles'})
            out.append(Entry(
                id=hashlib.sha256(relpath.encode('utf-8')).hexdigest()[:12], rel=relpath, name=c.name, folder=rel,
                size=st.st_size, mtime=st.st_mtime, mime=MEDIA_TYPES[suffix],
                kind='video' if suffix in VIDEO_TYPES else 'audio', subs=subs))

    # -- queries ------------------------------------------------------------
    def listing(self) -> list[dict]:
        self.scan()
        with self._lock:
            result = []
            for i in self._order:
                e = self._entries[i]
                info = self._probe_cache.get(self._key(e), {})
                result.append({
                    'id': e.id, 'title': Path(e.name).stem, 'ext': Path(e.name).suffix.lower().lstrip('.'),
                    'folder': e.folder, 'size': e.size, 'kind': e.kind,
                    'subs': [{'n': n, 'label': s['label'], 'lang': s['lang']} for n, s in enumerate(e.subs)],
                    'duration': info.get('duration'), 'playable': info.get('playable', self._guess(e)),
                    'codec': info.get('codec'), 'height': info.get('height'),
                })
            return result

    def get(self, ident: str) -> Entry | None:
        self.scan()
        with self._lock:
            return self._entries.get(ident)

    @staticmethod
    def _key(e: Entry):
        return (e.rel, e.size, e.mtime)

    @staticmethod
    def _guess(e: Entry):
        if e.mime in ('video/mp4', 'video/webm', 'audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/wav', 'audio/flac'):
            return 'likely'
        return 'unsure'

    # -- file access --------------------------------------------------------
    def open(self, entry: Entry, rel: str | None = None):
        """Open without following symlinks in ANY path component. Returns (fd, size)."""
        parts = (rel or entry.rel).split('/')
        if any(p in ('', '.', '..') or '\0' in p for p in parts):
            raise OSError('bad path')
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
        nofollow = getattr(os, 'O_NOFOLLOW', 0)
        dirfd = os.open(self.root, flags | getattr(os, 'O_DIRECTORY', 0))
        try:
            for part in parts[:-1]:
                nxt = os.open(part, flags | nofollow | getattr(os, 'O_DIRECTORY', 0), dir_fd=dirfd)
                os.close(dirfd)
                dirfd = nxt
            fd = os.open(parts[-1], flags | nofollow, dir_fd=dirfd)
        finally:
            os.close(dirfd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise OSError('not a regular file')
        return fd, st.st_size

    def subtitle_vtt(self, entry: Entry, n: int) -> str | None:
        if not 0 <= n < len(entry.subs):
            return None
        fd, size = self.open(entry, entry.subs[n]['rel'])
        try:
            if size > MAX_SUB_BYTES:
                return None
            data = os.read(fd, size)
        finally:
            os.close(fd)
        text = srt_to_vtt(data)
        return text

    # -- optional ffprobe ---------------------------------------------------
    def _start_prober(self):
        if self._probe_thread and self._probe_thread.is_alive():
            return
        self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True, name='ffprobe')
        self._probe_thread.start()

    def _probe_loop(self):
        while True:
            with self._lock:
                if not self._probe_queue:
                    return
                e = self._probe_queue.pop(0)
            info = self._run_probe(e)
            with self._lock:
                self._probe_cache[self._key(e)] = info

    def _run_probe(self, e: Entry) -> dict:
        try:
            fd, _ = self.open(e)
        except OSError:
            return {}
        try:
            r = subprocess.run(  # noqa: S603 - fixed argv, file passed via stdin-less /dev/fd
                [self.ffprobe, '-v', 'error', '-show_entries',
                 'format=duration:stream=codec_type,codec_name,height', '-of', 'json', f'/dev/fd/{fd}'],
                capture_output=True, text=True, timeout=10, pass_fds=(fd,), check=False)
            data = json.loads(r.stdout or '{}')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}
        finally:
            os.close(fd)
        vc = ac = None
        height = None
        for s in data.get('streams', []):
            if s.get('codec_type') == 'video' and vc is None:
                vc, height = s.get('codec_name'), s.get('height')
            elif s.get('codec_type') == 'audio' and ac is None:
                ac = s.get('codec_name')
        try:
            duration = round(float(data.get('format', {}).get('duration')))
        except (TypeError, ValueError):
            duration = None
        if vc is None:
            playable = 'unsure'
        elif vc in SAFE_VIDEO and (ac is None or ac in SAFE_AUDIO) and e.mime != 'video/x-matroska':
            playable = 'likely'
        elif vc in SAFE_VIDEO:
            playable = 'unsure'
        else:
            playable = 'unlikely'
        return {'duration': duration, 'playable': playable, 'codec': '/'.join(x for x in (vc, ac) if x), 'height': height}