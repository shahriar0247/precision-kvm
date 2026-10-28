# Transport measurements

Recorded on 2026-10-28 using the actual Windows host and Mac over their gigabit Ethernet interfaces. These are authenticated TLS echo probes, with no hooks installed and no input events posted.

| Run | Requested Hz | Achieved Hz | Median RTT ms | p99 RTT ms | Maximum RTT ms | Unanswered |
|---|---:|---:|---:|---:|---:|---:|
| Repeated, 3 seconds | 120 | 120.31 | 1.157 | 2.138 | 10.315 | 0 |
| Repeated, 3 seconds | 240 | 240.31 | 1.086 | 6.021 | 10.438 | 0 |
| Repeated, 3 seconds | 500 | 500.25 | 0.975 | 2.564 | 4.555 | 0 |
| Repeated, 3 seconds | 1000 | 998.23 | 0.896 | 2.452 | 4.000 | 0 |
| Sustained, 15 seconds | 120 | 120.06 | 1.061 | 10.044 | 116.662 | 0 |
| Sustained, 15 seconds | 1000 | 992.71 | 0.941 | 2.836 | 15.239 | 0 |
| Instrumented, 15 seconds | 120 | 119.96 | 1.092 | 1.884 | 3.474 | 0 |
| Instrumented, 15 seconds | 1000 | 995.58 | 0.917 | 1.397 | 17.846 | 0 |

The initial run is also preserved, including scheduling stalls at 120 Hz (79.21 Hz achieved). The sustained run exposes an occasional long tail despite low median RTT. No claim of jitter-free input is justified by these results. Queue high water was one in the probes; the 1000 Hz sustained probe had up to 11 echoes in flight. Neither figure is a hardware input queue measurement.

The instrumented repeat did not reproduce the 116.662 ms outlier. Its TLS send calls took at most 0.403 ms, while maximum send intervals reached 15.715 ms at 1000 Hz and receive gaps reached 17.608 ms. Recorded echo processing was under 0.01 ms for the worst RTT samples. This separates sending/echo work from observed scheduling or network delays; it does not establish the cause of the earlier outlier. No speculative network-driver or OS configuration changes were made.

The probe covers TLS framing, pacing, network round trips and echo processing. It does not measure mouse polling, Windows capture latency, Quartz injection latency, scroll behavior, volume behavior, displayed cursor refresh or input-to-photon latency. Those require a live sharing session, which was intentionally left stopped for the pre-launch handoff.

Local verification: Ruff, mypy, wheel packaging and the unit/integration tests. Native macOS framework event construction and Accessibility checks passed without posting input. Primary-display edge behavior, native hook lifecycle and the visible Tk control panel still need the first live smoke test.

## Clipboard/input isolation

The follow-up synthetic test transferred **32 MiB** of generated UTF-8 clipboard data over a second authenticated TLS connection in **1.937 seconds**, with a verified bundle checksum. In parallel, a six-second 1000 Hz input transport probe achieved **998.73 Hz**, median RTT **0.898 ms**, p99 **2.033 ms**, maximum **12.242 ms**, and **5,993/5,993 echoes**. Input queue high water remained one. See `clipboard-input-isolation.json`.

No native clipboard backend was loaded by that test, and no input events were posted. It verifies the network/protocol isolation with synthetic data, not the cost of encoding arbitrary images or the complete hardware input path. Shared CPU and network scheduling can still affect latency despite separate connections.
