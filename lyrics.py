"""Offline NetEase LRC merging and mutagen audio tag writing."""

from __future__ import annotations

import base64
from bisect import bisect_left, bisect_right
from functools import partial
from pathlib import Path
import re

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


_TIME = r"(\d+):([0-5]\d)(?:[.:](\d+))?"
_LINE_TIME = re.compile(r"\[" + _TIME + r"\]")
_WORD_TIME = re.compile(r"<" + _TIME + r">")
_METADATA = re.compile(r"\[([a-z][a-z0-9_-]*):([^\]\r\n]*)\]", re.I)
_INSTRUMENTAL = {"纯音乐", "纯音乐请欣赏", "此歌曲为没有填词的纯音乐请您欣赏", "instrumental", "instrumentalonly"}
_TOLERANCE_MS = 300


def _is_instrumental(text: str) -> bool:
    return re.sub(r"[\s，,。.!！]+", "", text).casefold() in _INSTRUMENTAL


def _parse_lrc(content: str) -> tuple[list[str], dict[int, list[str]], list[str]]:
    metadata: list[str] = []
    rows: list[tuple[int, str]] = []
    plain: list[str] = []
    offset = 0
    for raw in content.lstrip("\ufeff").splitlines():
        line = raw.strip()
        while match := _METADATA.match(line):
            key, value = match.groups()
            if key.lower() == "offset":
                if re.fullmatch(r"[+-]?\d+", value.strip()):
                    offset = int(value)
            elif match[0] not in metadata:
                metadata.append(match[0])
            line = line[match.end():].lstrip()

        times: list[int] = []
        while match := _LINE_TIME.match(line):
            minutes, seconds, fraction = match.groups()
            times.append(int(minutes) * 60000 + int(seconds) * 1000
                         + int(((fraction or "0") + "000")[:3]))
            line = line[match.end():].lstrip()

        # Consecutive leading tags repeat a line; tags after words are word timing.
        text = _WORD_TIME.sub("", _LINE_TIME.sub("", line)).strip()
        if not text or _is_instrumental(text):
            continue
        if times:
            rows.extend((time, text) for time in times)
        elif text not in plain:
            plain.append(text)

    groups: dict[int, list[str]] = {}
    for time, text in rows:
        # LRC positive offsets advance lyrics. Bake the offset into each timestamp
        # and omit its metadata tag to prevent players from applying it twice.
        time = max(0, time - offset)
        group = groups.setdefault(time, [])
        if text not in group:
            group.append(text)
    return metadata, dict(sorted(groups.items())), plain


def _align(base: dict[int, list[str]], extra: dict[int, list[str]]) -> None:
    """Match timestamp groups once, preferring exact then nearest matches."""
    times = list(base)
    candidates: list[tuple[int, int, int]] = []
    for other_time in extra:
        start = bisect_left(times, other_time - _TOLERANCE_MS)
        end = bisect_right(times, other_time + _TOLERANCE_MS)
        candidates.extend((abs(time - other_time), time, other_time)
                          for time in times[start:end])
    used_base: set[int] = set()
    used_extra: set[int] = set()
    for _, time, other_time in sorted(candidates):
        if time in used_base or other_time in used_extra:
            continue
        used_base.add(time)
        used_extra.add(other_time)
        base[time].extend(text for text in extra[other_time] if text not in base[time])


def merge_lyrics(payload: dict, translation=True, romanization=False) -> str:
    """Merge /lyric/new's lrc, tlyric and romalrc into millisecond LRC.

    Original metadata is retained except offset, which is applied separately to
    each source (positive = earlier, negative = later; times clamp at zero).
    Translation/romanization groups match once within an inclusive 300 ms,
    preferring exact then nearest times, with earlier times breaking ties.
    Unmatched additions are omitted. Output order is original, translation,
    romanization, all using the original timestamp. Duplicate text is removed
    only within that timestamp, so repeated choruses remain intact.

    Enhanced angle/square word timing becomes line timing. Untimed originals
    fall back to plain text; absent/empty or instrumental-only lyrics return "".
    """
    if not payload or payload.get("nolyric") is True:
        return ""

    def content(key: str) -> str:
        section = payload.get(key)
        value = section.get("lyric") if isinstance(section, dict) else None
        return value if isinstance(value, str) else ""

    metadata, base, plain = _parse_lrc(content("lrc"))
    if not base:
        return "\n".join(plain)
    for enabled, key in ((translation, "tlyric"), (romanization, "romalrc")):
        if enabled:
            _align(base, _parse_lrc(content(key))[1])

    result = metadata[:]
    for time, texts in base.items():
        minutes, remainder = divmod(time, 60000)
        seconds, milliseconds = divmod(remainder, 1000)
        tag = f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}]"
        result.extend(tag + text for text in texts)
    return "\n".join(result)


