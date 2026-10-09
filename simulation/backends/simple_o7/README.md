# Standalone SIMPLE executor (O6 / legacy O7)

Deploy this directory to `<REMOTE_WORKSPACE>/connection-simple-o7`, alongside `code/`.
Replace `<REMOTE_WORKSPACE>` with the deployment workspace and set `SIMPLE_SOURCE` to
the actual `code/SIMPLE-o7-verify` path before running `start.sh`.
It has no dependency on Connection's brain package. The HTTP service uses the standard library;
the O6 worker reads physical evidence with NumPy from the existing simulation image.
The worker uses the existing `simple:260904` image and mounts `SIMPLE-o7-verify` read-only.

1. Copy `config.example.json` to `config.json`.
2. Create `.env` containing a random `SIMPLE_O7_TOKEN`; use the same token in Connection's `.env`.
3. Run `bash start.sh`. The service listens on port 18770.
4. Check `/health`, then authenticated `/v1/scene` before submitting any command.

The default config selects O6. It runs upstream `validate_g1_o6_cycle.py --task grasp`
with the complete source overlay at `generated-data/o6-planner-production-20260923/src`.
This is CuRobo planning with AMO body control and arm torque PD in MuJoCo.
The only exposed action is `pick_hold_can`, using the calibrated `graspnet1b:2` soup can.
Native success must also pass independent terminal trajectory checks: unsupported hand contact,
at least 8 cm lift, speed at most 2 cm/s, a continuous 1 s hold, and collision/tilt limits.
Seeds 101, 102 and 103 passed through Connection's actual Planner and Runtime on 2026-09-24.

The backend key and wire version remain `simple_o7` / `connection/simple-o7/v1`.
Legacy O7 config (no `robot_variant`, or `o7`) still uses `pick_hold_coke` and the original
prepare → reachability → CuRobo → SONIC pipeline. O7's default Coke scene failed reachability;
that historical result does not describe the O6 task. The two object identities are distinct.
Neither variant provides a persistent multi-step world, navigation, release/place, live cameras, or GR00T inference.

## Single-can reception episode (`/reception/nav`, `/reception/vla`)

Since 2026-10-08 the same facade also serves Connection's `fq/reception-lan/v1` NAV and VLA
endpoints under `/reception/nav` and `/reception/vla` (no bearer token, as on the robot LAN).
The first `table_2` navigation of a task starts one worker (`service.reception_worker`) that owns
one persistent MuJoCo episode; each brain command advances one segment of it:
`nav_table2 → pick → nav_relay2 → nav_relay3 (lateral) → nav_table1 → place`.
The episode reuses the frozen O6 SONIC release (`sonic_release_v1`, left-hand `cross_table`
configuration) read-only; `service/reception_task.py` only adds the walk to the pick table and
splits the carry into the contract's relay legs. Semantic targets map to formal-room poses
(`service/reception_task_info.py`); real-map coordinates are not used.
Results are measured physics (arrival error, grasp contact and lift, placement gate);
the simulation clock is paused between commands. Cancel ends the episode; a new attempt needs a new task.
`python -m service.reception_episode <dir>` runs all six segments without HTTP;
`python -m service.render_reception <episode_dir> --commands commands.json` renders a replay video.

Command records survive service restart. A missing/unfinished record is never replayed automatically.
Use `python3 manage.py start` / `python3 manage.py stop` for the existing container.
Stop atomically fences new admission and refuses any unresolved command. The panel uses these commands over SSH.
Cancel the original command and check its stop acknowledgement before stopping the container.
Do not delete the journal or episode files to clear an unresolved command.

Full operator instructions and validation limits: `docs/联调说明_SIMPLE_O7仿真.md` in Connection.
