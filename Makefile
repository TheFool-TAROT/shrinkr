.PHONY: help install dev run test cov lint fmt bench micro docker up clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## 安装运行依赖
	pip install -r requirements.txt

dev: ## 安装开发依赖
	pip install -r requirements-dev.txt

run: ## 本地启动（热重载）
	uvicorn app.main:app --reload --port 8000

test: ## 跑测试
	pytest

cov: ## 跑测试 + 覆盖率
	pytest --cov=app --cov-report=term-missing

lint: ## 静态检查
	ruff check .

fmt: ## 自动修格式
	ruff check --fix . && ruff format .

bench: ## 压测读路径（需要先 make run）
	python bench/loadtest.py --requests 20000 --concurrency 100

micro: ## 数据结构微基准
	python bench/micro.py

docker: ## 构建镜像
	docker build -t shrinkr:local .

up: ## docker compose 一键起
	docker compose up -d

clean: ## 清掉本地数据
	rm -rf data .pytest_cache .ruff_cache && find . -name __pycache__ -type d -exec rm -rf {} +