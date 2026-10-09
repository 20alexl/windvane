#!/usr/bin/env python3
"""Cut the long static stretches out of the live recording.

    python3 demo/trim.py demo/windvane.mp4 demo/windvane.gif [--cap 2] [--fps 10] [--colors 128]

The defaults keep a take under 5 MiB (4.8 MB for the 1.0.13 take), the
largest file the plugin directory accepts anywhere in the repository; the
palette is the lever, the frame rate and the cap do little. At 32 colours
the accent reds, yellows and blues wash out to grey; 64 is the fallback
when a longer take passes the limit. Check the size after a new take.

A live take waits for the model and for the compaction, and those waits are
most of the recording: a spinner and a token counter ticking for twenty
seconds. This reads the mp4 vhs rendered beside the gif, finds the frames
where the screen changed in a small way only (a spinner tick, a counter, a
typed character) and keeps at most ``--cap`` seconds of them after the last
large change; the rest of each stretch is cut, so the gif jumps from the
spinner to the result. Large changes (a line of output, a tool row, a
screenshot pause ending) are always kept.

Needs ffmpeg and ffprobe on PATH (or in ~/.local/bin, where the demo
installs them). Standard library only: frames are read as small grey
images and compared as integers.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

# The frames are compared at this width (the height follows the aspect
# ratio); a pixel counts as changed when its grey level moves across one of
# eight bands, which ignores the codec's noise.
SAMPLE_WIDTH = 160
BANDS = bytes(min(7, b >> 5) for b in range(256))
# A frame whose changed pixels exceed this share of the image is a large
# change, which always resets the stretch and is always kept.
LARGE_SHARE = 0.01


def tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    local = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
    if os.access(local, os.X_OK):
        return local
    sys.exit(f"{name} not found on PATH or in ~/.local/bin")


def probe(ffprobe: str, path: str) -> tuple[float, int, int]:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,width,height", "-of", "json", path],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stdout
    stream = json.loads(out)["streams"][0]
    num, _, den = str(stream["r_frame_rate"]).partition("/")
    fps = float(num) / float(den or 1)
    return fps, int(stream["width"]), int(stream["height"])


def frames(ffmpeg: str, path: str, width: int, height: int):
    """Yields each frame as a bytes object of grey pixels, small."""
    size = width * height
    proc = subprocess.Popen(
        [ffmpeg, "-v", "error", "-i", path, "-vf", f"scale={width}:{height}", "-f", "rawvideo",
         "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(size)
        if len(chunk) < size:
            break
        yield chunk
    proc.wait()


def changed_pixels(a: bytes, b: bytes) -> int:
    """How many pixels differ between two frames, by band."""
    x = int.from_bytes(a.translate(BANDS), "big") ^ int.from_bytes(b.translate(BANDS), "big")
    return len(x.to_bytes(len(a), "big").translate(None, b"\x00"))


def keep_ranges(sizes: list[int], total: int, fps: float, cap: float) -> list[tuple[int, int]]:
    """The frame ranges to keep: every large change, and up to ``cap``
    seconds of small changes after each."""
    large_at = max(1, int(total * LARGE_SHARE))
    window = int(round(cap * fps))
    kept: list[bool] = []
    since_large = 0
    for i, n in enumerate(sizes):
        if i == 0 or n >= large_at:
            since_large = 0
            kept.append(True)
            continue
        since_large += 1
        kept.append(since_large <= window)
    ranges: list[tuple[int, int]] = []
    start = None
    for i, k in enumerate(kept):
        if k and start is None:
            start = i
        if not k and start is not None:
            ranges.append((start, i - 1))
            start = None
    if start is not None:
        ranges.append((start, len(kept) - 1))
    return ranges


def main() -> int:
    ap = argparse.ArgumentParser(description="Cut the long static stretches out of the live recording.")
    ap.add_argument("mp4")
    ap.add_argument("gif")
    ap.add_argument("--cap", type=float, default=2.0, help="seconds of a static stretch kept (default 2)")
    ap.add_argument("--fps", type=int, default=10, help="the gif's frame rate (default 10)")
    ap.add_argument("--colors", type=int, default=128, help="palette size (default 128)")
    ap.add_argument("--width", type=int, default=0,
                    help="scale the gif to this width (default: the recording's own)")
    args = ap.parse_args()

    ffmpeg, ffprobe = tool("ffmpeg"), tool("ffprobe")
    fps, width, height = probe(ffprobe, args.mp4)
    sw = SAMPLE_WIDTH
    sh = max(2, (height * sw // width) // 2 * 2)

    sizes: list[int] = []
    prev = None
    for frame in frames(ffmpeg, args.mp4, sw, sh):
        sizes.append(0 if prev is None else changed_pixels(prev, frame))
        prev = frame
    if not sizes:
        sys.exit("no frames read")

    ranges = keep_ranges(sizes, sw * sh, fps, args.cap)
    kept = sum(b - a + 1 for a, b in ranges)
    select = "+".join(f"between(n,{a},{b})" for a, b in ranges)
    scale = f"scale={args.width}:-1:flags=lanczos," if args.width else ""
    vf = (
        f"select='{select}',setpts=N/{fps}/TB,fps={args.fps},{scale}"
        f"split[s0][s1];[s0]palettegen=max_colors={args.colors}:stats_mode=diff[p];"
        f"[s1][p]paletteuse=dither=none:diff_mode=rectangle"
    )
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", args.mp4, "-vf", vf, args.gif],
                   check=True, stdin=subprocess.DEVNULL)
    print(f"{len(sizes)} frames at {fps:g} fps ({len(sizes) / fps:.1f} s) -> {kept} kept "
          f"({kept / fps:.1f} s) in {len(ranges)} stretches; wrote {args.gif} "
          f"({os.path.getsize(args.gif) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
