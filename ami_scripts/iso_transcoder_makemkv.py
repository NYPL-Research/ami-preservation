#!/usr/bin/env python3

"""
iso_transcoder_makemkv.py
DVD ISO -> MKV -> MP4 workflow with OCR subtitle extraction.

Modes:
  - Default: MakeMKV -> OCR subtitles -> transcode MP4
  - --extract-subs-only: extract bitmap subtitle tracks only (.idx/.sub), no OCR, no MP4
  - --srt-only: extract + OCR subtitles only, no MP4

Dependencies:
  - MakeMKV CLI: makemkvcon
  - MKVToolNix: mkvmerge, mkvextract
  - FFmpeg
  - Python packages: pymediainfo, colorama

OCR dependencies:
  - Primary OCR: vobsub-to-srt
  - Fallback OCR: subtile-ocr

Install notes for macOS:
  1. Install Apple Command Line Tools:
       xcode-select --install

  2. Install Rust/Cargo (required for subtile-ocr):
       curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
       source "$HOME/.cargo/env"

  3. Install Tesseract and additional language data:
       brew install tesseract
       brew install tesseract-lang

  4. If cargo builds fail on macOS with errors like:
       fatal error: 'stdio.h' file not found
     set:
       export SDKROOT="$(xcrun --show-sdk-path)"

  5. Install OCR tools:
       npm install -g vobsub-to-srt
       cargo install subtile-ocr

  6. Optional: add this to ~/.zshrc so SDKROOT persists:
       export SDKROOT="$(xcrun --show-sdk-path)"
"""

import argparse
from pathlib import Path
import subprocess
import logging
import tempfile
import sys
import os
import shutil
import json
import re
from pymediainfo import MediaInfo
from collections import defaultdict

CATEGORIES = {
    "NTSC DVD SD (D1 Resolution)": {
        "Width": 720,
        "Height": 480,
        "DisplayAspectRatio": "1.333",
        "PixelAspectRatio": "0.889",
        "FrameRate": "59.940",
    },
    "NTSC DVD Widescreen": {
        "Width": 720,
        "Height": 480,
        "DisplayAspectRatio": "1.777",
        "PixelAspectRatio": "1.185",
        "FrameRate": "59.940",
    },
    "NTSC DVD SD (4SIF Resolution)": {
        "Width": 704,
        "Height": 480,
        "DisplayAspectRatio": "1.333",
        "PixelAspectRatio": "0.909",
        "FrameRate": "59.940",
    },
    "NTSC DVD (SIF Resolution)": {
        "Width": 352,
        "Height": 240,
        "DisplayAspectRatio": "1.339",
        "PixelAspectRatio": "0.913",
        "FrameRate": "59.940",
    },
    "NTSC DVD (China Video Disc Resolution)": {
        "Width": 352,
        "Height": 480,
        "DisplayAspectRatio": "1.333",
        "PixelAspectRatio": "1.818",
        "FrameRate": "59.940",
    },
    "PAL DVD SD (D1 Resolution)": {
        "Width": 720,
        "Height": 576,
        "DisplayAspectRatio": "1.333",
        "PixelAspectRatio": "1.067",
        "FrameRate": "50.000",
    },
    "PAL DVD Widescreen": {
        "Width": 720,
        "Height": 576,
        "DisplayAspectRatio": "1.778",
        "PixelAspectRatio": "1.422",
        "FrameRate": "50.000",
    },
    "PAL DVD (CIF Resolution)": {
        "Width": 352,
        "Height": 288,
        "DisplayAspectRatio": "1.333",
        "PixelAspectRatio": "1.092",
        "FrameRate": "50.000",
    },
    "PAL DVD Half-D1 Resolution": {
        "Width": 352,
        "Height": 576,
        "DisplayAspectRatio": "1.333",
        "PixelAspectRatio": "2.182",
        "FrameRate": "50.000",
    },
    "PAL DVD Half-D1 Resolution Widescreen": {
        "Width": 352,
        "Height": 576,
        "DisplayAspectRatio": "1.778",
        "PixelAspectRatio": "2.909",
        "FrameRate": "50.000",
    },
}

NON_LATIN_LANGS = {"chi", "zho", "jpn", "kor"}

SUBTILE_LANG_MAP = {
    "eng": "eng",
    "fre": "fra",
    "fra": "fra",
    "ger": "deu",
    "deu": "deu",
    "spa": "spa",
    "chi": "chi_sim",
    "zho": "chi_sim",
    "jpn": "jpn",
    "kor": "kor",
}

LATIN_LANGS = {"eng", "fre", "fra", "ger", "deu", "spa", "ita", "por", "dut", "nld"}
HAN_LANGS = {"chi", "zho"}
JP_LANGS = {"jpn"}
KR_LANGS = {"kor"}

