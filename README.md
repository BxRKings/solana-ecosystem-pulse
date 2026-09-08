# Solana Ecosystem Pulse

A no-key, dependency-free Python report that turns public Solana RPC, DeFiLlama, and CoinGecko data into three reproducible outputs:

- an interactive dark HTML dashboard;
- a human-readable Markdown report;
- structured JSON for agents and downstream automation.

The collector continues when one public source fails. Missing metrics remain `null` or `Unavailable`; the report never fills gaps with stale or invented values.

## Run

Requires Python 3.9 or newer. No package install or API key is needed.

```bash
python3 pulse.py
```

Outputs:

- `docs/index.html` — dashboard, suitable for GitHub Pages;
- `docs/latest.json` — machine-readable snapshot beside the dashboard;
- `reports/latest.md` — portable written report;
- `reports/latest.json` — complete snapshot including source health and partial-data warnings.

For a foreground refresh loop, choose a period of at least 60 seconds:

```bash
python3 pulse.py --watch-seconds 900
```

The default is a single run. No background service or schedule is installed.

## Metrics

Solana RPC supplies finalized slot and block time, epoch progress, recent TPS, non-vote TPS, average slot time, circulating supply, validator activity, stake concentration, commission, and delinquency. The 33% Nakamoto coefficient is the number of largest active vote accounts whose combined activated stake reaches one third of observed active stake; it is a snapshot estimate, not a claim about every possible attack model.

The performance parser accepts both the current `samplePeriodSecs` field and the older `samplePeriod` spelling used by some RPC examples.

DeFiLlama supplies Solana TVL, stablecoin supply, and 24-hour DEX volume. CoinGecko supplies SOL/USD price and its 24-hour change. Every remote family gets an explicit source-health result.

## Anomaly detection

Thresholds live in `config.json`:

- latest performance-sample TPS versus the median of recent samples;
- delinquent validator percentage;
- absolute 24-hour SOL price move;
- absolute 24-hour DEX-volume move.

The JSON includes stable metric names, values, severity, and a plain-language message. Thresholds are deliberately simple and explainable. They identify review candidates; they do not predict price or declare network incidents.

## Automation strategy

`pulse.py` is idempotent and writes fixed output paths, so it can run from a developer shell, CI job, container, or process supervisor. `--watch-seconds` supports a configurable foreground loop. This repository does not activate a cron job or require hosted infrastructure.

The dashboard is a generated static artifact with all values embedded at collection time. A new collector run atomically refreshes the JSON, Markdown, and HTML artifacts for the next publish step.

## Data-source notes

- Solana public mainnet RPC is rate-limited and may return partial failures. Configure another standards-compatible public RPC URL in `config.json` if permitted.
- DeFiLlama and CoinGecko are independent off-chain sources with their own methodologies and availability.
- Validator stake is reported by `getVoteAccounts`; counts and concentration can change between calls.
- Public endpoints can change schemas. Source-level errors are recorded and affected values stay unavailable.
- Social sentiment and X scraping are omitted because they would add account/API-key dependence and weak provenance. The dashboard favors reproducible public endpoints.

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile pulse.py
```

Tests use deterministic fake responses; they do not depend on network availability. The project is MIT licensed and contains original implementation work.

Built with Codex for the Superteam Canada “Develop Solana Ecosystem Auto-Updating Report & Interactive Dashboard” bounty.
