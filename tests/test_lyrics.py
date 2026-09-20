"""Run with .venv/bin/python -B -m unittest discover -s tests -v."""

import base64
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from lyrics import merge_lyrics, write_tags


class MergeLyricsTests(unittest.TestCase):
    @staticmethod
    def payload(original, translated="", romanized=""):
        return {"lrc": {"lyric": original}, "tlyric": {"lyric": translated},
                "romalrc": {"lyric": romanized}}

    def test_chinese_and_option_defaults(self):
        payload = self.payload("[00:01.000]你好，世界", "[00:01]Hello, world",
                               "[00:01]ni hao, shi jie")
        original = deepcopy(payload)
        self.assertEqual(merge_lyrics(payload),
                         "[00:01.000]你好，世界\n[00:01.000]Hello, world")
        self.assertEqual(merge_lyrics(payload, romanization=True),
                         "[00:01.000]你好，世界\n[00:01.000]Hello, world\n"
                         "[00:01.000]ni hao, shi jie")
        self.assertEqual(merge_lyrics(payload, translation=False, romanization=True),
                         "[00:01.000]你好，世界\n[00:01.000]ni hao, shi jie")
        self.assertEqual(merge_lyrics(payload, False, False), "[00:01.000]你好，世界")
        self.assertEqual(payload, original)

    def test_inclusive_300ms_and_reject_301ms_on_both_sides(self):
        for delta in (-301, -300, -299, 0, 299, 300, 301):
            with self.subTest(delta=delta):
                time = 2000 + delta
                translated = f"[00:{time // 1000:02d}.{time % 1000:03d}]译文"
                result = merge_lyrics(self.payload("[00:02]原文", translated))
                expected = "[00:02.000]原文"
                if abs(delta) <= 300:
                    expected += "\n[00:02.000]译文"
                self.assertEqual(result, expected)

    def test_unmatched_additions_are_not_inserted(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[00:02]第一句\n[00:05]第二句",
            "[00:00]孤立翻译\n[00:02.301]不匹配\n[00:08]末尾翻译",
            "[00:01.699]bu pi pei"), romanization=True),
            "[00:02.000]第一句\n[00:05.000]第二句")

    def test_exact_match_wins_over_earlier_nearby_line(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[00:01]先唱\n[00:01.2]后唱", "[00:01.2]后唱的翻译")),
            "[00:01.000]先唱\n[00:01.200]后唱\n[00:01.200]后唱的翻译")

    def test_nearest_match_and_one_use_per_translation(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[00:01]第一句\n[00:01.4]第二句", "[00:01.25]仅一次")),
            "[00:01.000]第一句\n[00:01.400]第二句\n[00:01.400]仅一次")
        self.assertEqual(merge_lyrics(self.payload(
            "[00:01]第一句\n[00:01.4]第二句", "[00:01.2]等距")),
            "[00:01.000]第一句\n[00:01.000]等距\n[00:01.400]第二句")

    def test_multiple_timestamps_sort_and_preserve_repeated_chorus(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[00:03.5][00:01.25]副歌\n[00:02]间奏",
            "[00:01.250][00:03.500]Chorus")),
            "[00:01.250]副歌\n[00:01.250]Chorus\n[00:02.000]间奏\n"
            "[00:03.500]副歌\n[00:03.500]Chorus")

    def test_offsets_apply_globally_and_independently_before_matching(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[ar:歌手]\n[00:01.500]原文\n[offset:+500]",
            "[offset:-200]\n[00:00.800]翻译",
            "[offset:300]\n[00:01.300]yuan wen"), romanization=True),
            "[ar:歌手]\n[00:01.000]原文\n[00:01.000]翻译\n[00:01.000]yuan wen")

    def test_offset_clamps_at_zero_and_last_valid_value_wins(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[offset:-100]\n[00:00.100]开始\n[offset:300]\n[offset:invalid]")),
            "[00:00.000]开始")

    def test_metadata_bom_crlf_and_fraction_precision(self):
        self.assertEqual(merge_lyrics(self.payload(
            "\ufeff[ti:中文歌名]\r\n[ar:甲 / 乙][al:专辑]\r\n[by:制作人]\r\n"
            "[re:Editor]\r\n[ve:1.0]\r\n[length:04:00]\r\n[ti:中文歌名]\r\n"
            "[123:04.1239]终曲\r\n[00:01.5]一\r\n[00:02.05]二\r\n"
            "[00:03:005]三\r\n[00:04]四",
            "[ar:翻译者]\n[00:04]Four")),
            "[ti:中文歌名]\n[ar:甲 / 乙]\n[al:专辑]\n[by:制作人]\n"
            "[re:Editor]\n[ve:1.0]\n[length:04:00]\n[00:01.500]一\n"
            "[00:02.050]二\n[00:03.005]三\n[00:04.000]四\n[00:04.000]Four\n"
            "[123:04.123]终曲")

    def test_enhanced_and_square_word_timing_become_line_lyrics(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[00:01][00:03]<00:01.0>你<00:01.2>好<00:01.5>\n"
            "[00:05]世[00:05.2]界[00:05.5]\n[00:07]前缀<00:07.2>后缀",
            "[00:01]<00:01.1>Hello <00:01.3>world<00:01.6>")),
            "[00:01.000]你好\n[00:01.000]Hello world\n[00:03.000]你好\n"
            "[00:05.000]世界\n[00:07.000]前缀后缀")

    def test_deduplicate_per_timestamp_across_all_sources(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[00:01][00:01]你好\n[00:01]你好\n[00:01]Hello\n[00:03]你好",
            "[00:01]你好\n[00:01]Hello\n[00:01]您好",
            "[00:01]您好\n[00:01]ni hao\n[00:01]ni hao"), romanization=True),
            "[00:01.000]你好\n[00:01.000]Hello\n[00:01.000]您好\n"
            "[00:01.000]ni hao\n[00:03.000]你好")

    def test_empty_missing_or_metadata_only_lyrics(self):
        for payload in ({}, {"lrc": None}, {"lrc": {}}, {"lrc": {"lyric": None}},
                        self.payload(""), self.payload(" \r\n\t"),
                        self.payload("[ti:标题]\n[offset:200]\n[00:01] \n[00:02]<00:02.1>"),
                        self.payload("", "[00:01]只有翻译", "[00:01]romaji")):
            with self.subTest(payload=payload):
                self.assertEqual(merge_lyrics(payload, romanization=True), "")

    def test_instrumental_and_explicit_no_lyrics(self):
        for text in ("[99:00.00]纯音乐，请欣赏", "纯音乐，请欣赏。", "纯音乐",
                     "[00:00]此歌曲为没有填词的纯音乐，请您欣赏", "[00:00]Instrumental"):
            with self.subTest(text=text):
                self.assertEqual(merge_lyrics(self.payload(text, "[99:00]Instrumental")), "")
        payload = self.payload("[00:01]不应输出")
        payload["nolyric"] = True
        self.assertEqual(merge_lyrics(payload), "")
        self.assertEqual(merge_lyrics(self.payload("[00:01]纯音乐让我心动")),
                         "[00:01.000]纯音乐让我心动")

    def test_plain_text_fallback(self):
        self.assertEqual(merge_lyrics(self.payload(
            "[ti:歌名]\n第一行中文\n\n第二行\n第一行中文", "unmatched translation")),
            "第一行中文\n第二行")


