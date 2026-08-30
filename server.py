#!/usr/bin/env python3
"""크랙 로컬 빌더 서버. 의존성 0 (stdlib만).

실행:  python3 server.py            → http://127.0.0.1:8787
자체검사: python3 server.py --selftest
"""
import json
import os
import re
import hashlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# 데이터 위치. 다른 드라이브/폴더에 두려면 CRACK_DATA 환경변수만 지정한다.
#   Windows:  set CRACK_DATA=D:\크랙데이터 && python server.py
#   Linux:    CRACK_DATA=~/크랙데이터 python3 server.py
ROOT = Path(__file__).parent
DATA = Path(os.environ.get("CRACK_DATA") or ROOT / "data").expanduser()
DRAFTS = DATA / "drafts"
CHATS = DATA / "chats"
BACKUPS = DATA / "backups"
PROVIDERS = DATA / "providers.json"   # 손으로 추가한 제공사 (키는 여기 안 들어간다)
PERSONAS = DATA / "personas.json"     # 대화 프로필 목록
IMAGES = DATA / "images"              # 올린 그림. 파일로 둔다 — base64 로
                                      # 스토리 JSON 에 넣으면 500장에 터진다
MAX_DRAFTS = 0  # 0 = 무제한. 지우는 건 사용자가 정한다
MAX_CHATS = 0
KEEP_BACKUPS = 14  # 하루 1개씩 이만큼 보관
SCAN_TURNS = 4  # 최근 몇 턴을 키워드 스캔할지
EVENT_EVERY = 20  # 몇 턴마다 사건 기록을 남길지

# ── 주입 규칙 ────────────────────────────────────────────────
# 프롬프트는 매 턴 항상. 키워드북 노트는 키워드가 최근 SCAN_TURNS 턴에
# 등장했을 때만. 매 턴 새로 판정한다(sticky 없음 — 이모지 문체 모드가
# 스캔 윈도우를 벗어나면 자연히 꺼져야 하므로).

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")


def keyword_hits(kw: str, haystack: str) -> bool:
    """부분문자열 포함. 2자 미만 한글/영문 키워드는 오발동이 심해 무시.
    이모지는 1자라도 허용 — 문체 모드 스위치가 이모지 한 글자다."""
    kw = kw.strip()
    if not kw:
        return False
    if len(kw) < 2 and not EMOJI.search(kw):
        return False
    return kw.lower() in haystack.lower()


def in_scope(note, start_name):
    """이 노트가 지금 시작 설정에서 쓰이는가.
    scope 가 'all'(또는 비어 있음)이면 항상. 아니면 이름이 같을 때만."""
    sc = note.get("scope") or "all"
    return sc == "all" or sc == start_name


def active_notes(notes, history, user_input, scan_turns=SCAN_TURNS,
                 start_name=None):
    """발동한 노트를 등록순으로 반환. history는 [{role, content}, ...].
    start_name 을 주면 그 시작 설정에 걸린 노트만 본다."""
    window = history[-scan_turns * 2:] if scan_turns else history
    hay = "\n".join(m.get("content", "") for m in window) + "\n" + (user_input or "")
    return [n for n in notes
            if in_scope(n, start_name)
            and any(keyword_hits(k, hay) for k in n.get("keywords", []))]


# ── 이미지 ───────────────────────────────────────────────────
# 모델은 URL 을 만들지 않는다. {img::3} 처럼 번호만 고르고, 번호 →
# 주소 대응은 스토리에 저장해 둔다. 주소가 바뀌어도 옛 대화가 살아난다.
IMG_RE = re.compile(r"\{img::\s*(\d+)\s*\}")

# 언제 넣을지. 스토리 프롬프트가 아니라 여기서 붙는다 — 스토리마다
# 같은 문장을 베껴 적게 하지 않기 위해서.
IMG_RULES = {
    "off": "이미지는 사용자가 요청할 때만 {img::번호} 로 낸다.",
    "fit": ("장면이 목록의 이름과 맞아떨어질 때 그 번호를 {img::번호} 로 낸다. "
            "억지로 끼워 맞추지 않는다 — 맞는 게 없으면 내지 않는다."),
    "each": ("답변마다 장면에 가장 맞는 이미지 1장을 {img::번호} 로 낸다. "
             "서술 뒤 줄바꿈하고 토큰만 한 줄에 적는다."),
}


def img_map(story):
    """{번호: (이름, 주소)}. 번호를 적어둔 것이 먼저고, 번호 없는 것은
    남는 자리를 순서대로 채운다 — 안 그러면 적어둔 번호를 덮어써 버린다."""
    items = [im for im in (story.get("images") or []) if isinstance(im, dict)]
    out, rest = {}, []
    for im in items:
        try:
            out[int(im["n"])] = im
        except (KeyError, TypeError, ValueError):
            rest.append(im)
    n = 1
    for im in rest:
        while n in out:
            n += 1
        out[n] = im
    return {k: (str(v.get("label") or "").strip(),
                str(v.get("url") or "").strip()) for k, v in out.items()}


def img_lines(story):
    """프롬프트에 넣을 목록. 이름 없는 건 뺀다 — 모델이 고를 근거가 없다."""
    m = img_map(story)
    return " ".join(f"{n}={lb}" for n, (lb, _) in sorted(m.items()) if lb)


def img_to_label(text, story):
    """모델에게 보낼 때. {img::3} → [이미지: 칸나 무표정].
    번호만 남기면 자기가 뭘 골랐는지 모른 채 다음 턴을 쓴다."""
    m = img_map(story)
    def one(mo):
        n = int(mo.group(1))
        lb = m.get(n, ("", ""))[0]
        return f"[이미지: {lb}]" if lb else f"[이미지 {n}]"
    return IMG_RE.sub(one, str(text or ""))


# 올릴 수 있는 것만. 확장자로 정하지 않는다 — 파일 앞머리(매직 넘버)를 본다.
IMG_KINDS = [
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
]
MAX_IMG = 8 * 1024 * 1024


def sniff_image(blob):
    """(확장자, mime) 또는 None. webp 는 RIFF....WEBP 라 따로 본다."""
    for magic, ext, mime in IMG_KINDS:
        if blob.startswith(magic):
            return ext, mime
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def save_image(blob):
    """내용 해시로 저장한다 — 같은 그림을 두 번 올려도 파일 하나."""
    kind = sniff_image(blob)
    if not kind:
        raise ValueError("PNG · JPG · GIF · WEBP 만 올릴 수 있어요")
    if len(blob) > MAX_IMG:
        raise ValueError("8MB 까지만 올릴 수 있어요")
    ext, _ = kind
    name = hashlib.sha256(blob).hexdigest()[:16] + ext
    IMAGES.mkdir(parents=True, exist_ok=True)
    dst = IMAGES / name
    if not dst.exists():
        tmp = IMAGES / f".{name}.tmp"
        tmp.write_bytes(blob)
        tmp.replace(dst)
    return name


def persona_name(persona):
    """persona 는 문자열(이름만) 또는 {name, profile} 둘 다 받는다."""
    if isinstance(persona, dict):
        return str(persona.get("name") or "").strip()
    return str(persona or "").strip()


def subst(text, story, persona=""):
    """{char} = 지금 서술 중인 캐릭터(스토리 이름), {user} = 쓰는 페르소나 이름.
    크랙 표기를 그대로 따른다: {{char}} 도 같이 받는다."""
    char = (story.get("title") or "").strip() or "그"
    user = persona_name(persona) or "당신"
    out = str(text or "")
    for k, v in (("char", char), ("user", user)):
        out = out.replace("{{" + k + "}}", v).replace("{" + k + "}", v)
    return out


def story_start(story):
    """지금 쓰는 시작 설정. 채팅은 늘 첫 번째를 쓴다."""
    starts = story.get("starts") or []
    return starts[0] if starts else {}


# 한글은 형태소 분석기가 없으면 낱말을 못 자른다. 조사가 붙어도 앞부분은
# 그대로라 2글자 조각으로 겹침을 세면 이름·지명은 충분히 잡힌다.
# ponytail: 2-gram 겹침. 동의어("우산"↔"양산")는 못 잡는다.
#           그게 문제가 되면 임베딩으로 올린다.
_STOP = {"그리고", "하지만", "그래서", "그러나", "이것", "저것", "무엇",
         "user", "assistant", "그런", "이런", "저런", "합니다", "했다"}


def _grams(text, n=2):
    t = re.sub(r"[^0-9A-Za-z가-힣]+", " ", str(text or "")).lower()
    out = set()
    for w in t.split():
        if w in _STOP or len(w) < n:
            continue
        out.update(w[i:i + n] for i in range(len(w) - n + 1))
    return out


def split_chronicle(chronicle):
    """줄거리를 덩어리로 자른다. '■' 블록 안에 '- 이름' 문단이 있으면
    그 문단까지 쪼갠다(인물별). 제목 줄은 각 덩어리에 붙여 둔다."""
    text = (chronicle or "").strip()
    if not text:
        return []
    out = []
    for blk in re.split(r"\n(?=■)", text):
        blk = blk.strip()
        if not blk:
            continue
        lines = blk.split("\n")
        head = lines[0] if lines[0].startswith("■") else ""
        body = "\n".join(lines[1:]) if head else blk
        # 들여쓰기 없는 '- ' 줄 = 인물 문단의 시작
        parts = [x.strip() for x in re.split(r"\n(?=- )", body) if x.strip()]
        if head and len(parts) > 1:
            out += [f"{head}\n{x}" for x in parts]
        else:
            out.append(blk)
    return out


