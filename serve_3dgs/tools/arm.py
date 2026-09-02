"""arm.py - XLeRobot arm control tools (MotrixSim backend)."""

import numpy as np

RIGHT_EE_LINK = "Fixed_Jaw_2"
LEFT_EE_LINK = "Fixed_Jaw"
RIGHT_ARM_ACTUATORS = ["Rotation_R", "Pitch_R", "Elbow_R", "Wrist_Pitch_R", "Wrist_Roll_R"]
LEFT_ARM_ACTUATORS = ["Rotation_L", "Pitch_L", "Elbow_L", "Wrist_Pitch_L", "Wrist_Roll_L"]
RIGHT_ARM_START_LINK = "Base_2"
LEFT_ARM_START_LINK = "Base"
GRIPPER_ACTUATOR_R = "Jaw_R"
GRIPPER_ACTUATOR_L = "Jaw_L"

_graspable_links = set()

_ik_chain_r = None
_ik_solver = None


def _ensure_ik(env, side="right"):
    global _ik_chain_r, _ik_solver
    if _ik_chain_r is not None:
        return
    from motrixsim.ik import IkChain, DlsSolver
    start = RIGHT_ARM_START_LINK if side == "right" else LEFT_ARM_START_LINK
    ee = RIGHT_EE_LINK if side == "right" else LEFT_EE_LINK
    _ik_chain_r = IkChain(env.model, end_link=ee, start_link=start)
    _ik_solver = DlsSolver(max_iter=200, step_size=0.2, tolerance=0.005, damping=1e-4)
    print(f"[arm] IK chain: start={start}, ee={ee}, dof={_ik_chain_r.num_dof_pos}, "
          f"links={_ik_chain_r.num_links}", flush=True)
    for i in range(_ik_chain_r.num_links):
        link = _ik_chain_r.get_link(i)
        print(f"  link[{i}] {link.name}", flush=True)


def _find_actuator(env, name: str):
    for i in range(env.model.num_actuators):
        act = env.model.get_actuator(i)
        if act.name == name:
            return act
    return None


def _set_arm_ctrl(env, qpos, side="right"):
    actuators = RIGHT_ARM_ACTUATORS if side == "right" else LEFT_ARM_ACTUATORS
    for i, name in enumerate(actuators):
        act = _find_actuator(env, name)
        if act is not None:
            act.set_ctrl(env.data, float(qpos[i]))


def get_arm_info(env, side: str = "right"):
    env.forward_kinematic()
    ee_link = RIGHT_EE_LINK if side == "right" else LEFT_EE_LINK
    try:
        ee_pos = np.asarray(env.get_body_xpos(ee_link), dtype=float).reshape(-1)
        ee_quat = np.asarray(env.get_body_xquat(ee_link), dtype=float).reshape(-1)
    except KeyError:
        ee_pos = np.zeros(3)
        ee_quat = np.array([0, 0, 0, 1.0])

    gripper_act = GRIPPER_ACTUATOR_R if side == "right" else GRIPPER_ACTUATOR_L
    gripper = _find_actuator(env, gripper_act)
    gripper_pos = [0.0]
    if gripper is not None:
        gripper_pos = [float(gripper.get_ctrl(env.data))]

    arm_acts = RIGHT_ARM_ACTUATORS if side == "right" else LEFT_ARM_ACTUATORS
    arm_qpos = []
    for act_name in arm_acts:
        act = _find_actuator(env, act_name)
        if act is not None:
            arm_qpos.append(float(act.get_ctrl(env.data)))

    base_pos = np.zeros(3)
    try:
        base_pos = np.asarray(env.get_body_xpos("YD_link"), dtype=float).reshape(-1)
    except KeyError:
        pass

    return {
        "ee_pos": ee_pos.tolist(),
        "ee_quat": ee_quat.tolist(),
        "gripper_pos": [round(float(p), 4) for p in gripper_pos],
        "gripper_closed": bool(env.grasped_object),
        "arm_qpos": arm_qpos,
        "arm_qvel": [],
        "base_qpos": base_pos.tolist(),
        "torso_qpos": [],
    }


