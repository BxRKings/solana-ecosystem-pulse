#!/usr/bin/env python3
"""Generate a no-key Solana ecosystem health snapshot in JSON, Markdown, and HTML."""

import argparse
import html
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


USER_AGENT = "solana-ecosystem-pulse/1.0 (+https://github.com/BxRKings/solana-ecosystem-pulse)"
SOURCES = {
    "solana_rpc": "https://api.mainnet-beta.solana.com",
    "defillama_chains": "https://api.llama.fi/v2/chains",
    "defillama_dex": "https://api.llama.fi/overview/dexs/Solana",
    "defillama_stablecoins": "https://stablecoins.llama.fi/stablecoincharts/Solana",
    "coingecko": "https://api.coingecko.com/api/v3/simple/price",
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_number(value, default=None):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return default


def rounded(value, digits=2):
    value = safe_number(value)
    return round(value, digits) if value is not None else None


def get_path(value, *path, default=None):
    current = value
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


class JsonClient:
    def __init__(self, timeout=20):
        self.timeout = timeout

    def fetch(self, url, payload=None):
        data = None
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class Collector:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or JsonClient(config.get("http_timeout_seconds", 20))
        self.errors = []
        self.status = {}

    def _record(self, source, fn):
        try:
            result = fn()
            self.status[source] = "ok"
            return result
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            self.status[source] = "error"
            self.errors.append({"source": source, "error": str(exc)[:240]})
            return None

    def rpc(self, method, params=None):
        payload = {"jsonrpc": "2.0", "id": method, "method": method}
        if params is not None:
            payload["params"] = params
        response = self.client.fetch(self.config["rpc_url"], payload)
        if response.get("error"):
            raise ValueError("RPC %s: %s" % (method, response["error"]))
        return response.get("result")

    def collect_rpc(self):
        limit = int(self.config.get("performance_sample_limit", 60))
        methods = {
            "health": ("getHealth", None),
            "slot": ("getSlot", [{"commitment": "finalized"}]),
            "epoch": ("getEpochInfo", [{"commitment": "finalized"}]),
            "performance": ("getRecentPerformanceSamples", [limit]),
            "validators": ("getVoteAccounts", [{"commitment": "finalized"}]),
            "supply": ("getSupply", [{"commitment": "finalized"}]),
        }
        result = {}
        for key, (method, params) in methods.items():
            value = self._record("solana_rpc:%s" % method, lambda m=method, p=params: self.rpc(m, p))
            result[key] = value
        slot = result.get("slot")
        if isinstance(slot, int):
            result["block_time"] = self._record(
                "solana_rpc:getBlockTime", lambda: self.rpc("getBlockTime", [slot])
            )
        return result

    def collect_external(self):
        coin_query = urllib.parse.urlencode({
            "ids": "solana",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        })
        dex_query = urllib.parse.urlencode({
            "excludeTotalDataChart": "true",
            "excludeTotalDataChartBreakdown": "true",
            "dataType": "dailyVolume",
        })
        return {
            "chains": self._record("defillama_chains", lambda: self.client.fetch(SOURCES["defillama_chains"])),
            "dex": self._record("defillama_dex", lambda: self.client.fetch(SOURCES["defillama_dex"] + "?" + dex_query)),
            "stablecoins": self._record("defillama_stablecoins", lambda: self.client.fetch(SOURCES["defillama_stablecoins"])),
            "price": self._record("coingecko", lambda: self.client.fetch(SOURCES["coingecko"] + "?" + coin_query)),
        }

    def collect(self):
        generated_at = utc_now()
        rpc_data = self.collect_rpc()
        external = self.collect_external()
        report = build_report(generated_at, rpc_data, external, self.config)
        report["source_status"] = self.status
        report["errors"] = self.errors
        report["sources"] = SOURCES
        return report


def performance_metrics(samples):
    def period(sample):
        return safe_number(sample.get("samplePeriodSecs"), safe_number(sample.get("samplePeriod"), 0))

    valid = [s for s in (samples or []) if period(s) > 0]
    seconds = sum(period(s) for s in valid)
    transactions = sum(safe_number(s.get("numTransactions"), 0) for s in valid)
    non_vote = sum(safe_number(s.get("numNonVoteTransactions"), 0) for s in valid)
    slots = sum(safe_number(s.get("numSlots"), 0) for s in valid)
    per_sample = [s.get("numTransactions", 0) / period(s) for s in valid]
    current = per_sample[0] if per_sample else None
    median = statistics.median(per_sample) if per_sample else None
    return {
        "tps": rounded(transactions / seconds if seconds else None),
        "non_vote_tps": rounded(non_vote / seconds if seconds else None),
        "latest_sample_tps": rounded(current),
        "sample_median_tps": rounded(median),
        "average_slot_ms": rounded((seconds * 1000 / slots) if slots else None),
        "sample_count": len(valid),
        "sample_window_seconds": seconds,
    }


def validator_metrics(accounts, top_limit):
    current = list((accounts or {}).get("current") or [])
    delinquent = list((accounts or {}).get("delinquent") or [])
    active_stake = sum(safe_number(v.get("activatedStake"), 0) for v in current)
    delinquent_stake = sum(safe_number(v.get("activatedStake"), 0) for v in delinquent)
    sorted_current = sorted(current, key=lambda v: safe_number(v.get("activatedStake"), 0), reverse=True)
    cumulative = 0
    nakamoto = 0
    for validator in sorted_current:
        cumulative += safe_number(validator.get("activatedStake"), 0)
        nakamoto += 1
        if active_stake and cumulative / active_stake >= 1 / 3:
            break
    top = []
    for validator in sorted_current[:top_limit]:
        stake = safe_number(validator.get("activatedStake"), 0)
        top.append({
            "vote_pubkey": validator.get("votePubkey"),
            "activated_stake_sol": rounded(stake / 1_000_000_000),
            "stake_percent": rounded(stake * 100 / active_stake if active_stake else None, 3),
            "commission_percent": safe_number(validator.get("commission")),
            "last_vote": safe_number(validator.get("lastVote")),
        })
    total_count = len(current) + len(delinquent)
    return {
        "active_count": len(current),
        "delinquent_count": len(delinquent),
        "delinquent_percent": rounded(len(delinquent) * 100 / total_count if total_count else None),
        "active_stake_sol": rounded(active_stake / 1_000_000_000),
        "delinquent_stake_sol": rounded(delinquent_stake / 1_000_000_000),
        "nakamoto_coefficient_33_percent": nakamoto if active_stake else None,
        "top_by_stake": top,
    }


def find_solana_chain(chains):
    for item in chains or []:
        if str(item.get("name", "")).lower() == "solana":
            return item
    return {}


def latest_stablecoin_value(points):
    if not isinstance(points, list) or not points:
        return None
    for point in reversed(points):
        total = get_path(point, "totalCirculatingUSD", "peggedUSD")
        if safe_number(total) is not None:
            return total
    return None


def ecosystem_metrics(external):
    solana = find_solana_chain(external.get("chains"))
    price = get_path(external.get("price") or {}, "solana", default={}) or {}
    dex = external.get("dex") or {}
    return {
        "sol_price_usd": rounded(price.get("usd")),
        "sol_price_change_24h_percent": rounded(price.get("usd_24h_change")),
        "price_last_updated_unix": safe_number(price.get("last_updated_at")),
        "tvl_usd": rounded(solana.get("tvl")),
        "stablecoin_supply_usd": rounded(latest_stablecoin_value(external.get("stablecoins"))),
        "dex_volume_24h_usd": rounded(dex.get("total24h")),
        "dex_volume_change_24h_percent": rounded(dex.get("change_1d")),
    }


def detect_anomalies(network, validators, ecosystem, thresholds):
    anomalies = []
    latest = network.get("latest_sample_tps")
    median = network.get("sample_median_tps")
    threshold = thresholds.get("tps_percent_from_sample_median", 35)
    if latest is not None and median:
        change = (latest - median) * 100 / median
        if abs(change) >= threshold:
            anomalies.append({
                "metric": "latest_sample_tps",
                "severity": "warning",
                "change_percent": rounded(change),
                "message": "Latest performance sample differs from the sample median by %.1f%%." % change,
            })
    delinquent = validators.get("delinquent_percent")
    if delinquent is not None and delinquent >= thresholds.get("delinquent_validator_percent", 5):
        anomalies.append({
            "metric": "delinquent_validator_percent",
            "severity": "warning",
            "value": delinquent,
            "message": "Delinquent validators are %.2f%% of observed vote accounts." % delinquent,
        })
    price_move = ecosystem.get("sol_price_change_24h_percent")
    if price_move is not None and abs(price_move) >= thresholds.get("sol_price_24h_percent", 10):
        anomalies.append({
            "metric": "sol_price_change_24h_percent",
            "severity": "info",
            "value": price_move,
            "message": "SOL price moved %.2f%% over 24 hours." % price_move,
        })
    dex_move = ecosystem.get("dex_volume_change_24h_percent")
    if dex_move is not None and abs(dex_move) >= thresholds.get("dex_volume_24h_percent", 20):
        anomalies.append({
            "metric": "dex_volume_change_24h_percent",
            "severity": "info",
            "value": dex_move,
            "message": "Solana DEX volume changed %.2f%% versus the preceding 24-hour period." % dex_move,
        })
    return anomalies


def build_report(generated_at, rpc_data, external, config):
    network = performance_metrics(rpc_data.get("performance"))
    network.update({
        "health": rpc_data.get("health"),
        "finalized_slot": safe_number(rpc_data.get("slot")),
        "finalized_block_time_unix": safe_number(rpc_data.get("block_time")),
        "epoch": get_path(rpc_data.get("epoch") or {}, "epoch"),
        "epoch_slot_index": get_path(rpc_data.get("epoch") or {}, "slotIndex"),
        "epoch_slots": get_path(rpc_data.get("epoch") or {}, "slotsInEpoch"),
        "epoch_progress_percent": rounded(
            get_path(rpc_data.get("epoch") or {}, "slotIndex", default=0) * 100 /
            get_path(rpc_data.get("epoch") or {}, "slotsInEpoch", default=1)
            if get_path(rpc_data.get("epoch") or {}, "slotsInEpoch") else None
        ),
        "circulating_supply_sol": rounded(
            get_path(rpc_data.get("supply") or {}, "value", "circulating", default=0) / 1_000_000_000
            if get_path(rpc_data.get("supply") or {}, "value", "circulating") is not None else None
        ),
    })
    validators = validator_metrics(rpc_data.get("validators"), int(config.get("top_validator_limit", 10)))
    ecosystem = ecosystem_metrics(external)
    anomalies = detect_anomalies(network, validators, ecosystem, config.get("anomaly_thresholds", {}))
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "network": network,
        "validators": validators,
        "ecosystem": ecosystem,
        "anomalies": anomalies,
    }


