"""
serve_3dgs — MotrixSim + 3DGS simulation backend for FQPlanner.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_CUDA_HOME = os.path.join(_PROJECT_ROOT, ".cuda", "v12.4")
CUDA_HOME = os.environ.get(
    "CUDA_HOME",
    _LOCAL_CUDA_HOME if os.path.isdir(_LOCAL_CUDA_HOME) else "/usr/local/cuda-12.4",
)
if os.path.isdir(CUDA_HOME):
    os.environ.setdefault("CUDA_HOME", CUDA_HOME)
    _cuda_bin = os.path.join(CUDA_HOME, "bin")
    if _cuda_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _cuda_bin + os.pathsep + os.environ.get("PATH", "")
    if os.name == "nt":
        os.environ["PATH"] = os.path.join(CUDA_HOME, "lib", "x64") + os.pathsep + os.environ["PATH"]
    os.environ.setdefault("LD_LIBRARY_PATH", os.path.join(CUDA_HOME, "lib64") + os.pathsep + os.environ.get("LD_LIBRARY_PATH", ""))


def _prepend_env_paths(name, paths):
    valid = [str(path) for path in paths if Path(path).exists()]
    if valid:
        os.environ[name] = os.pathsep.join(valid + ([os.environ[name]] if os.environ.get(name) else []))


def _configure_windows_build_environment():
    """Expose MSVC and Windows SDK tools for gsplat's JIT CUDA extension."""
    if os.name != "nt":
        return
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    msvc_root = program_files_x86 / "Microsoft Visual Studio" / "2019" / "Community" / "VC" / "Tools" / "MSVC"
    msvc_versions = sorted(
        (path for path in msvc_root.glob("*") if (path / "bin" / "HostX64" / "x64" / "cl.exe").is_file()),
        reverse=True,
    )
    sdk_include_root = program_files_x86 / "Windows Kits" / "10" / "Include"
    sdk_versions = sorted((path for path in sdk_include_root.glob("*") if path.is_dir()), reverse=True)
    if not msvc_versions or not sdk_versions:
        return
    msvc = msvc_versions[0]
    sdk_version = sdk_versions[0].name
    sdk_root = program_files_x86 / "Windows Kits" / "10"
    _prepend_env_paths("PATH", [
        msvc / "bin" / "HostX64" / "x64",
        sdk_root / "bin" / sdk_version / "x64",
    ])
    _prepend_env_paths("INCLUDE", [
        msvc / "include",
        sdk_root / "Include" / sdk_version / "ucrt",
        sdk_root / "Include" / sdk_version / "shared",
        sdk_root / "Include" / sdk_version / "um",
        sdk_root / "Include" / sdk_version / "winrt",
    ])
    _prepend_env_paths("LIB", [
        msvc / "lib" / "x64",
        sdk_root / "Lib" / sdk_version / "ucrt" / "x64",
        sdk_root / "Lib" / sdk_version / "um" / "x64",
    ])


_configure_windows_build_environment()

LOOP_SLEEP_SEC = 0.01


