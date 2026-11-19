#!/usr/bin/env python3
"""Вставка рекламных мидроллов в готовый вертикальный клип по правилам из ads/registry.json.

Использование:
    python3 tools/ads.py <clip.mp4> <ad_id> <out.mp4>
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_registry():
    return json.load(open(os.path.join(ROOT, "ads", "registry.json")))


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def speech_pauses(clip, noise_db=-28, min_len=0.2):
    """Середины пауз в звуке клипа (silencedetect)."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", clip, "-vn", "-af",
                          f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def midroll_points(clip, content_dur, ad_dur, sched, pauses=None):
    """Моменты вставки во времени ИСХОДНОГО клипа."""
    mode = sched.get("mode", "middle_or_fixed")
    if mode == "middle_or_fixed":
        if content_dur + ad_dur < 120:
            return [content_dur / 2]  # центр рекламы = центр итогового ролика
        pts, shift = [], 0.0
        for t in sched["from_120s"]["at_seconds"]:
            pts.append(t - shift)
            shift += ad_dur
        return [p for p in pts if p < content_dur]
    if mode == "window":
        final = content_dur + ad_dur
        if final < sched["short_threshold"]:
            lo, hi = 1.0, content_dur - sched["under_20s"]["not_last_seconds"]
            target = content_dur / 2
        else:
            lo, hi = sched["from_20s"]["preferred"]
            hi = min(hi, content_dur - sched["from_20s"]["not_last_seconds"])
            target = (lo + hi) / 2
        pool = pauses if pauses is not None else speech_pauses(clip)
        cands = [p for p in pool if lo <= p <= hi]
        return [min(cands, key=lambda p: abs(p - target)) if cands else target]
    raise ValueError(mode)


def insert_midroll(clip, ad_id, out, pauses=None):
    reg = load_registry()
    ad = reg["ads"][ad_id]
    assert ad["type"] == "midroll", f"{ad_id} не мидролл"
    W, H = reg["canvas"]["width"], reg["canvas"]["height"]
    ad_file = os.path.join(ROOT, ad["file"])
    AD = probe_duration(ad_file)
    D = probe_duration(clip)
    pts = midroll_points(clip, D, AD, ad["schedule"], pauses)
    ck = ad["chroma_key"]
    lufs = ad["behavior"]["ad_audio_loudness_lufs"]
    pl = ad["placement"]
    if pl["mode"] == "center_scaled":
        rh = pl["render_height"]
        assert W * rh / (W * H) >= pl["min_screen_area"]
        geo, oy = f"scale=-2:{rh}:flags=lanczos,crop={W}:{rh}", (H - rh) // 2
    else:  # native_canvas: шаблон уже 1080x1920, накладываем 1:1
        geo, oy = f"scale={W}:{H}", 0
    dim = ad["behavior"].get("dim_background", False)

    cuts = [0.0] + pts + [D]
    n_ads = len(pts)
    fc = [f"[1:v]fps=60,colorkey={ck['color']}:{ck['similarity']}:{ck['blend']},despill=type=green,"
          f"{geo},format=yuva420p,split={n_ads}" + "".join(f"[adv{i}]" for i in range(n_ads)),
          f"[1:a]aresample=48000,loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000,asplit={n_ads}" +
          "".join(f"[ada{i}]" for i in range(n_ads)),
          f"[0:v]split={len(cuts)-1}" + "".join(f"[cv{i}]" for i in range(len(cuts) - 1)),
          f"[0:a]asplit={len(cuts)-1}" + "".join(f"[ca{i}]" for i in range(len(cuts) - 1))]
    segs = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        L = b - a
        if i < n_ads:
            en = f"gte(t,{L:.3f})"
            look = f",boxblur=24:2:enable='{en}',eq=brightness=-0.18:enable='{en}'" if dim else ""
            fc += [f"[cv{i}]trim={a}:{b},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={AD}{look}[bv{i}]",
                   f"[adv{i}]setpts=PTS-STARTPTS+{L:.3f}/TB[av{i}]",
                   f"[bv{i}][av{i}]overlay=0:{oy}:eof_action=pass[sv{i}]",
                   f"[ca{i}]atrim={a}:{b},asetpts=PTS-STARTPTS,apad=pad_dur={AD}[pa{i}]",
                   f"[ada{i}]adelay={int(round(L*1000))}:all=1[dl{i}]",
                   f"[pa{i}][dl{i}]amix=inputs=2:normalize=0:duration=first[sa{i}]"]
        else:
            fc += [f"[cv{i}]trim={a}:{b},setpts=PTS-STARTPTS[sv{i}]",
                   f"[ca{i}]atrim={a}:{b},asetpts=PTS-STARTPTS[sa{i}]"]
        segs.append(f"[sv{i}][sa{i}]")
    fc.append("".join(segs) + f"concat=n={len(segs)}:v=1:a=1[vo][ao]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", clip, "-i", ad_file, "-filter_complex", ";".join(fc),
                    "-map", "[vo]", "-map", "[ao]", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                    "-pix_fmt", "yuv420p", "-r", "60", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", out], check=True)
    starts, shift = [], 0.0
    for p in pts:
        starts.append(round(p + shift, 2))
        shift += AD
    return {"ad": ad_id, "final_duration": round(D + AD * n_ads, 2), "ad_starts": starts,
            "ad_duration": round(AD, 2)}


if __name__ == "__main__":
    print(json.dumps(insert_midroll(*sys.argv[1:4]), ensure_ascii=False))
