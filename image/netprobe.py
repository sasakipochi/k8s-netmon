#!/usr/bin/env python3
"""netprobe: インターネット接続品質を定期測定し Prometheus 形式で公開するエクスポーター.

- ping: 設定した複数ターゲットへ N 発ずつ ICMP echo を打ち、損失率と RTT 統計を記録
- speedtest: Ookla Speedtest CLI / Cloudflare (speed.cloudflare.com) で上下帯域を記録
- 各測定結果は /metrics (Prometheus) に公開し、同時に JSONL で生ログとしても保存する

設定は JSON (ConfigMap でマウント) で受け取る。依存は標準ライブラリ + prometheus_client のみ。

  netprobe.py                     … エクスポーターとして起動 (設定: $NETPROBE_CONFIG)
  netprobe.py fetch-ookla DIR     … Ookla Speedtest CLI を DIR にダウンロードする (initContainer 用)
"""

import http.client
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import tarfile
import threading
import time
import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger("netprobe")

# ---------------------------------------------------------------- metrics
PING_LABELS = ["target", "host", "layer"]
ping_up = Gauge("netprobe_ping_up", "1 if at least one reply was received in the last round", PING_LABELS)
ping_loss = Gauge("netprobe_ping_loss_ratio", "Packet loss ratio (0-1) of the last round", PING_LABELS)
ping_rtt = Gauge("netprobe_ping_rtt_seconds", "RTT statistics of the last round", PING_LABELS + ["stat"])
ping_sent = Counter("netprobe_ping_packets_sent_total", "ICMP echo requests sent", PING_LABELS)
ping_recv = Counter("netprobe_ping_packets_received_total", "ICMP echo replies received", PING_LABELS)
ping_last = Gauge("netprobe_ping_last_run_timestamp_seconds", "Unix time of the last ping round", PING_LABELS)

ST_LABELS = ["backend"]
st_down = Gauge("netprobe_speedtest_download_bits_per_second", "Download throughput", ST_LABELS)
st_up = Gauge("netprobe_speedtest_upload_bits_per_second", "Upload throughput", ST_LABELS)
st_lat = Gauge("netprobe_speedtest_latency_seconds", "Latency measured by the speedtest (idle / under load)",
               ST_LABELS + ["phase"])
st_jitter = Gauge("netprobe_speedtest_jitter_seconds", "Idle jitter measured by the speedtest", ST_LABELS)
st_loss = Gauge("netprobe_speedtest_packet_loss_ratio", "Packet loss reported by the speedtest (Ookla only)", ST_LABELS)
st_bytes = Counter("netprobe_speedtest_bytes_total", "Bytes transferred by speedtests", ST_LABELS + ["direction"])
st_ok = Gauge("netprobe_speedtest_success", "1 if the last speedtest succeeded", ST_LABELS)
st_last = Gauge("netprobe_speedtest_last_run_timestamp_seconds", "Unix time of the last speedtest attempt", ST_LABELS)
st_dur = Gauge("netprobe_speedtest_duration_seconds", "Wall time of the last speedtest", ST_LABELS)
st_info = Gauge("netprobe_speedtest_info", "Server/ISP information of the last successful test (value is always 1)",
                ST_LABELS + ["server_id", "server_name", "server_location", "isp"])
st_fail = Counter("netprobe_speedtest_failures_total", "Failed speedtests", ST_LABELS)


# ---------------------------------------------------------------- raw log
class RawLog:
    """測定結果を <dir>/<kind>-YYYY-MM.jsonl に追記する (月ごとにファイルを分ける)."""

    def __init__(self, directory):
        self.dir = directory
        self.lock = threading.Lock()
        if self.dir:
            os.makedirs(self.dir, exist_ok=True)

    def write(self, kind, record):
        if not self.dir:
            return
        now = datetime.now(timezone.utc)
        record = {"time": now.isoformat(timespec="seconds"), "kind": kind, **record}
        path = os.path.join(self.dir, f"{kind}-{now:%Y-%m}.jsonl")
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self.lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                log.warning("raw log write failed: %s", e)


# ---------------------------------------------------------------- ping
PING_SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
PING_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")


RTT_STATS = ("min", "avg", "max", "mdev")