COMMON_LATIN_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "he", "her",
    "his", "i", "in", "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "our",
    "she", "so", "that", "the", "their", "them", "there", "they", "this", "to", "was",
    "we", "were", "what", "when", "where", "who", "why", "with", "you", "your",
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "est", "dans", "que",
    "der", "die", "das", "und", "ist", "ein", "eine", "mit", "nicht", "ich",
    "el", "los", "las", "una", "uno", "con", "del", "por", "para", "es", "en"
}

COMMON_ENGLISH_WORDS = {
    "the", "and", "of", "to", "in", "is", "it", "you", "that", "he", "was", "for",
    "on", "are", "as", "with", "his", "they", "i", "at", "be", "this", "have", "from",
    "or", "one", "had", "by", "not", "but", "what", "all", "were", "when", "we", "there",
    "can", "an", "your", "which", "their", "if", "do", "will", "each", "how", "them",
    "like", "still", "real", "life", "lights", "stage", "down", "up", "blank", "jump",
    "kill", "might", "feel", "write", "white", "hate", "gonna", "drawin"
}

SUBTILE_OCR_INSTALL_MSG = """
Required command 'subtile-ocr' not found in PATH.

macOS setup for subtile-ocr:
  1. Install Apple Command Line Tools:
       xcode-select --install

  2. Install Rust/Cargo:
       curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
       source "$HOME/.cargo/env"

  3. Install Tesseract and language data:
       brew install tesseract
       brew install tesseract-lang

  4. If Cargo build fails with 'stdio.h file not found':
       export SDKROOT="$(xcrun --show-sdk-path)"

  5. Install subtile-ocr:
       cargo install subtile-ocr

Optional ~/.zshrc addition:
       export SDKROOT="$(xcrun --show-sdk-path)"
""".strip()

try:
    from colorama import Fore, Style
except ImportError:
    print("colorama is not installed. Please install it by running: python3 -m pip install colorama")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def normalize_lang_for_filename(lang):
    return (lang or "und").lower().replace(" ", "_")


def warn_on_duplicate_sub_languages(sub_tracks, mkv_name):
    lang_counts = defaultdict(int)
    for track in sub_tracks:
        lang_counts[track["lang"]] += 1
    duplicates = {lang: count for lang, count in lang_counts.items() if count > 1}
    if duplicates:
        logging.warning(f"[{mkv_name}] Duplicate subtitle language tags detected: {duplicates}")


