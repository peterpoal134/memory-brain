# -*- coding: utf-8 -*-
"""ComfyUI + Qwen Image 2.1 生图客户端。

默认走 SSH 隧道后的 http://127.0.0.1:8188。
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path


def _workflow(prompt: str, width: int, height: int, steps: int, seed: int) -> dict:
    return {
        "451": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
        "453": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
        "454": {"class_type": "VAELoader", "inputs": {
            "vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "452": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["453", 0], "prompt": prompt,
            "negative_prompt": "text, watermark, logo, low quality, deformed",
            "resolution": 1024, "images": []}},
        "456": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "458": {"class_type": "KSampler", "inputs": {
            "model": ["451", 0], "positive": ["452", 0], "negative": ["452", 1],
            "latent_image": ["456", 0], "seed": seed, "steps": steps, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "457": {"class_type": "VAEDecode", "inputs": {
            "samples": ["458", 0], "vae": ["454", 0]}},
        "461": {"class_type": "SaveImage", "inputs": {
            "images": ["457", 0], "filename_prefix": "assistant_draw"}},
    }


def generate(prompt: str, size: str = "standard", base: str = "http://127.0.0.1:8188",
             out_dir: str = "/tmp") -> Path:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    hd = str(size or "").lower() in ("hd", "high", "1024", "高清")
    width = height = 1024 if hd else 512
    steps = 24 if hd else 20
    seed = random.randint(1, 2**31 - 1)
    body = json.dumps({"prompt": _workflow(prompt, width, height, steps, seed),
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
    deadline = time.time() + (300 if hd else 180)
    item = None
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            with urllib.request.urlopen(base.rstrip("/") + f"/history/{pid}", timeout=15) as r:
                hist = json.loads(r.read().decode())
        except Exception:
            continue
        if pid in hist:
            item = hist[pid]
            break
    if not item:
        raise RuntimeError("生图超时")
    outputs = item.get("outputs") or {}
    images = []
    for node in outputs.values():
        images.extend(node.get("images") or [])
    if not images:
        raise RuntimeError("生图完成但没有图片")
    img = images[0]
    q = urllib.parse.urlencode({"filename": img.get("filename", ""),
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    with urllib.request.urlopen(base.rstrip("/") + "/view?" + q, timeout=60) as r:
        data = r.read()
    out = Path(out_dir) / f"assistant-draw-{int(time.time())}-{seed}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def _upload_image(path: str, base: str) -> str:
    """把本地参考图传到 ComfyUI input，返回 filename。"""
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


def _poll(pid: str, base: str, timeout: int = 300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            with urllib.request.urlopen(base.rstrip("/") + f"/history/{pid}", timeout=15) as r:
                hist = json.loads(r.read().decode())
        except Exception:
            continue
        if pid in hist:
            return hist[pid]
    raise RuntimeError("生图超时")


def generate_self(prompt: str, size: str = "standard", base: str = "http://127.0.0.1:8188",
                  reference: str = "", room: str = "", extras: list[str] | None = None,
                  outfit: str = "", accessories: list[str] | None = None,
                  out_dir: str = "/tmp") -> Path:
    """用参考图生图/改图；传 room 时把人物放进房间并改姿势。"""
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    if not reference or not Path(reference).exists():
        raise ValueError("参考图不存在")
    ref_name = _upload_image(reference, base)
    room_name = _upload_image(room, base) if room and Path(room).exists() else ""
    extra_names = [_upload_image(x, base) for x in (extras or []) if x and Path(x).exists()]
    outfit_name = _upload_image(outfit, base) if outfit and Path(outfit).exists() else ""
    acc_names = [_upload_image(x, base) for x in (accessories or []) if x and Path(x).exists()]
    prompt = ("Use the exact same character as image 1. Keep the same face, hairstyle, hair color, "
              "eye color, body proportions, blue dress, white apron and whale motif. Do not redesign "
              "the character. " + prompt)
    if room_name:
        prompt += " Place her naturally in the room from image 2, change the pose and scene as asked."
    if extra_names:
        prompt += (" Use the object in image 3 as the exact toy/prop; she should hold or wear it, "
                   "and its design and colors must stay the same.")
    outfit_index = 3 + len(extra_names)
    if outfit_name:
        prompt += (f" Image {outfit_index} is a flat-lay clothing item. Put it on her body as a real outfit, "
                   "preserving its cut, color and pattern; do not hold it as a prop; remove hanger/background.")
    acc_start = outfit_index + (1 if outfit_name else 0)
    if acc_names:
        ids = ", ".join(str(acc_start + i) for i in range(len(acc_names)))
        prompt += (f" Images {ids} are accessories; wear them on the body/hair/face as appropriate, "
                   "do not hold them in her hands.")
    seed = random.randint(1, 2**31 - 1)
    workflow = {
        "451": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
        "453": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
        "454": {"class_type": "VAELoader", "inputs": {
            "vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "469": {"class_type": "QwenImage21Cache", "inputs": {
            "model": ["451", 0], "device": "auto", "dtype": "default"}},
        "470": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "474": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["453", 0], "vae": ["454", 0], "prompt": prompt,
            "negative_prompt": ("different character, different face, different hair color, "
                                "different hairstyle, different eye color, different outfit design, "
                                "text, watermark, low quality, deformed"),
            "resolution": 1024, "images.image_1": ["470", 0]}},
        "458": {"class_type": "KSampler", "inputs": {
            "model": ["469", 0], "positive": ["474", 0], "negative": ["474", 1],
            "latent_image": ["474", 2], "seed": seed, "steps": 25, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "457": {"class_type": "VAEDecode", "inputs": {"samples": ["458", 0], "vae": ["454", 0]}},
        "461": {"class_type": "SaveImage", "inputs": {
            "images": ["457", 0], "filename_prefix": "assistant_self"}},
    }
    if room_name:
        workflow["471"] = {"class_type": "LoadImage", "inputs": {"image": room_name}}
        workflow["474"]["inputs"]["images.image_2"] = ["471", 0]
    for i, name in enumerate(extra_names[:3], start=3):
        nid = str(470 + i)
        workflow[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        workflow["474"]["inputs"][f"images.image_{i}"] = [nid, 0]
    if outfit_name:
        i = outfit_index
        nid = str(470 + i)
        workflow[nid] = {"class_type": "LoadImage", "inputs": {"image": outfit_name}}
        workflow["474"]["inputs"][f"images.image_{i}"] = [nid, 0]
    for j, name in enumerate(acc_names[:3], start=acc_start):
        nid = str(470 + j)
        workflow[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        workflow["474"]["inputs"][f"images.image_{j}"] = [nid, 0]
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
    item = _poll(pid, base, 300)
    images = []
    for node in (item.get("outputs") or {}).values():
        images.extend(node.get("images") or [])
    if not images:
        raise RuntimeError("生图完成但没有图片")
    img = images[0]
    q = urllib.parse.urlencode({"filename": img.get("filename", ""),
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    with urllib.request.urlopen(base.rstrip("/") + "/view?" + q, timeout=60) as r:
        data = r.read()
    out = Path(out_dir) / f"assistant-self-{int(time.time())}-{seed}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
