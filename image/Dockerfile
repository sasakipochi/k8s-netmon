# netprobe: ping / speedtest を定期実行して Prometheus 形式で公開するエクスポーター
#
# Ookla Speedtest CLI はライセンス上の理由で同梱しない。
# 使う場合は Helm チャートの initContainer が `netprobe.py fetch-ookla` で起動時に取得する。
FROM python:3.14-slim-trixie

LABEL org.opencontainers.image.title="netprobe" \
      org.opencontainers.image.description="Internet connection quality exporter (ping + speedtest) for Prometheus" \
      org.opencontainers.image.source="https://github.com/sasakipochi/k8s-netmon" \
      org.opencontainers.image.licenses="MIT"

# ping バイナリに cap_net_raw を付けておく (hostNetwork でホストの ping_group_range が閉じている場合用)
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping ca-certificates libcap2-bin \
 && setcap cap_net_raw+p /bin/ping \
 && apt-get purge -y libcap2-bin && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY netprobe.py /app/netprobe.py

# Ookla CLI は $HOME/.config にライセンス同意情報を書くので書き込み可能な場所にする
ENV HOME=/tmp PYTHONUNBUFFERED=1 PROMETHEUS_DISABLE_CREATED_SERIES=true
USER 65532:65532
EXPOSE 9101
ENTRYPOINT ["python3", "/app/netprobe.py"]
