"""Office coordinates and a reproducible copy of its static collision layout.

O6 manipulation uses a table-centred frame: +X is office north, +Y west,
and Z=0 is the original O6 table height (office Z=.75 m).
"""
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

SCENE_ID = 'office_v2'
ORIGIN = (10.8, 11.55, .75)
SPAWN = (-6.05, -.5)
SIM_GOALS = {
    'table_2': (-.62, 0., 0.),
    'relay2': (.27727, 4.55, 0.),
    'relay3': (.27727, 6.15, 0.),
    'table_1': (-.40, 7.20, math.pi),
}
SCENE = {
    'table_2': {'type': 'table', 'sim': 'office source table; calibrated soup can',
                'stance_xyt': list(SIM_GOALS['table_2'])},
    'door_1': {'type': 'door', 'sim': 'office meeting-room doorway; lateral passage',
               'stance_xyt': [list(SIM_GOALS['relay2']), list(SIM_GOALS['relay3'])]},
    'table_1': {'type': 'table', 'sim': 'office meeting table',
                'stance_xyt': list(SIM_GOALS['table_1'])},
    'cola_can_1': {'type': 'can', 'sim': 'dynamic graspnet1b:2 calibrated soup can'},
}


def office_to_local(position):
    x, y, z = position
    return [y - ORIGIN[1], ORIGIN[0] - x, z - ORIGIN[2]]


def prepare_room(assets, output):
    """Preserve upstream assets; write only this episode's derived manifest."""
    source = Path(assets) / 'scene.xml'
    tree = ET.parse(source)
    boxes = []
    for geom in tree.findall('./worldbody/geom'):
        if geom.get('name') in {'floor', 'meeting_table'}:
            continue  # engine ground and the measured destination support body
        if geom.get('type', 'sphere') != 'box':
            continue
        if geom.get('contype', '1') == '0' and geom.get('conaffinity', '1') == '0':
            continue
        pos = [float(v) for v in geom.get('pos', '0 0 0').split()]
        size = [float(v) for v in geom.get('size').split()]
        if geom.get('quat') or geom.get('euler'):
            raise ValueError('Office collision boxes must be axis aligned: ' + geom.get('name', ''))
        boxes.append(dict(name='office_' + geom.get('name'), position=office_to_local(pos),
                          size=[2 * size[1], 2 * size[0], 2 * size[2]], category='office_collision'))
    result = dict(source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  floor_z=-.75, translation=[0., 0., 0.], boxes=boxes, office_assets=str(Path(assets).resolve()),
                  coordinate_transform={'office_origin': ORIGIN, 'local_x': 'office +Y', 'local_y': 'office -X'})
    destination = Path(output) / 'office_room.json'
    destination.write_text(json.dumps(result, indent=2))
    return destination


def install_visuals():
    """Use the authored office surfaces as well as the planner's collision boxes.

    Patch only this worker's scene-builder hook; no upstream files are edited.
    Collision boxes remain identical to the planner manifest.
    """
    import numpy as np
    import mujoco
    import simple.scenes.formal_room as room_module
    original = room_module.add_mujoco_room

    def add_office(spec, room):
        if not room.get('office_assets'):
            return original(spec, room)
        root = ET.parse(Path(room['office_assets']) / 'scene.xml').getroot()
        for node in root.findall('./asset/texture'):
            if not node.get('file'):
                continue
            tex = spec.add_texture(name='office_' + node.get('name'), type=mujoco.mjtTexture.mjTEXTURE_2D)
            tex.file = str(Path(room['office_assets']) / node.get('file'))
        for node in root.findall('./asset/material'):
            mat = spec.add_material(name='office_' + node.get('name'))
            if node.get('rgba'):
                mat.rgba = [float(v) for v in node.get('rgba').split()]
            if node.get('texture'):
                mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = 'office_' + node.get('texture')
            if node.get('texrepeat'):
                mat.texrepeat = [float(v) for v in node.get('texrepeat').split()]
            mat.texuniform = node.get('texuniform', 'false') == 'true'
        nodes = {g.get('name'): g for g in root.findall('./worldbody/geom')}
        for box in room['boxes']:
            node = nodes.get(box['name'].removeprefix('office_'))
            material = 'office_' + node.get('material') if node is not None and node.get('material') else ''
            spec.worldbody.add_geom(name=box['name'], type=mujoco.mjtGeom.mjGEOM_BOX,
                pos=box['position'], size=np.asarray(box['size']) / 2,
                material=material, rgba=[.7, .7, .7, 1], friction=[1., .005, .0001])
        # Authored visual-only finishes never add invisible collision obstacles.
        for node in nodes.values():
            if node.get('name') == 'meeting_table' or node.get('type') != 'box':
                continue
            visual_only = node.get('contype', '1') == '0' and node.get('conaffinity', '1') == '0'
            if not visual_only and node.get('name') != 'floor':
                continue
            pos = office_to_local([float(v) for v in node.get('pos', '0 0 0').split()])
            size = [float(v) for v in node.get('size').split()]
            spec.worldbody.add_geom(name='office_visual_' + node.get('name'),
                type=mujoco.mjtGeom.mjGEOM_BOX, pos=pos, size=[size[1], size[0], size[2]],
                material='office_' + node.get('material'), contype=0, conaffinity=0,
                group=5 if 'ceiling' in node.get('name', '') else 2)
    room_module.add_mujoco_room = add_office
