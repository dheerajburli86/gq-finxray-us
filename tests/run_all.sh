#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
fail=0
for t in tests/test_*.py; do
  printf "  %-34s " "$(basename "$t")"
  if python "$t" >/tmp/out.$$ 2>&1; then echo PASS; else echo FAIL; cat /tmp/out.$$; fail=1; fi
done
rm -f /tmp/out.$$
[ $fail -eq 0 ] && echo && echo "ALL PASS" || { echo; echo "FAILURES"; exit 1; }