def main():
    parser = argparse.ArgumentParser(description="serve_3dgs - MotrixSim + 3DGS backend")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("SERVE_3DGS_PORT", 5002)))
    parser.add_argument("--gs_assets", type=str, default="")
    parser.add_argument("--robot_gs_dir", type=str, default="")
    parser.add_argument("--scene_config", type=str,
                        default=os.environ.get("SERVE_3DGS_SCENE_CONFIG", ""))
    parser.add_argument("--no-viewer", action="store_true")
    parser.add_argument("--gs_w", type=int, default=320)
    parser.add_argument("--gs_h", type=int, default=240)
    parser.add_argument("--viewer_gs_fps", type=float, default=5.0)
    parser.add_argument("--viewer_cameras", type=str,
                        default=os.environ.get("SERVE_3DGS_VIEWER_CAMERAS", ""))
    parser.add_argument("--no-gs-screens", action="store_true")
    parser.add_argument("--no-composite", action="store_true",
                        help="Disable Hunyuan3D composite mesh rendering (faster; avoids per-frame mesh reload/thrash)")
    parser.add_argument("--physics_steps_per_loop", type=int, default=10)
    parser.add_argument("--scene", type=str, default="",
                        help="Scene override: robot_nav (default), robot_only, path to MJCF/XML, or path to navigation JSON")
    parser.add_argument("--robot", type=str, default="",
                        help="Robot name override (default: read from assets/config.yaml)")
    parser.add_argument("--act-url", type=str, default=None,
                        help="ACT service URL override (default: read from robot_api/config.yaml)")
    args = parser.parse_args()

    from backend.gs_config import GSConfig
    from backend.sim_env import SimEnv
    from backend.viewer_screens import (
        camera_screen_bindings,
        create_viewer_screen_images,
        update_viewer_screen_images,
    )
    from service.server import (
        start_server, process_commands, apply_base_velocity, get_lock,
        set_act_config, has_active_act_command,
    )

    gs_cfg = GSConfig(
        assets_dir=args.gs_assets,
        scene=args.scene,
        robot_gs_dir=args.robot_gs_dir or None,
        scene_config=args.scene_config or None,
        robot_name=args.robot,
    )
    if args.no_composite and gs_cfg.composite_mesh_objects:
        print(f"Composite mesh rendering disabled (--no-composite): "
              f"skipping {len(gs_cfg.composite_mesh_objects)} object(s)")
        gs_cfg.composite_mesh_objects = []
    viewer_camera_names = (
        tuple(name.strip() for name in args.viewer_cameras.split(",") if name.strip())
        if args.viewer_cameras
        else gs_cfg.default_viewer_cameras
    )
    print(f"Loading scene: {gs_cfg.scene_xml}")
    env = SimEnv(
        gs_cfg.scene_xml,
        gs_cfg,
        enable_renderers=not args.no_gs_screens,
    )
    print(f"Model loaded: {env.model.num_links} links, {env.model.num_dof_pos} DOFs")

    start_server(env, port=args.port)
    set_act_config(args.act_url)
    print(f"API: http://localhost:{args.port}")

    try:
        from motrixsim.render import RenderApp

        if not args.no_viewer:
            print("Starting viewer (close window to exit)...")
            gs_screen_bindings = ()
            if not args.no_gs_screens and viewer_camera_names:
                try:
                    gs_screen_bindings = camera_screen_bindings(env.model, viewer_camera_names)
                    print(
                        "3DGS viewer screens: "
                        + ", ".join(f"{b.camera_name}->cam{b.camera_id}" for b in gs_screen_bindings),
                        flush=True,
                    )
                except ValueError as exc:
                    print(f"3DGS viewer screens disabled: {exc}", flush=True)

            gs_update_interval = 1.0 / max(float(args.viewer_gs_fps), 0.1)
            next_gs_update_at = 0.0
            gs_screen_warning_reported = False
            with RenderApp() as render:
                render.launch(env.model)
                render.sync(env.data)
                gs_screen_images = {}
                if gs_screen_bindings:
                    gs_screen_images = create_viewer_screen_images(
                        render,
                        gs_screen_bindings,
                        width=args.gs_w,
                        height=args.gs_h,
                    )
                if gs_screen_images:
                    try:
                        update_viewer_screen_images(
                            env,
                            gs_screen_bindings,
                            gs_screen_images,
                            width=args.gs_w,
                            height=args.gs_h,
                        )
                        render.sync(env.data)
                        next_gs_update_at = time.perf_counter() + gs_update_interval
                    except Exception as exc:
                        gs_screen_warning_reported = True
                        print(f"3DGS viewer screen update failed: {exc}", flush=True)
                while not render.is_closed:
                    with get_lock():
                        process_commands(env)
                        apply_base_velocity(env)
                    for _ in range(args.physics_steps_per_loop):
                        env.step()
                    env.forward_kinematic()
                    now = time.perf_counter()
                    act_gs_interval = gs_update_interval * (10 if has_active_act_command() else 1)
                    if gs_screen_images and now >= next_gs_update_at:
                        try:
                            update_viewer_screen_images(
                                env,
                                gs_screen_bindings,
                                gs_screen_images,
                                width=args.gs_w,
                                height=args.gs_h,
                            )
                        except Exception as exc:
                            if not gs_screen_warning_reported:
                                print(f"3DGS viewer screen update failed: {exc}", flush=True)
                                gs_screen_warning_reported = True
                        next_gs_update_at = now + act_gs_interval
                    render.sync(env.data)
                    time.sleep(LOOP_SLEEP_SEC)
        else:
            print("Running headless (Ctrl+C to stop)...")
            while True:
                process_commands(env)
                apply_base_velocity(env)
                for _ in range(args.physics_steps_per_loop):
                    env.step()
                time.sleep(LOOP_SLEEP_SEC)
    except ImportError:
        print("RenderApp not available, running headless...")
        while True:
            process_commands(env)
            apply_base_velocity(env)
            for _ in range(args.physics_steps_per_loop):
                env.step()
            time.sleep(LOOP_SLEEP_SEC)
    except KeyboardInterrupt:
        pass

    print("Shutdown.")


if __name__ == "__main__":
    main()