def verify_dependencies(extract_subs_only=False, srt_only=False):
    try:
        subprocess.run(
            ["makemkvcon", "-r", "info", "disc:9999"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        pass
    except FileNotFoundError:
        logging.error("makemkvcon is not found in PATH. Please install MakeMKV.")
        sys.exit(1)

    if shutil.which("ffmpeg") is None and not (extract_subs_only or srt_only):
        logging.error("Required command 'ffmpeg' not found in PATH.")
        logging.error("Install FFmpeg with: brew install ffmpeg")
        sys.exit(1)

    for cmd in ["mkvmerge", "mkvextract"]:
        if shutil.which(cmd) is None:
            logging.error(f"Required command '{cmd}' not found in PATH.")
            logging.error("Install MKVToolNix with: brew install mkvtoolnix")
            sys.exit(1)

    if not extract_subs_only:
        if shutil.which("vobsub-to-srt") is None:
            logging.error("Required command 'vobsub-to-srt' not found in PATH.")
            logging.error("Install it with: npm install -g vobsub-to-srt")
            sys.exit(1)

        if shutil.which("subtile-ocr") is None:
            if shutil.which("cargo") is None:
                logging.error("Cargo is not installed.")
                logging.error("Install Rust/Cargo first:")
                logging.error("  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh")
                logging.error('  source "$HOME/.cargo/env"')
                sys.exit(1)

            for line in SUBTILE_OCR_INSTALL_MSG.splitlines():
                logging.error(line)
            sys.exit(1)


def process_iso_with_makemkv(iso_path, output_directory):
    logging.info(f"Processing ISO {iso_path} with MakeMKV")
    output_path = Path(tempfile.mkdtemp())

    makemkv_command = ["makemkvcon", "mkv", f"iso:{iso_path}", "all", str(output_path)]
    try:
        subprocess.run(makemkv_command, check=True)
        logging.info(f"MKV files created from {iso_path} in {output_path}")
        return output_path
    except subprocess.CalledProcessError:
        logging.error(f"MakeMKV processing failed for {iso_path}")
        return None


def build_ffmpeg_command(input_file, output_file, srt_files=None, dar=None):
    if srt_files is None:
        srt_files = []

    ffmpeg_command = ["ffmpeg", "-i", str(input_file)]
    ffmpeg_command.extend([
        "-c:v", "libx264",
        "-movflags", "faststart",
        "-pix_fmt", "yuv420p",
        "-crf", "21",
        "-vf", "idet,bwdif=1",
        "-c:a", "aac",
        "-b:a", "320000",
        "-ar", "48000"
    ])
    if dar:
        ffmpeg_command.extend(["-aspect", str(dar)])
        
    ffmpeg_command.extend(["-map", "0:v", "-map", "0:a"])
    ffmpeg_command.append(str(output_file))
    return ffmpeg_command


def get_subtitle_tracks(mkv_file):
    cmd = ["mkvmerge", "-J", str(mkv_file)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        logging.error(f"mkvmerge failed to probe {mkv_file.name}: {e.stderr}")
        return []

    sub_tracks = []
    for track in data.get("tracks", []):
        if track.get("type") == "subtitles" and track.get("codec") == "VobSub":
            props = track.get("properties", {})
            sub_tracks.append({
                "id": track.get("id"),
                "lang": props.get("language", "und"),
                "track_number": props.get("number"),
                "default": props.get("default_track", False),
                "forced": props.get("forced_track", False),
                "bytes": props.get("tag_number_of_bytes"),
                "frames": props.get("tag_number_of_frames"),
                "source_id": props.get("tag_source_id"),
            })

    lang_counts = defaultdict(int)
    for track in sub_tracks:
        lang_counts[track["lang"]] += 1
    for track in sub_tracks:
        track["duplicate_lang"] = lang_counts[track["lang"]] > 1

    warn_on_duplicate_sub_languages(sub_tracks, mkv_file.name)

    for track in sub_tracks:
        logging.info(
            f"[{mkv_file.name}] Subtitle track ID {track['id']} | "
            f"lang={track['lang']} | track_no={track['track_number']} | "
            f"frames={track['frames']} | bytes={track['bytes']} | "
            f"default={track['default']} | forced={track['forced']} | "
            f"duplicate_lang={track['duplicate_lang']} | "
            f"source_id={track['source_id']}"
        )

    return sub_tracks


def run_vobsub_to_srt(idx_path, srt_path):
    cmd = [
        "vobsub-to-srt",
        "-i", str(idx_path),
        "-o", str(srt_path),
        "-q", "accurate"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr


def run_subtile_ocr(idx_path, srt_path, lang):
    cmd = ["subtile-ocr"]
    if lang:
        cmd.extend(["-l", lang])
    cmd.extend(["-o", str(srt_path), str(idx_path)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr


def strip_srt_structure(text):
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.isdigit():
            continue
        if "-->" in s:
            continue
        lines.append(s)
    return "\n".join(lines)


def count_chars_in_ranges(text, ranges):
    total = 0
    for ch in text:
        cp = ord(ch)
        for start, end in ranges:
            if start <= cp <= end:
                total += 1
                break
    return total


def _alpha_tokens_with_case(text):
    return re.findall(r"[A-Za-z']+", text)


def _looks_gibberish_line(line):
    tokens = _alpha_tokens_with_case(line)
    if len(tokens) < 2:
        return False

    lower_tokens = [t.lower() for t in tokens]
    common_hits = sum(1 for t in lower_tokens if t in COMMON_ENGLISH_WORDS)
    uppercaseish = sum(1 for t in tokens if len(t) >= 4 and t.upper() == t)
    weird = sum(1 for t in lower_tokens if len(t) >= 7 and not re.search(r"[aeiou].*[aeiou]", t))
    vowelish = sum(1 for t in lower_tokens if re.search(r"[aeiouy]", t))

    upper_ratio = uppercaseish / len(tokens)
    weird_ratio = weird / len(tokens)
    vowel_ratio = vowelish / len(tokens)

    return common_hits == 0 and upper_ratio >= 0.5 and (weird_ratio >= 0.4 or vowel_ratio < 0.7)


def english_text_plausibility(text, strict=False):
    cleaned = strip_srt_structure(text)
    lower_cleaned = cleaned.lower()
    tokens = re.findall(r"[a-z']+", lower_cleaned)

    if len(tokens) < 4:
        return False

    common_hits = sum(1 for t in tokens if t in COMMON_ENGLISH_WORDS)
    common_ratio = common_hits / len(tokens)

    vowelish = sum(1 for t in tokens if re.search(r"[aeiouy]", t))
    vowel_ratio = vowelish / len(tokens)

    weird_tokens = sum(
        1 for t in tokens
        if len(t) >= 8 and not re.search(r"[aeiou].*[aeiou]", t)
    )
    weird_ratio = weird_tokens / len(tokens)

    text_lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    substantive_lines = [ln for ln in text_lines if len(re.findall(r"[A-Za-z']+", ln)) >= 2]
    gibberish_lines = sum(1 for ln in substantive_lines if _looks_gibberish_line(ln))
    gibberish_ratio = gibberish_lines / max(len(substantive_lines), 1)

    if strict:
        if common_ratio >= 0.04 and weird_ratio < 0.28 and gibberish_ratio < 0.35:
            return True
        if common_ratio >= 0.025 and vowel_ratio >= 0.78 and weird_ratio < 0.22 and gibberish_ratio < 0.25:
            return True
        return False

    if common_ratio >= 0.02 and gibberish_ratio < 0.55:
        return True
    if vowel_ratio >= 0.70 and weird_ratio < 0.35 and gibberish_ratio < 0.45:
        return True

    return False


def latin_text_plausibility(text):
    cleaned = strip_srt_structure(text)
    visible = "".join(ch for ch in cleaned if not ch.isspace())
    if len(visible) < 25:
        return False

    tokens = re.findall(r"[A-Za-zÀ-ÿ']+", cleaned)
    if len(tokens) < 4:
        return False

    lower_tokens = [t.lower() for t in tokens]
    common_hits = sum(1 for t in lower_tokens if t in COMMON_LATIN_WORDS)
    vowelish = sum(1 for t in lower_tokens if re.search(r"[aeiouyà-ÿ]", t))
    very_weird = sum(
        1 for t in lower_tokens
        if len(t) >= 8 and not re.search(r"[aeiouyà-ÿ].*[aeiouyà-ÿ]", t)
    )

    common_ratio = common_hits / max(len(lower_tokens), 1)
    vowel_ratio = vowelish / max(len(lower_tokens), 1)
    weird_ratio = very_weird / max(len(lower_tokens), 1)

    if common_ratio >= 0.02:
        return True
    if vowel_ratio >= 0.70 and weird_ratio < 0.35:
        return True

    return False


def script_matches_expected_lang(text, lang):
    cleaned = strip_srt_structure(text)

    han_count = count_chars_in_ranges(cleaned, [
        (0x3400, 0x4DBF),
        (0x4E00, 0x9FFF),
        (0xF900, 0xFAFF),
    ])
    hira_kata_count = count_chars_in_ranges(cleaned, [
        (0x3040, 0x309F),
        (0x30A0, 0x30FF),
    ])
    hangul_count = count_chars_in_ranges(cleaned, [
        (0x1100, 0x11FF),
        (0x3130, 0x318F),
        (0xAC00, 0xD7AF),
    ])

    if lang in HAN_LANGS:
        return han_count >= 10
    if lang in JP_LANGS:
        return (hira_kata_count >= 5) or (han_count >= 10)
    if lang in KR_LANGS:
        return hangul_count >= 10
    if lang == "eng":
        return english_text_plausibility(cleaned, strict=False)
    if lang in LATIN_LANGS:
        return latin_text_plausibility(cleaned)

    return True


def srt_is_suspicious(srt_path, expected_lang=None):
    if not srt_path.exists():
        return True

    try:
        text = srt_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True

    if not text.strip():
        return True

    if srt_path.stat().st_size < 250:
        return True

    visible = "".join(ch for ch in text if not ch.isspace())
    if len(visible) < 25:
        return True

    alnum_like = sum(ch.isalnum() for ch in visible)
    if alnum_like < 8:
        return True

    if expected_lang and not script_matches_expected_lang(text, expected_lang):
        return True

    return False


def move_failed_ocr_outputs(output_dir, final_base_name, track_id, temp_srt, extracted_idx, extracted_sub):
    failed_srt = output_dir / f"{final_base_name}_track{track_id}_und_ocr_failed.srt"
    counter = 1
    while failed_srt.exists():
        failed_srt = output_dir / f"{final_base_name}_track{track_id}_und_ocr_failed_{counter}.srt"
        counter += 1

    if temp_srt.exists():
        shutil.move(str(temp_srt), str(failed_srt))
        logging.warning(f"Saved suspicious OCR output for review: {failed_srt.name}")

    if extracted_idx.exists() or extracted_sub.exists():
        logging.warning(
            f"Retaining bitmap subtitle assets for track {track_id}: "
            f"{extracted_idx.name}" + (f", {extracted_sub.name}" if extracted_sub.exists() else "")
        )


def extract_and_ocr(mkv_file, output_dir, final_base_name, bitmap_only=False):
    sub_tracks = get_subtitle_tracks(mkv_file)
    generated_srts = []

    if not sub_tracks:
        logging.info(f"No VobSub tracks found in {mkv_file.name} for extraction.")
        return generated_srts

    for track in sub_tracks:
        track_id = track["id"]
        lang = track["lang"]
        lang_for_name = normalize_lang_for_filename(lang)
        subtile_lang = SUBTILE_LANG_MAP.get(lang, "eng")

        logging.info(
            f"[{mkv_file.name}] Extracting Subtitle Track ID {track_id} "
            f"(Lang: {lang}, Frames: {track['frames']}, Bytes: {track['bytes']})"
        )

        extracted_base = output_dir / f"{final_base_name}_track{track_id}_{lang_for_name}"
        extracted_idx = extracted_base.with_suffix(".idx")
        extracted_sub = extracted_base.with_suffix(".sub")
        temp_srt = extracted_base.with_suffix(".srt")

        mkvextract_cmd = [
            "mkvextract", str(mkv_file), "tracks", f"{track_id}:{extracted_idx}"
        ]

        try:
            subprocess.run(mkvextract_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"mkvextract failed for track ID {track_id}: {e.stderr}")
            continue

        if not extracted_idx.exists():
            logging.error(f"mkvextract failed to create .idx for track ID {track_id}")
            continue

        if not extracted_sub.exists():
            logging.warning(
                f"Expected companion .sub file was not found for track ID {track_id}. "
                f"Check extraction results."
            )

        logging.info(
            f"Extracted bitmap subtitles: {extracted_idx.name}"
            + (f" and {extracted_sub.name}" if extracted_sub.exists() else "")
        )

        if bitmap_only:
            logging.info(
                f"Skipping OCR for track ID {track_id} because --extract-subs-only is set."
            )
            continue

        if temp_srt.exists():
            temp_srt.unlink()

        primary_backend = "subtile-ocr" if lang in NON_LATIN_LANGS else "vobsub-to-srt"
        used_fallback = False

        if primary_backend == "subtile-ocr":
            logging.info(
                f"[Track {track_id}] Using subtile-ocr as primary for language '{lang}' ({subtile_lang})."
            )
            ok, stderr = run_subtile_ocr(extracted_idx, temp_srt, subtile_lang)
        else:
            logging.info(
                f"[Track {track_id}] Using vobsub-to-srt as primary for language '{lang}'."
            )
            ok, stderr = run_vobsub_to_srt(extracted_idx, temp_srt)

        if not ok:
            logging.warning(
                f"Primary OCR failed for track ID {track_id}. Attempting subtile-ocr fallback."
            )
            if temp_srt.exists():
                temp_srt.unlink()
            ok, stderr = run_subtile_ocr(extracted_idx, temp_srt, subtile_lang)
            used_fallback = True

        suspicious = True
        if ok and temp_srt.exists():
            suspicious = srt_is_suspicious(temp_srt, expected_lang=lang)

        if primary_backend == "vobsub-to-srt" and (not ok or suspicious):
            logging.warning(
                f"OCR output for track ID {track_id} looks suspicious or failed; "
                f"retrying with subtile-ocr ({subtile_lang})."
            )
            if temp_srt.exists():
                temp_srt.unlink()
            ok, stderr = run_subtile_ocr(extracted_idx, temp_srt, subtile_lang)
            used_fallback = True
            if ok and temp_srt.exists():
                suspicious = srt_is_suspicious(temp_srt, expected_lang=lang)

        if ok and temp_srt.exists() and lang == "eng" and track.get("duplicate_lang") and used_fallback:
            try:
                text = temp_srt.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                text = ""
            if not english_text_plausibility(text, strict=True):
                logging.warning(
                    f"Track {track_id} is a duplicate 'eng' subtitle track that required fallback OCR "
                    f"and still does not look convincingly English."
                )
                suspicious = True

        if not ok or not temp_srt.exists():
            logging.error(f"OCR failed for track ID {track_id}: {stderr}")
            move_failed_ocr_outputs(output_dir, final_base_name, track_id, temp_srt, extracted_idx, extracted_sub)
            continue

        if suspicious:
            logging.warning(
                f"OCR output for track ID {track_id} still looks suspicious "
                f"({temp_srt.stat().st_size} bytes): {temp_srt.name}"
            )
            logging.warning(
                f"Track {track_id} OCR looks weak; saving as review output with undefined language."
            )
            move_failed_ocr_outputs(output_dir, final_base_name, track_id, temp_srt, extracted_idx, extracted_sub)
            continue

        logging.info(
            f"OCR output for track ID {track_id} looks plausible "
            f"({temp_srt.stat().st_size} bytes)."
        )

        final_srt = output_dir / f"{final_base_name}_{lang_for_name}.srt"
        counter = 1
        while final_srt.exists():
            final_srt = output_dir / f"{final_base_name}_{lang_for_name}_{counter}.srt"
            counter += 1

        shutil.move(str(temp_srt), str(final_srt))
        logging.info(f"Success -> Created SRT: {final_srt.name}")
        generated_srts.append((final_srt, lang))

        try:
            if extracted_idx.exists():
                extracted_idx.unlink()
            if extracted_sub.exists():
                extracted_sub.unlink()
        except Exception as e:
            logging.warning(f"Could not remove extracted VobSub files: {e}")

    return generated_srts


def verify_mkv_compatibility(mkv_files):
    if not mkv_files:
        return False

    base_props = None

    for mkv_file in mkv_files:
        media_info = MediaInfo.parse(str(mkv_file))
        current_props = {}

        for track in media_info.tracks:
            if track.track_type == "Video":
                current_props.update({
                    "width": track.width,
                    "height": track.height,
                    "frame_rate": track.frame_rate,
                    "pixel_format": track.pixel_format,
                    "codec_id": track.codec_id
                })
            elif track.track_type == "Audio":
                current_props.update({
                    "audio_format": track.format,
                    "channels": track.channel_s,
                    "sampling_rate": track.sampling_rate
                })

        if base_props is None:
            base_props = current_props
        elif current_props != base_props:
            logging.error(f"File {mkv_file.name} has different properties than the first file:")
            for key in base_props:
                if key in current_props and base_props[key] != current_props[key]:
                    logging.error(f"  {key}: {base_props[key]} != {current_props[key]}")
            return False

    return True


def concatenate_mkvs(mkv_files):
    if not mkv_files:
        logging.error("No MKV files provided for concatenation.")
        return None

    if not verify_mkv_compatibility(mkv_files):
        logging.error("MKV files are not compatible for concatenation")
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as tmp_mkv_file:
            mkvmerge_command = ["mkvmerge", "-o", tmp_mkv_file.name, str(mkv_files[0])]
            for mkv_file in mkv_files[1:]:
                mkvmerge_command.extend(["+", str(mkv_file)])

            logging.info(f"Attempting concatenation with command: {' '.join(mkvmerge_command)}")
            result = subprocess.run(mkvmerge_command, capture_output=True, text=True)

            if result.returncode == 0:
                logging.info("Concatenation successful")
                return tmp_mkv_file.name
            else:
                logging.warning("First concatenation attempt failed, trying with --append-mode track")
                mkvmerge_command.insert(1, "--append-mode")
                mkvmerge_command.insert(2, "track")

                result = subprocess.run(mkvmerge_command, capture_output=True, text=True)

                if result.returncode == 0:
                    logging.info("Concatenation successful with --append-mode track")
                    return tmp_mkv_file.name
                else:
                    logging.error(f"Both concatenation attempts failed. Error: {result.stderr}")
                    if os.path.exists(tmp_mkv_file.name):
                        os.unlink(tmp_mkv_file.name)
                    return None

    except Exception as e:
        logging.error(f"Unexpected error during concatenation: {str(e)}")
        if "tmp_mkv_file" in locals() and os.path.exists(tmp_mkv_file.name):
            os.unlink(tmp_mkv_file.name)
        return None

def get_container_dar(mkv_file):
    cmd = ["ffmpeg", "-i", str(mkv_file)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    import re
    match = re.search(r'\[SAR \d+:\d+ DAR (\d+:\d+)\]', result.stderr)
    if match:
        return match.group(1)
    return None

def transcode_mkv_files(
    mkv_directory,
    iso_basename,
    output_directory,
    force_concat,
    extract_subs_only=False,
    srt_only=False,
):
    mkv_files = sorted(mkv_directory.glob("*.mkv"))
    if not mkv_files:
        logging.error(f"No MKV files found in {mkv_directory}")
        return False

    if force_concat and not (extract_subs_only or srt_only):
        logging.info(f"Found {len(mkv_files)} MKV files to concatenate")
        concatenated_mkv = concatenate_mkvs(mkv_files)

        if concatenated_mkv:
            final_base_name = f"{iso_basename}_sc"
            output_file = output_directory / f"{final_base_name}.mp4"

            _generated_srts = extract_and_ocr(
                concatenated_mkv,
                output_directory,
                final_base_name,
                bitmap_only=extract_subs_only,
            )

            logging.info(f"Transcoding concatenated MKV to {output_file}")
            try:
                dar = get_container_dar(concatenated_mkv)
                ffmpeg_command = build_ffmpeg_command(concatenated_mkv, output_file, _generated_srts, dar=dar)
                subprocess.run(ffmpeg_command, check=True)
                return True
            except subprocess.CalledProcessError as e:
                logging.error(f"Transcoding failed for concatenated MKV: {e}")
                return False
            finally:
                if os.path.exists(concatenated_mkv):
                    os.unlink(concatenated_mkv)
        else:
            logging.error("Concatenation failed, falling back to individual file processing")
            return transcode_mkv_files(
                mkv_directory,
                iso_basename,
                output_directory,
                force_concat=False,
                extract_subs_only=extract_subs_only,
                srt_only=srt_only,
            )

    else:
        success = True
        for idx, mkv_file in enumerate(mkv_files, start=1):
            final_base_name = (
                f"{iso_basename}f01r{str(idx).zfill(2)}_sc"
                if len(mkv_files) > 1
                else f"{iso_basename}_sc"
            )

            generated_srts = extract_and_ocr(
                mkv_file,
                output_directory,
                final_base_name,
                bitmap_only=extract_subs_only,
            )

            if extract_subs_only:
                logging.info(
                    f"Skipping MP4 transcode for {mkv_file.name} because --extract-subs-only is set."
                )
                continue

            if srt_only:
                logging.info(
                    f"Skipping MP4 transcode for {mkv_file.name} because --srt-only is set."
                )
                continue

            output_file = output_directory / f"{final_base_name}.mp4"
            logging.info(f"Transcoding {mkv_file} to {output_file}")
            try:
                dar = get_container_dar(mkv_file)
                ffmpeg_command = build_ffmpeg_command(mkv_file, output_file, generated_srts, dar=dar)
                subprocess.run(ffmpeg_command, check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Transcoding failed for {mkv_file}: {e}")
                success = False

        return success


def verify_transcoding(iso_paths, make_mkv_failures, output_directory):
    total_isos = len(iso_paths) + len(make_mkv_failures)
    successful_isos = []
    failed_isos = make_mkv_failures[:]

    print(f"\n{Style.BRIGHT}File Creation Summary:{Style.RESET_ALL}")

    for iso_path in iso_paths:
        iso_basename = iso_path.stem.replace("_pm", "")
        expected_output_files = [
            file for file in os.listdir(output_directory)
            if file.startswith(iso_basename) and file.endswith(".mp4")
        ]

        if not expected_output_files:
            logging.error(f"No MP4 files were created for ISO: {iso_path}")
            failed_isos.append(iso_path)
        else:
            logging.info(f"{len(expected_output_files)} MP4 files were created for ISO: {iso_path}")
            successful_isos.append(iso_path)

    print(f"\n{Style.BRIGHT}Processing Summary:{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Total ISOs processed: {total_isos}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}Successfully processed: {len(successful_isos)}{Style.RESET_ALL}")
    print(f"{Fore.RED}Failed to process: {len(failed_isos)}{Style.RESET_ALL}")

    if failed_isos:
        print(f"\n{Fore.RED}List of failed ISOs:{Style.RESET_ALL}")
        for iso in failed_isos:
            print(f" - {iso}")


def extract_video_properties(file_path):
    media_info = MediaInfo.parse(file_path)
    for track in media_info.tracks:
        if track.track_type == "Video":
            return {
                "Width": track.width,
                "Height": track.height,
                "DisplayAspectRatio": track.display_aspect_ratio,
                "PixelAspectRatio": track.pixel_aspect_ratio,
                "FrameRate": track.frame_rate,
            }
    return None


def classify_mp4(mp4_files):
    classification_counts = defaultdict(int)
    outliers = []

    for file in mp4_files:
        properties = extract_video_properties(file)
        if not properties:
            outliers.append(file)
            continue

        classified = False
        for category, criteria in CATEGORIES.items():
            match = True
            for key, value in criteria.items():
                prop_val = properties.get(key)
                if prop_val is None:
                    match = False
                    break
                try:
                    if float(prop_val) != float(value):
                        match = False
                        break
                except (ValueError, TypeError):
                    if str(prop_val) != str(value):
                        match = False
                        break
            if match:
                classification_counts[category] += 1
                classified = True
                break

        if not classified:
            outliers.append(file)

    return classification_counts, outliers


def summarize_classifications(classification_counts, outliers):
    print("\nClassification Summary:")
    for category, count in classification_counts.items():
        print(f"- {category}: {count} MP4(s)")

    print("\nOutliers:")
    if outliers:
        for outlier in sorted(outliers):
            print(f"- {outlier}")
    else:
        print("None")


def post_process_check(output_directory):
    mp4_files = list(Path(output_directory).glob("*.mp4"))
    classification_counts, outliers = classify_mp4(mp4_files)
    summarize_classifications(classification_counts, outliers)


def organize_files(input_directory, output_directory):
    pm_dir = output_directory / "PreservationMasters"
    sc_dir = output_directory / "ServiceCopies"
    
    pm_dir.mkdir(exist_ok=True)
    sc_dir.mkdir(exist_ok=True)
    
    iso_files_moved = 0
    for iso_file in input_directory.glob("*.iso"):
        if iso_file.is_file() and not iso_file.name.startswith("._"):
            try:
                shutil.move(str(iso_file), str(pm_dir / iso_file.name))
                iso_files_moved += 1
            except Exception as e:
                logging.error(f"Failed to move {iso_file.name} to PreservationMasters: {e}")
                
    sc_files_moved = 0
    for file in output_directory.glob("*_sc*"):
        if file.is_file() and not file.name.startswith("._"):
            try:
                shutil.move(str(file), str(sc_dir / file.name))
                sc_files_moved += 1
            except Exception as e:
                logging.error(f"Failed to move {file.name} to ServiceCopies: {e}")

    logging.info(f"Moved {iso_files_moved} ISO files to PreservationMasters")
    logging.info(f"Moved {sc_files_moved} service copy files to ServiceCopies")


def main():
    parser = argparse.ArgumentParser(
        description="Transcode MKV files created from ISO images to H.264 MP4s and extract OCR subtitles."
    )
    parser.add_argument("-i", "--input", dest="i", required=True,
                        help="Path to the input directory with ISO files")
    parser.add_argument("-o", "--output", dest="o", required=True,
                        help="Output directory for MP4 and subtitle files")
    parser.add_argument("-f", "--force", action="store_true",
                        help="Force concatenation of MKV files before transcoding")
    parser.add_argument("--extract-subs-only", action="store_true",
                        help="Extract bitmap subtitle tracks only (.idx/.sub); skip OCR and MP4 transcoding")
    parser.add_argument("--srt-only", action="store_true",
                        help="Extract and OCR subtitle tracks only; skip MP4 transcoding")
    args = parser.parse_args()

    if args.extract_subs_only and args.srt_only:
        parser.error("Use only one of --extract-subs-only or --srt-only.")

    verify_dependencies(
        extract_subs_only=args.extract_subs_only,
        srt_only=args.srt_only
    )

    input_directory = Path(args.i)
    if not input_directory.is_dir():
        logging.error(f"Error: The input path (-i) must be a directory containing ISO files, not a single file.")
        sys.exit(1)
        
    output_directory = Path(args.o)
    output_directory.mkdir(parents=True, exist_ok=True)

    iso_files = [file for file in sorted(input_directory.glob("*.iso")) if not file.name.startswith("._")]
    processed_iso_paths = []
    make_mkv_failures = []

    for iso_file in iso_files:
        iso_basename = iso_file.stem.replace("_pm", "")
        mkv_output_dir = process_iso_with_makemkv(iso_file, output_directory)

        if mkv_output_dir:
            transcode_success = transcode_mkv_files(
                mkv_output_dir,
                iso_basename,
                output_directory,
                args.force,
                extract_subs_only=args.extract_subs_only,
                srt_only=args.srt_only,
            )
            if transcode_success:
                processed_iso_paths.append(iso_file)
            else:
                logging.error(f"Skipping verification for {iso_file} due to processing failure.")

            for mkv_file in mkv_output_dir.glob("*.mkv"):
                mkv_file.unlink()
            mkv_output_dir.rmdir()
        else:
            logging.error(f"MakeMKV processing failed for {iso_file}.")
            make_mkv_failures.append(iso_file)

    if args.extract_subs_only:
        print(f"\n{Style.BRIGHT}Subtitle Extraction Summary:{Style.RESET_ALL}")
        print(f"{Fore.CYAN}Total ISOs processed: {len(processed_iso_paths) + len(make_mkv_failures)}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Successfully processed: {len(processed_iso_paths)}{Style.RESET_ALL}")
        print(f"{Fore.RED}Failed to process: {len(make_mkv_failures)}{Style.RESET_ALL}")
        if make_mkv_failures:
            print(f"\n{Fore.RED}List of failed ISOs:{Style.RESET_ALL}")
            for iso in make_mkv_failures:
                print(f" - {iso}")
    elif args.srt_only:
        print(f"\n{Style.BRIGHT}Subtitle OCR Summary:{Style.RESET_ALL}")
        print(f"{Fore.CYAN}Total ISOs processed: {len(processed_iso_paths) + len(make_mkv_failures)}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Successfully processed: {len(processed_iso_paths)}{Style.RESET_ALL}")
        print(f"{Fore.RED}Failed to process: {len(make_mkv_failures)}{Style.RESET_ALL}")
        if make_mkv_failures:
            print(f"\n{Fore.RED}List of failed ISOs:{Style.RESET_ALL}")
            for iso in make_mkv_failures:
                print(f" - {iso}")
    else:
        verify_transcoding(processed_iso_paths, make_mkv_failures, output_directory)
        post_process_check(output_directory)

    print(f"\n{Style.BRIGHT}Organizing Files...{Style.RESET_ALL}")
    organize_files(input_directory, output_directory)


if __name__ == "__main__":
    main()