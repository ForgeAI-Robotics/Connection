#!/usr/bin/env python3
"""Offline cross-repository HTTP contract check; no robot, USB or cloud calls.

Usage: venv/core/bin/python scripts/check_vla_camera_contract.py --vla-root ../sonic_wbc
Only a temporary loopback server is started. The production runtime is not started.
"""
import argparse
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vla-root', required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.vla_root.resolve()))
    from g1_brain_vla_bridge import bridge_service as bridge
    from brain.adapters import camera, vla_camera
    from brain.adapters.execution import parse_capture_scene
    from brain.adapters.http_client import HttpContractError
    from PIL import Image
    import cv2
    import numpy as np

    rgb = np.zeros((48, 64, 3), dtype=np.uint8)
    rgb[:, :, 0] = 255
    wire = cv2.imencode('.jpg', rgb)[1].tobytes()
    passed = []
    with tempfile.TemporaryDirectory(prefix='vla-camera-contract-') as scratch, \
            patch.dict('os.environ', {'BRAIN_CONTROL_TOKEN':'offline-fixture-'+'x'*32,
                                     'BRAIN_CONTROL_TOKEN_FILE':''}):
        config = dict(runtime_dir=scratch, dream={'base_url':'http://127.0.0.1:1','timeout_sec':.01},
            camera={'host':'127.0.0.1','port':1,'keys':{'pick':'left_wrist','place':'ego_view'},
                    'max_age_sec':.5,'snapshot_timeout_sec':.01},
            verification={'grasp_photo_verification_enabled':False,'place_photo_verification_enabled':False},
            backend={'timeout_sec':.01,'commands':{'pick':[],'place':[]}})
        runtime = bridge.BridgeRuntime(config, Path(bridge.__file__).parent)
        server = bridge.BridgeHTTPServer(('127.0.0.1', 0), runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_address[1]}'

        def feed():
            runtime.camera_stream.ingest(dict(image_codec='jpeg-bgr-v2',
                images={key:wire for key in ('ego_view','left_wrist','right_wrist')},
                timestamps={key:time.time()-.01 for key in ('ego_view','left_wrist','right_wrist')}))

        try:
            client = vla_camera.VlaCameraClient(url)
            feed()
            with patch.object(runtime, 'control_status', side_effect=AssertionError('motion dependency queried')):
                metadata, raw = client.latest()
            with Image.open(io.BytesIO(raw)) as image:
                r, g, b = image.getpixel((0,0))
                assert r > 245 and b < 8 and g < 8
            assert metadata['camera_key'] == 'ego_view' and 'image_base64' not in metadata
            passed.append('actual VLA HTTP -> authenticated Brain client, image integrity/color/provenance')
            with patch.dict('os.environ', {'BRAIN_CONTROL_TOKEN':''}):
                try:
                    client.latest()
                    raise AssertionError('missing authentication accepted')
                except HttpContractError:
                    passed.append('missing authentication fails closed')
            feed()
            route = dict(mode='real', backend='vla_camera', available=True, url=url)
            with patch.object(vla_camera, 'real_camera_route', return_value=route), \
                    patch.object(camera, '_endpoint_cfg', return_value=dict(api_base='https://example.invalid/v1',
                        model='offline-model',key_names=('FIXTURE_KEY',))), \
                    patch.object(camera,'_vlm_cfg',return_value={}), \
                    patch.object(camera,'_api_key',return_value='offline'), patch('openai.OpenAI') as model:
                model.return_value.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='红色的离线测试图片'))])
                result = camera.capture_scene('看看前面')
            evidence = parse_capture_scene(result)
            assert evidence and evidence['camera_frame']['camera_key']=='ego_view'
            assert datetime.fromisoformat(evidence['observation']['observed_at']) <= datetime.fromisoformat(evidence['completed_at'])
            assert runtime._tasks == {} and runtime._active_command_id is None
            passed.append('capture_scene -> existing Runtime evidence, cloud mocked, no motion task')
            runtime.camera_stream.ingest(dict(images={'ego_view':wire},timestamps={}))
            try:
                client.latest()
                raise AssertionError('missing source time accepted')
            except HttpContractError:
                passed.append('invalid source time returns unavailable through actual HTTP')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            runtime.close()
    print(json.dumps({'ok':True,'scope':'offline loopback; synthetic images; no robot/cloud/service mutation',
                      'checks':passed},ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
