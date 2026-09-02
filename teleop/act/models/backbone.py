import torch
import torchvision
from torch import nn
from torchvision.models._utils import IntermediateLayerGetter
from .position_encoding import build_position_encoding


class FrozenBatchNorm2d(torch.nn.Module):
    def __init__(self, n):
        super().__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def forward(self, x):
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = 1e-5
        scale = w * (rv + eps).rsqrt()
        bias = b - rm * scale
        return x * scale + bias


class Backbone(nn.Module):
    def __init__(self, name="resnet18", train_backbone=True):
        super().__init__()
        backbone = getattr(torchvision.models, name)(
            pretrained=True, norm_layer=FrozenBatchNorm2d
        )
        return_layers = {"layer4": "0"}
        self.body = IntermediateLayerGetter(backbone, return_layers=return_layers)
        self.num_channels = 512 if name in ("resnet18", "resnet34") else 2048

    def forward(self, tensor):
        xs = self.body(tensor)
        return xs


class Joiner(nn.Sequential):
    def __init__(self, backbone, position_embedding):
        super().__init__(backbone, position_embedding)

    def forward(self, tensor):
        xs = self[0](tensor)
        out = []
        pos = []
        for name, x in xs.items():
            out.append(x)
            pos.append(self[1](x).to(x.dtype))
        return out, pos


def build_backbone(args):
    position_embedding = build_position_encoding(args)
    backbone = Backbone(args.backbone, train_backbone=args.lr_backbone > 0)
    model = Joiner(backbone, position_embedding)
    model.num_channels = backbone.num_channels
    return model
