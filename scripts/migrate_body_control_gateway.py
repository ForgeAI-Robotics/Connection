#!/usr/bin/env python3
"""Explicit relay ledger migration. Preview by default; never touches a robot."""
import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path

from brain.storage.body_control import ControlStore
from contracts.body_control import stamp


def migrate(root, binding, *, apply=False, preserve_unresolved=False):
    root = Path(root)
    store = ControlStore(root)
    if not store.path.is_file():
        raise ValueError('No existing body ledger; new installations need no migration')
    fd = os.open(root / 'body_control.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.writer():
            state = store.read()
            if state['schema'] != 1:
                raise ValueError('Only schema=1 relay ledgers can be migrated')
            unresolved = (state.get('current') is not None or state.get('busy') is not None
                    or any(r.get('state') != 'released' or r.get('evidence', {}).get('exit_confirmed') is not True
                           for r in state['leases'].values())
                    or any(r.get('receipt', {}).get('state') == 'pending' for r in state['requests'].values()))
            if unresolved and not preserve_unresolved:
                raise ValueError('Unresolved ownership/effects: reconcile with the old version first; ledger unchanged')
            if (set(binding) != {'robot_serial','control_resource','action_endpoint','dream','gateway'}
                    or binding['robot_serial'] != state['binding']['robot_serial']
                    or binding['control_resource'] != 'g1:' + binding['robot_serial'] + ':body'
                    or not binding['gateway'].startswith('https://')):
                raise ValueError('Invalid target binding; the robot must remain the same')
            raw = store.path.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            backup = root / ('body_control.relay-' + sha + '.json')
            report = dict(source_schema=1, target_schema=2, source_sha256=sha, backup=str(backup),
                          old_binding=state['binding'], new_binding=binding, applied=False,
                          unresolved_preserved=bool(unresolved))
            if not apply:
                return report
            if backup.exists():
                if backup.read_bytes() != raw:
                    raise ValueError('Backup conflict')
            else:
                backup_fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(backup_fd, 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if hashlib.sha256(backup.read_bytes()).hexdigest() != sha:
                raise ValueError('Backup verification failed')
            updated = deepcopy(state)
            updated.update(schema=2, binding=dict(binding), authority_id=None)
            if unresolved:
                # Install the new code without pretending the old robot occupancy
                # disappeared. Preserve original requests/evidence and block all grants.
                updated['busy'] = None
                for row in updated['leases'].values():
                    if row.get('state') != 'released':
                        row['migration_source_state'] = row['state']
                        row.update(state='recovery_required', revoked=True, legacy_control=True)
            updated['migration'] = dict(report, migrated_at=stamp())
            updated['revision'] += 1
            store.write(updated)
            return dict(report, applied=True)
    finally:
        os.close(fd)


def migrate_session(root, receipt=None, *, apply=False, preserve_unresolved=False):
    """Upgrade a reconciled schema=2 ledger; preserve all old results verbatim."""
    root = Path(root); store = ControlStore(root)
    staging = receipt is None and preserve_unresolved
    receipt = receipt or {}
    minimum = 1 if staging else receipt.get('minimum_next_ownership_epoch')
    if not staging and (type(minimum) is not int or minimum < 1 or not receipt.get('migration_id')
            or receipt.get('permission_restored') is not False
            or receipt.get('original_results_preserved') is not True
            or (receipt.get('exit_evidence') or {}).get('exit_confirmed') is not True):
        raise ValueError('Verified NAV/gateway migration receipt required')
    fd = os.open(root / 'body_control.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.writer():
            state = store.read()
            if state['schema'] != 2 and not (state['schema'] == 3 and not staging
                    and state.get('control_session_migration', {}).get('unresolved_preserved') is True):
                raise ValueError('Expected schema=2 or a staged unresolved migration')
            if state.get('busy'):
                raise ValueError('Old local operation still busy; reconcile before migration')
            unresolved = [r for r in state['leases'].values() if r.get('state') != 'released']
            proof = receipt.get('exit_evidence') or {}
            peer_old = receipt.get('previous_gateway_state') or {}
            old_keys = ('robot_serial','control_resource','task_id','command_id','control_authorization',
                        'source_id','action_endpoint','publisher_instance_id','input_session_id',
                        'gateway_instance_id','controller_instance_id')
            for row in ([] if staging else unresolved):
                binding = row.get('gateway_binding') or {}
                if (not binding or any(k not in binding or peer_old.get(k) != binding[k] for k in old_keys)
                        or not all(proof.get(k) is True for k in ('exit_confirmed','source_output_fenced',
                            'body_forwarding_fenced','receiver_callbacks_sealed','receiver_decoded_caches_cleared',
                            'input_session_retired','body_stopped','body_hold_verified'))
                        or proof.get('confirmation_basis') not in ('original_session_retired_current_hold',
                            'noncurrent_target_fenced_current_hold','old_controller_absent_new_unused_hold')):
                    raise ValueError('Unresolved ownership has no matching peer exit proof; ledger unchanged')
            if state.get('current') and state['current'] not in state['leases']:
                raise ValueError('Unknown current occupancy; ledger unchanged')
            raw=store.path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
            backup=root/('body_control.timed-permit-'+sha+'.json')
            report=dict(source_schema=state['schema'],target_schema=3,unresolved_preserved=staging,source_sha256=sha,backup=str(backup),
                        minimum_next_ownership_epoch=max(state['epoch']+1,minimum),applied=False)
            if not apply:return report
            if backup.exists():
                if backup.read_bytes()!=raw:raise ValueError('Backup conflict')
            else:
                with os.fdopen(os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as f:
                    f.write(raw);f.flush();os.fsync(f.fileno())
            updated=deepcopy(state)
            updated.update(schema=3,epoch=max(state['epoch'],minimum-1),authority_id=None)
            for original in unresolved:
                row=updated['leases'][original['lease_id']]
                if staging:
                    row.update(legacy_control=True, revoked=True, state='recovery_required')
                    continue
                row['migration_original_evidence']=deepcopy(row.get('evidence'))
                row['migration_source_state']=row['state']
                row['migration_exit_evidence']=deepcopy(proof)
                row.update(state='released',evidence=dict(exit_confirmed=True,cleanup=True,
                    scope='verified peer migration exit for exact old binding',migration_id=receipt['migration_id']))
            if not staging:updated['current']=None
            updated['control_session_migration']=dict(report,peer_receipt=deepcopy(receipt),migrated_at=stamp())
            updated['revision']+=1;store.write(updated)
            return dict(report,applied=True)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--stage-control-session', action='store_true', help='Upgrade schema while retaining all old occupancy blocks')
    parser.add_argument('--session-migration-receipt', help='Verified peer migration JSON; schema 2 to 3')
    parser.add_argument('--robot-serial')
    parser.add_argument('--nav-url')
    parser.add_argument('--gateway-url')
    parser.add_argument('--nav-endpoint', default='tcp://127.0.0.1:5556')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--preserve-unresolved', action='store_true',
                        help='Keep unknown old owners blocked; this does not certify physical exit')
    args = parser.parse_args()
    if args.session_migration_receipt or args.stage_control_session:
        if args.stage_control_session and args.session_migration_receipt:
            parser.error('Choose staging or verified receipt, not both')
        receipt=json.loads(Path(args.session_migration_receipt).read_text()) if args.session_migration_receipt else None
        print(json.dumps(migrate_session(args.root,receipt,apply=args.apply,
            preserve_unresolved=args.stage_control_session),ensure_ascii=False,indent=2))
        return
    if not all((args.robot_serial,args.nav_url,args.gateway_url)):
        parser.error('relay migration requires robot-serial, nav-url and gateway-url')
    binding = dict(robot_serial=args.robot_serial, control_resource='g1:' + args.robot_serial + ':body',
                   action_endpoint=args.nav_endpoint, dream=args.nav_url, gateway=args.gateway_url)
    print(json.dumps(migrate(args.root, binding, apply=args.apply,
                             preserve_unresolved=args.preserve_unresolved), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
