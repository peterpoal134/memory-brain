# -*- coding: utf-8 -*-
"""ComfyUI + MiniMax H3 5 秒视频客户端。"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path


def _workflow(prompt: str, seconds: int, seed: int) -> dict:
    # MiniMax H3 的帧数按 17k+5 网格，124 帧 ≈ 5.17 秒
    length = max(124, int(round(float(seconds) * 24)))
    return {
        "6": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "13": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "11": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "24": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "112": {"class_type": "MiniMaxH3TurboLoRA", "inputs": {
            "model": ["6", 0], "lora_name": "minimax_h3_turbo_v4_step600_ema.safetensors",
            "strength": 1.0, "low_vram": True}},
        "104": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["13", 0], "vae": ["11", 0], "prompt": prompt,
            "width": 608, "height": 352, "length": length}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["112", 0], "scheduler": "simple", "steps": 4, "denoise": 1.0}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "16": {"class_type": "BasicGuider", "inputs": {
            "model": ["112", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "MiniMaxH3TurboSampler", "inputs": {}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
            "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 1], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {
            "images": ["10", 0], "audio": ["23", 0], "fps": 24}},
        "92": {"class_type": "SaveVideo", "inputs": {
            "video": ["91", 0], "filename_prefix": "video/assistant_video", "format": "auto"}},
    }


def generate(prompt: str, seconds: int = 5, base: str = "http://127.0.0.1:8188",
             out_dir: str = "/tmp") -> Path:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    seconds = max(3, min(10, int(seconds or 5)))
    seed = random.randint(1, 2**31 - 1)
    body = json.dumps({"prompt": _workflow(prompt, seconds, seed),
                       "client_id": "astrbot-memory-brain"}, ensure_ascii=False).encode()
    req = urllib.request.Request(base.rstrip("/") + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            queued = json.loads(r.read().decode())
    except Exception as exc:
        raise RuntimeError(f"连不上 ComfyUI（{base}）：{exc}") from exc
    pid = queued.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 没返回 prompt_id：{queued}")
    deadline = time.time() + 600
    item = None
    while time.time() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(base.rstrip("/") + f"/history/{pid}", timeout=20) as r:
                hist = json.loads(r.read().decode())
        except Exception:
            continue
        if pid in hist:
            item = hist[pid]
            break
    if not item:
        raise RuntimeError("视频生成超时")
    videos = []
    for node in (item.get("outputs") or {}).values():
        videos.extend(node.get("images") or [])
    if not videos:
        raise RuntimeError("视频完成但没有文件")
    v = videos[0]
    q = urllib.parse.urlencode({"filename": v.get("filename", ""),
                                "subfolder": v.get("subfolder", ""),
                                "type": v.get("type", "output")})
    with urllib.request.urlopen(base.rstrip("/") + "/view?" + q, timeout=120) as r:
        data = r.read()
    out = Path(out_dir) / f"assistant-video-{int(time.time())}-{seed}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def _upload_image(path: str, base: str) -> str:
    data = Path(path).read_bytes()
    boundary = "----assistant" + str(random.randint(100000, 999999))
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="character.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())
    return str(out.get("name") or "character.png")


def _poll(pid: str, base: str, timeout: int = 600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(base.rstrip("/") + f"/history/{pid}", timeout=20) as r:
                hist = json.loads(r.read().decode())
        except Exception:
            continue
        if pid in hist:
            return hist[pid]
    raise RuntimeError("视频生成超时")


def generate_self(prompt: str, seconds: int = 5, base: str = "http://127.0.0.1:8188",
                  reference: str = "", out_dir: str = "/tmp") -> Path:
    """用参考图生视频，第一帧就是她的形象。"""
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    if not reference or not Path(reference).exists():
        raise ValueError("参考图不存在")
    ref_name = _upload_image(reference, base)
    prompt = ("Keep the exact same character as the first frame. Same face, hairstyle, hair color, "
              "eye color, body proportions, blue dress, white apron and whale motif. Do not redesign "
              "the character. Action: " + prompt)
    seconds = max(3, min(10, int(seconds or 5)))
    try:
        from PIL import Image as _PILImage
        _w, _h = _PILImage.open(reference).size
        width, height = (608, 352) if _w >= _h else (352, 608)
    except Exception:
        width, height = 352, 608
    length = max(124, int(round(float(seconds) * 24)))
    seed = random.randint(1, 2**31 - 1)
    workflow = {
        "6": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "13": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "11": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "24": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "200": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "112": {"class_type": "MiniMaxH3TurboLoRA", "inputs": {
            "model": ["6", 0], "lora_name": "minimax_h3_turbo_v4_step600_ema.safetensors",
            "strength": 1.0, "low_vram": True}},
        "104": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["13", 0], "vae": ["11", 0], "prompt": prompt,
            "width": width, "height": height, "length": length, "first_frame": ["200", 0]}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["112", 0], "scheduler": "simple", "steps": 4, "denoise": 1.0}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "16": {"class_type": "BasicGuider", "inputs": {
            "model": ["112", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "MiniMaxH3TurboSampler", "inputs": {}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
            "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 1], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {
            "images": ["10", 0], "audio": ["23", 0], "fps": 24}},
        "92": {"class_type": "SaveVideo", "inputs": {
            "video": ["91", 0], "filename_prefix": "video/assistant_selfvideo", "format": "auto"}},
    }
    body = json.dumps({"prompt": workflow, "client_id": "astrbot-memory-brain"}, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                base.rstrip("/") + "/prompt", data=body,
                headers={"Content-Type": "application/json"}, method="POST"), timeout=30) as r:
            queued = json.loads(r.read().decode())
    except Exception as exc:
        raise RuntimeError(f"连不上 ComfyUI（{base}）：{exc}") from exc
    pid = queued.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 没返回 prompt_id：{queued}")
    item = _poll(pid, base, 600)
    videos = []
    for node in (item.get("outputs") or {}).values():
        videos.extend(node.get("images") or [])
    if not videos:
        raise RuntimeError("视频完成但没有文件")
    v = videos[0]
    q = urllib.parse.urlencode({"filename": v.get("filename", ""),
                                "subfolder": v.get("subfolder", ""),
                                "type": v.get("type", "output")})
    with urllib.request.urlopen(base.rstrip("/") + "/view?" + q, timeout=120) as r:
        data = r.read()
    out = Path(out_dir) / f"assistant-selfvideo-{int(time.time())}-{seed}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
