# netmon のビルド・デプロイ
#   make            … イメージをビルドしてレジストリへ push し、Helm でデプロイ
#   make test       … ユニットテスト + helm lint/template
#   make dashboard  … tools/gen_dashboard.py から Grafana ダッシュボード JSON を再生成
#   make package    … Helm チャート (.tgz) を dist/ に作成
#
# 変数: REGISTRY (既定 localhost:5000), VALUES (既定 values-local.yaml、なければ examples/values-k3s.yaml), NAMESPACE

VERSION   := $(shell sed -n 's/^appVersion: "\(.*\)"/\1/p' chart/netmon/Chart.yaml)
REGISTRY  ?= localhost:5000
IMAGE     := $(REGISTRY)/netprobe:$(VERSION)
NAMESPACE ?= netmon
VALUES    ?= $(if $(wildcard values-local.yaml),values-local.yaml,examples/values-k3s.yaml)
export KUBECONFIG ?= $(HOME)/.kube/config

.PHONY: all image push dashboard test lint deploy package registry status uninstall

all: push deploy

image:
	docker build -t $(IMAGE) image/

push: image
	docker push $(IMAGE)

dashboard:
	python3 tools/gen_dashboard.py > chart/netmon/dashboards/netmon.json

test: lint
	@if python3 -c 'import prometheus_client' 2>/dev/null; then \
	  python3 -m unittest discover -s tests; \
	else \
	  echo "(prometheus_client がないのでコンテナ内で実行)"; \
	  docker run --rm -v $(CURDIR):/src:ro -w /src python:3.14-slim-trixie \
	    sh -c 'pip install -q --root-user-action=ignore -r image/requirements.txt && python3 -m unittest discover -s tests'; \
	fi

lint: dashboard
	helm lint chart/netmon
	@for f in examples/*.yaml; do helm template netmon chart/netmon -f $$f > /dev/null && echo "template OK: $$f" || exit 1; done

deploy: lint
	helm upgrade --install netmon chart/netmon -n $(NAMESPACE) --create-namespace -f $(VALUES) \
	  --set probe.image.repository=$(REGISTRY)/netprobe --wait

package: lint
	helm package chart/netmon -d dist/

# k3s から pull するためのローカルレジストリ (127.0.0.1 のみで待ち受け、Docker 起動時に自動起動)。
# k3s (containerd) は localhost のレジストリには HTTP で接続できるので、registries.yaml の設定は不要
registry:
	docker run -d --restart=unless-stopped --name local-registry \
	  -p 127.0.0.1:5000:5000 -v local-registry:/var/lib/registry registry:3.1.2

status:
	kubectl -n $(NAMESPACE) get pods,pvc,ingress
	kubectl -n $(NAMESPACE) logs deploy/netmon-probe --tail=20

uninstall:
	helm uninstall netmon -n $(NAMESPACE)
	@echo "PVC (Prometheus/Grafana のデータ) は helm.sh/resource-policy: keep で残しています。消す場合:"
	@echo "  kubectl -n $(NAMESPACE) delete pvc -l app.kubernetes.io/part-of=netmon"
