"""크랙빌더 이미지 생성 — Azure Functions.

server.py 의 nai_generate() 를 그대로 옮긴 것. 다른 점 둘:
  - 그림을 파일이 아니라 Blob 에 넣는다 (Functions 는 디스크가 없다)
  - 키는 앱 설정(NAI_KEY)에서 읽는다
"""
import base64
import hashlib
import io
import json
import logging
import os
import random
import zipfile

import azure.functions as func
from azure.storage.blob import BlobServiceClient, ContentSettings

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

NAI_URL = "https://image.novelai.net/ai/generate-image"
CONTAINER = "images"
NAI_UC = ("lowres, artistic error, film grain, scan artifacts, worst quality, "
          "bad quality, jpeg artifacts, very displeasing, chromatic aberration, "
          "extra digits, fewer digits, bad anatomy, bad hands, watermark, "
          "signature, logo, text")

# Cloudflare 가 User-Agent 없는 요청을 403 error code: 1010 으로 막는다.
# 키가 멀쩡해도 그렇다 — 키 문제로 오해하기 딱 좋은 자리라 적어 둔다.
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "*/*", "Origin": "https://novelai.net",
    "Referer": "https://novelai.net/",
}


def _blobs():
    cs = os.environ["AzureWebJobsStorage"]
    svc = BlobServiceClient.from_connection_string(cs)
    try:
        svc.create_container(CONTAINER, public_access="blob")
    except Exception:
        pass                       # 이미 있으면 그만
    return svc.get_container_client(CONTAINER)


def _save(blob, ext=".png"):
    """내용 해시로 이름을 짓는다 — 같은 그림은 한 번만 올라간다."""
    name = hashlib.sha256(blob).hexdigest()[:16] + ext
    c = _blobs()
    c.upload_blob(name, blob, overwrite=True,
                  content_settings=ContentSettings(
                      content_type="image/png",
                      cache_control="public, max-age=31536000, immutable"))
    return c.url + "/" + name


def _json(body, code=200):
    return func.HttpResponse(json.dumps(body, ensure_ascii=False),
                             status_code=code, mimetype="application/json")


@app.route(route="nai", methods=["POST"])
def nai(req: func.HttpRequest) -> func.HttpResponse:
    import urllib.error
    import urllib.request

    key = os.environ.get("NAI_KEY", "")
    if not key:
        return _json({"error": "NAI_KEY 앱 설정이 비어 있어요"}, 400)
    try:
        data = req.get_json()
    except ValueError:
        return _json({"error": "본문이 JSON 이 아니에요"}, 400)

    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return _json({"error": "프롬프트를 적어 주세요"}, 400)
    uc = str(data.get("uc") or "").strip() or NAI_UC
    seed = int(data.get("seed") or 0) or random.randint(1, 2 ** 32 - 1)

    body = {"input": prompt,
            "model": str(data.get("model") or "nai-diffusion-4-5-full"),
            "action": "generate",
            "parameters": {
                "params_version": 3,
                "width": int(data.get("width") or 1216),
                "height": int(data.get("height") or 832),
                "scale": float(data.get("scale") or 7),
                "cfg_rescale": 0.74, "uncond_scale": 0,
                "sampler": "k_euler_ancestral", "noise_schedule": "karras",
                "steps": int(data.get("steps") or 28),
                "n_samples": 1, "seed": seed,
                "ucPreset": 0, "qualityToggle": True, "autoSmea": False,
                "dynamic_thresholding": False, "controlnet_strength": 1,
                "legacy": False, "add_original_image": True,
                "legacy_v3_extend": False, "skip_cfg_above_sigma": None,
                "use_coords": False, "characterPrompts": [],
                "prefer_brownian": True,
                "deliberate_euler_ancestral_bug": False,
                # v4 계열은 아래 두 덩어리가 없으면 프롬프트를 조용히 무시한다
                "v4_prompt": {"caption": {"base_caption": prompt,
                                          "char_captions": []},
                              "use_coords": False, "use_order": True},
                "v4_negative_prompt": {"caption": {"base_caption": uc,
                                                   "char_captions": []},
                                       "legacy_uc": False},
                "negative_prompt": uc,
            }}

    headers = dict(BROWSER)
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = "Bearer " + key
    r = urllib.request.Request(NAI_URL, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            got = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        logging.warning("NAI %s: %s", e.code, detail)
        return _json({"error": f"NovelAI {e.code}: {detail[:150]}"}, 502)
    except Exception as e:
        return _json({"error": f"{type(e).__name__}: {e}"[:200]}, 502)

    # 응답은 PNG 가 아니라 ZIP 이다 — 이 API 의 유일한 함정
    z = zipfile.ZipFile(io.BytesIO(got))
    return _json({"url": _save(z.read(z.namelist()[0])), "seed": seed})


@app.route(route="ping", methods=["GET"])
def ping(req: func.HttpRequest) -> func.HttpResponse:
    """살아 있는지 + 키가 꽂혔는지. 키 값은 내려보내지 않는다."""
    return _json({"ok": True, "has_key": bool(os.environ.get("NAI_KEY"))})
