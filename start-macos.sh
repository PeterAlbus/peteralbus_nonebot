#!/bin/bash
set -eo pipefail

# 前台启动；常驻运行由 launchd 管理。
NB_WORKDIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNTIME_CONFIG="$NB_WORKDIR/.env.macos"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ "$#" -eq 0 ] || fail "用法: $0（前台启动；服务管理参见 docs/deploy-macos.md）"
[ -f "$RUNTIME_CONFIG" ] || fail "请复制 .env.macos.example 为 .env.macos 并填写 Conda 路径。"
source "$RUNTIME_CONFIG"

[[ "${CONDA_BASE:-}" == /* ]] || fail "请在 .env.macos 中填写 CONDA_BASE 的绝对路径。"
[[ "${CONDA_ENV_PATH:-}" == /* ]] || fail "请在 .env.macos 中填写 CONDA_ENV_PATH 的绝对路径。"
[ -f "$CONDA_BASE/etc/profile.d/conda.sh" ] || fail "找不到 Conda 初始化脚本: $CONDA_BASE/etc/profile.d/conda.sh"
[ -x "$CONDA_ENV_PATH/bin/python" ] || fail "找不到环境中的 Python: $CONDA_ENV_PATH/bin/python"
[ -x "$CONDA_ENV_PATH/bin/nb" ] || fail "找不到环境中的 nb；请先在此环境安装项目依赖。"
[ -f "$NB_WORKDIR/.env" ] || fail "请复制 .env.example 为 .env 并填写机器人配置。"

CONDA_ENV_PATH="$(cd -- "$CONDA_ENV_PATH" && pwd -P)"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV_PATH"
[ "$(cd -- "$CONDA_PREFIX" && pwd -P)" = "$CONDA_ENV_PATH" ] || fail "Conda 激活后的环境与配置不一致。"

cd "$NB_WORKDIR"
exec "$CONDA_ENV_PATH/bin/nb" --python "$CONDA_ENV_PATH/bin/python" --no-venv run