def relevant_chronicle(chronicle, history, user_input, top=3,
                       scan_turns=SCAN_TURNS):
    """최근 대화와 겹치는 덩어리만 고른다. 겹치는 게 없으면 빈 문자열.
    덩어리가 top 개 이하면 그냥 전부 준다 — 고르는 값이 없다."""
    blocks = split_chronicle(chronicle)
    if not blocks:
        return ""
    if len(blocks) <= top:
        return "\n\n".join(blocks)
    window = history[-scan_turns * 2:] if scan_turns else history
    hay = _grams("\n".join(m.get("content", "") for m in window)
                 + "\n" + (user_input or ""))
    if not hay:
        return ""
    scored = []
    for i, b in enumerate(blocks):
        # 제목 줄(■ ~20턴)은 점수에 넣지 않는다 — 숫자만 겹쳐서 다 뜬다
        body = "\n".join(l for l in b.split("\n") if not l.startswith("■"))
        n = len(hay & _grams(body))
        if n:
            scored.append((n, i, b))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    # 원래 순서를 지킨다 — 시간순이 깨지면 읽는 쪽이 헷갈린다
    picked = sorted(scored[:top], key=lambda x: x[1])
    return "\n\n".join(b for _, _, b in picked)


def build_system(story, history=None, user_input="", persona="", usernote="",
                 memory="", chronicle="", chron_all=False, img_off=False):
    """매 턴 호출. system 프롬프트 문자열을 만든다.
    순서: 프롬프트 → 가이드 → 사건 기록 → 장기 기억 → 프로필 → 유저 노트
          → 발동한 노트.
    사건 기록은 기본적으로 지금 대화와 겹치는 덩어리만 넣는다.
    chron_all=True 면 통째로."""
    start = story_start(story)
    parts = [story.get("prompt", "").strip()]
    # 시작 설정의 플레이 가이드도 매 턴 들어간다
    if (start.get("guide") or "").strip():
        parts.append(start["guide"].strip())
    # 이미지 — 규칙과 목록 모두 여기서 넣는다. 스토리 프롬프트에 적을 필요 없다.
    # 스토리에서 끄거나(imgOn=False) 방에서 끄면 목록째 뺀다 — 규칙만 남기면
    # 모델이 없는 번호를 지어낸다.
    imgs = "" if (img_off or story.get("imgOn") is False) else img_lines(story)
    if imgs:
        parts.append(f"[이미지]\n{IMG_RULES.get(story.get('imgRule'), IMG_RULES['fit'])}\n"
                     "번호만 고른다. 주소·파일명·확장자를 쓰지 않는다. "
                     "목록에 없는 번호✕\n" + imgs)
    # 사건 기록 — 쌓인 연대기 중 지금 이야기와 겹치는 부분만
    chron = (chronicle or "").strip()
    if chron and not chron_all:
        chron = relevant_chronicle(chron, history or [], user_input)
    if chron:
        parts.append(f"[지금까지 있었던 일]\n{chron}")
    # 장기 기억 — 창 밖으로 밀려난 옛 대화의 요약
    if (memory or "").strip():
        parts.append(f"[장기 기억]\n{memory.strip()}")
    # 대화 프로필 — {user} 가 누구인지
    prof = (persona.get("profile") if isinstance(persona, dict) else "") or ""
    nm = persona_name(persona)
    if prof.strip():
        parts.append(f"[{nm or '{user}'} 프로필]\n{prof.strip()}")
    # 유저 노트 — 이 방에만 거는 규칙
    if (usernote or "").strip():
        parts.append(f"[유저 노트]\n{usernote.strip()}")
    for n in active_notes(story.get("notes", []), history or [], user_input,
                          start_name=start.get("name")):
        parts.append(f"[{n.get('title','')}]\n{n.get('info','')}".strip())
    # 치환은 마지막에 한 번 — 프롬프트·가이드·프로필·노트 전부 같은 규칙을 탄다
    return subst("\n\n".join(p for p in parts if p), story, persona)


# ── 모델 ─────────────────────────────────────────────────────
# 모델 목록을 코드에 적지 않는다. 두 곳에서 제공사를 찾는다.
#   1) ~/.hermes/config.yaml 의 custom_providers (사용자가 직접 등록한 것)
#   2) ~/.hermes/.env 에 있는 *_API_KEY  → 아래 표로 엔드포인트를 안다
# 그 다음 각 제공사의 /v1/models 를 실제로 물어 모델을 통째로 가져온다.
# 실패하면 숨기지 않고 이유를 같이 돌려준다(키 오타를 눈으로 봐야 하므로).
HERMES = Path.home() / ".hermes"
CACHE = DATA / "models-cache.json"

# 키 이름 → 엔드포인트. 이것만 있으면 .env 에 키를 넣는 순간 자동으로 붙는다.
# 여기 없는 제공사는 config.yaml 의 custom_providers 로 등록하면 된다.
ENDPOINTS = {
    "KIMI_API_KEY":      ("moonshot", "Kimi",   "🌙", "#7aa2f7", "https://api.moonshot.ai/v1"),
    "MOONSHOT_API_KEY":  ("moonshot", "Kimi",   "🌙", "#7aa2f7", "https://api.moonshot.ai/v1"),
    "XAI_API_KEY":       ("xai",      "Grok",   "✖", "#e8e8e8", "https://api.x.ai/v1"),
    "OPENAI_API_KEY":    ("openai",   "OpenAI", "◍", "#10a37f", "https://api.openai.com/v1"),
    "ANTHROPIC_API_KEY": ("anthropic-api", "Anthropic API", "◆", "#d97757", "https://api.anthropic.com/v1"),
    "DEEPSEEK_API_KEY":  ("deepseek", "DeepSeek", "🐳", "#4d9de0", "https://api.deepseek.com/v1"),
    "GROQ_API_KEY":      ("groq",     "Groq",   "⚡", "#f55036", "https://api.groq.com/openai/v1"),
    "MISTRAL_API_KEY":   ("mistral",  "Mistral", "🌬", "#ff7000", "https://api.mistral.ai/v1"),
    "OPENROUTER_API_KEY": ("openrouter", "OpenRouter", "🔀", "#8b5cf6", "https://openrouter.ai/api/v1"),
    "TOGETHER_API_KEY":  ("together", "Together", "🤝", "#0f6fff", "https://api.together.xyz/v1"),
    "HERMES_CUSTOM_INTEGRATE_API_NVIDIA_COM_API_KEY":
        ("nvidia", "NVIDIA", "🟩", "#76b900", "https://integrate.api.nvidia.com/v1"),
    "HERMES_CUSTOM_API_UPSTAGE_AI_API_KEY":
        ("upstage", "Upstage", "☀️", "#ffb300", "https://api.upstage.ai/v1"),
    "UPSTAGE_API_KEY":   ("upstage", "Upstage", "☀️", "#ffb300", "https://api.upstage.ai/v1"),
}

_catalog = []          # [{id,name,icon,color,desc,models:[{id,name}]}]
_by_model = {}         # "provider/model" -> 제공사 dict
_problems = []         # [{name, reason}] — 키가 있는데 실패한 제공사


def _env_all():
    """~/.hermes/.env + 실제 환경변수를 합쳐 {이름: 값} 으로."""
    out = {}
    f = HERMES / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                if v.strip():
                    out[k.strip()] = v.strip()
    for k, v in os.environ.items():
        if k.endswith("_API_KEY") and v:
            out[k] = v
    return out


def _env(name):
    return _env_all().get(name, "") if name else ""


def _pkey(p):
    """제공사의 실제 키. 손으로 넣은 것이 먼저, 없으면 .env 의 key_env."""
    return p.get("key") or _env(p.get("key_env"))


def hermes_ok():
    return (HERMES / ".env").exists() or (HERMES / "config.yaml").exists()