def parse_ping(out, count):
    """iputils ping -q の出力を解析する. RTT は応答がなければ None."""
    m = PING_SUMMARY.search(out)
    sent, recv = (int(m.group(1)), int(m.group(2))) if m else (count, 0)
    r = PING_RTT.search(out)
    return {
        "sent": sent,
        "received": recv,
        "loss": 1.0 - recv / sent if sent else 1.0,
        "rtt_ms": dict(zip(RTT_STATS, map(float, r.groups()))) if r else None,
        "parsed": bool(m),
    }


def ping_once(target, cfg, rawlog):
    name, host, layer = target["name"], target["host"], target.get("layer", "internet")
    count, interval, timeout = cfg["count"], cfg["interval"], cfg["timeout"]
    cmd = ["ping", "-n", "-q", "-c", str(count), "-i", str(interval), "-W", str(timeout), host]
    if target.get("ipv6"):
        cmd.insert(1, "-6")
    deadline = count * interval + timeout + 10
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        out = ""
    res = parse_ping(out, count)
    sent, recv, loss = res["sent"], res["received"], res["loss"]
    labels = dict(target=name, host=host, layer=layer)
    ping_sent.labels(**labels).inc(sent)
    ping_recv.labels(**labels).inc(recv)
    ping_loss.labels(**labels).set(loss)
    ping_up.labels(**labels).set(1 if recv else 0)
    ping_last.labels(**labels).set(time.time())
    rec = {"target": name, "host": host, "layer": layer, "sent": sent, "received": recv, "loss": round(loss, 4)}
    for stat in RTT_STATS:
        if res["rtt_ms"]:
            v = res["rtt_ms"][stat]
            ping_rtt.labels(**labels, stat=stat).set(v / 1000)
            rec[f"rtt_{stat}_ms"] = v
        else:
            # 全損時は直前の値を残さず消す (グラフ上で欠損として見えるように)
            try:
                ping_rtt.remove(name, host, layer, stat)
            except KeyError:
                pass
    if not res["parsed"]:
        rec["error"] = out.strip()[-300:]
    rawlog.write("ping", rec)
    log.info("ping %-12s %-16s loss=%.0f%% avg=%s", name, host, loss * 100, rec.get("rtt_avg_ms"))


def ping_round(cfg, rawlog):
    with ThreadPoolExecutor(max_workers=max(1, len(cfg["targets"]))) as ex:
        for t in cfg["targets"]:
            ex.submit(ping_once, t, cfg, rawlog)


# ---------------------------------------------------------------- speedtest: Ookla
# Ookla Speedtest CLI は個人・非商用利用のみ許諾されたプロプライエタリソフトウェアなので、
# コンテナイメージには同梱しない。利用者が EULA に同意した場合のみ fetch-ookla で各自の環境に取得する。
OOKLA_URL = "https://install.speedtest.net/app/cli/ookla-speedtest-{version}-linux-{arch}.tgz"
OOKLA_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64",
              "armv7l": "armhf", "armv6l": "armel", "i686": "i386"}


def fetch_ookla(dest, version="1.2.0"):
    arch = OOKLA_ARCH.get(platform.machine())
    if not arch:
        raise SystemExit(f"unsupported architecture: {platform.machine()}")
    url = OOKLA_URL.format(version=version, arch=arch)
    log.info("downloading %s", url)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        member = tf.getmember("speedtest")
        path = os.path.join(dest, "speedtest")
        with tf.extractfile(member) as src, open(path, "wb") as dst:
            dst.write(src.read())
    os.chmod(path, 0o755)
    log.info("installed %s", path)


def parse_ookla_output(text):
    """speedtest --format=json の出力 (1行1JSON) から result とエラーメッセージを取り出す."""
    result, errors = None, []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "result":
            result = obj
        elif obj.get("level") == "error":
            errors.append(obj.get("message", ""))
    return result, errors


def summarize_ookla(result):
    d, u, pi = result["download"], result["upload"], result["ping"]
    srv = result.get("server", {})
    return {
        "download_bps": d["bandwidth"] * 8,
        "upload_bps": u["bandwidth"] * 8,
        "download_bytes": d["bytes"],
        "upload_bytes": u["bytes"],
        "latency_idle_ms": pi["latency"],
        "jitter_ms": pi["jitter"],
        "latency_download_ms": d.get("latency", {}).get("iqm"),
        "latency_upload_ms": u.get("latency", {}).get("iqm"),
        "packet_loss_pct": result.get("packetLoss"),
        "server_id": str(srv.get("id", "")),
        "server_name": srv.get("name", ""),
        "server_location": srv.get("location", ""),
        "isp": result.get("isp", ""),
        "result_url": result.get("result", {}).get("url"),
    }


