# Standalone SIMPLE O7 executor

Deploy this directory to `<REMOTE_WORKSPACE>/connection-simple-o7`, alongside `code/`.
It has no dependency on Connection's brain package. Python service dependencies are standard-library only.
The worker uses the existing `simple:260904` image and mounts `SIMPLE-o7-verify` read-only.

1. Copy `config.example.json` to `config.json`.
2. Create `.env` containing a random `SIMPLE_O7_TOKEN`; use the same token in Connection's `.env`.
3. Run `bash start.sh`. The service listens on port 18770.
4. Check `/health`, then authenticated `/v1/scene` before submitting any command.

The executor exposes one compound `pick_hold_coke` action per task/episode. It does not provide a persistent general-purpose multi-step world, navigation, release/place, live cameras, or GR00T inference.
One command runs existing prepare → IK reachability → CuRobo planning → SONIC/MuJoCo execution scripts, preserving source fingerprints and per-episode artifacts.
The canonical scene's default initial pose currently fails reachability; the bridge reports that failure instead of claiming a successful grasp.

Command records survive service restart. A missing/unfinished record is never replayed automatically.
Cancel the original command and check its stop acknowledgement before stopping the container.
Do not delete the journal or episode files to clear an unresolved command.

Full operator instructions and validation limits: `docs/联调说明_SIMPLE_O7仿真.md` in Connection.
