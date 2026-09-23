#!/bin/sh
# 一键核对：构建镜像 -> 启动页面服务(健康检查) -> 运行名为 verify 的单次服务
# 宿主机端口可用 HOST_PORT 覆盖，例如 HOST_PORT=9090 ./scripts/verify.sh
set -eu

cd "$(dirname "$0")/.."

export HOST_PORT="${HOST_PORT:-8080}"

echo "==> [1/3] 构建镜像"
docker compose build

echo "==> [2/3] 启动页面服务并等待健康检查 (宿主机端口 ${HOST_PORT})"
docker compose up -d web

cleanup() {
  docker compose stop verify >/dev/null 2>&1 || true
  docker compose down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# wait for healthy
i=0
status=""
while [ "$i" -lt 30 ]; do
  cid="$(docker compose ps -q web 2>/dev/null || true)"
  if [ -n "$cid" ]; then
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || true)"
    [ "$status" = "healthy" ] && break
  fi
  i=$((i + 1)); sleep 1
done
if [ "$status" != "healthy" ]; then
  echo "页面服务未通过健康检查（当前状态: ${status:-unknown}）" >&2
  docker compose logs web || true
  exit 1
fi

echo "==> [3/3] 运行 verify 单次服务（测试 / 同优分类 / 零增程 / HTTP 冒烟）"
docker compose run --rm verify