def speedtest_ookla(cfg):
    if not cfg.get("acceptLicense"):
        raise RuntimeError("Ookla EULA not accepted (set speedtest.ookla.acceptLicense: true)")
    cmd = [cfg.get("binary", "speedtest"), "--accept-license", "--accept-gdpr", "--format=json", "--progress=no"]
    if cfg.get("serverId"):
        cmd.append(f"--server-id={cfg['serverId']}")
    # サーバ選択時などに一時的に失敗することがあるので1回だけリトライする
    for _ in range(2):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.get("timeout", 180))
        result, errors = parse_ookla_output(p.stdout + "\n" + p.stderr)
        if result:
            return summarize_ookla(result)
    raise RuntimeError(f"no result (rc={p.returncode}): {' / '.join(errors)[-300:]}")


# ---------------------------------------------------------------- speedtest: Cloudflare
CF_HOST = "speed.cloudflare.com"
CF_BASE = f"https://{CF_HOST}"
UA = "netprobe/1.0 (personal network quality monitor)"


def _cf_latency(samples=10):
    """keep-alive 接続で __down?bytes=0 を繰り返し、Cloudflare 側が測った TCP RTT (Server-Timing の cfL4 rtt) を集める.

    クライアント側の TTFB は Worker の処理時間 (数十 ms) を含んでしまうため使わない。
    """
    conn = http.client.HTTPSConnection(CF_HOST, timeout=10)
    rtts = []
    try:
        for _ in range(samples):
            conn.request("GET", "/__down?bytes=0", headers={"User-Agent": UA})
            r = conn.getresponse()
            r.read()
            timing = ",".join(v for k, v in r.getheaders() if k.lower() == "server-timing")
            m = re.search(r"[?&]rtt=(\d+)", timing)
            if m:
                rtts.append(int(m.group(1)) / 1000)  # us -> ms
    finally:
        conn.close()
    if not rtts:
        raise RuntimeError("cfL4 rtt not found in Server-Timing header")
    med = sorted(rtts)[len(rtts) // 2]
    jitter = sum(abs(a - b) for a, b in zip(rtts, rtts[1:])) / max(1, len(rtts) - 1)
    return med, jitter


def _cf_transfer(direction, streams, duration, chunk):
    """duration 秒間、streams 本の並列接続で chunk バイトずつ転送し続け、合計バイト数を返す."""
    stop_at = time.perf_counter() + duration
    total = [0] * streams
    payload = b"\0" * chunk if direction == "up" else None

    def worker(i):
        while time.perf_counter() < stop_at:
            if direction == "down":
                req = urllib.request.Request(f"{CF_BASE}/__down?bytes={chunk}", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    while True:
                        buf = r.read(256 * 1024)
                        if not buf:
                            break
                        total[i] += len(buf)
                        if time.perf_counter() >= stop_at:
                            break
            else:
                req = urllib.request.Request(f"{CF_BASE}/__up", data=payload, method="POST",
                                             headers={"User-Agent": UA, "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    r.read()
                total[i] += chunk

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=streams) as ex:
        list(ex.map(worker, range(streams)))
    return sum(total), time.perf_counter() - t0


def speedtest_cloudflare(cfg):
    streams, duration = cfg.get("streams", 4), cfg.get("duration", 10)
    lat, jit = _cf_latency()
    db, dt = _cf_transfer("down", streams, duration, cfg.get("downloadChunkBytes", 25_000_000))
    ub, ut = _cf_transfer("up", streams, duration, cfg.get("uploadChunkBytes", 5_000_000))
    return {
        "download_bps": db * 8 / dt,
        "upload_bps": ub * 8 / ut,
        "download_bytes": db,
        "upload_bytes": ub,
        "latency_idle_ms": lat,
        "jitter_ms": jit,
        "server_id": "",
        "server_name": "speed.cloudflare.com",
        "server_location": "",
        "isp": "",
    }


BACKENDS = {"ookla": speedtest_ookla, "cloudflare": speedtest_cloudflare}


def speedtest_round(cfg, rawlog):
    # 帯域を食い合わないよう、バックエンドは順番に実行する
    for name, bcfg in cfg["backends"].items():
        if not bcfg.get("enabled"):
            continue
        t0 = time.time()
        st_last.labels(name).set(t0)
        try:
            r = BACKENDS[name](bcfg)
        except Exception as e:  # noqa: BLE001  測定失敗は記録して続行する
            st_ok.labels(name).set(0)
            st_fail.labels(name).inc()
            rawlog.write("speedtest", {"backend": name, "success": False, "error": str(e)[:500]})
            log.warning("speedtest %s failed: %s", name, e)
            continue
        dur = time.time() - t0
        st_ok.labels(name).set(1)
        st_dur.labels(name).set(dur)
        st_down.labels(name).set(r["download_bps"])
        st_up.labels(name).set(r["upload_bps"])
        st_bytes.labels(name, "download").inc(r["download_bytes"])
        st_bytes.labels(name, "upload").inc(r["upload_bytes"])
        st_lat.labels(name, "idle").set(r["latency_idle_ms"] / 1000)
        st_jitter.labels(name).set(r["jitter_ms"] / 1000)
        for phase in ("download", "upload"):
            v = r.get(f"latency_{phase}_ms")
            if v is not None:
                st_lat.labels(name, phase).set(v / 1000)
        if r.get("packet_loss_pct") is not None:
            st_loss.labels(name).set(r["packet_loss_pct"] / 100)
        # info は最新1件だけ残す
        _drop_info(name)
        st_info.labels(name, r["server_id"], r["server_name"], r["server_location"], r["isp"]).set(1)
        rawlog.write("speedtest", {"backend": name, "success": True, "duration_s": round(dur, 1), **r})
        log.info("speedtest %-10s down=%.1fMbps up=%.1fMbps lat=%.1fms (%.0fs)", name,
                 r["download_bps"] / 1e6, r["upload_bps"] / 1e6, r["latency_idle_ms"], dur)


def _drop_info(backend):
    for labels in list(st_info._metrics):  # noqa: SLF001  prometheus_client に部分削除 API がないため
        if labels[0] == backend:
            st_info.remove(*labels)


# ---------------------------------------------------------------- scheduler
def every(interval, offset, fn, stop):
    """interval 秒ごと (壁時計に揃えて + offset 秒) に fn を実行する."""
    while not stop.is_set():
        now = time.time()
        nxt = (now - offset) // interval * interval + interval + offset
        if stop.wait(nxt - now):
            return
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("job failed")


def init_counters(pc, sc):
    """カウンタを 0 で作っておく. 初回測定で系列が突然現れると increase() が最初の増分を取りこぼすため."""
    for t in pc.get("targets", []) if pc.get("enabled") else []:
        labels = dict(target=t["name"], host=t["host"], layer=t.get("layer", "internet"))
        ping_sent.labels(**labels)
        ping_recv.labels(**labels)
    for name, b in sc.get("backends", {}).items() if sc.get("enabled") else []:
        if b.get("enabled"):
            st_fail.labels(name)
            for direction in ("download", "upload"):
                st_bytes.labels(name, direction)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if sys.argv[1:2] == ["fetch-ookla"]:
        fetch_ookla(sys.argv[2] if len(sys.argv) > 2 else ".", *sys.argv[3:4])
        return
    with open(os.environ.get("NETPROBE_CONFIG", "/etc/netprobe/config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    rawlog = RawLog(cfg.get("rawLogDir"))
    start_http_server(int(cfg.get("port", 9101)))
    log.info("listening on :%s, config=%s", cfg.get("port", 9101), json.dumps(cfg, ensure_ascii=False))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    pc, sc = cfg["ping"], cfg["speedtest"]
    init_counters(pc, sc)
    jobs = []
    if pc.get("enabled") and pc.get("targets"):
        jobs.append((pc["intervalSeconds"], 0, lambda: ping_round(pc, rawlog)))
        if pc.get("runOnStart", True):
            threading.Thread(target=ping_round, args=(pc, rawlog), daemon=True).start()
    if sc.get("enabled"):
        # ping と重ならないよう、既定では毎時 offsetSeconds 秒 (例: :02:30) に開始
        jobs.append((sc["intervalSeconds"], sc.get("offsetSeconds", 150), lambda: speedtest_round(sc, rawlog)))
        if sc.get("runOnStart", False):
            threading.Thread(target=speedtest_round, args=(sc, rawlog), daemon=True).start()
    threads = [threading.Thread(target=every, args=(*j, stop), daemon=True) for j in jobs]
    for t in threads:
        t.start()
    stop.wait()
    log.info("shutting down")


if __name__ == "__main__":
    main()
