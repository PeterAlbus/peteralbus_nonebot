#!/bin/bash
set -eo pipefail

OPS_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
OPS_RUNTIME_CONFIG="$OPS_REPO_ROOT/.env.macos"
OPS_API_CONFIG="$OPS_REPO_ROOT/.ops-api.env"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ -f "$OPS_RUNTIME_CONFIG" ] || fail "请先配置 .env.macos 中的 Conda 路径。"
[ -f "$OPS_API_CONFIG" ] || fail "请复制 ops-api.env.example 为 .ops-api.env 并填写固定 Token 与 NapCat 账号。"

source "$OPS_RUNTIME_CONFIG"
set -a
source "$OPS_API_CONFIG"
set +a

[[ "${CONDA_BASE:-}" == /* ]] || fail "请在 .env.macos 中填写 CONDA_BASE 绝对路径。"
[[ "${CONDA_ENV_PATH:-}" == /* ]] || fail "请在 .env.macos 中填写 CONDA_ENV_PATH 绝对路径。"
[ -f "$CONDA_BASE/etc/profile.d/conda.sh" ] || fail "找不到 Conda 初始化脚本。"
[ -x "$CONDA_ENV_PATH/bin/python" ] || fail "找不到 Conda 环境中的 Python。"

source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV_PATH"
cd "$OPS_REPO_ROOT"
exec "$CONDA_ENV_PATH/bin/python" -m ops_api
