# Brain-driven ROS navigation in the office simulation

This adapter connects the existing `reception.single_can.nav_only` Brain SOP to
the colleague's 8001 navigation service, its 9882 control service, ROS g1pilot,
SONIC and MuJoCo. It does not use the O6 reception walk specifications.

The separately prepared fixture copies the verified `http_nav_8001` setup into
a fresh run directory. Both HTTP services and the colleague's navigation
overlay retain their source hashes. That overlay contains a mode-specific
lateral yaw-gate fix; it is not identical to the original unpatched ROS package.
The source project is mounted read-only, and the run directory is mounted at
`/project/http_nav_8001` inside a Docker container with `--network none`.

`bridge.py` exposes HTTP over a UNIX socket. An SSH local forward connects a
local TCP port to that socket, so neither ROS nor the simulation needs external
network access. Its configuration contains:

- `instance_id`: a unique simulation-run identity;
- `brain_contract`: the selected reception Skill's `points` snapshot (the
  original four-leg `contracts.reception_lan.NAVIGATION_LEGS` remains supported);
- `office_goals`: map-bound poses loaded from the fixture's reviewed
  viewpoints and relays, keyed by string leg index;
- `events`: an append-only request/response evidence path.

Only the registered scene goal coordinates are translated. Task, command and leg
identities, motion modes, orientation requirements and upstream results remain
unchanged. The bridge neither schedules the next leg nor owns command state.
The original 8001 service handles command identity, sequence, dispatch, query
and cancellation. Upstream failures and unknown results remain failures and
unknown results.

The existing `reception_simple` physical-simulation routing is reused, with
`scene=office_ros_8001` identifying this executor. `/reception/vla/health` only
identifies the same isolated fixture and declares `manipulation_supported=false`;
all manipulation operations return 501. The explicit `开始接待（仅导航）` SOP
uses navigation alone. For a complete flow with local simulated pick/place,
submit `开始接待（导航仿真＋操控模拟）`: Brain binds
`reception_ros_sim_manipulation`, routes only navigation to this bridge, and
routes manipulation to its local simulated client. The latter does not use the
bridge VLA endpoint or drive the arms. A health response does not establish
physical manipulation capability.
Both bridge health responses continue to declare `navigation_only=true` and the configured
`navigation_legs`, allowing the current Brain to reject a mismatched Skill
before submitting actions. Mixed simulation additionally requires
`scene=office_ros_8001`; the fixed-waypoint SIMPLE executor is not accepted
for this mode. A five-leg declaration requires an upstream 8001
fixture that actually accepts that same route; the bridge does not add or
bypass upstream route admission.

`run_brain.py <fresh-output-dir> --navigation-url http://127.0.0.1:<tunnel-port>`
starts the standard `BrainApplication`, Runtime, clients and Verifier on
loopback port 15000. The explicit simulation profile and task ledger are isolated
from the main running Brain. Real-robot dispatch permission stays disabled.
The registered SOP does not require an LLM; semantic selection, recovery-model
advice and learning are disabled by default for the historical bounded run.
Use `--config config/brain.yaml --skill-file
src/brain/packages/definitions/reception_single_can_v2.yaml` to reuse current
project/model settings and select the five-leg Skill. Task storage and execution
profile remain isolated, real-robot permission remains disabled, and reflection
and timeline capture remain off. Configured LLM recovery advice is retained;
the model still cannot execute suggestions automatically. These adapter options
alone do not establish a successful five-leg ROS simulation run.

Submit through its normal `POST /publish_task`, then query `/api/task_status`.
Use the normal task controls for any explicit retry and preserve the original
command. A failed navigation lock can revoke simulation localization approval;
inspect its current ground-truth pose and readiness before approving a retry.
This is simulation-only approval, not field localization or hardware approval.

`render.py <reports-dir>` replays the recorded poses, with Brain phases and
retry counts from the actual task timeline. It removes only leading idle time
before the first navigation submission, preserves failed attempts, and validates
every encoded frame. The default is 20 fps; use `--fps 50` with a fixture that
records every tenth 500 Hz physical step. The renderer checks that timestamps
match the requested frame rate and does not interpolate poses or accelerate
playback. Keep raw 500 Hz physics, command results, bridge events,
Brain ledger, source hashes and the final video under ignored experiment data.

For a separately prepared GR00T experiment, `render.py --manipulation --model
<fixture-mission.xml>` labels the complete reception attempt and overlays saved
policy RGB inputs when available. The last actual input is held until the next
inference frame; it is not presented as a live camera stream. This rendering
option does not add manipulation support to `bridge.py`. The temporary pick
task facade, its lack of a verified place entry, and actual failures are recorded
in the [dated integration evidence](../../../docs/多端联调_操控与抓取.md#e-n1-s9).

The fixture uses ground-truth localization and synthetic scans. Its motor-health
input represents numerical/controller readiness. Neither a passing task nor a
collision-free rollout establishes real-robot safety or robust success rate.