def move_arm(env, target_pos, max_steps=1000, pos_threshold=0.03, gain=1.5):
    _ensure_ik(env)

    target_pos = np.asarray(target_pos, dtype=np.float32).reshape(3)
    ee_quat = np.asarray(env.get_body_xquat(RIGHT_EE_LINK), dtype=np.float32).reshape(4)

    for step in range(int(max_steps)):
        env.forward_kinematic()
        ee_pos = np.asarray(env.get_body_xpos(RIGHT_EE_LINK), dtype=np.float32).reshape(3)

        if np.linalg.norm(ee_pos - target_pos) < pos_threshold:
            return True

        target_pose = np.concatenate([target_pos, ee_quat]).astype(np.float32)
        result = _ik_solver.solve(_ik_chain_r, env.data, target_pose)
        solved = result[0]
        iters = int(solved[0])
        residual = float(solved[1])
        qpos = solved[2:]

        if step == 0 or step % 20 == 0:
            print(f"[move_arm] step={step} iters={iters} residual={residual:.4f} "
                  f"qpos={qpos[:5].round(4)} ee={ee_pos.round(3)} target={target_pos.round(3)}",
                  flush=True)

        _set_arm_ctrl(env, qpos)
        env.step()

    env.forward_kinematic()
    ee_pos = np.asarray(env.get_body_xpos(RIGHT_EE_LINK), dtype=np.float32).reshape(3)
    print(f"[move_arm] done ee={ee_pos.round(3)} target={target_pos.round(3)}", flush=True)
    return bool(np.linalg.norm(ee_pos - target_pos) < pos_threshold)


def open_gripper(env, side: str = "right", steps=10):
    if env.grasped_object:
        env.grasped_object = None
    act_name = GRIPPER_ACTUATOR_R if side == "right" else GRIPPER_ACTUATOR_L
    act = _find_actuator(env, act_name)
    if act is not None:
        act.set_ctrl(env.data, -1.0)
    env.step(steps)


def close_gripper(env, side: str = "right", steps=10):
    act_name = GRIPPER_ACTUATOR_R if side == "right" else GRIPPER_ACTUATOR_L
    act = _find_actuator(env, act_name)
    if act is not None:
        act.set_ctrl(env.data, 1.0)
    env.step(steps)


def get_obj_pos(env, obj_name):
    return np.asarray(env.get_body_xpos(obj_name), dtype=float).reshape(-1)


def is_grasped(env, obj_name, threshold=0.035):
    return env.grasped_object == obj_name


def grasp(env, obj_name, snap_threshold=0.15):
    if obj_name not in env._link_name_to_idx:
        print(f"[grasp] object '{obj_name}' not found in model")
        available = [n for n in env.model.link_names if n and n not in (
            "YD_link", "ZQ_Link", "YQ_Link", "ZH_Link", "YH_Link",
            "ZQL_Link", "YQL_Link", "ZHL_Link", "YHL_Link",
            "Base_Link", "Left_Shoulder_Base_Link", "Right_Shoulder_Base_Link",
        )]
        print(f"[grasp] available: {available}")
        return False

    obj_pos = get_obj_pos(env, obj_name)
    env.forward_kinematic()
    ee_pos = np.asarray(env.get_body_xpos(RIGHT_EE_LINK), dtype=float).reshape(-1)
    dist = float(np.linalg.norm(obj_pos - ee_pos))
    print(f"[grasp] {obj_name} pos={obj_pos.round(3)} ee={ee_pos.round(3)} dist={dist:.3f}")

    if dist > snap_threshold:
        print(f"[grasp] object too far ({dist:.3f} > {snap_threshold}), attempting approach...")
        move_arm(env, obj_pos, max_steps=50, pos_threshold=snap_threshold)

    close_gripper(env, side="right", steps=5)
    env.grasped_object = obj_name
    lift_pos = obj_pos.copy()
    lift_pos[2] += 0.2
    move_arm(env, lift_pos, max_steps=20, pos_threshold=0.05)
    return True


def place(env, obj_name, target_pos, snap_threshold=0.15):
    if obj_name not in env._link_name_to_idx:
        print(f"[place] object '{obj_name}' not found")
        return False

    target_pos = np.asarray(target_pos, dtype=float)
    print(f"[place] {obj_name} target={target_pos.round(3)}")

    move_arm(env, target_pos, max_steps=50, pos_threshold=snap_threshold)
    if env.grasped_object == obj_name:
        env.grasped_object = None
    open_gripper(env, side="right", steps=5)

    lift_pos = target_pos.copy()
    lift_pos[2] += 0.2
    move_arm(env, lift_pos, max_steps=20, pos_threshold=0.05)
    return True
