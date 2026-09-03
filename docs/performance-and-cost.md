# Performance and cost engineering

Model gateway performance has at least three independent bottlenecks: gateway work, provider queue
and generation, and client/network delivery. Optimizing one without separating them can move cost or
tail latency elsewhere.

## Latency decomposition

For a successful complete request:

\[
L_{total}=L_{ingress}+L_{prompt}+L_{limit}+L_{cache}+L_{release}+L_{route}
+L_{connect}+L_{provider}+L_{schema}+L_{evidence}+L_{egress}
\]

For a stream:

\[
TTFT=L_{ingress}+L_{prompt}+L_{limit}+L_{cache}+L_{release}+L_{route}
+L_{connect}+L_{provider\_queue}+L_{first\_token}+L_{flush}
\]

and approximate post-first-token generation time is:

\[
L_{generation}\approx\frac{T_{output}-1}{throughput_{tokens/s}}
\]

The current telemetry records end-to-end latency and TTFT but not every component span. The tracing
plan in [Observability](observability.md) is required before attributing a p99 regression precisely.

Do not add component p99 values to estimate total p99. Percentiles do not compose unless the same
requests occupy every tail. Measure end-to-end latency and use traces to explain it.

## Fallback latency

For `k` failed attempts followed by success:

\[
L_{fallback}=\sum_{i=1}^{k}L_{failed,i}+L_{successful}+L_{gateway\ overhead}
\]

Even when final success rate remains high, first-route failure can destroy p99 and cost. Track
first-attempt success and fallback count in addition to terminal status.

The caller budget must be a single deadline across attempts. Giving every route the original timeout
turns a 10-second request into 20 or 30 seconds under failure.

## Queueing and concurrency

Little's Law provides a first capacity estimate:

\[
N_{inflight}=\lambda E[S]
\]

At 40 requests/second and mean provider time 2.5 seconds, mean concurrency is roughly 100 before
shadow traffic and retries. Long streams increase open socket and file-descriptor demand even when
CPU use is low.

Utilization near one causes nonlinear queue growth. For an idealized M/M/1 queue with service rate
`\mu` and arrival rate `\lambda`:

\[
E[W]=\frac{1}{\mu-\lambda}
\]

Real model serving is not M/M/1—batching and token generation are stateful—but the divergence as
`\lambda` approaches capacity is the useful lesson. Bound queue depth and shed work before the caller
deadline is already lost.

## Gateway CPU costs

Gateway work is usually smaller than model inference but can dominate cached or local-fast requests:

- Pydantic request parsing and serialization;
- feature-hash tokenization and scope hashing;
- linear semantic-cache scan within a scope;
- YAML/registry access;
- SSE/NDJSON parsing;
- assembling stream text;
- JSON decoding and schema validation;
- SQLite transaction and fsync behavior;
- Prometheus label lookup and log serialization.

Profile with representative prompt/output sizes. A 10-byte benchmark does not predict a 1 MB
structured response or 10,000-entry cache scope.

## Semantic-cache lookup cost

For `N_s` entries in the exact policy scope and `z` non-zero request features:

\[
T_{lookup}=O(N_s z)
\]

The total entry limit bounds worst-case work, but one hot scope can still contain most entries. Use an
approximate vector index or lexical inverted index when measured scan latency consumes a material
part of the gateway budget. Preserve exact scope partitioning before approximate search.

## Provider cost

Predicted route admission cost:

\[
\hat{C}(r)=\frac{\hat{T}_{in}P_{in}(r)+T_{out,max}P_{out}(r)}{10^6}
\]

Completed-response estimate:

\[
C(r)=\frac{T_{in}P_{in}(r)+T_{out}P_{out}(r)}{10^6}
\]

Cost per successful user request should include all attempts and shadow/judge overhead allocated by a
declared method:

\[
C_{success,true}=\frac{
\sum C_{user\ attempts}+\sum C_{shadow}+\sum C_{evaluation/judge}
}{successful\ user\ requests}
\]

The current final evidence row does not retain every failed attempt's token cost, so it can
underestimate the first term. Add an attempt table/event stream before using the metric for precise
chargeback.

## Cache economics

Let:

- `h` be cache hit rate;
- `C_m` be mean provider cost on a miss;
- `C_l` be lookup infrastructure cost;
- `C_s` be storage/invalidation cost per request;
- `C_f` be expected cost of a false semantic hit.

Expected request cost is approximately:

\[
E[C]=(1-h)C_m+C_l+C_s+P(false\ hit)C_f
\]

The last term can dominate in high-stakes domains. A wrong cached decision may cost far more than the
saved tokens. Optimize threshold against task risk, not only hit rate.

## Shadow and evaluation budget

For user arrival rate `\lambda`, canary fraction `c`, shadow fraction `s` among non-canary requests,
and mean candidate call cost `C_c`:

\[
C_{shadow/hour}=3600\lambda(1-c)sC_c
\]

