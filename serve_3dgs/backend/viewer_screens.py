"""Viewer UI image widgets for 3DGS camera feeds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont


DEFAULT_VIEWER_CAMERA_NAMES = ("overhead_cam", "head_cam", "right_arm_cam", "left_arm_cam")


@dataclass(frozen=True)
class ViewerScreenBinding:
    camera_name: str
    camera_id: int


@dataclass(frozen=True)
class WidgetLayoutSpec:
    left: int
    top: int
    width: int
    height: int


def viewer_widget_layout_specs(
    camera_names: Sequence[str],
    width: int,
    height: int,
    *,
    margin: int = 10,
    gap: int = 10,
    cols: int = 2,
) -> tuple[WidgetLayoutSpec, ...]:
    """Lay out camera feeds as viewer UI widgets, not scene geometry."""
    if width <= 0 or height <= 0:
        raise ValueError("viewer widget dimensions must be positive")
    if cols <= 0:
        raise ValueError("viewer widget column count must be positive")

    specs = []
    for index, camera_name in enumerate(camera_names):
        row = index // cols
        col = index % cols
        specs.append(
            WidgetLayoutSpec(
                left=int(margin + col * (width + gap)),
                top=int(margin + row * (height + gap)),
                width=int(width),
                height=int(height),
            )
        )
    return tuple(specs)


def _iter_model_cameras(model) -> Iterable:
    cameras = getattr(model, "cameras", None)
    return getattr(cameras, "cameras", cameras)


def camera_screen_bindings(model, camera_names: Sequence[str]) -> tuple[ViewerScreenBinding, ...]:
    available: Dict[str, int] = {}
    for index, camera in enumerate(_iter_model_cameras(model)):
        name = getattr(camera, "name", "")
        if name:
            available[name] = index

    missing = [name for name in camera_names if name not in available]
    if missing:
        raise ValueError(
            f"missing viewer camera(s): {', '.join(missing)}; available: {', '.join(sorted(available))}"
        )

    return tuple(
        ViewerScreenBinding(
            camera_name=name,
            camera_id=available[name],
        )
        for name in camera_names
    )


def _make_layout(layout_cls, spec: WidgetLayoutSpec):
    return layout_cls(left=spec.left, top=spec.top, width=spec.width, height=spec.height)


def create_viewer_screen_images(
    render,
    bindings: Sequence[ViewerScreenBinding],
    *,
    width: int,
    height: int,
    layout_cls=None,
) -> Dict[str, object]:
    if layout_cls is None:
        from motrixsim.render import Layout

        layout_cls = Layout

    images: Dict[str, object] = {}
    specs = viewer_widget_layout_specs([binding.camera_name for binding in bindings], width, height)
    for binding, spec in zip(bindings, specs):
        placeholder = np.zeros((int(height), int(width), 3), dtype=np.uint8)
        image = render.create_image(placeholder)
        render.widgets.create_image_widget(image, layout=_make_layout(layout_cls, spec))
        images[binding.camera_name] = image
    return images


def _overlay_camera_name(frame: np.ndarray, name: str) -> np.ndarray:
    """Draw camera name label at top-left corner of the frame."""
    img = Image.fromarray(frame)
    draw = ImageDraw.Draw(img)
    font_size = max(14, int(min(img.size) / 20))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()
    text = name
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 4
    draw.rectangle([(pad, pad), (pad + tw + pad * 2, pad + th + pad * 2)], fill=(0, 0, 0, 180))
    draw.text((pad * 2, pad + 2), text, fill=(0, 255, 0), font=font)
    return np.array(img)


def update_viewer_screen_images(
    env,
    bindings: Sequence[ViewerScreenBinding],
    images: Dict[str, object],
    *,
    width: int,
    height: int,
) -> int:
    updated = 0
    for binding in bindings:
        image = images.get(binding.camera_name)
        if image is None:
            continue
        frame = env.render_frame(
            cam_id=binding.camera_id,
            width=int(width),
            height=int(height),
            cache_background=False,
        )
        frame = _overlay_camera_name(frame, binding.camera_name)
        image.pixels = np.ascontiguousarray(frame)
        updated += 1
    return updated
