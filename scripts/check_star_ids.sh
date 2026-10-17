#!/usr/bin/env bash
# ★ 写完测试文件后**必须**先跑这个 —— ★ 不能进 Python 标识符。
# 失效形态：def test_★xxx -> SyntaxError: invalid character '★' (U+2605)
#   已踩 4 次（v0.6.3 ×1 / v0.7.0 ×2 / v0.7.1 ×1）。
#
# 用法：bash scripts/check_star_ids.sh [文件...]
#        不给文件则扫 tests/ 下全部 .py
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

if [ "$#" -gt 0 ]; then
  files=("$@")
else
  mapfile -t files < <(find tests scripts -name '*.py' -type f 2>/dev/null)
fi

bad=0
for f in "${files[@]}"; do
  # Python 标识符位置（行首缩进后的 def/class 后）出现 ★ 或其他非 ASCII 符号
  hits=$(grep -nE '^\s*(def|class)\s+[^(:]*[★☆▲●◆■□※→←]' "$f" 2>/dev/null)
  if [ -n "$hits" ]; then
    echo "❌ $f —— 标识符含非法符号："
    echo "$hits" | sed 's/^/    /'
    bad=1
  fi
done

if [ "$bad" -eq 0 ]; then
  echo "✅ 全部 ${#files[@]} 个文件的 def/class 名标识符合法"
  exit 0
fi

echo ""
echo "修法：把 'def test_★xxx' 改成 'def test_标记xxx'（中文可进标识符，★ 不可）"
exit 1