PY ?= python3
export PYTHONPATH := .

.PHONY: help install test up down run run-local run-k8s incidents dashboard \
        demo-crash demo-cpu demo-memory demo-health \
        kind-up kind-down mon-k8s demo-pod-kill demo-bad-image demo-k8s-crash demo-k8s-cpu demo-k8s-memory demo-k8s-health zip clean

help:
	@echo "Docker mode : make up && make run            (then: make demo-crash|demo-cpu|demo-memory|demo-health)"
	@echo "Local mode  : make run-local                 (no Docker; app is a child process)"
	@echo "Kind mode   : make kind-up mon-k8s run-k8s   (then: make demo-pod-kill|demo-bad-image|demo-k8s-*)"
	@echo "Other       : make test | incidents | dashboard | down | kind-down | zip | clean"

install:
	$(PY) -m pip install -r requirements-dev.txt

test:
	$(PY) -m pytest tests -q

# ---- Docker Compose mode -----------------------------------------------------
up:
	docker compose up -d --build
	@echo "app http://localhost:8081  prometheus http://localhost:9090  grafana http://localhost:3000 (admin/admin)"

down:
	docker compose down

run:
	$(PY) -m orchestrator run --config config/docker.yaml

run-local:
	$(PY) -m orchestrator run --config config/local.yaml

incidents:
	$(PY) -m orchestrator incidents --config $${SHL_CONFIG:-config/docker.yaml}

demo-crash:
	$(PY) scripts/chaos/chaos.py container-crash --backend docker
demo-cpu:
	$(PY) scripts/chaos/chaos.py cpu --backend docker
demo-memory:
	$(PY) scripts/chaos/chaos.py memory --backend docker
demo-health:
	$(PY) scripts/chaos/chaos.py health-fail --backend docker

# ---- Kubernetes (Kind) mode ------------------------------------------------------
kind-up:
	kind create cluster --config kubernetes/kind-config.yaml
	docker build -t shl-app:latest .
	kind load docker-image shl-app:latest --name shl
	kubectl apply -f kubernetes/namespace.yaml -f kubernetes/rbac -f kubernetes/deployments -f kubernetes/services
	kubectl -n shl rollout status deployment/shl-app --timeout=120s

kind-down:
	kind delete cluster --name shl

mon-k8s:
	PROMETHEUS_CONFIG=./monitoring/prometheus/prometheus.k8s.yml docker compose up -d prometheus grafana

run-k8s:
	$(PY) -m orchestrator run --config config/kubernetes.yaml

demo-pod-kill:
	$(PY) scripts/chaos/chaos.py pod-kill --backend kubernetes
demo-bad-image:
	$(PY) scripts/chaos/chaos.py bad-image --backend kubernetes
demo-k8s-crash:
	$(PY) scripts/chaos/chaos.py container-crash --backend kubernetes
demo-k8s-cpu:
	$(PY) scripts/chaos/chaos.py cpu --backend kubernetes
demo-k8s-memory:
	$(PY) scripts/chaos/chaos.py memory --backend kubernetes
demo-k8s-health:
	$(PY) scripts/chaos/chaos.py health-fail --backend kubernetes

dashboard:
	$(PY) monitoring/grafana/generate_dashboard.py

zip:
	cd .. && zip -qr autonomous-self-healing-infrastructure.zip autonomous-self-healing-infrastructure \
	  -x '*/__pycache__/*' '*/.pytest_cache/*' '*/data/*' '*.pyc' '*/.git/*'

clean:
	rm -rf data .pytest_cache; find . -name __pycache__ -type d -exec rm -rf {} +