def format_value(value, suffix=""):
    if value is None:
        return "Unavailable"
    if isinstance(value, float):
        return ("{:,.2f}".format(value)).rstrip("0").rstrip(".") + suffix
    return "{:,}{}".format(value, suffix) if isinstance(value, int) else str(value) + suffix


def compact_usd(value):
    value = safe_number(value)
    if value is None:
        return "Unavailable"
    for scale, suffix in ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= scale:
            return "$%s%s" % (("%.2f" % (value / scale)).rstrip("0").rstrip("."), suffix)
    return "$" + format_value(value)


def render_markdown(report):
    n, v, e = report["network"], report["validators"], report["ecosystem"]
    rows = [
        ("Network health", n.get("health")),
        ("Finalized slot", n.get("finalized_slot")),
        ("Average TPS", n.get("tps")),
        ("Non-vote TPS", n.get("non_vote_tps")),
        ("Average slot time", format_value(n.get("average_slot_ms"), " ms")),
        ("Epoch progress", format_value(n.get("epoch_progress_percent"), "%")),
        ("Active validators", v.get("active_count")),
        ("Delinquent validators", format_value(v.get("delinquent_percent"), "%")),
        ("33% Nakamoto coefficient", v.get("nakamoto_coefficient_33_percent")),
        ("SOL price", "$" + format_value(e.get("sol_price_usd"))),
        ("SOL 24h change", format_value(e.get("sol_price_change_24h_percent"), "%")),
        ("Solana TVL", "$" + format_value(e.get("tvl_usd"))),
        ("Stablecoin supply", "$" + format_value(e.get("stablecoin_supply_usd"))),
        ("DEX volume 24h", "$" + format_value(e.get("dex_volume_24h_usd"))),
    ]
    lines = [
        "# Solana Ecosystem Pulse",
        "",
        "Generated %s. Values marked unavailable were not invented or carried forward." % report["generated_at"],
        "",
        "## Current snapshot",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    lines.extend("| %s | %s |" % (label, format_value(value) if not isinstance(value, str) else value) for label, value in rows)
    lines.extend(["", "## Anomalies", ""])
    if report["anomalies"]:
        lines.extend("- **%s:** %s" % (item["severity"], item["message"]) for item in report["anomalies"])
    else:
        lines.append("- No configured threshold was crossed in this snapshot.")
    lines.extend(["", "## Top validators by activated stake", "", "| Vote account | Stake (SOL) | Share | Commission |", "| --- | ---: | ---: | ---: |"]) 
    for item in v.get("top_by_stake", []):
        lines.append("| `%s` | %s | %s%% | %s%% |" % (
            item.get("vote_pubkey"), format_value(item.get("activated_stake_sol")),
            format_value(item.get("stake_percent")), format_value(item.get("commission_percent"))))
    lines.extend(["", "## Source health", "", "| Source | Status |", "| --- | --- |"]) 
    for source, status in sorted(report.get("source_status", {}).items()):
        lines.append("| `%s` | %s |" % (source, status))
    if report.get("errors"):
        lines.extend(["", "## Partial-data warnings", ""])
        lines.extend("- `%s`: %s" % (item["source"], item["error"]) for item in report["errors"])
    return "\n".join(lines) + "\n"


def render_html(report):
    n, v, e = report["network"], report["validators"], report["ecosystem"]
    cards = [
        ("Network", n.get("health")), ("TPS", n.get("tps")),
        ("Slot time", format_value(n.get("average_slot_ms"), " ms")),
        ("Epoch", format_value(n.get("epoch_progress_percent"), "%")),
        ("Validators", v.get("active_count")),
        ("Delinquent", format_value(v.get("delinquent_percent"), "%")),
        ("SOL", compact_usd(e.get("sol_price_usd"))),
        ("TVL", compact_usd(e.get("tvl_usd"))),
        ("Stablecoins", compact_usd(e.get("stablecoin_supply_usd"))),
        ("DEX volume", compact_usd(e.get("dex_volume_24h_usd"))),
    ]
    card_html = "".join('<article><span>%s</span><strong>%s</strong></article>' % (html.escape(str(k)), html.escape(format_value(val) if not isinstance(val, str) else val)) for k, val in cards)
    anomalies = "".join("<li><b>%s</b> %s</li>" % (html.escape(x["severity"]), html.escape(x["message"])) for x in report["anomalies"]) or "<li>No configured threshold was crossed.</li>"
    validator_rows = "".join("<tr><td><code>%s…</code></td><td>%s</td><td>%s%%</td><td>%s%%</td></tr>" % (
        html.escape(str(x.get("vote_pubkey", ""))[:16]), format_value(x.get("activated_stake_sol")),
        format_value(x.get("stake_percent")), format_value(x.get("commission_percent"))) for x in v.get("top_by_stake", []))
    source_rows = "".join("<tr><td><code>%s</code></td><td class='%s'>%s</td></tr>" % (html.escape(k), html.escape(status), html.escape(status)) for k, status in sorted(report.get("source_status", {}).items()))
    css = """
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui;background:#070b18;color:#eef3ff}body{margin:0;background:radial-gradient(circle at 15% 0,#163057 0,transparent 35%),#070b18}.wrap{max-width:1120px;margin:auto;padding:44px 22px 70px}h1{font-size:clamp(2rem,5vw,4rem);margin:.1em 0;background:linear-gradient(90deg,#7cf8c9,#7ab7ff);-webkit-background-clip:text;color:transparent}p{color:#aab8d4}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:30px 0}article,section{background:#0d1428;border:1px solid #233456;border-radius:16px;padding:18px;box-shadow:0 12px 34px #0005}article span{display:block;color:#8ca0c8;font-size:.82rem;text-transform:uppercase;letter-spacing:.08em}article strong{display:block;margin-top:10px;font-size:1.55rem}.panels{display:grid;grid-template-columns:1fr 1fr;gap:16px}table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;padding:10px 7px;border-bottom:1px solid #243453}th{color:#8ca0c8}.ok{color:#7cf8c9}.error{color:#ff9292}li{margin:.7em 0}code{color:#93c5fd}@media(max-width:800px){.panels{grid-template-columns:1fr}}"""
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Solana Ecosystem Pulse</title><style>%s</style></head><body><main class="wrap"><p>NO-KEY, DEPENDENCY-FREE NETWORK REPORT</p><h1>Solana Ecosystem Pulse</h1><p>Generated %s UTC. Failed sources stay unavailable instead of silently reusing stale values.</p><div class="grid">%s</div><div class="panels"><section><h2>Anomaly watch</h2><ul>%s</ul></section><section><h2>Source health</h2><table><tr><th>Source</th><th>Status</th></tr>%s</table></section></div><section style="margin-top:16px"><h2>Top validators by activated stake</h2><table><tr><th>Vote account</th><th>Stake (SOL)</th><th>Share</th><th>Commission</th></tr>%s</table></section><p>Data: Solana RPC, DeFiLlama, and CoinGecko. This report is informational and may be partial.</p></main></body></html>""" % (css, html.escape(report["generated_at"]), card_html, anomalies, source_rows, validator_rows)


def write_outputs(report, root):
    reports = root / "reports"
    docs = root / "docs"
    reports.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
    rendered_json = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (reports / "latest.json").write_text(rendered_json, encoding="utf-8")
    (reports / "latest.md").write_text(render_markdown(report), encoding="utf-8")
    (docs / "latest.json").write_text(rendered_json, encoding="utf-8")
    (docs / "index.html").write_text(render_html(report), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--watch-seconds", type=int, default=0, help="repeat in foreground; zero runs once")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    config = json.loads((root / args.config).read_text(encoding="utf-8"))
    while True:
        report = Collector(config).collect()
        write_outputs(report, root)
        print("Generated %s with %d warning(s)" % (report["generated_at"], len(report["errors"])))
        if args.watch_seconds <= 0:
            break
        time.sleep(max(60, args.watch_seconds))


if __name__ == "__main__":
    main()
