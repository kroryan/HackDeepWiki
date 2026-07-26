#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

test -x dist/hackdeepwiki/hackdeepwiki
test ! -e AppDir || {
  echo "AppDir already exists; run scripts/clean_build.py --appdir first" >&2
  exit 2
}

mkdir -p AppDir/usr/bin
cp -a dist/hackdeepwiki/. AppDir/usr/bin/
cp public/hackdeepwiki.png AppDir/hackdeepwiki.png

cp scripts/appimage/AppRun AppDir/AppRun
cp scripts/appimage/hackdeepwiki.desktop AppDir/hackdeepwiki.desktop
chmod +x AppDir/AppRun

read_manifest() {
  python -c '
import json,sys
data=json.load(open("build/components.json", encoding="utf-8"))
value=data
for part in sys.argv[1].split("."):
    value=value[part]
print(value)
' "$1"
}

tool_url="$(read_manifest appimage.tool.url)"
tool_sha="$(read_manifest appimage.tool.sha256)"
runtime_url="$(read_manifest appimage.runtime.url)"
runtime_sha="$(read_manifest appimage.runtime.sha256)"

curl -fL --retry 3 -o appimagetool-x86_64.AppImage "$tool_url"
printf '%s  %s\n' "$tool_sha" appimagetool-x86_64.AppImage | sha256sum -c -
curl -fL --retry 3 -o appimage-runtime-x86_64 "$runtime_url"
printf '%s  %s\n' "$runtime_sha" appimage-runtime-x86_64 | sha256sum -c -
chmod +x appimagetool-x86_64.AppImage

export APPIMAGE_EXTRACT_AND_RUN=1
./appimagetool-x86_64.AppImage \
  --no-appstream \
  --runtime-file appimage-runtime-x86_64 \
  AppDir HackDeepWiki-x86_64.AppImage