def load_list(path):
    """JSON 배열 파일 하나. 없거나 깨졌으면 빈 목록."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_list(path, items, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)
    if private:
        # 키가 들어있는 파일이다 — 소유자만 읽게 한다
        # ponytail: 평문 저장. 127.0.0.1 단일 사용자 도구라 이 선까지.
        #           여럿이 쓰게 되면 OS 키체인으로 옮긴다.
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


def load_providers():
    return load_list(PROVIDERS)


def save_providers(items):
    save_list(PROVIDERS, items, private=True)


def discover_providers():
    """설정에서 제공사를 찾아낸다. 목록을 코드에 적지 않는다."""
    found, seen = [], set()

    # 0) 설정 페이지에서 손으로 추가한 것 — 가장 우선
    for p in load_providers():
        if not p.get("url"):
            continue
        found.append({"id": p.get("id") or p["url"], "name": p.get("name") or p["url"],
                      "icon": p.get("icon") or "➕", "color": "#9a9a9a",
                      "url": p["url"].rstrip("/"), "key_env": None,
                      "key": p.get("key", ""), "manual": True,
                      "src": "manual"})

    # 1) config.yaml 의 custom_providers — 사용자가 직접 등록한 것이 최우선
    cfg = HERMES / "config.yaml"
    if cfg.exists():
        txt = cfg.read_text(encoding="utf-8")
        block = re.search(r"^custom_providers:\n((?:[ \t]+.*\n|\n)*)", txt, re.M)
        if block:
            for item in re.split(r"\n\s*-\s+", "\n" + block.group(1)):
                url = re.search(r"base_url:\s*(\S+)", item)
                key = re.search(r"key_env:\s*(\S+)", item)
                nm = re.search(r"name:\s*(.+)", item)
                if not (url and key):
                    continue
                u = url.group(1).rstrip("\\/")
                if "/v1" not in u:          # 채팅 엔드포인트가 아닌 것(이미지 등)은 건너뛴다
                    continue
                pid = key.group(1).lower()
                found.append({"id": pid, "name": (nm.group(1).strip() if nm else pid),
                              "icon": "◈", "color": "#9a9a9a",
                              "url": u, "key_env": key.group(1), "src": "hermes"})
                seen.add(key.group(1))

    # 2) .env 의 키들 — 아는 엔드포인트면 자동으로 켠다
    for k in _env_all():
        if k in seen or k not in ENDPOINTS:
            continue
        pid, name, icon, color, url = ENDPOINTS[k]
        if any(f["id"] == pid for f in found):
            continue
        found.append({"id": pid, "name": name, "icon": icon, "color": color,
                      "url": url, "key_env": k, "src": "hermes"})

    # 3) 키가 필요 없는 것들 — 떠 있을 때만 붙는다
    if shutil.which("claude"):
        found.insert(0, {"id": "anthropic", "name": "Anthropic", "icon": "◆",
                         "color": "#d97757", "kind": "claude", "src": "cli",
                         "desc": "Claude Code CLI 구독을 그대로 쓴다"})
    if AGY:
        found.insert(0, {"id": "antigravity", "name": "Antigravity", "icon": "🚀",
                         "color": "#4285f4", "kind": "agy", "src": "cli",
                         "desc": "Antigravity CLI 구독을 그대로 쓴다"})
    found.append({"id": "ollama", "name": "로컬 (ollama)", "icon": "🖥️",
                  "color": "#9a9a9a", "url": "http://127.0.0.1:11434/v1",
                  "key_env": None, "src": "local", "desc": "내 GPU 에서 돈다"})
    return found


def _hermes_cached(pid, url="", name=""):
    """Hermes 가 이미 긁어둔 전체 모델 목록. 구형까지 들어있다.
    캐시 키는 'anthropic', 'xai', 'kimi-coding', 'custom:https://...' 처럼
    제각각이라 id·표시이름·호스트 조각 전부로 맞춰본다."""
    f = HERMES / "provider_models_cache.json"
    if not f.exists():
        return []
    try:
        cache = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    # 맞춰볼 낱말들: id, 이름, 호스트의 각 조각(api.moonshot.ai → moonshot)
    host = re.sub(r"^https?://|/v1/?$", "", url or "").lower()
    words = {pid.lower(), (name or "").lower()}
    words |= {w for w in re.split(r"[.\-_/]", host)
              if len(w) > 3 and w not in ("api", "com", "http", "https",
                                          "www", "integrate", "openai")}
    words.discard("")
    out = []
    for k, v in cache.items():
        kl = k.lower()
        if any(w in kl or kl.split("-")[0] == w for w in words) or \
           (host and host in kl):
            out += [m for m in (v.get("models") or []) if isinstance(m, str)]
    return out


def _claude_models():
    """CLI 는 목록 API 가 없다. Hermes 캐시 + claude 응답을 합친다."""
    ids = list(_hermes_cached("anthropic"))
    try:
        c = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - c.get("at", 0) < 7 * 86400:
            ids += [m["id"] for m in c.get("models", [])]
        else:
            raise ValueError
    except Exception:
        try:
            r = subprocess.run(["claude", "models"], capture_output=True,
                               text=True, timeout=120)
            fresh = re.findall(r"`(claude-[a-z0-9.\-]+)`", r.stdout)
        except Exception:
            fresh = []
        ids += fresh
        if fresh:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(
                {"at": time.time(), "models": [{"id": i} for i in fresh]},
                ensure_ascii=False), encoding="utf-8")
    return [{"id": i, "name": i} for i in dict.fromkeys(ids)]


# Antigravity CLI. PATH 에 없을 수 있어 알려진 자리도 같이 본다.
AGY = shutil.which("agy") or next(
    (str(p) for p in (Path("/mnt/d/agy/bin/agy"),
                      Path.home() / ".local/bin/agy") if p.is_file()), "")
AGY_CACHE = DATA / "agy-models.json"
AGY_SANDBOX = DATA / "agy-run"      # 코딩 에이전트가 뒤질 빈 작업 폴더


def _agy_models():
    """`agy models` 는 '아이디<탭>표시이름' 을 뱉는다. 로그인 안 됐으면 빈 목록."""
    try:
        c = json.loads(AGY_CACHE.read_text(encoding="utf-8"))
        if time.time() - c.get("at", 0) < 7 * 86400 and c.get("models"):
            return c["models"]
    except Exception:
        pass
    try:
        r = subprocess.run([AGY, "models"], capture_output=True, text=True,
                           timeout=120)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        mid, _, nm = line.partition("\t")
        mid, nm = mid.strip(), nm.strip()
        # 'Fetching...' 같은 안내문은 탭이 없어 여기까지 오지 않는다
        if mid and nm:
            out.append({"id": mid, "name": nm})
    if out:
        AGY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AGY_CACHE.write_text(json.dumps({"at": time.time(), "models": out},
                                        ensure_ascii=False), encoding="utf-8")
    return out


CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"


def claude_usage():
    """Claude Code 구독 사용량. 실패하면 그냥 None — 없어도 되는 정보다."""
    try:
        tok = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))\
            ["claudeAiOauth"]["accessToken"]
    except Exception:
        return None
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": "Bearer " + tok,
                     "anthropic-beta": "oauth-2025-04-20"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    # limits 배열이 정본이다 — 계정마다 있는 한도가 달라서 키를 고정하지 않는다
    KINDS = {"session": "세션", "weekly_all": "주간", "weekly_scoped": "주간"}
    rows = []
    for lim in (d.get("limits") or []):
        pct = lim.get("percent")
        if pct is None:
            continue
        label = KINDS.get(lim.get("kind"), lim.get("kind") or "")
        # weekly_scoped 는 모델별 한도 — 어느 모델인지 붙여야 구분된다
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if model:
            label = f"{label} {model}"
        rows.append({"label": label, "pct": round(float(pct)),
                     "severity": lim.get("severity") or "normal",
                     "resets_at": lim.get("resets_at")})
    # 추가 크레딧(있고 켜본 적 있을 때만)
    sp = d.get("spend") or {}
    if sp.get("percent") is not None and (d.get("extra_usage") or {}).get("credits_ever_enabled"):
        rows.append({"label": "크레딧", "pct": round(float(sp["percent"])),
                     "severity": sp.get("severity") or "normal",
                     "resets_at": None})
    return {"rows": rows} if rows else None


# 잔액 조회는 표준이 없다. 호스트별로 아는 곳만. 없으면 안 보여줄 뿐이다.
#   (호스트 조각, 경로, 값을 꺼내는 함수)
BALANCE = [
    ("moonshot", "/users/me/balance",
     lambda d: (d.get("data") or {}).get("available_balance")),
    ("openrouter", "/credits",
     lambda d: (d.get("data") or {}).get("total_credits", 0)
     - (d.get("data") or {}).get("total_usage", 0)),
    ("deepseek", "/user/balance",
     lambda d: float((d.get("balance_infos") or [{}])[0].get("total_balance", 0))),
]


def provider_balance(p):
    """제공사 잔액(USD). 모르는 곳이면 None — 없는 게 정상이다."""
    url = p.get("url") or ""
    key = _pkey(p)
    if not key:
        return None
    for frag, path, pick in BALANCE:
        if frag not in url.lower():
            continue
        try:
            req = urllib.request.Request(
                url.rstrip("/") + path, headers={"Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=6) as r:
                v = pick(json.loads(r.read()))
            return round(float(v), 2) if v is not None else None
        except Exception:
            return None
    return None


def _why(code, body, key_env=""):
    """서버가 돌려준 원문 JSON 을 그대로 뿌리면 읽을 수가 없다. 한 줄로 줄인다."""
    try:
        msg = json.loads(body).get("error")
        if isinstance(msg, dict):
            msg = msg.get("message", "")
    except Exception:
        msg = ""
    msg = (msg or body or "").strip().replace("\n", " ")
    if code in (401, 403) or "api key" in msg.lower():
        return f"API 키가 거부됐어요 ({key_env or '키'} 확인)"
    if code == 400 and "key" in msg.lower():
        return f"API 키가 거부됐어요 ({key_env or '키'} 확인)"
    if code == 429:
        return "요청이 너무 많아요 (429)"
    return f"HTTP {code} — {msg[:80]}" if msg else f"HTTP {code}"


def _fetch_models(p, timeout=8):
    """제공사의 모델 전부. API 응답 + Hermes 캐시(구형 포함)를 합친다.
    반환: (모델들, 실패이유)"""
    if p.get("kind") == "claude":
        m = _claude_models()
        return m, ("" if m else "claude 모델 목록을 못 찾음")
    if p.get("kind") == "agy":
        m = _agy_models()
        return m, ("" if m else "로그인이 필요해요 (터미널에서 agy 실행)")

    # probe(추가 전 검사)는 캐시를 안 쓴다 — 죽은 키가 캐시 덕에 통과하면 안 된다
    cached = [] if p.get("probe") else _hermes_cached(
        p["id"], p.get("url", ""), p.get("name", ""))
    headers = {}
    key = _pkey(p)
    if p.get("key_env") and not key:
        return [], "키 없음"
    if key:
        headers["Authorization"] = "Bearer " + key
    live, why = [], ""
    try:
        req = urllib.request.Request(p["url"] + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            live = [m["id"] for m in json.loads(r.read()).get("data", [])
                    if m.get("id")]
    except urllib.error.HTTPError as e:
        why = _why(e.code, e.read(400).decode("utf-8", "replace"),
                   p.get("key_env", ""))
    except Exception as e:
        why = "연결할 수 없어요" if isinstance(e, urllib.error.URLError) \
              else str(e)[:80]

    # API 가 최신만 주더라도 캐시의 구형 모델을 버리지 않는다
    ids = list(dict.fromkeys(live + cached))
    if not ids:
        return [], (why or "모델이 0개")
    out = sorted(({"id": i, "name": i.split("/")[-1]} for i in ids),
                 key=lambda m: m["id"])
    # 키가 죽었어도 캐시 덕에 목록은 뜬다 — 그 사실을 숨기지 않는다
    return out, ("" if live else why)


def refresh_models():
    """서버 시작 시 1회. 살아있는 제공사와 그 모델 전부를 담는다."""
    global _catalog, _by_model, _problems
    cat, by, bad = [], {}, []
    for p in discover_providers():
        models, why = _fetch_models(p)
        if why and why != "키 없음":
            bad.append({"name": p["name"], "reason": why})
        if not models:
            continue
        cat.append({"id": p["id"], "name": p["name"], "icon": p["icon"],
                    "color": p["color"], "desc": p.get("desc", ""),
                    "src": p.get("src", "hermes"),
                    "models": models})
        for m in models:
            by[f"{p['id']}/{m['id']}"] = p
    _catalog, _by_model, _problems = cat, by, bad
    return cat


def chat(model_id, system, history, user_input, max_tokens=0):
    """한 턴 생성. model_id 는 "제공사/모델" 형식. 실패는 예외로 올린다.
    max_tokens 0 = 안 보냄(모델 기본값). 사고 토큰이 예산을 먼저 먹어
    빈 응답이 나는 걸 피하려면 굳이 보내지 않는 쪽이 안전하다."""
    p = _by_model.get(model_id)
    if not p:
        raise ValueError(f"모르는 모델: {model_id}")
    name = model_id.split("/", 1)[1]

    msgs = ([{"role": "system", "content": system}] +
            [{"role": m["role"], "content": m["content"]} for m in history] +
            [{"role": "user", "content": user_input}])

    if p.get("kind") == "claude":
        # Claude Code CLI. -p 는 대화형 다이얼로그를 전부 건너뛴다.
        convo = "\n\n".join(f"{m['role']}: {m['content']}" for m in msgs[1:])
        r = subprocess.run(
            ["claude", "-p", convo, "--append-system-prompt", system,
             "--model", name, "--max-turns", "1", "--tools", ""],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[:300])
        return r.stdout.strip()

    if p.get("kind") == "agy":
        # Antigravity CLI. --append-system-prompt 가 없어 system 을 본문 앞에 붙인다.
        # 코딩 에이전트라 파일을 건드리지 않도록 빈 폴더에서 돌린다.
        convo = "\n\n".join(f"{m['role']}: {m['content']}" for m in msgs[1:])
        AGY_SANDBOX.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [AGY, "-p", f"{system}\n\n---\n\n{convo}", "--model", name,
             "--print-timeout", "5m"],
            capture_output=True, text=True, timeout=330, cwd=str(AGY_SANDBOX))
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            raise RuntimeError(((r.stderr or out) or "빈 응답")[:300])
        return out

    body = {"model": name, "messages": msgs, "stream": False}
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    headers = {"Content-Type": "application/json"}
    key = _pkey(p)
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        p["url"] + "/chat/completions",
        json.dumps(body, ensure_ascii=False).encode(), headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


EVENT_PROMPT = (
    "아래는 롤플레이 대화의 한 구간이다. 이 구간에서 '실제로 일어난 일'만 "
    "한국어 개조식 3~6줄로 적어라. 사건, 장소·시간 이동, 인물이 알게 된 사실, "
    "관계 변화, 주고받은 약속만 남긴다. 묘사·감정·문체·대사는 버린다. "
    "머리말이나 설명 없이 항목만 출력한다."
)


def turn_count(history):
    """유저 발화 수 = 턴 수. 프롤로그(assistant)로 시작해도 어긋나지 않는다."""
    return sum(1 for m in history if m.get("role") == "user")


CHAR_PROMPT = (
    "아래는 롤플레이의 기존 줄거리와 대화다. 둘을 종합해 등장인물별로 다시 정리해라. "
    "인물마다 '- 이름' 줄로 시작하고 그 아래 그 인물에 관한 사실만 개조식으로 적는다. "
    "그 인물이 한 일, 알고 있는 것, 모르는 것, 다른 인물과의 관계 변화, 약속을 남긴다. "
    "인물이 실제로 보거나 들은 것만 그 인물 몫으로 적고, 모르는 사실은 적지 않는다. "
    "어느 인물에도 속하지 않는 사건은 마지막에 '- 그 밖' 으로 묶는다. "
    "묘사·감정·문체·대사는 버린다. 머리말이나 설명 없이 항목만 출력한다."
)

# 줄거리가 이만큼 넘으면 프롬프트를 잡아먹기 시작한다. 크랙 본문이 7000자
# 상한이라 그 절반쯤을 줄거리가 차지하면 경고할 때다.
CHRONICLE_WARN = 3500


FULL_PROMPT = (
    "아래는 롤플레이의 기존 줄거리와 전체 대화다. 둘을 종합해 처음부터 다시 "
    "하나의 줄거리로 요약해라. 한국어 개조식, 시간 순서대로. "
    "사건, 장소·시간 이동, 인물이 알게 된 사실, 관계 변화, 주고받은 약속만 "
    "남기고 묘사·감정·문체·대사는 버린다. 중복은 합친다. "
    "머리말이나 설명 없이 항목만 출력한다."
)


def log_events(model_id, msgs, chronicle, upto):
    """구간의 사건만 뽑아 기록에 덧붙인다(누적분 추가).
    실패하면 기록을 건드리지 않는다 — 다음 턴에 다시 시도한다."""
    if not msgs:
        return chronicle, False
    convo = "\n".join(f"{m.get('role')}: {m.get('content','')}" for m in msgs)
    try:
        out = chat(model_id, EVENT_PROMPT, [], convo, max_tokens=800)
    except Exception:
        return chronicle, False
    out = (out or "").strip()
    if not out:
        return chronicle, False
    return ((chronicle + "\n\n" if chronicle.strip() else "")
            + f"■ ~{upto}턴\n{out}"), True


def recap_full(model_id, msgs, chronicle, upto, mode="full"):
    """전체/인물별: 기존 줄거리 + 대화를 종합해 처음부터 다시 쓴다.
    누적분 추가와 달리 결과가 하나의 덩어리다."""
    convo = "\n".join(f"{m.get('role')}: {m.get('content','')}" for m in msgs)
    prior = f"[기존 줄거리]\n{chronicle}\n\n" if (chronicle or "").strip() else ""
    if not convo.strip() and not prior:
        return chronicle, False
    byc = mode == "chars"
    try:
        out = chat(model_id, CHAR_PROMPT if byc else FULL_PROMPT, [],
                   f"{prior}[전체 대화]\n{convo}", max_tokens=2000)
    except Exception:
        return chronicle, False
    out = (out or "").strip()
    if not out:
        return chronicle, False
    head = "■ 인물별" if byc else "■ 전체"
    return f"{head} ~{upto}턴\n{out}", True


def sub_model(data, main):
    """요약·기록에 쓸 모델. 설정에서 고른 게 있으면 그것, 없으면 본편과 같은 것.
    요약은 창작이 아니라 추출이라 싼 모델로 충분하다 — 본편 모델의
    구독 한도를 20턴마다 갉아먹지 않게 하는 것이 목적."""
    sub = str(data.get("sub_model") or "").strip()
    return sub if sub and "/" in sub else main


SUMMARY_PROMPT = (
    "아래는 롤플레이 대화의 오래된 부분이다. 이어서 진행할 때 필요한 것만 "
    "한국어 개조식으로 요약해라. 인물이 알게 된 사실, 관계 변화, 약속, "
    "장소·시간 이동, 남은 목표를 남기고 묘사·문체는 버린다. "
    "이미 있는 요약이 주어지면 그것과 합쳐 하나로 다시 써라. "
    "설명이나 머리말 없이 요약만 출력한다."
)


def condense(model_id, old_msgs, memory=""):
    """창 밖으로 밀려난 대화를 요약해 기억에 합친다.
    실패하면 기존 기억을 그대로 둔다 — 기억이 날아가는 것보다 낫다."""
    if not old_msgs:
        return memory
    convo = "\n".join(f"{m.get('role')}: {m.get('content','')}" for m in old_msgs)
    prior = f"[기존 요약]\n{memory}\n\n" if (memory or "").strip() else ""
    try:
        out = chat(model_id, SUMMARY_PROMPT, [],
                   f"{prior}[요약할 대화]\n{convo}", max_tokens=1200)
    except Exception:
        return memory
    out = (out or "").strip()
    return out or memory


# ── 저장 (임시저장 draft + 채팅 세션 chat, 같은 코드) ─────────
def list_items(folder, limit):
    out = []
    for f in folder.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": f.stem, "title": d.get("title") or "제목 없음",
                    "saved_at": d.get("saved_at", 0),
                    "image": d.get("image", ""),
                    "subtitle": d.get("subtitle", ""),
                    "preview": d.get("preview", "")})
    return sorted(out, key=lambda x: -x["saved_at"])


def save_item(folder, data, limit):
    folder.mkdir(parents=True, exist_ok=True)
    did = data.get("id") or uuid.uuid4().hex[:8]
    data["id"] = did
    data["saved_at"] = time.time()
    # 임시파일에 쓰고 교체한다 — 쓰는 도중 죽어도 기존 파일이 안 깨진다
    dst = folder / f"{did}.json"
    tmp = folder / f".{did}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(dst)
    if limit:   # 0 이면 무제한 — 오래된 것을 지우지 않는다
        for old in list_items(folder, limit)[limit:]:
            (folder / f"{old['id']}.json").unlink(missing_ok=True)
    backup()
    return data


def backup():
    """하루에 한 번, 첫 저장 때 zip 하나. 이미 오늘 것이 있으면 안 만든다."""
    dst = BACKUPS / f"crack-{date.today().isoformat()}.zip"
    if dst.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    # backups 를 포함하면 어제 zip 이 오늘 zip 에 들어가 눈덩이가 된다.
    # drafts / chats 만 담는다.
    tmp = dst.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in (DRAFTS, CHATS):
            for f in sorted(folder.glob("*.json")):
                z.write(f, f"{folder.name}/{f.name}")
    tmp.replace(dst)
    for f in sorted(BACKUPS.glob("crack-*.zip"))[:-KEEP_BACKUPS]:
        f.unlink(missing_ok=True)
    return dst


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # /api/<종류>  →  (폴더, 최대개수). drafts 와 chats 는 완전히 같은 코드다.
    KINDS = {"drafts": (DRAFTS, MAX_DRAFTS), "chats": (CHATS, MAX_CHATS)}

    def _kind(self):
        seg = self.path.split("?")[0].split("/")
        if len(seg) >= 3 and seg[1] == "api" and seg[2] in self.KINDS:
            return self.KINDS[seg[2]], (seg[3] if len(seg) > 3 else "")
        return None, ""

    def do_GET(self):
        if self.path.startswith("/img/"):
            # 데이터 폴더는 소스 밖에 있을 수 있어 기본 정적 서빙이 못 찾는다.
            # 이름만 취해 폴더 밖으로 못 나가게 한다.
            name = os.path.basename(urllib.parse.unquote(self.path[5:]))
            f = IMAGES / name
            kind = None
            if name and f.is_file():
                kind = sniff_image(f.read_bytes()[:16])
            if not kind:
                return self._json({"error": "없는 이미지"}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", kind[1])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            return self.wfile.write(body)

        if self.path == "/api/personas":
            return self._json(load_list(PERSONAS))
        if self.path == "/api/providers":
            # 키는 절대 돌려주지 않는다. 들어있는지 여부만.
            return self._json([{"id": p.get("id"), "name": p.get("name"),
                                "url": p.get("url"), "icon": p.get("icon", ""),
                                "has_key": bool(p.get("key"))}
                               for p in load_providers()])
        if self.path == "/api/usage":
            out = claude_usage() or {}
            # 잔액을 아는 제공사만. 하나가 느려도 나머지는 나와야 한다.
            bal = {}
            for p in discover_providers():
                v = provider_balance(p)
                if v is not None:
                    bal[p["id"]] = v
            if bal:
                out["balances"] = bal
            return self._json(out)
        if self.path == "/api/models":
            return self._json({"hermes": hermes_ok(), "providers": _catalog,
                                "problems": _problems})
        (kind, item) = self._kind()
        if kind:
            folder, limit = kind
            if not item:
                return self._json(list_items(folder, limit))
            f = folder / f"{item}.json"
            if not f.exists():
                return self._json({"error": "없음"}, 404)
            return self._json(json.loads(f.read_text(encoding="utf-8")))
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if self.path == "/api/images":
            # 본문이 그림 자체다. JSON 으로 읽으면 당연히 깨진다 —
            # 다른 경로보다 먼저 처리해야 하는 이유.
            try:
                if n <= 0 or n > MAX_IMG:
                    raise ValueError("8MB 까지만 올릴 수 있어요")
                name = save_image(self.rfile.read(n))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"저장하지 못했어요: {e}"}, 500)
            return self._json({"url": "/img/" + name})
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/recap":
            # 손으로 누르는 요약. mode: full(전체 다시) | append(누적분 추가)
            rhist = data.get("history") or []
            chron = str(data.get("chronicle") or "")
            logged = int(data.get("logged_turns") or 0)
            mdl = data.get("model") or (
                f"{_catalog[0]['id']}/{_catalog[0]['models'][0]['id']}"
                if _catalog else "")
            turns = turn_count(rhist)
            rmode = data.get("mode")
            mdl = sub_model(data, mdl)     # 요약은 서브모델로
            if rmode in ("full", "chars"):
                out, ok = recap_full(mdl, rhist, chron, turns, rmode)
            else:
                seg = rhist[logged * 2:]   # 아직 기록 안 한 구간만
                if not seg:
                    return self._json({"error": "새로 기록할 대화가 없어요"}, 400)
                out, ok = log_events(mdl, seg, chron, turns)
            if not ok:
                return self._json({"error": "요약하지 못했어요"}, 502)
            return self._json({"chronicle": out, "logged_turns": turns,
                               "too_long": len(out) > CHRONICLE_WARN,
                               "warn_at": CHRONICLE_WARN})
        if self.path == "/api/personas":
            # 이름은 필수, 나머지는 비어도 된다
            name = str(data.get("name", "")).strip()
            if not name:
                return self._json({"field": "name",
                                   "error": "이름을 입력해 주세요"}, 400)
            items = load_list(PERSONAS)
            pid = str(data.get("id") or "").strip() or uuid.uuid4().hex[:8]
            row = {"id": pid, "name": name,
                   "profile": str(data.get("profile", "")).strip()}
            for i, it in enumerate(items):
                if it.get("id") == pid:      # 수정
                    items[i] = row
                    break
            else:
                items.append(row)            # 추가
            save_list(PERSONAS, items)
            return self._json(row)
        if self.path == "/api/providers":
            return self._add_provider(data)
        (kind, _) = self._kind()
        if kind:
            folder, limit = kind
            return self._json(save_item(folder, data, limit))
        if self.path == "/api/chat":
            story = data.get("story", {})
            hist = data.get("history", [])
            msg = data.get("message", "")
            persona = data.get("persona") or ""
            usernote = str(data.get("usernote") or "")
            memory = str(data.get("memory") or "")
            max_tokens = int(data.get("max_tokens") or 0)
            keep = int(data.get("keep_turns") or 0)

            # 기본은 카탈로그 첫 번째 — 모델 이름을 하드코딩하지 않는다
            m = data.get("model") or (
                f"{_catalog[0]['id']}/{_catalog[0]['models'][0]['id']}"
                if _catalog else "")

            # 사건 기록: N턴마다 자동 요약. 주기·방식은 설정에서 받는다.
            chronicle = str(data.get("chronicle") or "")
            logged = int(data.get("logged_turns") or 0)
            every = int(data.get("event_every") or EVENT_EVERY)
            mode = data.get("event_mode") or "append"
            turns = turn_count(hist) + 1        # 지금 보내는 이 발화 포함
            sub = sub_model(data, m)            # 요약·기록 담당
            wrote_events = False
            if every > 0 and turns - logged >= every:
                upto = logged + (turns - logged) // every * every
                if mode in ("full", "chars"):
                    # 다시 쓰기 — 기존 줄거리 + 처음부터의 대화를 종합
                    chronicle, wrote_events = recap_full(
                        sub, hist[:upto * 2], chronicle, upto, mode)
                else:
                    # 아직 기록 안 한 구간 = logged턴 다음부터 upto턴까지
                    seg = hist[logged * 2:upto * 2] or hist[-every * 2:]
                    chronicle, wrote_events = log_events(sub, seg, chronicle, upto)
                if wrote_events:
                    logged = upto

            # 장기 기억: 창(keep 턴)을 넘긴 옛 대화는 요약해 접어 넣는다
            summarized = False
            if keep > 0 and len(hist) > keep * 2:
                cut = len(hist) - keep * 2
                new_memory = condense(sub, hist[:cut], memory)
                summarized = new_memory != memory
                memory = new_memory
                hist = hist[cut:]

            system = build_system(story, hist, msg, persona, usernote, memory,
                                  chronicle, bool(data.get("chron_all")),
                                  bool(data.get("img_off")))
            # 사용자가 친 말 안의 {user}/{char} 도 같이 치환한다.
            # {img::N} 은 이름으로 풀어 보낸다 — 번호만 보면 뭘 골랐는지 모른다.
            msg = img_to_label(subst(msg, story, persona), story)
            hist = [dict(m2, content=img_to_label(
                subst(m2.get("content", ""), story, persona), story))
                    for m2 in hist]
            fired = [n["title"] for n in
                     active_notes(story.get("notes", []), hist, msg,
                                  start_name=story_start(story).get("name"))]
            try:
                reply = chat(m, system, hist, msg, max_tokens)
            except Exception as e:
                return self._json({"error": str(e)[:400]}, 502)
            return self._json({"reply": reply, "fired": fired,
                               "memory": memory, "summarized": summarized,
                               "chronicle": chronicle, "logged_turns": logged,
                               "wrote_events": wrote_events,
                               "too_long": len(chronicle) > CHRONICLE_WARN,
                               "warn_at": CHRONICLE_WARN,
                               "system_chars": len(system)})
        return self._json({"error": "없음"}, 404)

    def _add_provider(self, data):
        """손으로 추가. 저장 전에 실제로 /models 를 찔러본다 —
        안 되는 걸 목록에 넣어두면 나중에 채팅에서 터진다."""
        url = str(data.get("url", "")).strip().rstrip("/")
        key = str(data.get("key", "")).strip()
        name = str(data.get("name", "")).strip()
        # 어느 칸이 틀렸는지 같이 돌려준다 — 화면에서 그 칸만 빨갛게 칠한다
        if not url:
            return self._json({"field": "url", "error": "주소를 입력해 주세요"}, 400)
        if not url.startswith(("http://", "https://")):
            return self._json({"field": "url",
                               "error": "주소는 http:// 또는 https:// 로 시작해야 해요"}, 400)
        # 헤더는 latin-1 로만 나간다. 한글이 섞인 키는 여기서 잡아야
        # urllib 이 UnicodeEncodeError 를 그대로 토해내지 않는다.
        try:
            (key or "").encode("latin-1")
        except UnicodeEncodeError:
            return self._json({"field": "key",
                               "error": "키에 쓸 수 없는 문자가 있어요 (영문·숫자·기호만)"}, 400)
        probe = {"id": "probe", "name": name or url, "url": url, "key": key,
                 "key_env": None, "probe": True}
        models, why = _fetch_models(probe, timeout=12)
        if not models:
            # 키가 거부됐으면 키 칸, 주소를 못 찾았으면 주소 칸
            field = "key" if ("키" in why or "401" in why or "403" in why) else "url"
            return self._json({"field": field,
                               "error": why or "모델을 가져오지 못했어요"}, 400)

        pid = str(data.get("id", "")).strip().lower() or \
            re.sub(r"[^a-z0-9]+", "-",
                   re.sub(r"^https?://|/v\d+/?$", "", url).lower()).strip("-")
        items = [p for p in load_providers() if p.get("id") != pid]   # 같은 id 는 덮어쓴다
        items.append({"id": pid, "name": name or pid, "url": url,
                      "icon": str(data.get("icon", "")).strip() or "➕",
                      "key": key})
        save_providers(items)
        refresh_models()
        return self._json({"id": pid, "name": name or pid, "models": len(models)})

    def do_DELETE(self):
        if self.path.startswith("/api/personas/"):
            pid = urllib.parse.unquote(self.path.split("/api/personas/", 1)[1])
            save_list(PERSONAS,
                      [x for x in load_list(PERSONAS) if x.get("id") != pid])
            return self._json({"ok": True})
        if self.path.startswith("/api/providers/"):
            pid = urllib.parse.unquote(self.path.split("/api/providers/", 1)[1])
            items = [p for p in load_providers() if p.get("id") != pid]
            save_providers(items)
            refresh_models()
            return self._json({"ok": True})
        (kind, item) = self._kind()
        if not kind or not item:
            return self._json({"error": "없음"}, 404)
        (kind[0] / f"{item}.json").unlink(missing_ok=True)
        return self._json({"ok": True})

    def log_message(self, *a):
        pass


def selftest():
    story = {
        "prompt": "P",
        "notes": [
            {"title": "메스가키", "info": "M", "keywords": ["!메스가키", "😊"]},
            {"title": "관계", "info": "R", "keywords": ["!관계"]},
            {"title": "짧음", "info": "S", "keywords": ["해"]},
        ],
    }
    B = lambda h, u="": build_system(story, h, u)
    assert B([]) == "P", "키워드 없으면 프롬프트만"
    assert "[메스가키]\nM" in B([], "!메스가키"), "이번 입력에서 발동"
    # assistant 출력의 이모지가 다음 턴 노트를 다시 부른다 (자기점화)
    assert "M" in B([{"role": "assistant", "content": "모드:[😊]"}], "안녕")
    # 스캔 윈도우를 벗어나면 꺼진다
    old = [{"role": "assistant", "content": "😊"}] + \
          [{"role": "user", "content": "x"}] * 10
    assert "M" not in B(old, "안녕"), "윈도우 밖 키워드는 꺼져야 함"
    # 1자 한글 키워드는 오발동 방지로 무시
    assert "S" not in B([], "그래서 했어요"), "1자 키워드는 무시"
    assert "R" not in B([], "!메스가키"), "다른 노트는 안 붙음"

    # 시작 설정별 적용 대상(scope)
    scoped = {
        "prompt": "P",
        "starts": [{"name": "밤", "guide": "G"}],
        "notes": [
            {"title": "밤전용", "info": "N", "keywords": ["!밤"], "scope": "밤"},
            {"title": "낮전용", "info": "D", "keywords": ["!밤"], "scope": "낮"},
            {"title": "공통", "info": "C", "keywords": ["!밤"], "scope": "all"},
        ],
    }
    got = build_system(scoped, [], "!밤")
    assert "[밤전용]\nN" in got, got          # 지금 시작 설정 것은 붙고
    assert "[낮전용]" not in got, got         # 다른 시작 설정 것은 안 붙는다
    assert "[공통]\nC" in got, got            # all 은 항상
    assert "G" in got, "플레이 가이드가 빠졌다"
    # 키워드가 없으면 scope 가 맞아도 안 붙는다
    assert "[밤전용]" not in build_system(scoped, [], "안녕")
    # scope 키가 아예 없는 옛 노트는 전체 적용으로 본다
    assert in_scope({}, "밤") and in_scope({"scope": ""}, "밤")

    # {char}/{user} 치환 — 프롬프트·가이드·노트 전부 같은 규칙
    ps = {
        "title": "삼칠풀",
        "prompt": "{char}가 {user}를 본다",
        "starts": [{"name": "기본", "guide": "{user}는 비서관"}],
        "notes": [{"title": "n", "info": "{char}는 {{user}}를 안다",
                   "keywords": ["!켜"], "scope": "all"}],
    }
    got = build_system(ps, [], "!켜", persona="빈집털이범")
    assert "삼칠풀가 빈집털이범를 본다" in got, got
    assert "빈집털이범는 비서관" in got, got          # 가이드도 치환
    assert "삼칠풀는 빈집털이범를 안다" in got, got    # 노트 안 {{user}} 도
    assert "{char}" not in got and "{user}" not in got, got
    # 페르소나가 없으면 '당신', 스토리 제목이 없으면 '그'
    plain = build_system({"prompt": "{char}와 {user}"}, [], "")
    assert plain == "그와 당신", plain
    # 치환할 게 없으면 원문 그대로
    assert subst("그냥 글", ps, "빈집털이범") == "그냥 글"

    # 대화 프로필 + 유저 노트 — 키워드와 무관하게 늘 들어간다
    who = {"name": "빈집털이범", "profile": "26세 비서관. {char}를 경계한다."}
    got = build_system(ps, [], "", persona=who, usernote="답변은 짧게.")
    assert "[빈집털이범 프로필]" in got, got
    assert "26세 비서관. 삼칠풀를 경계한다." in got, got   # 프로필도 치환
    assert "[유저 노트]\n답변은 짧게." in got, got
    assert "[n]" not in got, "키워드 없는데 노트가 붙었다"
    assert "빈집털이범가 빈집털이범를 본다" not in got     # {char}는 스토리 이름
    # 문자열 페르소나(옛 형식)도 그대로 동작
    old = build_system(ps, [], "", persona="빈집털이범")
    assert "삼칠풀가 빈집털이범를 본다" in old and "프로필]" not in old, old
    # 프로필/유저 노트가 비면 머리글도 안 나온다
    empty = build_system(ps, [], "", persona={"name": "가", "profile": "  "},
                         usernote="")
    assert "프로필]" not in empty and "유저 노트]" not in empty, empty
    assert persona_name({"name": " 가 "}) == "가" and persona_name(None) == ""

    # 장기 기억 블록
    memgot = build_system(ps, [], "", memory="지안과 약속함")
    assert "[장기 기억]\n지안과 약속함" in memgot, memgot
    assert "[장기 기억]" not in build_system(ps, [], "", memory="   ")

    # 요약: 모델이 죽어도 기존 기억을 날리지 않는다
    assert condense("없는/모델", [{"role": "user", "content": "x"}],
                    "옛 기억") == "옛 기억"
    assert condense("없는/모델", [], "옛 기억") == "옛 기억"

    # 사건 기록: 턴 세기 + 실패해도 기록을 안 건드린다
    h = [{"role": "assistant", "content": "프롤로그"},
         {"role": "user", "content": "가"}, {"role": "assistant", "content": "나"},
         {"role": "user", "content": "다"}]
    assert turn_count(h) == 2, turn_count(h)   # 프롤로그는 턴이 아니다
    assert turn_count([]) == 0
    got, ok = log_events("없는/모델", h, "기존", 20)
    assert got == "기존" and ok is False, (got, ok)
    got, ok = log_events("없는/모델", [], "기존", 20)
    assert got == "기존" and ok is False
    # 전체 다시 쓰기도 실패 시 기록을 지키고, 머리글이 다르다
    got, ok = recap_full("없는/모델", h, "기존", 20)
    assert got == "기존" and ok is False, (got, ok)
    assert recap_full("없는/모델", [], "", 0) == ("", False)
    assert recap_full("없는/모델", [], "", 0, "chars") == ("", False)
    # 경고 기준은 프롬프트를 잡아먹기 전에 떠야 한다
    assert 1000 < CHRONICLE_WARN < 7000, CHRONICLE_WARN
    # 사건 기록 블록
    cgot = build_system(ps, [], "", chronicle="■ ~20턴\n- 지안을 만남")
    assert "[지금까지 있었던 일]\n■ ~20턴" in cgot, cgot
    assert "[지금까지 있었던 일]" not in build_system(ps, [], "", chronicle="  ")

    # 줄거리 쪼개기: ■ 블록 + 인물 문단
    ch = ("■ 인물별 ~20턴\n- 정지안\n  - 서류를 건넴\n\n- 백서린\n  - 명단을 보여줌\n"
          "\n■ 인물별 ~40턴\n- 키무라 유나\n  - 삼도회 회장")
    blocks = split_chronicle(ch)
    assert len(blocks) == 3, blocks
    assert all(b.startswith("■") for b in blocks), blocks   # 제목이 붙어 다닌다
    assert "정지안" in blocks[0] and "백서린" in blocks[1]
    assert split_chronicle("") == [] and split_chronicle("  ") == []
    # ■ 가 없는 옛 기록도 통째로 하나
    assert split_chronicle("그냥 줄거리") == ["그냥 줄거리"]

    # 관련 덩어리만 고르기
    R = lambda u, top=1: relevant_chronicle(ch, [], u, top=top)
    assert "정지안" in R("지안이 뭐라 했지?") and "백서린" not in R("지안이 뭐라 했지?")
    assert "백서린" in R("서린의 명단은?")
    assert "유나" in R("유나 정체가 뭐야?")
    assert R("유나 정체가 뭐야?") == R("유나 정체가 뭐야?")
    # 제목 줄(■ 인물별 ~20턴)이 점수에 끼면 '인물별'이 겹쳐 아무거나 뜬다
    assert relevant_chronicle(ch, [], "인물별 기록", top=1) == "", \
        "제목 줄은 점수에 넣지 않아야 한다"
    assert R("전혀 상관없는 요리 이야기") == "", R("전혀 상관없는 요리 이야기")
    # 덩어리가 top 이하면 고를 게 없으니 전부
    assert relevant_chronicle(ch, [], "아무말", top=9) == "\n\n".join(blocks)
    # 시간순이 뒤집히지 않는다
    two = relevant_chronicle(ch, [], "유나와 지안", top=2)
    assert two.index("정지안") < two.index("유나"), two
    # 최근 대화(history)로도 걸린다 — 이번 발화에 없어도
    assert "백서린" in relevant_chronicle(
        ch, [{"role": "user", "content": "서린을 다시 만난다"}], "계속.", top=1)
    # 통째로 넣기
    allgot = build_system(ps, [], "상관없는 말", chronicle=ch, chron_all=True)

    assert "정지안" in allgot and "유나" in allgot, allgot
    # 덩어리가 top(3)보다 많을 때만 고르기가 의미 있다
    ch4 = ch + "\n\n■ 인물별 ~60턴\n- 사이토 칸나\n  - 태성그룹 회장"
    assert len(split_chronicle(ch4)) == 4
    one = build_system(ps, [], "칸나를 만난다", chronicle=ch4)
    assert "칸나" in one and "정지안" not in one, one
    # 관련 없으면 블록째 안 들어간다
    none = build_system(ps, [], "상관없는 요리 이야기", chronicle=ch4)
    assert "[지금까지 있었던 일]" not in none, none

    # 이미지: 번호 → 이름/주소
    ist = {"title": "T", "images": [
        {"n": 1, "label": "칸나 무표정", "url": "BA/C02.webp"},
        {"n": 3, "label": "유나 애교", "url": "BA/D06.webp"},
        {"label": "이름없음", "url": ""},          # n 없으면 등록 순서
        {"n": 5, "label": "", "url": "BA/X.webp"},  # 이름 없으면 목록에서 뺀다
    ]}
    m = img_map(ist)
    assert m[1] == ("칸나 무표정", "BA/C02.webp") and m[3][0] == "유나 애교", m
    # 번호 없는 것이 적어둔 번호를 덮어쓰면 안 된다
    assert m[2][0] == "이름없음", m
    assert 5 in m and m[5][0] == ""
    lines = img_lines(ist)
    assert "1=칸나 무표정" in lines and "3=유나 애교" in lines
    assert "5=" not in lines, "이름 없는 건 목록에 없어야 한다: " + lines
    # 모델에게 갈 때는 이름으로 풀린다
    assert img_to_label("보라 {img::1} 그리고 {img::3}", ist) == \
        "보라 [이미지: 칸나 무표정] 그리고 [이미지: 유나 애교]"
    assert img_to_label("{img:: 3 }", ist) == "[이미지: 유나 애교]"   # 공백 허용
    assert img_to_label("{img::99}", ist) == "[이미지 99]"            # 없는 번호
    assert img_to_label("그냥 글", ist) == "그냥 글"
    assert img_lines({"title": "T"}) == ""      # 이미지가 없으면 빈 문자열

    # 올린 그림: 확장자가 아니라 파일 앞머리로 종류를 정한다
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
    assert sniff_image(png) == (".png", "image/png")
    assert sniff_image(b"\xff\xd8\xff\xe0" + b"0" * 20)[0] == ".jpg"
    assert sniff_image(b"GIF89a" + b"0" * 20)[0] == ".gif"
    assert sniff_image(b"RIFF" + b"1234" + b"WEBP" + b"0" * 20)[0] == ".webp"
    assert sniff_image(b"RIFF" + b"1234" + b"WAVE" + b"0" * 20) is None  # 소리는 ✕
    assert sniff_image(b"<?php echo 1;") is None
    assert sniff_image(b"") is None
    n1 = save_image(png)
    assert n1.endswith(".png") and (IMAGES / n1).is_file()
    assert (IMAGES / n1).read_bytes() == png
    assert save_image(png) == n1, "같은 그림은 파일 하나여야 한다"
    assert save_image(png + b"x") != n1
    try:
        save_image(b"not an image")
        raise SystemExit("이미지가 아닌 것을 받았다")
    except ValueError:
        pass
    for junk in (n1, save_image(png + b"x")):
        (IMAGES / junk).unlink(missing_ok=True)
    # 프롬프트에 목록이 들어가고, 없으면 블록째 안 들어간다
    igot = build_system(dict(ps, images=ist["images"]), [], "")
    assert "{img::번호}" in igot and "1=칸나 무표정" in igot, igot
    assert "[이미지]" not in build_system(ps, [], "")
    # 규칙은 스토리 프롬프트가 아니라 서버가 붙인다
    assert IMG_RULES["fit"] in igot, igot          # 기본값
    ieach = build_system(dict(ps, images=ist["images"], imgRule="each"), [], "")
    assert IMG_RULES["each"] in ieach and IMG_RULES["fit"] not in ieach, ieach
    ioff = build_system(dict(ps, images=ist["images"], imgRule="off"), [], "")
    assert IMG_RULES["off"] in ioff, ioff
    # 모르는 값이 와도 기본으로 떨어진다
    ibad = build_system(dict(ps, images=ist["images"], imgRule="없는값"), [], "")
    assert IMG_RULES["fit"] in ibad, ibad
    # 방에서 끄면 목록째 빠진다 — 규칙만 남으면 모델이 없는 번호를 지어낸다
    ioff2 = build_system(dict(ps, images=ist["images"]), [], "", img_off=True)
    assert "[이미지]" not in ioff2 and "칸나 무표정" not in ioff2, ioff2
    # 스토리에서 끈 경우도 마찬가지
    soff = build_system(dict(ps, images=ist["images"], imgOn=False), [], "")
    assert "[이미지]" not in soff and "칸나 무표정" not in soff, soff
    # 없거나 True 면 켜진 것 — 옛 스토리엔 이 값이 아예 없다
    for v in ({}, {"imgOn": True}):
        assert "[이미지]" in build_system(
            dict(ps, images=ist["images"], **v), [], ""), v

    # 서브모델: 고른 게 있으면 그것, 없거나 이상하면 본편 모델로 떨어진다
    assert sub_model({}, "main/m") == "main/m"
    assert sub_model({"sub_model": ""}, "main/m") == "main/m"
    assert sub_model({"sub_model": "   "}, "main/m") == "main/m"
    assert sub_model({"sub_model": None}, "main/m") == "main/m"
    assert sub_model({"sub_model": "쓰레기"}, "main/m") == "main/m"   # 슬래시 없음
    assert sub_model({"sub_model": "antigravity/gemini-3.7-flash-low"},
                     "main/m") == "antigravity/gemini-3.7-flash-low"

    # 제공사 목록에 CLI 두 개가 다 뜨는가 (설치돼 있을 때만)
    provs = discover_providers()
    kinds = {p.get("kind") for p in provs}
    if AGY:
        agy = [p for p in provs if p.get("kind") == "agy"]
        assert len(agy) == 1, agy
        assert agy[0]["src"] == "cli" and agy[0]["id"] == "antigravity", agy[0]
        # 키를 안 쓴다 — key_env 를 넣으면 '키 없음' 으로 빠진다
        assert not agy[0].get("key_env"), agy[0]

    # agy models 출력 파싱: 탭이 있는 줄만 모델이다
    def _parse(out):
        rows = [ln.partition("\t") for ln in out.splitlines() if "\t" in ln]
        return [{"id": a.strip(), "name": c.strip()} for a, _, c in rows
                if a.strip() and c.strip()]
    got = _parse("Fetching available models...\n"
                 "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
                 "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
                 "\n")
    assert got == [{"id": "gemini-3.7-flash-high", "name": "Gemini 3.7 Flash (High)"},
                   {"id": "claude-opus-4-6-thinking",
                    "name": "Claude Opus 4.6 (Thinking)"}], got
    assert _parse("Error: Please sign in to view available models.") == []
    # 탭 없는 안내문이 모델로 새어들면 안 된다 (구분자는 탭뿐)
    assert _parse("Fetching available models...\nSome notice line\n") == []

    # 저장 → 디스크에서 그대로 읽히는가 + 백업이 도는가
    import tempfile
    global DATA, DRAFTS, CHATS, BACKUPS
    with tempfile.TemporaryDirectory() as td:
        DATA = Path(td); DRAFTS = DATA / "drafts"
        CHATS = DATA / "chats"; BACKUPS = DATA / "backups"
        save_item(DRAFTS, {"title": "가", "prompt": "P"}, 50)
        got = save_item(CHATS, {"title": "나", "preview": "미리"}, MAX_CHATS)
        assert [x["title"] for x in list_items(DRAFTS, 50)] == ["가"]
        assert (CHATS / f"{got['id']}.json").exists(), "디스크에 실제로 써야 함"

        # limit 0 = 무제한. 몇 개를 넣든 하나도 안 지워야 한다
        for i in range(12):
            save_item(CHATS, {"title": f"방{i}"}, MAX_CHATS)
        assert len(list(CHATS.glob("*.json"))) == 13, "무제한인데 지워졌다"

        # 삭제하면 파일이 실제로 사라진다
        (CHATS / f"{got['id']}.json").unlink()
        assert not (CHATS / f"{got['id']}.json").exists()
        assert got["id"] not in [x["id"] for x in list_items(CHATS, MAX_CHATS)]

        zips = list(BACKUPS.glob("crack-*.zip"))
        assert len(zips) == 1, f"하루 1개여야 함: {zips}"
        save_item(CHATS, {"title": "다"}, MAX_CHATS)   # 같은 날 두 번째 저장
        assert len(list(BACKUPS.glob("crack-*.zip"))) == 1, "하루 1개만"

        # 하루 1개라 첫 zip 은 그 시점 내용만 담는다. 강제로 다시 떠서
        # 두 폴더가 다 들어가는지, 백업이 백업을 삼키지 않는지 본다.
        zips[0].unlink()
        # 미끼: backups 폴더도 담으면 이 파일이 zip 안에 들어온다
        (BACKUPS / "미끼.json").write_text("{}", encoding="utf-8")
        z2 = backup()
        with zipfile.ZipFile(z2) as z:
            names = z.namelist()
        assert any(n.startswith("drafts/") for n in names), names
        assert any(n.startswith("chats/") for n in names), names
        assert not any("미끼" in n for n in names), \
            f"백업이 backups 폴더를 담으면 눈덩이가 된다: {names}"

        # 손으로 추가한 제공사: 저장/삭제 왕복 + 키가 discover 로 넘어가는가
        global PROVIDERS
        PROVIDERS = DATA / "providers.json"
        assert load_providers() == [], "파일 없으면 빈 목록"
        save_providers([{"id": "손", "name": "손", "url": "https://x/v1",
                         "key": "sk-비밀"}])
        assert [p["id"] for p in load_providers()] == ["손"]
        mine = [p for p in discover_providers() if p.get("src") == "manual"]
        assert len(mine) == 1 and mine[0]["url"] == "https://x/v1", mine
        # _pkey 가 손으로 넣은 키를 .env 보다 먼저 쓴다
        assert _pkey(mine[0]) == "sk-비밀", "수동 키가 우선이어야 함"
        assert _pkey({"key_env": None}) == "", "키 없으면 빈 문자열"
        save_providers([])
        assert load_providers() == [] and not any(
            p.get("src") == "manual" for p in discover_providers()), "삭제가 안 먹었다"

        # 대화 프로필 목록 파일 왕복 (임시 폴더 안에서)
        global PERSONAS
        PERSONAS = DATA / "personas.json"
        assert load_list(PERSONAS) == []
        save_list(PERSONAS, [{"id": "a", "name": "재현", "profile": "20살"}])
        assert [x["name"] for x in load_list(PERSONAS)] == ["재현"]
        save_list(PERSONAS, [])
        assert load_list(PERSONAS) == []
        # 출처가 섞이면 모델 창의 구분이 무너진다 — 모든 제공사가 src 를 갖는다
        srcs = {p.get("src") for p in discover_providers()}
        assert None not in srcs, f"src 없는 제공사가 있다: {srcs}"
        assert srcs <= {"manual", "hermes", "cli", "local"}, srcs

        # 잔액 파서 — 응답 모양이 바뀌면 여기서 걸린다
        pick = dict((f, fn) for f, _, fn in BALANCE)
        assert pick["moonshot"]({"data": {"available_balance": 20.9}}) == 20.9
        assert pick["openrouter"](
            {"data": {"total_credits": 10, "total_usage": 3}}) == 7
        assert pick["deepseek"](
            {"balance_infos": [{"total_balance": "5.5"}]}) == 5.5
        # 키가 없으면 네트워크를 아예 안 탄다
        assert provider_balance({"url": "https://api.moonshot.ai/v1",
                                 "key_env": None}) is None
        # 모르는 호스트도 안 탄다
        assert provider_balance({"url": "https://nope.example/v1",
                                 "key": "k", "key_env": None}) is None

    # 모델 카탈로그: 응답이 안 오는 제공사는 목록에서 빠지고 이유가 남아야 한다
    global _catalog, _by_model, _problems
    saved = (_catalog, _by_model, _problems)
    orig = globals()["discover_providers"]
    orig_cache = globals()["_hermes_cached"]
    try:
        globals()["_hermes_cached"] = lambda pid, url="", name="": []
        globals()["discover_providers"] = lambda: [
            {"id": "죽은곳", "name": "죽은곳", "icon": "x", "color": "#000",
             "url": "http://127.0.0.1:9/v1", "key_env": None}]
        assert refresh_models() == [], "응답 없는 제공사가 목록에 떴다"
        assert _problems and _problems[0]["reason"], "실패 이유를 안 남겼다"
        assert chat_fails("죽은곳/뭐든"), "없는 모델을 받아주면 안 된다"

        # API 가 최신만 줘도 Hermes 캐시의 구형 모델이 살아남아야 한다
        globals()["_hermes_cached"] = lambda pid, url="", name="": ["구형-1", "구형-2"]
        globals()["discover_providers"] = lambda: [
            {"id": "죽은곳", "name": "죽은곳", "icon": "x", "color": "#000",
             "url": "http://127.0.0.1:9/v1", "key_env": None}]
        cat = refresh_models()
        got = [m["id"] for m in cat[0]["models"]]
        assert got == ["구형-1", "구형-2"], f"캐시 구형 모델이 사라졌다: {got}"
        assert _problems, "연결 실패를 조용히 숨겼다"
    finally:
        globals()["discover_providers"] = orig
        globals()["_hermes_cached"] = orig_cache
        _catalog, _by_model, _problems = saved
    print("selftest ok")


def chat_fails(model_id):
    try:
        chat(model_id, "", [], "안녕")
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--backup" in sys.argv:
        print("백업:", backup() or "오늘 것이 이미 있음")
    else:
        # 예전 버전은 서버 폴더 바로 밑에 저장했다. 있으면 한 번만 옮긴다.
        for old, new in ((ROOT / "drafts", DRAFTS), (ROOT / "chats", CHATS)):
            if old.exists() and old.resolve() != new.resolve():
                new.mkdir(parents=True, exist_ok=True)
                for f in old.glob("*.json"):
                    f.replace(new / f.name)
                old.rmdir() if not any(old.iterdir()) else None
                print(f"이전: {old} → {new}")
        DRAFTS.mkdir(parents=True, exist_ok=True)
        CHATS.mkdir(parents=True, exist_ok=True)
        print(f"데이터: {DATA}")
        print("Hermes:", "연결됨" if hermes_ok() else "없음 (키 없이 로컬만)")
        print("모델 확인 중…")
        for p in refresh_models():
            print(f"  {p['icon']} {p['name']}: {len(p['models'])}개")
        if not _catalog:
            print("  ★ 쓸 수 있는 모델이 없다. ~/.hermes/.env 에 키를 넣거나"
                  " ollama 를 켜라")
        print("→ http://127.0.0.1:8787")
        # 브라우저 keep-alive 연결 하나가 서버 전체를 막는다 → 스레드 서버
        ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()