def _open_audio(path: Path):
    """Open an audio file, falling back to content sniffing.

    Endpoints sometimes report a container that does not match what the CDN
    actually serves, so a mislabelled extension must still be taggable.
    """
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError:
        audio = None
    if isinstance(audio, (MP3, FLAC, MP4, OggVorbis, OggOpus)):
        return audio
    try:
        head = path.open("rb").read(64)
    except OSError as exc:
        raise ValueError(f"Cannot read audio file {path}: {exc}") from exc
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        open_audio = partial(MP3, ID3=ID3)
    elif head[:4] == b"fLaC":
        open_audio = FLAC
    elif head[4:8] == b"ftyp":
        open_audio = MP4
    elif head[:4] == b"OggS" and b"OpusHead" in head:
        open_audio = OggOpus
    elif head[:4] == b"OggS" and b"\x01vorbis" in head:
        open_audio = OggVorbis
    else:
        raise ValueError(
            f"Unsupported audio format: {path} "
            "(supported: MP3, FLAC, MP4/M4A, Ogg Vorbis, Ogg Opus)"
        )
    try:
        return open_audio(path)
    except mutagen.MutagenError as exc:
        # A truncated or corrupt file must fail with a readable message and
        # must never be modified.
        raise ValueError(f"Cannot read audio file {path}: {exc}") from exc


def write_tags(
    path: Path,
    song: dict,
    lyrics: str,
    embed_lyrics=True,
    cover: bytes | None = None,
) -> None:
    """Write title, artists, album, optional lyrics and JPEG/PNG front cover.

    Supports MP3 (ID3v2.3), FLAC, MP4/M4A, Ogg Vorbis and Ogg Opus, detected by
    file content rather than extension. ``song`` contains id, name, artists (one
    string), album (one string); id is not used. embed_lyrics=False preserves
    existing lyrics; True with empty lyrics clears them. cover=None preserves
    existing art. Other tags are left intact. Unsupported audio/cover formats
    raise ValueError. I/O and save errors propagate to the caller; this function
    never deletes an audio file.
    """
    path = Path(path)
    audio = _open_audio(path)

    picture = None
    if cover is not None:
        if cover.startswith(b"\x89PNG\r\n\x1a\n"):
            mime, image_format = "image/png", MP4Cover.FORMAT_PNG
        elif cover.startswith(b"\xff\xd8\xff"):
            mime, image_format = "image/jpeg", MP4Cover.FORMAT_JPEG
        else:
            raise ValueError(f"Unsupported cover format for {path}: expected JPEG or PNG")
        picture = Picture()
        picture.type = 3
        picture.desc = "Cover"
        picture.mime = mime
        picture.data = cover

    title, artists, album = song["name"], song["artists"], song["album"]
    if audio.tags is None:
        audio.add_tags()

    if isinstance(audio, MP3):
        audio.tags.add(TIT2(encoding=1, text=[title]))
        audio.tags.add(TPE1(encoding=1, text=[artists]))
        audio.tags.add(TALB(encoding=1, text=[album]))
        if embed_lyrics:
            audio.tags.delall("USLT")
            if lyrics:
                audio.tags.add(USLT(encoding=1, lang="und", desc="", text=lyrics))
        if picture is not None:
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=1, mime=picture.mime, type=3,
                                desc=picture.desc, data=cover))
        audio.tags.update_to_v23()
        audio.save(v2_version=3)
    elif isinstance(audio, MP4):
        audio["\xa9nam"] = [title]
        audio["\xa9ART"] = [artists]
        audio["\xa9alb"] = [album]
        if embed_lyrics:
            audio.pop("\xa9lyr", None)
            if lyrics:
                audio["\xa9lyr"] = [lyrics]
        if picture is not None:
            audio["covr"] = [MP4Cover(cover, imageformat=image_format)]
        audio.save()
    else:
        audio["title"] = [title]
        audio["artist"] = [artists]
        audio["album"] = [album]
        if embed_lyrics:
            audio.pop("lyrics", None)
            if lyrics:
                audio["lyrics"] = [lyrics]
        if picture is not None:
            if isinstance(audio, FLAC):
                audio.clear_pictures()
                audio.add_picture(picture)
            else:
                audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
        audio.save()
