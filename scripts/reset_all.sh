#!/bin/bash
# 彻底重置：删除所有容器、数据卷、日志、Docker 缓存，从头开始
set -e

echo "=== PolyMatrix Engine 彻底重置 ==="

# 1. 停止并删除容器 + 数据卷 + compose 镜像
echo "[1/4] 停止容器、删除数据卷和镜像..."
docker compose down -v --rmi all 2>/dev/null || docker-compose down -v --rmi all 2>/dev/null || true

# 2. 删除本机 data/logs（本地运行时产生的日志）
echo "[2/4] 删除本地日志目录..."
rm -rf ./data/logs 2>/dev/null || true

# 3. 清理 Docker 构建缓存
echo "[3/4] 清理 Docker 构建缓存..."
docker builder prune -af 2>/dev/null || true

# 4. 重新构建并启动（无缓存）
echo "[4/4] 无缓存重新构建..."
docker compose build --no-cache 2>/dev/null || docker-compose build --no-cache
docker compose up -d 2>/dev/null || docker-compose up -d

echo ""
echo "=== 重置完成 ==="
echo "数据库、Redis、日志已清空，容器已重新创建。"
echo "首次启动需等待 postgres 健康检查通过，约 10-20 秒。"
