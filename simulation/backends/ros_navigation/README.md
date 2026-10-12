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
- `brain_contract`: the existing `contracts.reception_lan.NAVIGATION_LEGS`;
- `office_goals`: four map-bound poses loaded from the fixture's reviewed
  viewpoints and relays, keyed by string leg index;
- `events`: an append-only request/response evidence path.

Only the four scene goal coordinates are translated. Task, command and leg
identities, motion modes, orientation requirements and upstream results remain
unchanged. The bridge neither schedules the next leg nor owns command state.
The original 8001 service handles command identity, sequence, dispatch, query
and cancellation. Upstream failures and unknown results remain failures and
unknown results.

The existing `reception_simple` physical-simulation routing is reused, with
`scene=office_ros_8001` identifying this executor. `/reception/vla/health` only
identifies the same isolated fixture and declares `manipulation_supported=false`;
all manipulation operations return 501. Run only the explicit
`开始接待（仅导航）` SOP. A health response does not establish manipulation capability.

`run_brain.py <fresh-output-dir> --navigation-url http://127.0.0.1:<tunnel-port>`
starts the standard `BrainApplication`, Runtime, clients and Verifier on
loopback port 15000. The explicit simulation profile and task ledger are isolated
from the main running Brain. Real-robot dispatch permission stays disabled.
The registered SOP does not require an LLM; semantic selection, recovery-model
advice and learning are disabled for this bounded integration run.

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
in the [dated integration evidence](../../../docs/多端联调_20261010_新房子ROS仅导航.md#8-大脑真实-ros真实-gr00t两次完整任务尝试).

The fixture uses ground-truth localization and synthetic scans. Its motor-health
input represents numerical/controller readiness. Neither a passing task nor a
collision-free rollout establishes real-robot safety or robust success rate.
