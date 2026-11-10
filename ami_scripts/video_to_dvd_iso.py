#!/usr/bin/env python3

import argparse
import subprocess
import json
import os
from fractions import Fraction
import shutil

def probe_video_format(input_file):
    """
    Uses ffprobe to inspect the input video for format (NTSC/PAL) AND duration.
    """
    
    # This is the corrected line.
    # "stream=height,avg_frame_rate" and "format=duration" are now one string
    # separated by a colon.
    entries = "stream=height,avg_frame_rate:format=duration"
    
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", entries,  # Use the combined string here
        "-of", "json",
        input_file
    ]
    
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print("Error running ffprobe:", e.stderr)
        exit(1)
    
    info = json.loads(result.stdout)

    # Get duration from format info
    try:
        duration_sec = float(info["format"]["duration"])
    except KeyError:
        print("Error: Could not determine video duration from ffprobe.")
        exit(1)

    if "streams" not in info or len(info["streams"]) == 0:
        print("No video stream found in the file.")
        exit(1)

    stream = info["streams"][0]
    height = stream.get("height")
    avg_frame_rate = stream.get("avg_frame_rate")
    video_format = None

    if height is not None:
        if height == 480:
            video_format = "ntsc"
        elif height == 576:
            video_format = "pal"
        else:
            if avg_frame_rate and avg_frame_rate != "0/0":
                try:
                    fr = float(Fraction(avg_frame_rate))
                    if abs(fr - 29.97) < 1:
                        video_format = "ntsc"
                    elif abs(fr - 25) < 1:
                        video_format = "pal"
                    else:
                        video_format = "ntsc"  # default fallback
                except Exception:
                    video_format = "ntsc"
            else:
                video_format = "ntsc"
    else:
        try:
            if avg_frame_rate and avg_frame_rate != "0/0":
                fr = float(Fraction(avg_frame_rate))
                if abs(fr - 29.97) < 1:
                    video_format = "ntsc"
                elif abs(fr - 25) < 1:
                    video_format = "pal"
                else:
                    video_format = "ntsc"  # default fallback
            else:
                video_format = "ntsc"
        except Exception:
            video_format = "ntsc"
            
    print(f"Determined video format: {video_format.upper()} (height: {height}, frame rate: {avg_frame_rate})")
    print(f"Video duration: {duration_sec:.2f} seconds")
    # Return both format and duration
    return video_format, duration_sec

