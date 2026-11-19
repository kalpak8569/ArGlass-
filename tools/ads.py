#!/usr/bin/env python3
"""Вставка рекламных мидроллов в готовый вертикальный клип по правилам из ads/registry.json.

Использование:
    python3 tools/ads.py <clip.mp4> <ad_id> <out.mp4>
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def midroll_points(content_dur, ad_dur, sched):
    """Возвращает моменты вставки во времени ИСХОДНОГО клипа."""
    final_one = content_dur + ad_dur
    if final_one < sched["from_120s"].get("threshold", 120):
        # одна вставка ровно в середине итогового ролика
        return [content_dur / 2]
    # две вставки: старт рекламы на 0:30 и 1:30 итогового ролика
    pts, shift = [], 0.0
    for t in sched["from_120s"]["at_seconds"]:
        pts.append(t - shift)
        shift += ad_dur
    return [p for p in pts if p < content_dur]


def insert_midroll(clip, ad_id, out):
    reg = json.load(open(os.path.join(ROOT, "ads", "registry.json")))
    ad = reg["ads"][ad_id]
    assert ad["type"] == "midroll", f"{ad_id} не мидролл"
    W, H = reg["canvas"]["width"], reg["canvas"]["height"]
    ad_file = os.path.join(ROOT, ad["file"])
    AD = probe_duration(ad_file)
    D = probe_duration(clip)
    pts = midroll_points(D, AD, ad["schedule"])
    rh = ad["placement"]["render_height"]
    assert W * rh / (W * H) >= ad["placement"]["min_screen_area"]
    ck = ad["chroma_key"]
    lufs = ad["behavior"]["ad_audio_loudness_lufs"]

    cuts = [0.0] + pts + [D]
    fc = []
    n_ads = len(pts)
    # реклама: ключ, масштаб, центр
    fc.append(f"[1:v]fps=60,colorkey={ck['color']}:{ck['similarity']}:{ck['blend']},despill=type=green,"
              f"scale=-2:{rh}:flags=lanczos,crop={W}:{rh},format=yuva420p,split={n_ads}" +
              "".join(f"[adv{i}]" for i in range(n_ads)))
    fc.append(f"[1:a]aresample=48000,loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000,asplit={n_ads}" +
              "".join(f"[ada{i}]" for i in range(n_ads)))
    fc.append(f"[0:v]split={len(cuts)-1}" + "".join(f"[cv{i}]" for i in range(len(cuts) - 1)))
    fc.append(f"[0:a]asplit={len(cuts)-1}" + "".join(f"[ca{i}]" for i in range(len(cuts) - 1)))
    segs = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        L = b - a
        if i < n_ads:
            en = f"gte(t,{L:.3f})"
            fc.append(f"[cv{i}]trim={a}:{b},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={AD},"
                      f"boxblur=24:2:enable='{en}',eq=brightness=-0.18:enable='{en}'[bv{i}]")
            fc.append(f"[adv{i}]setpts=PTS-STARTPTS+{L:.3f}/TB[av{i}]")
            fc.append(f"[bv{i}][av{i}]overlay=0:{(H - rh) // 2}:eof_action=pass[sv{i}]")
            fc.append(f"[ca{i}]atrim={a}:{b},asetpts=PTS-STARTPTS,apad=pad_dur={AD}[pa{i}]")
            d_ms = int(round(L * 1000))
            fc.append(f"[ada{i}]adelay={d_ms}|{d_ms}[dl{i}]")
            fc.append(f"[pa{i}][dl{i}]amix=inputs=2:normalize=0:duration=first[sa{i}]")
        else:
            fc.append(f"[cv{i}]trim={a}:{b},setpts=PTS-STARTPTS[sv{i}]")
            fc.append(f"[ca{i}]atrim={a}:{b},asetpts=PTS-STARTPTS[sa{i}]")
        segs.append(f"[sv{i}][sa{i}]")
    fc.append("".join(segs) + f"concat=n={len(segs)}:v=1:a=1[vo][ao]")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", clip, "-i", ad_file, "-filter_complex", ";".join(fc),
           "-map", "[vo]", "-map", "[ao]", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
           "-pix_fmt", "yuv420p", "-r", "60", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    final = D + AD * n_ads
    starts, shift = [], 0.0
    for p in pts:
        starts.append(p + shift)
        shift += AD
    return {"final_duration": round(final, 2), "ad_starts": [round(s, 2) for s in starts],
            "ad_centers": [round(s + AD / 2, 2) for s in starts],
            "screen_area": round(W * rh / (W * H), 3)}


if __name__ == "__main__":
    print(json.dumps(insert_midroll(*sys.argv[1:4]), ensure_ascii=False))
