#!/usr/bin/env python3
"""Grafana ダッシュボード JSON を生成する.

  python3 tools/gen_dashboard.py > chart/netmon/dashboards/netmon.json

色はエンティティ (ping 先・測定手段) ごとに固定。緑/黄/赤は状態 (しきい値) 専用で、系列色には使わない。
"""

import json

DS = {"type": "prometheus", "uid": "${datasource}"}

# エンティティ固定色 (dark テーマ上で CVD 検証済みの順序)
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
TARGET_COLORS = dict(zip(["router", "upstream", "cloudflare-dns", "google-dns", "iij"], SERIES_COLORS))
BACKEND_COLORS = {"ookla": SERIES_COLORS[0], "cloudflare": SERIES_COLORS[1]}
PHASE_COLORS = dict(zip(["idle", "download", "upload"], SERIES_COLORS))

_next_id = iter(range(1, 1000))


def color_overrides(mapping):
    return [{"matcher": {"id": "byName", "options": name},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
            for name, c in mapping.items()]


def target(expr, legend="", instant=False, fmt=None):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "refId": "A"}
    if instant:
        t.update(instant=True, range=False)
    if fmt:
        t["format"] = fmt
    return t


def stat(title, expr, unit, x, w, thresholds=None, desc="", decimals=None):
    steps = thresholds or [{"color": "text", "value": None}]
    p = {
        "id": next(_next_id), "type": "stat", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": 0, "w": w, "h": 4},
        "targets": [target(expr, instant=True)],
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps},
                                     "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none",
                    "textMode": "value", "justifyMode": "center"},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def timeseries(title, targets, unit, grid, overrides=(), desc="", points=False, minv=0, log=False):
    custom = {
        "drawStyle": "line", "lineWidth": 2, "lineInterpolation": "stepAfter", "fillOpacity": 0,
        "showPoints": "always" if points else "never", "pointSize": 8, "spanNulls": False,
        "axisSoftMin": minv,
    }
    if log:
        custom["scaleDistribution"] = {"type": "log", "log": 10}
        custom.pop("axisSoftMin")
    return {
        "id": next(_next_id), "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "gridPos": grid, "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}},
                        "overrides": list(overrides)},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def row(title, y):
    return {"id": next(_next_id), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


GOOD, WARN, BAD = "green", "yellow", "red"
TGT = 'target=~"$target"'

panels = [
    stat("下り速度 (最新)", 'netprobe_speedtest_download_bits_per_second{backend="$backend"}', "bps", 0, 4,
         desc="選択した測定手段による最新のスピードテスト結果"),
    stat("上り速度 (最新)", 'netprobe_speedtest_upload_bits_per_second{backend="$backend"}', "bps", 4, 4),
    stat("RTT (現在)",
         'quantile(0.5, netprobe_ping_rtt_seconds{stat="avg", layer="internet"})', "s", 8, 4,
         desc="layer=internet の ping 先それぞれの平均 RTT の中央値"),
    # 各ラウンドの送信数は同じなので、損失率の時間平均 = 期間全体の損失率。
    # (カウンタの increase() は Pod 再起動直後の系列で補間がずれ、負の値になることがあるため使わない)
    stat("損失率 (表示期間)",
         'avg(avg_over_time(netprobe_ping_loss_ratio{layer="internet"}[$__range]))',
         "percentunit", 12, 4, decimals=2,
         thresholds=[{"color": GOOD, "value": None}, {"color": WARN, "value": 0.005}, {"color": BAD, "value": 0.02}],
         desc="表示期間全体での layer=internet 宛て ping の損失率。0.5% 以上で黄、2% 以上で赤"),
    stat("最終 ping から", "time() - max(netprobe_ping_last_run_timestamp_seconds)", "s", 16, 4, decimals=0,
         thresholds=[{"color": GOOD, "value": None}, {"color": BAD, "value": 660}],
         desc="測定が止まっていないかの確認。5分間隔なので 11 分を超えたら赤"),
    stat("測定通信量 (24h)", "sum(increase(netprobe_speedtest_bytes_total[24h]))", "decbytes", 20, 4,
         desc="スピードテストが消費した上下合計のデータ量"),

    row("Ping (5分ごと)", 4),
    timeseries("RTT 平均", [target(f'netprobe_ping_rtt_seconds{{stat="avg", {TGT}}}', "{{target}}")], "s",
               {"x": 0, "y": 5, "w": 12, "h": 13}, color_overrides(TARGET_COLORS), log=True,
               desc="各ラウンド (既定20発) の平均 RTT。lan/upstream が悪化していれば宅内、internet だけなら回線側"),
    timeseries("パケット損失率", [target(f"netprobe_ping_loss_ratio{{{TGT}}}", "{{target}}")], "percentunit",
               {"x": 12, "y": 5, "w": 12, "h": 13}, color_overrides(TARGET_COLORS)),
    timeseries("ジッタ (RTT の標準偏差 mdev)", [target(f'netprobe_ping_rtt_seconds{{stat="mdev", {TGT}}}', "{{target}}")],
               "s", {"x": 0, "y": 18, "w": 12, "h": 13}, color_overrides(TARGET_COLORS)),
    timeseries("RTT 最大", [target(f'netprobe_ping_rtt_seconds{{stat="max", {TGT}}}', "{{target}}")], "s",
               {"x": 12, "y": 18, "w": 12, "h": 13}, color_overrides(TARGET_COLORS), log=True,
               desc="ラウンド内の最悪値。平均は良いのに最大が跳ねる場合は Wi-Fi の干渉や瞬間的な輻輳を疑う"),

    row("スピードテスト (1時間ごと)", 31),
    timeseries("下り速度", [target("netprobe_speedtest_download_bits_per_second", "{{backend}}")], "bps",
               {"x": 0, "y": 32, "w": 12, "h": 9}, color_overrides(BACKEND_COLORS), points=True),
    timeseries("上り速度", [target("netprobe_speedtest_upload_bits_per_second", "{{backend}}")], "bps",
               {"x": 12, "y": 32, "w": 12, "h": 9}, color_overrides(BACKEND_COLORS), points=True),
    timeseries("負荷時の遅延 (Bufferbloat)",
               [target('netprobe_speedtest_latency_seconds{backend="ookla"}', "{{phase}}")], "s",
               {"x": 0, "y": 41, "w": 12, "h": 8}, color_overrides(PHASE_COLORS), points=True,
               desc="Ookla による無負荷時 (idle) と、下り/上り転送中の遅延。転送中だけ大きく伸びるならルータのバッファ過多。Ookla 無効時は表示されない"),
    {
        "id": next(_next_id), "type": "table", "title": "最新の測定サーバ", "datasource": DS,
        "gridPos": {"x": 12, "y": 41, "w": 12, "h": 8},
        "targets": [target("netprobe_speedtest_info", instant=True, fmt="table")],
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "Value": True, "__name__": True, "instance": True, "job": True},
            "indexByName": {"backend": 0, "server_name": 1, "server_location": 2, "server_id": 3, "isp": 4},
            "renameByName": {"backend": "測定手段", "server_name": "サーバ", "server_location": "場所",
                             "server_id": "ID", "isp": "ISP"}}}],
        "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True},
    },
]

dashboard = {
    "uid": "netmon",
    "title": "インターネット接続品質",
    "tags": ["netmon"],
    "timezone": "browser",
    "schemaVersion": 41,
    "editable": True,
    "graphTooltip": 1,  # 全パネルで十字カーソルを共有
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "label": "データソース", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0},
        {"name": "target", "label": "ping 先", "type": "query", "datasource": DS,
         "query": {"query": "label_values(netprobe_ping_up, target)", "refId": "q"},
         "definition": "label_values(netprobe_ping_up, target)",
         "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"}, "refresh": 2, "sort": 0},
        {"name": "backend", "label": "速度測定 (上段)", "type": "query", "datasource": DS,
         "query": {"query": "label_values(netprobe_speedtest_success, backend)", "refId": "b"},
         "definition": "label_values(netprobe_speedtest_success, backend)",
         "multi": False, "includeAll": False, "current": {}, "refresh": 2, "sort": 1},
    ]},
    "panels": panels,
}

print(json.dumps(dashboard, ensure_ascii=False, indent=2))