Add offline evaluation:

\[
C_{eval}=N_{cases}(C_{candidate}+C_{judge})
\]

Provider rate/token quotas must include both. Pause shadow and evaluation first during quota pressure;
they should run in separate priority/concurrency pools.

## Local-model memory

An Ollama route shifts cost from per-token billing to reserved hardware, memory, power, and operator
time. Context length can be limited by KV-cache memory even when model weights fit.

For a decoder-only transformer with:

- `L` layers;
- batch size `B`;
- cached sequence length `S`;
- `H_kv` key/value heads;
- head dimension `D_h`;
- `b` bytes per KV element;

KV cache is approximately:

\[
M_{KV}=L\cdot2\cdot B\cdot S\cdot H_{kv}\cdot D_h\cdot b
\]

The factor two stores keys and values. With 32 layers, 8 KV heads, head dimension 128, 32,768 tokens,
batch one, and 2-byte values:

\[
M_{KV}=32\cdot2\cdot1\cdot32768\cdot8\cdot128\cdot2
=4\ GiB
\]

That excludes model weights, temporary activations, allocator fragmentation, runtime overhead, and
other concurrent sequences. Grouped-query attention lowers `H_kv`; KV quantization lowers `b` with a
quality/performance trade-off.

For concurrent sequences with different lengths, capacity planning should use the serving engine's
paged-KV allocation and fragmentation behavior rather than multiplying one idealized number.

## Local-model compute

A rough dense-transformer decode estimate starts near two floating-point operations per active model
parameter per generated token, plus attention and runtime overhead:

\[
FLOPs/token \gtrsim 2N_{parameters}
\]

At decode time, memory bandwidth often limits small-batch throughput because weights are read for
each token. Quantization reduces bytes moved and can improve throughput, but kernels, context length,
batching, and hardware determine realized speed. Benchmark the exact model file, quantization,
runtime, prompt length, and concurrency.

## Route latency priors

`expected_p95_latency_ms` is both a hard admission check and utility input. A stale value can route
too much traffic to a degraded model or reject a healthy one.

Update priors from a controlled window segmented by:

- complete versus stream;
- input and output token buckets;
- region and network path;
- service tier;
- cache excluded;
- cold versus warm local model;
- success versus fallback.

Avoid feeding instantaneous p95 directly into routing; noisy feedback can cause traffic oscillation.
Use smoothing, minimum samples, bounds, and canary the policy change.

## Benchmark methodology

The included benchmark measures local gateway/evidence overhead with deterministic routes:

```bash
python scripts/run_benchmark.py --requests 1000 --concurrency 50 --assert-slo
```

It creates a temporary database, bypasses cache, issues distinct requests, and reports evidence-based
metrics plus wall-clock throughput. The CI p99 threshold is a regression tripwire, not a universal
performance claim.

For serious load testing, vary:

- message count and bytes;
- schema size and output size;
- cache scope size and hit ratio;
- concurrent complete and long-lived streaming traffic;
- provider latency/error distributions;
- fallback depth;
- shadow percentage;
- SQLite or replacement-store latency;
- slow clients and proxy buffering.

Report hardware, process count, catalogue, source commit, runner, duration, warm-up, and confidence
interval. Throughput without a latency/error constraint is not useful.

## Cost/latency trade-off table

| Lever | Expected benefit | Common cost or risk |
|---|---|---|
| Lower output-token ceiling | Lower generation time and spend | Truncation or reduced task quality |
| Cheaper route | Lower token cost | Quality, schema, or latency regression |
| Semantic cache | Lower cost and latency | False reuse, staleness, retention risk |
| Higher cache threshold | Fewer false hits | Lower hit rate |
| More fallback routes | Higher terminal availability | Higher tail latency and duplicate cost |
| Shorter provider timeout | Faster failure recovery | Aborts slow requests that would succeed |
| More concurrency | Higher throughput until saturation | Queueing, rate limits, memory pressure |
| Shadow traffic | Better pre-release evidence | Extra spend/capacity |
| Larger local batch | Better accelerator utilization | Higher per-request latency and KV memory |
| Quantized local model | Lower memory/bandwidth | Potential quality loss and kernel variance |

## Performance acceptance checklist

- [ ] End-to-end deadlines are propagated across every attempt.
- [ ] Provider connection pools match route concurrency and quotas.
- [ ] Complete and streaming TTFT/latency are measured separately.
- [ ] Token-length distributions accompany latency percentiles.
- [ ] Semantic-cache scan time and memory are bounded under worst scope.
- [ ] Failed-attempt and shadow costs are included in economic metrics.
- [ ] Local model KV memory is budgeted for context and concurrency.
- [ ] Load tests include errors, fallback, slow clients, and evidence-store degradation.
- [ ] Route priors have measurement window, sample count, and owner.
- [ ] Capacity leaves headroom for incident failover without saturating fallback routes.