class WriteTagsTests(unittest.TestCase):
    formats = (("mp3", "libmp3lame", MP3), ("flac", "flac", FLAC),
               ("m4a", "aac", MP4), ("ogg", "libvorbis", OggVorbis),
               ("opus", "libopus", OggOpus))
    song = {"id": 123, "name": "中文标题", "artists": "歌手甲 / 歌手乙", "album": "中文专辑"}
    lyrics = "[00:00.000]你好，世界\n[00:00.000]Hello, world"
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII="
    )

    @classmethod
    def setUpClass(cls):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("These integration tests require ffmpeg")
        temp = tempfile.TemporaryDirectory(prefix="lyrics-tests-")
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        for extension, codec, _ in cls.formats + (("wav", "pcm_s16le", None),):
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.15",
                 "-c:a", codec, str(cls.root / f"source.{extension}")],
                check=True, capture_output=True, timeout=30,
            )
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "lavfi", "-i", "color=c=blue:s=16x16", "-frames:v", "1",
             "-threads", "1", str(cls.root / "cover.jpg")],
            check=True, capture_output=True, timeout=30,
        )
        cls.jpeg = (cls.root / "cover.jpg").read_bytes()

    def copy_audio(self, extension):
        path = self.root / f"{self._testMethodName}.{extension}"
        shutil.copyfile(self.root / f"source.{extension}", path)
        return path

    def assert_tags(self, audio, song, lyrics, cover=None, mime="image/png"):
        if isinstance(audio, MP3):
            self.assertEqual(audio.tags.version, (2, 3, 0))
            self.assertEqual(audio.tags["TIT2"].text, [song["name"]])
            self.assertEqual(audio.tags["TPE1"].text, [song["artists"]])
            self.assertEqual(audio.tags["TALB"].text, [song["album"]])
            frames = audio.tags.getall("USLT")
            self.assertEqual([frame.text for frame in frames], [lyrics] if lyrics else [])
            pictures = audio.tags.getall("APIC")
        elif isinstance(audio, MP4):
            self.assertEqual(audio["\xa9nam"], [song["name"]])
            self.assertEqual(audio["\xa9ART"], [song["artists"]])
            self.assertEqual(audio["\xa9alb"], [song["album"]])
            self.assertEqual(audio.get("\xa9lyr", []), [lyrics] if lyrics else [])
            pictures = audio.get("covr", [])
        else:
            self.assertEqual(audio["title"], [song["name"]])
            self.assertEqual(audio["artist"], [song["artists"]])
            self.assertEqual(audio["album"], [song["album"]])
            self.assertEqual(audio.get("lyrics", []), [lyrics] if lyrics else [])
            pictures = (audio.pictures if isinstance(audio, FLAC) else
                        [Picture(base64.b64decode(value))
                         for value in audio.get("metadata_block_picture", [])])
        self.assertEqual(len(pictures), 1 if cover is not None else 0)
        if cover is not None:
            picture = pictures[0]
            if isinstance(audio, MP4):
                self.assertEqual(bytes(picture), cover)
                expected_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                self.assertEqual(picture.imageformat, expected_format)
            else:
                self.assertEqual(picture.data, cover)
                self.assertEqual(picture.mime, mime)
                self.assertEqual(picture.type, 3)
        self.assertGreater(audio.info.length, 0)

    def test_real_audio_roundtrip_png_all_formats(self):
        song_before = deepcopy(self.song)
        for extension, _, audio_class in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                write_tags(path, self.song, self.lyrics, cover=self.png)
                audio = mutagen.File(path)
                self.assertIsInstance(audio, audio_class)
                self.assert_tags(audio, self.song, self.lyrics, self.png)
                if extension == "mp3":
                    with path.open("rb") as stream:
                        self.assertEqual(stream.read(4), b"ID3\x03")
                # Decode the saved file too: tags must not damage its audio stream.
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
                     "-i", str(path), "-map", "0:a:0", "-f", "null", "-"],
                    check=True, capture_output=True, timeout=30,
                )
        self.assertEqual(self.song, song_before)

    def test_rewrite_replaces_managed_tags_and_preserves_unrelated_tags(self):
        changed = {"id": 456, "name": "新标题", "artists": "新歌手", "album": "新专辑"}
        for extension, _, _ in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                write_tags(path, self.song, self.lyrics, cover=self.png)
                audio = mutagen.File(path)
                if isinstance(audio, MP3):
                    audio.tags.add(TXXX(encoding=1, desc="Keep", text=["保留"]))
                    audio.save(v2_version=3)
                else:
                    audio["\xa9cmt" if isinstance(audio, MP4) else "comment"] = ["保留"]
                    audio.save()
                write_tags(path, changed, "[00:00.000]新歌词", cover=self.jpeg)
                write_tags(path, changed, "[00:00.000]新歌词", cover=self.jpeg)
                audio = mutagen.File(path)
                self.assert_tags(audio, changed, "[00:00.000]新歌词", self.jpeg, "image/jpeg")
                if isinstance(audio, MP3):
                    self.assertEqual(audio.tags["TXXX:Keep"].text, ["保留"])
                else:
                    self.assertEqual(audio["\xa9cmt" if isinstance(audio, MP4) else "comment"], ["保留"])

    def test_disabled_embedding_preserves_lyrics_and_missing_cover_preserves_art(self):
        changed = {**self.song, "name": "只改标题"}
        for extension, _, _ in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                write_tags(path, self.song, self.lyrics, cover=self.png)
                write_tags(path, changed, "不应写入", embed_lyrics=False)
                self.assert_tags(mutagen.File(path), changed, self.lyrics, self.png)

    def test_disabled_embedding_does_not_create_lyrics(self):
        for extension, _, _ in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                write_tags(path, self.song, self.lyrics, embed_lyrics=False)
                self.assert_tags(mutagen.File(path), self.song, "")

    def test_empty_lyrics_remove_stale_lyrics(self):
        for extension, _, _ in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                write_tags(path, self.song, self.lyrics)
                write_tags(path, self.song, "")
                self.assert_tags(mutagen.File(path), self.song, "")

    def test_files_without_initial_tags(self):
        for extension, _, _ in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                audio = mutagen.File(path)
                audio.delete()
                write_tags(path, self.song, self.lyrics)
                self.assert_tags(mutagen.File(path), self.song, self.lyrics)

    def test_unsupported_format_preserves_file_bytes(self):
        path = self.copy_audio("wav")
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported audio format.*wav"):
            write_tags(path, self.song, self.lyrics, cover=self.png)
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNotNone(mutagen.File(path))

    def test_unrecognized_and_corrupt_files_are_not_deleted(self):
        for name, data in (("unknown.bin", b"not audio"), ("broken.mp3", b"ID3\x04\x00")):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "Unsupported audio format|Cannot read audio file") as error:
                    write_tags(path, self.song, self.lyrics)
                self.assertIn(str(path), str(error.exception))
                self.assertEqual(path.read_bytes(), data)

    def test_invalid_cover_is_rejected_before_any_save(self):
        for extension, _, _ in self.formats:
            with self.subTest(format=extension):
                path = self.copy_audio(extension)
                original = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "cover format.*JPEG or PNG"):
                    write_tags(path, self.song, self.lyrics, cover=b"not an image")
                self.assertEqual(path.read_bytes(), original)

    def test_content_detection_does_not_depend_on_extension(self):
        path = self.root / "misleading.flac"
        shutil.copyfile(self.root / "source.mp3", path)
        write_tags(path, self.song, self.lyrics)
        # mutagen.File() keys off the extension, so the mislabelled file must be
        # reopened by the container it actually holds.
        audio = MP3(path, ID3=ID3)
        self.assertIsInstance(audio, MP3)
        self.assert_tags(audio, self.song, self.lyrics)

    def test_corrupt_file_keeps_content_and_reports_the_path(self):
        path = self.root / "truncated.flac"
        path.write_bytes(b"fLaC" + b"\x00" * 8)
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Cannot read audio file"):
            write_tags(path, self.song, self.lyrics)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
