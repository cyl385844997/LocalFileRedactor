#!/usr/bin/env bash
# ★ 写完测试文件后**必须**先跑这个 —— 测试函数名里不能有非法标识符字符。
#
# 已踩的坑（累计 5 次）：
#   v0.6.3 ×1  def test_★xxx    -> SyntaxError: invalid character '★' (U+2605)
#   v0.7.0 ×2  同上
#   v0.7.1 ×1  同上
#   v0.8.0 ×1  def test_找()在… -> SyntaxError: expected ':'
#              （连括号都进了函数名 —— 写中文说明时容易带出括号）
#
# 合法字符：中文、英文字母、数字、下划线
# 非法：★☆→← 等符号，以及 ( ) [ ] " ' , : 等标点
#
# 用法：bash scripts/check_star_ids.sh [文件...]
#        不给文件则扫 tests/ 与 src/ 下全部 .py
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

if [ "$#" -gt 0 ]; then
  files=("$@")
else
  mapfile -t files < <(find tests src -name '*.py' -type f 2>/dev/null)
fi

bad=0
for f in "${files[@]}"; do
  # 抓 def/class 后面紧跟的**名字**（标识符：从 def/class 后的第一个非空白起，
  # 到左括号或冒号为止）。名字里出现非 [中文A-Za-z0-9_] 字符即报错。
  #注意排除类型标注（def f(a: int) -> None）—— 它在括号内，不在名字里。
  hits=$(grep -oP '^\s*(?:def|class)\s+\K[^\s(:]+' "$f" 2>/dev/null \
    | grep -P '[^\x{4e00}-\x{9fff}A-Za-z0-9_]' || true)
  if [ -n "$hits" ]; then
    echo "❌ $f"
    echo "$hits" | sed 's/^/    /'
    bad=1
  fi
done

if [ "$bad" -eq 0 ]; then
  echo "✅ ${#files[@]} 个文件：def/class 名标识符全部合法"
  exit 0
fi

echo ""
echo "修法：名字里去掉符号与括号。"
echo "  def test_★xxx   ->  def test_标记xxx"
echo "  def test_找()xxx ->  def test_标记findxxx"
exit 1