def transcode_video(input_file, video_format, duration_sec):
    """
    Uses ffmpeg to transcode the input file to a DVD-compliant MPEG-2 file,
    calculating the bitrate to fit a 4.7GB (4.37 GiB) disc.
    """
    target = "ntsc-dvd" if video_format == "ntsc" else "pal-dvd"
    output_file = "dvd_compliant.mpg"

    # --- Bitrate Calculation ---
    # A standard single-layer DVD-R holds exactly 4,700,000,000 bytes.
    # We must account for MPEG-2 container muxing overhead (~4%) and ISO filesystem overhead.
    # We use a safe target of 94% of the absolute max capacity: 4,418,000,000 bytes.
    safe_target_bytes = 4700000000 * 0.94
    
    # Convert to kbits (FFmpeg uses base-1000 for bitrates, so 1k = 1000 bits)
    target_size_kbits = (safe_target_bytes * 8) / 1000
    
    # Standard DVD audio bitrate (AC3)
    audio_bitrate_k = 192  
    
    # Calculate the total bitrate available in kbit/s
    total_bitrate_k = target_size_kbits / duration_sec
    
    # Subtract the audio bitrate to get the target video bitrate
    video_bitrate_k = total_bitrate_k - audio_bitrate_k
    
    # DVD Spec max video bitrate is ~9800 kbit/s, but total (video+audio) can't exceed 10.08 Mbps.
    # Capping video at 8000k ensures safe playback compatibility on older hardware
    # and prevents short videos from exceeding DVD spec limits.
    MAX_VIDEO_BITRATE_K = 8000
    if video_bitrate_k > MAX_VIDEO_BITRATE_K:
        video_bitrate_k = MAX_VIDEO_BITRATE_K

    if video_bitrate_k < 1000:
        print(f"Warning: Calculated video bitrate is very low ({int(video_bitrate_k)}k).")
        print("The video is likely too long for good quality on a single DVD.")

    video_bitrate_str = f"{int(video_bitrate_k)}k"
    audio_bitrate_str = f"{audio_bitrate_k}k"
    max_rate_str = f"{MAX_VIDEO_BITRATE_K}k"
    
    # Set a DVD-compliant buffer size (1835k is standard)
    buffer_size_str = "1835k"

    cmd = [
        "ffmpeg", "-i", input_file,
        "-target", target,          
        "-b:v", video_bitrate_str,   
        "-maxrate", max_rate_str,    
        "-minrate", "0",             
        "-bufsize", buffer_size_str, 
        "-b:a", audio_bitrate_str,   
        "-acodec", "ac3",            
        output_file
    ]
    
    print("Running ffmpeg command:")
    print(" ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        print("Error: ffmpeg transcoding failed.")
        exit(1)
    return output_file

#
# --- No changes needed to author_dvd_structure() or create_iso() ---
#
def author_dvd_structure(mpg_file, video_format):
    """
    Uses dvdauthor to create the DVD structure (VIDEO_TS, AUDIO_TS, etc.).
    The VIDEO_FORMAT environment variable is set based on the detected format.
    """
    os.environ["VIDEO_FORMAT"] = video_format.upper()
    chapters = ("0,5:00,10:00,15:00,20:00,25:00,30:00,35:00,40:00,45:00,50:00,55:00,"
                "1:00:00,1:05:00,1:10:00,1:15:00,1:20:00,1:25:00,1:30:00,1:35:00,"
                "1:40:00,1:45:00,1:50:00,1:55:00,2:00:00,2:05:00,2:10:00,2:15:00,"
                "2:20:00,2:25:00,2:30:00,2:35:00,2:40:00,2:45:00")
    cmd1 = ["dvdauthor", "-o", "dvd_structure", "-t", "-c", chapters, mpg_file]
    print("Running dvdauthor command (creating structure):")
    print(" ".join(cmd1))
    try:
        subprocess.check_call(cmd1)
    except subprocess.CalledProcessError:
        print("Error: dvdauthor (creating structure) failed.")
        exit(1)
    cmd2 = ["dvdauthor", "-o", "dvd_structure", "-T"]
    print("Running dvdauthor command (finalizing):")
    print(" ".join(cmd2))
    try:
        subprocess.check_call(cmd2)
    except subprocess.CalledProcessError:
        print("Error: dvdauthor (finalizing) failed.")
        exit(1)

def create_iso(dvd_title, iso_filename):
    """
    Uses mkisofs to create a DVD-compliant ISO image.
    """
    cmd = ["mkisofs", "-dvd-video", "-V", dvd_title, "-o", iso_filename, "dvd_structure"]
    print("Running mkisofs command:")
    print(" ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        print("Error: mkisofs failed.")
        exit(1)
    return iso_filename

def main():
    parser = argparse.ArgumentParser(
        description="Create a DVD-compliant ISO disc image from a video file."
    )
    parser.add_argument("-i", "--input", required=True, help="Path to the input video file")
    parser.add_argument("-t", "--title", default="MyDVDTitle", help="DVD Title for the ISO image (default: MyDVDTitle)")    
    args = parser.parse_args()
    
    input_file = args.input
    dvd_title = args.title

    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' does not exist.")
        exit(1)

    work_dir = os.path.dirname(os.path.abspath(input_file))
    if work_dir:
        os.chdir(work_dir)

    base_name = os.path.splitext(os.path.basename(input_file))[0]
    iso_filename = base_name + ".iso"

    # Step 1: Determine format AND duration.
    video_format, duration_sec = probe_video_format(input_file)
    
    # Step 2: Transcode using the duration to calculate bitrate.
    mpg_file = transcode_video(input_file, video_format, duration_sec)
    
    # Step 3: Author the DVD structure (no change).
    author_dvd_structure(mpg_file, video_format)
    
    # Step 4: Create the ISO image (no change).
    create_iso(dvd_title, iso_filename)
    print(f"\nDVD ISO image created successfully: {iso_filename}")

    # Clean up intermediate files.
    try:
        os.remove(mpg_file)
        print(f"Removed intermediate file: {mpg_file}")
    except Exception as e:
        print(f"Warning: Could not remove {mpg_file}: {e}")
    try:
        shutil.rmtree("dvd_structure")
        print("Removed intermediate folder: dvd_structure")
    except Exception as e:
        print(f"Warning: Could not remove dvd_structure folder: {e}")

if __name__ == "__main__":
    main()