#!/bin/sh
# 版本比较与本地最新内核探测（被 entrypoint.sh 引用）。
# 纯 POSIX sh 写法（dash / busybox ash 兼容）：无数组、无 [[ ]]、无 local。
#
# vercmp A B -> stdout: 1（A>B）/ 0（相等）/ -1（A<B）
# 支持 v 前缀与 -alpha.N / -beta.N / -rc.N 后缀；稳定版大于同号预览版。
# newest_local <dir> <fallback> -> 本地已有版本（缓存目录 + 传入兜底）里最新的 tag。

_ver_pre_rank() {
  case "$1" in
    alpha*) _r=1;;
    beta*) _r=2;;
    pre*) _r=2;;
    rc*) _r=3;;
    *) _r=0;;
  esac
  _n="${1##*.}"
  case "$_n" in '' | *[!0-9]*) _n=0;; esac
  echo "$_r $_n"
}

vercmp() {
  _a="${1#v}"; _a="${_a#V}"
  _b="${2#v}"; _b="${_b#V}"
  _ac="${_a%%-*}"; _bc="${_b%%-*}"
  case "$_a" in *-*) _ap="${_a#*-}";; *) _ap="";; esac
  case "$_b" in *-*) _bp="${_b#*-}";; *) _bp="";; esac
  _ra="$_ac"; _rb="$_bc"
  while [ -n "${_ra}${_rb}" ]; do
    case "$_ra" in *.*) _x="${_ra%%.*}"; _ra="${_ra#*.}";; *) _x="$_ra"; _ra="";; esac
    case "$_rb" in *.*) _y="${_rb%%.*}"; _rb="${_rb#*.}";; *) _y="$_rb"; _rb="";; esac
    case "$_x" in '' | *[!0-9]*) _x=0;; esac
    case "$_y" in '' | *[!0-9]*) _y=0;; esac
    if [ "$_x" -gt "$_y" ]; then echo 1; return 0; fi
    if [ "$_x" -lt "$_y" ]; then echo -1; return 0; fi
  done
  if [ -z "$_ap" ] && [ -z "$_bp" ]; then echo 0; return 0; fi
  if [ -z "$_ap" ]; then echo 1; return 0; fi
  if [ -z "$_bp" ]; then echo -1; return 0; fi
  set -- $(_ver_pre_rank "$_ap"); _ar1="$1"; _ar2="$2"
  set -- $(_ver_pre_rank "$_bp"); _br1="$1"; _br2="$2"
  if [ "$_ar1" -gt "$_br1" ]; then echo 1; return 0; fi
  if [ "$_ar1" -lt "$_br1" ]; then echo -1; return 0; fi
  if [ "$_ar2" -gt "$_br2" ]; then echo 1; return 0; fi
  if [ "$_ar2" -lt "$_br2" ]; then echo -1; return 0; fi
  echo 0
}

newest_local() {
  _dir="$1"; _best="$2"
  for _d in "$_dir"/*/; do
    [ -d "$_d" ] || continue
    [ -x "${_d}sing-box" ] || continue
    _v="$(basename "$_d")"
    if [ "$(vercmp "$_v" "$_best")" = "1" ]; then _best="$_v"; fi
  done
  echo "$_best"
}

# max_version A B -> 较新的那个（相等取 B；空值取另一方）
max_version() {
  if [ -z "$1" ]; then echo "$2"; return 0; fi
  if [ -z "$2" ]; then echo "$1"; return 0; fi
  if [ "$(vercmp "$1" "$2")" = "1" ]; then echo "$1"; else echo "$2"; fi
}
