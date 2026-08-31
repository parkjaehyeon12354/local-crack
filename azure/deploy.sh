#!/usr/bin/env bash
# 크랙빌더를 Azure Functions 로 올린다.
#
# ★server.py 와 HTML 을 azure/ 안으로 **복사**해 함께 싼다 — Functions 는
#   패키지 밖 파일을 못 본다. 원본은 그대로 두고 여기서만 복제한다.
# ★윈도우 az CLI 는 WSL 경로(/tmp)를 못 읽어서 zip 을 C: 밑에 만든다.
set -euo pipefail
cd "$(dirname "$0")"
APP=${APP:-func-crack-8098496}
RG=${RG:-rg-crack}
ZIP_WIN='C:\Users\yuyub\pwtest\crack-full.zip'
ZIP=/mnt/c/Users/yuyub/pwtest/crack-full.zip

rm -rf pkg && mkdir pkg
cp function_app.py requirements.txt host.json pkg/
cp ../server.py pkg/
cp ../*.html ../mobile.css ../mobile.js pkg/

python3 - "$ZIP" <<'PY'
import sys, zipfile, os
z = zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED)
for root, _, files in os.walk("pkg"):
    for f in files:
        p = os.path.join(root, f)
        z.write(p, os.path.relpath(p, "pkg"))
z.close()
print("zip", round(os.path.getsize(sys.argv[1]) / 1024), "KB")
PY

az functionapp deployment source config-zip -n "$APP" -g "$RG" \
   --src "$ZIP_WIN" --build-remote true -o none
echo "배포 요청 보냄"
