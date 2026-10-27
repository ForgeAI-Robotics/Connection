"""Explicit simulated O6 observations for brain contract tests, no hardware IO."""
from copy import deepcopy
from datetime import datetime, timedelta
from contracts.body_control import digest, stamp
from contracts.vla import HOLDING_SCHEMA


def holding_status(task_id, command_id, authorization, *, robot='test-robot', observed=None):
    observed = deepcopy(observed or dict(source='operator_observation', reference='offline-fixture',
        observed_at=stamp(), object_grasped=True, holding='cola_can_1', clear_of_fridge=True))
    association = dict(task_id=task_id, command_id=command_id, owner='vla', **authorization)
    targets = {side: [100] * 6 for side in ('left', 'right')}
    anchor = dict(receiver_instance_id='o6-test', controller_instance_id='sonic-test',
        output_instance_id='output-test', association=association, target_generation=2,
        feedback_generation=0, targets_sha256=digest(targets))
    expires = observed.get('valid_until') or (datetime.fromisoformat(observed['observed_at']) + timedelta(seconds=60)).isoformat()
    payload = dict(schema=HOLDING_SCHEMA, robot_serial=robot, task_id=task_id, command_id=command_id,
        holding='cola_can_1', anchor=anchor, evidence=observed, observed_at=observed['observed_at'], valid_until=expires)
    proof = dict(payload, proof_id=digest(payload), valid=True, signature_verified=True, invalid_reason=None)
    hands = dict(robot_serial=robot, receiver_instance_id='o6-test', output_instance_id='output-test', association=association,
        command_writes=2, feedback_generation=0, targets=targets, holding='cola_can_1', holding_evidence=proof,
        capabilities=['authenticated-holding-evidence-v1'], hold_verified=True, hands_stopped=True,
        receiver_output_fenced=True)
    return dict(robot_serial=robot, observed_at=stamp(), holding='cola_can_1', current_holding='cola_can_1',
        holding_evidence=proof, execution_facts=dict(controller_instance_id='sonic-test', o6=hands))
