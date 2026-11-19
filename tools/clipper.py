#!/usr/bin/env python3
"""Сборка вертикального клипа 1080x1920 из записи стрима по job-файлу.

    python3 tools/clipper.py job.json

job.json:
{
  "source": "seg.mp4",            # запись (1920x1080)
  "source_offset": 1560,          # абсолютное время начала файла в VOD (для слов/частей), по умолчанию 0
  "out": "clips/x.mp4",
  "parts": [[a, b], ...],         # куски в абсолютном времени VOD
  "cam": [[x,y,w,h], ...],        # прямоугольник вебки на кадре для каждой части (или один для всех)
  "game": [[x,y,w,h], ...],       # область геймплея без вебки/рекламы стримера (или одна для всех)
  "words": "words.json",          # [[t_abs, "слово"], ...] — субтитры
  "hook": "ЗАГОЛОВОК", "hook_sub": "подзаголовок",
  "banner": "playerok_banner" | null,
  "midroll": "natria_vpn" | null,
  "series": {"title": "...", "part": 1, "total": 7} | null
}
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ads  # noqa: E402

ROOT = ads.ROOT
W, H = 1080, 1920
FONTS = "/usr/share/fonts/opentype/inter"
BG = "0x070B18"
ACCENT = "0x2E6BFF"

# Раскладки: (cam_h, subs_h, game_h); всё, что ниже — нижняя зона (баннер / плашки)
LAYOUTS = {
    "plain": (620, 140, 840),    # без постоянного баннера: нижняя зона 320 под хук/серию
    "banner": (500, 120, 608),   # с баннером >= 1/6 экрана: нижняя зона 692
}


def fit_crop(rect, aspect, vbias=0.5):
    """Вписать кроп с соотношением aspect в rect (x,y,w,h)."""
    x, y, w, h = rect
    if w / h > aspect:
        nw = int(h * aspect) // 2 * 2
        return x + (w - nw) // 2, y, nw, h
    nh = int(w / aspect) // 2 * 2
    return x, y + int((h - nh) * vbias), w, nh


def ts(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def esc(s):
    return s.replace("{", "(").replace("}", ")")


def map_time(parts, T):
    acc = 0.0
    for a, b in parts:
        if a - 0.05 <= T <= b + 0.05:
            return acc + max(0.0, T - a)
        acc += b - a
    return None


def make_ass(job, dur, layout, path):
    cam_h, subs_h, game_h = LAYOUTS[layout]
    subs_y = cam_h + subs_h // 2
    zone_top = cam_h + subs_h + game_h
    words = []
    if job.get("words"):
        src = job["words"]
        raw = json.load(open(src)) if isinstance(src, str) else src
        for T, w in raw:
            t = map_time(job["parts"], T)
            if t is not None and w.strip():
                words.append([t, w.strip()])
    words.sort()
    chunks, cur = [], []
    for t, w in words:
        if cur and (len(" ".join(x[1] for x in cur) + " " + w) > 18 or len(cur) >= 3
                    or t - cur[-1][0] > 0.9 or re.search(r"[.!?]$", cur[-1][1])):
            chunks.append(cur)
            cur = []
        cur.append([t, w])
    if cur:
        chunks.append(cur)
    fs = 74 if layout == "plain" else 66
    L = []
    for ci, c in enumerate(chunks):
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else dur
        end = min(nxt, c[-1][0] + 1.1)
        for wi, (t, w) in enumerate(c):
            en = c[wi + 1][0] if wi + 1 < len(c) else end
            if en <= t:
                continue
            txt = " ".join(("{\\c&H00E5FF&}" if j == wi else "{\\c&HFFFFFF&}") + esc(x.upper())
                           for j, (_, x) in enumerate(c))
            pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if wi == 0 else ""
            L.append(f"Dialogue: 1,{ts(t)},{ts(en)},Sub,,0,0,0,,{{\\an5\\pos(540,{subs_y}){pop}}}{txt}")
    # хук: в нижней зоне (plain) или поверх верха геймплея (banner, т.к. баннер занимает зону весь ролик)
    hook_y = zone_top + 140 if layout == "plain" else cam_h + subs_h + 90
    if job.get("hook"):
        L.append(f"Dialogue: 3,{ts(0)},{ts(3)},Hook,,0,0,0,,{{\\an5\\pos(540,{hook_y})\\fad(0,250)"
                 f"\\fscx80\\fscy80\\t(0,150,\\fscx100\\fscy100)}}{esc(job['hook'])}")
        if job.get("hook_sub"):
            L.append(f"Dialogue: 3,{ts(0)},{ts(3)},HookSub,,0,0,0,,{{\\an5\\pos(540,{hook_y + 80})\\fad(0,250)}}"
                     f"{esc(job['hook_sub'])}")
    ser = job.get("series")
    if ser:
        lab = f"ЧАСТЬ {ser['part']}/{ser['total']}"
        if layout == "plain":
            L.append(f"Dialogue: 2,{ts(3)},{ts(dur)},Series,,0,0,0,,{{\\an5\\pos(540,{zone_top + 95})}}{lab}")
            L.append(f"Dialogue: 2,{ts(3)},{ts(dur)},SeriesT,,0,0,0,,{{\\an5\\pos(540,{zone_top + 165})}}"
                     f"{esc(ser['title'])}")
        else:
            L.append(f"Dialogue: 2,{ts(0)},{ts(dur)},SeriesS,,0,0,0,,{{\\an9\\pos(1060,{cam_h + subs_h + 14})}}{lab}")
        if ser["part"] < ser["total"]:
            L.append(f"Dialogue: 4,{ts(dur - 2.2)},{ts(dur)},Next,,0,0,0,,{{\\an5\\pos(540,{cam_h + subs_h + game_h // 2})"
                     f"\\fad(150,0)\\fscx70\\fscy70\\t(0,180,\\fscx100\\fscy100)}}ПРОДОЛЖЕНИЕ В ЧАСТИ {ser['part'] + 1} →")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Inter Display Black,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,6,2,5,30,30,0,1
Style: Hook,Inter Display Black,82,&H0000E5FF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,7,0,5,30,30,0,1
Style: HookSub,Inter Display ExtraBold,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,5,30,30,0,1
Style: Series,Inter Display Black,72,&H0000E5FF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,6,0,5,30,30,0,1
Style: SeriesT,Inter Display ExtraBold,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,5,30,30,0,1
Style: SeriesS,Inter Display Black,40,&H0000E5FF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,3,10,0,9,30,30,0,1
Style: Next,Inter Display Black,64,&H00FFFFFF,&H00FFFFFF,&H00FF6B2E,&H00000000,0,0,0,0,100,100,0,0,3,18,0,5,30,30,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    open(path, "w").write(head + "\n".join(L) + "\n")
    # паузы в речи: перед словом, если до него >= 0.8 с тишины
    return [round(words[i + 1][0] - 0.15, 2) for i in range(len(words) - 1)
            if words[i + 1][0] - words[i][0] >= 0.8]


def render(job):
    reg = ads.load_registry()
    banner = job.get("banner")
    brands = {reg["ads"][k]["brand"] for k in (banner, job.get("midroll")) if k}
    for k in (banner, job.get("midroll")):
        if k and reg["ads"][k].get("exclusive") and len(brands) > 1:
            raise SystemExit(f"{k}: нельзя совмещать с рекламой других брендов")
    layout = job.get("layout") or ("banner" if banner else "plain")
    cam_h, subs_h, game_h = LAYOUTS[layout]
    zone_top = cam_h + subs_h + game_h
    zone_h = H - zone_top
    parts = job["parts"]
    off = job.get("source_offset", 0)
    n = len(parts)
    cams = job["cam"] if isinstance(job["cam"][0], list) else [job["cam"]] * n
    games = job["game"] if isinstance(job["game"][0], list) else [job["game"]] * n
    dur = sum(b - a for a, b in parts)
    out = job["out"]
    tmp = out + ".base.mp4" if job.get("midroll") else out
    ass_path = out + ".ass"
    pauses = make_ass(job, dur, layout, ass_path)

    ins, fc = [], []
    for i, (a, b) in enumerate(parts):
        ins += ["-ss", f"{a - off:.3f}", "-t", f"{b - a:.3f}", "-i", job["source"]]
        cx, cy, cw, ch = fit_crop(cams[i], W / cam_h, 0.4)
        gx, gy, gw, gh = fit_crop(games[i], W / game_h, 0.5)
        fc.append(f"[{i}:v]fps=60,split[s{i}a][s{i}b];"
                  f"[s{i}a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:flags=lanczos,unsharp=5:5:0.6[cam{i}];"
                  f"[s{i}b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{game_h}:flags=lanczos[gp{i}];"
                  f"color=c={BG}:s={W}x{H}:r=60:d={b - a:.3f}[bg{i}];"
                  f"[bg{i}][cam{i}]overlay=0:0[t{i}];[t{i}][gp{i}]overlay=0:{cam_h + subs_h}:shortest=1,setsar=1[v{i}];"
                  f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    lines = (f"drawbox=x=0:y={cam_h - 2}:w={W}:h=4:color={ACCENT}@0.9:t=fill,"
             f"drawbox=x=0:y={cam_h + subs_h - 2}:w={W}:h=4:color={ACCENT}@0.9:t=fill,"
             f"drawbox=x=0:y={zone_top - 2}:w={W}:h=4:color={ACCENT}@0.9:t=fill")
    fc.append(f"[vc]{lines}[vd]")
    last = "vd"
    if banner:
        ad = reg["ads"][banner]
        assert ad["type"] == "banner"
        ins += ["-stream_loop", "-1", "-i", os.path.join(ROOT, ad["file"])]
        ck = ad["chroma_key"]
        bb = ad["content_bbox"]
        # рисунок баннера (bbox) вписываем в нижнюю зону с отступами, проверяем площадь
        bh = min(zone_h - 150, int((W - 40) / (bb["w"] / bb["h"])))
        bw = int(bh * bb["w"] / bb["h"]) // 2 * 2
        area = bw * bh / (W * H)
        assert area >= ad["placement"]["min_screen_area"], f"баннер {area:.3f} < нормы"
        by = zone_top + 20
        fc.append(f"[{n}:v]fps=60,crop={bb['w']}:{bb['h']}:{bb['x']}:{bb['y']},"
                  f"colorkey={ck['color']}:{ck['similarity']}:{ck['blend']},despill=type=green,"
                  f"scale={bw}:{bh}:flags=lanczos,trim=0:{dur:.3f},setpts=PTS-STARTPTS[ban]")
        fc.append(f"[{last}][ban]overlay={(W - bw) // 2}:{by}:eof_action=pass[vb]")
        last = "vb"
        print(f"banner {banner}: {bw}x{bh} = {area:.1%} экрана", file=sys.stderr)
    fc.append(f"[{last}]ass={ass_path}:fontsdir={FONTS},format=yuv420p[vo]")
    fc.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[ao]")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", ";".join(fc),
                    "-map", "[vo]", "-map", "[ao]", "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "21", "-profile:v", "high", "-r", "60", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", tmp], check=True)
    info = {"out": out, "layout": layout, "duration": round(dur, 2), "banner": banner}
    if job.get("midroll"):
        info["midroll"] = ads.insert_midroll(tmp, job["midroll"], out, pauses if job.get("words") else None)
        os.remove(tmp)
    os.remove(ass_path)
    return info


if __name__ == "__main__":
    for p in sys.argv[1:]:
        jobs = json.load(open(p))
        for j in (jobs if isinstance(jobs, list) else [jobs]):
            print(json.dumps(render(j), ensure_ascii=False), flush=True)
