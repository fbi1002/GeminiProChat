#!/usr/bin/env bash
set -euo pipefail

echo "=== Juyu Offline Agent cloud build ==="
export JAVA_HOME="${JAVA_HOME_17_X64:-${JAVA_HOME:-}}"
export PATH="$JAVA_HOME/bin:$PATH"
java -version

WORK="${RUNNER_TEMP:-/tmp}/juyu-offline-build"
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"

echo "[1/7] Clone Juyu"
git clone --depth 1 https://github.com/goehou/Juyu-phone-agent.git Juyu

echo "[2/7] Clone llama.cpp into Juyu"
mkdir -p Juyu/third_party
git clone --depth 1 https://github.com/ggml-org/llama.cpp.git Juyu/third_party/llama.cpp

echo "[3/7] Decode and apply offline patch"
base64 -d "$GITHUB_WORKSPACE/juyu_offline_agent_patch.py.gz.b64" | gzip -dc > "$WORK/juyu_offline_agent_patch.py"
python3 -m pip install --user --disable-pip-version-check opencc-python-reimplemented
python3 "$WORK/juyu_offline_agent_patch.py" "$WORK/Juyu"

echo "[4/7] Install Android SDK components"
SDKMANAGER=""
for p in "${ANDROID_HOME:-}/cmdline-tools/latest/bin/sdkmanager" "${ANDROID_SDK_ROOT:-}/cmdline-tools/latest/bin/sdkmanager" "$(command -v sdkmanager || true)"; do
  if [ -n "$p" ] && [ -x "$p" ]; then SDKMANAGER="$p"; break; fi
done
if [ -z "$SDKMANAGER" ]; then
  echo "sdkmanager not found"
  exit 10
fi
yes | "$SDKMANAGER" --licenses >/dev/null 2>&1 || true
"$SDKMANAGER" "platforms;android-35" "build-tools;35.0.0" "ndk;29.0.13113456" "cmake;3.22.1"

echo "[5/7] Build debug APK"
cd "$WORK/Juyu"
chmod +x gradlew
./gradlew --version
./gradlew :app:assembleDebug --stacktrace --no-daemon

APK="$WORK/Juyu/app/build/outputs/apk/debug/app-debug.apk"
test -s "$APK"
sha256sum "$APK"
ls -lh "$APK"

echo "[6/7] Structural checks"
unzip -l "$APK" | grep -E 'lib/arm64-v8a/.*(ai-chat|llama|ggml).*\.so' | head -30
unzip -p "$APK" AndroidManifest.xml >/dev/null

echo "[7/7] Upload temporary unsigned/debug APK"
RESP="$(curl -fsS -F "file=@$APK" https://tmpfiles.org/api/v1/upload || true)"
echo "TMPFILES_RESPONSE=$RESP"
URL="$(python3 - <<'PY' "$RESP"
import json,sys
s=sys.argv[1]
try:
    u=json.loads(s)["data"]["url"]
    if "tmpfiles.org/" in u and "/dl/" not in u:
        u=u.replace("tmpfiles.org/","tmpfiles.org/dl/",1)
    print(u)
except Exception:
    pass
PY
)"
if [ -z "$URL" ]; then
  echo "UPLOAD_FAILED"
  exit 22
fi
echo "JuyuOfflineAPK_URL=$URL"

# trigger ci
