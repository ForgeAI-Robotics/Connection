import argparse
import numpy as np
import torch
from torch import nn
from torch.autograd import Variable
from .backbone import build_backbone
from .transformer import TransformerEncoder, TransformerEncoderLayer, build_transformer


def reparametrize(mu, logvar):
    std = logvar.div(2).exp()
    eps = Variable(std.data.new(std.size()).normal_())
    return mu + std * eps


def get_sinusoid_encoding_table(n_position, d_hid):
    def get_position_angle_vec(position):
        return [position / np.power(10000, 2 * (hid_j // 2) / d_hid) for hid_j in range(d_hid)]
    sinusoid_table = np.array([get_position_angle_vec(pos_i) for pos_i in range(n_position)])
    sinusoid_table[:, 0::2] = np.sin(sinusoid_table[:, 0::2])
    sinusoid_table[:, 1::2] = np.cos(sinusoid_table[:, 1::2])
    return torch.FloatTensor(sinusoid_table).unsqueeze(0)


class DETRVAE(nn.Module):
    def __init__(self, backbones, transformer, encoder, state_dim, action_dim, num_queries, camera_names):
        super().__init__()
        self.num_queries = num_queries
        self.camera_names = camera_names
        self.transformer = transformer
        self.encoder = encoder
        hidden_dim = transformer.d_model
        self.action_head = nn.Linear(hidden_dim, action_dim)
        self.is_pad_head = nn.Linear(hidden_dim, 1)
        self.query_embed = nn.Embedding(num_queries, hidden_dim)
        if backbones is not None:
            self.input_proj = nn.Conv2d(backbones[0].num_channels, hidden_dim, kernel_size=1)
            self.backbones = nn.ModuleList(backbones)
            self.input_proj_robot_state = nn.Linear(state_dim, hidden_dim)
        else:
            self.input_proj_robot_state = nn.Linear(state_dim, hidden_dim)
            self.backbones = None

        self.latent_dim = 32
        self.cls_embed = nn.Embedding(1, hidden_dim)
        self.encoder_action_proj = nn.Linear(action_dim, hidden_dim)
        self.encoder_joint_proj = nn.Linear(state_dim, hidden_dim)
        self.latent_proj = nn.Linear(hidden_dim, self.latent_dim * 2)
        self.register_buffer("pos_table", get_sinusoid_encoding_table(1 + 1 + num_queries, hidden_dim))
        self.latent_out_proj = nn.Linear(self.latent_dim, hidden_dim)
        self.additional_pos_embed = nn.Embedding(2, hidden_dim)

    def encode_images(self, image):
        all_cam_features = []
        all_cam_pos = []
        for cam_id in range(len(self.camera_names)):
            features, pos = self.backbones[cam_id](image[:, cam_id])
            features = features[0]
            pos = pos[0]
            all_cam_features.append(self.input_proj(features))
            all_cam_pos.append(pos)
        return all_cam_features, all_cam_pos

    def forward(self, qpos, image, env_state, actions=None, is_pad=None):
        if image.ndim == 4:
            image = image.unsqueeze(1)
        is_training = actions is not None
        bs, _ = qpos.shape
        if is_training:
            action_embed = self.encoder_action_proj(actions)
            qpos_embed = self.encoder_joint_proj(qpos)
            qpos_embed = torch.unsqueeze(qpos_embed, axis=1)
            cls_embed = self.cls_embed.weight
            cls_embed = torch.unsqueeze(cls_embed, axis=0).repeat(bs, 1, 1)
            encoder_input = torch.cat([cls_embed, qpos_embed, action_embed], axis=1)
            encoder_input = encoder_input.permute(1, 0, 2)
            cls_joint_is_pad = torch.full((bs, 2), False).to(qpos.device)
            is_pad = torch.cat([cls_joint_is_pad, is_pad], axis=1)
            pos_embed = self.pos_table.clone().detach()
            pos_embed = pos_embed.permute(1, 0, 2)
            encoder_output = self.encoder(encoder_input, pos=pos_embed, src_key_padding_mask=is_pad)
            encoder_output = encoder_output[0]
            latent_info = self.latent_proj(encoder_output)
            mu = latent_info[:, :self.latent_dim]
            logvar = latent_info[:, self.latent_dim:]
            latent_sample = reparametrize(mu, logvar)
            latent_input = self.latent_out_proj(latent_sample)
        else:
            mu = logvar = None
            latent_sample = torch.zeros([bs, self.latent_dim], dtype=torch.float32).to(qpos.device)
            latent_input = self.latent_out_proj(latent_sample)

        if self.backbones is not None:
            all_cam_features, all_cam_pos = self.encode_images(image)
            proprio_input = self.input_proj_robot_state(qpos)
            src = torch.cat(all_cam_features, axis=3)
            pos = torch.cat(all_cam_pos, axis=3)
            hs = self.transformer(src, None, self.query_embed.weight, pos, latent_input, proprio_input, self.additional_pos_embed.weight)[0]
        else:
            qpos = self.input_proj_robot_state(qpos)
            src = qpos.unsqueeze(1)
            pos = torch.zeros_like(src)
            hs = self.transformer(src, None, self.query_embed.weight, pos)[0]
        a_hat = self.action_head(hs)
        is_pad_hat = self.is_pad_head(hs)
        return a_hat, is_pad_hat, [mu, logvar]


def build_encoder(args):
    d_model = args.hidden_dim
    encoder_layer = TransformerEncoderLayer(d_model, args.nheads, args.dim_feedforward, args.dropout, "relu", args.pre_norm)
    encoder_norm = nn.LayerNorm(d_model) if args.pre_norm else None
    encoder = TransformerEncoder(encoder_layer, args.enc_layers, encoder_norm)
    return encoder


def build_vae(args):
    transformer = build_transformer(args)
    encoder = build_encoder(args)
    backbones = []
    for _ in args.camera_names:
        backbone = build_backbone(args)
        backbones.append(backbone)
    model = DETRVAE(backbones, transformer, encoder, state_dim=args.state_dim, action_dim=args.action_dim, num_queries=args.num_queries, camera_names=args.camera_names)
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"ACT model: {n_parameters / 1e6:.2f}M parameters")
    return model


def build_optimizer(model, args):
    param_dicts = [
        {"params": [p for n, p in model.named_parameters() if "backbone" not in n and p.requires_grad]},
        {"params": [p for n, p in model.named_parameters() if "backbone" in n and p.requires_grad], "lr": args.lr_backbone},
    ]
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr, weight_decay=args.weight_decay)
    return optimizer


def get_args_parser():
    parser = argparse.ArgumentParser("ACT", add_help=False)
    parser.add_argument("--lr", default=1e-4, type=float)
    parser.add_argument("--lr_backbone", default=1e-5, type=float)
    parser.add_argument("--weight_decay", default=1e-4, type=float)
    parser.add_argument("--backbone", default="resnet18", type=str)
    parser.add_argument("--position_embedding", default="sine", type=str)
    parser.add_argument("--camera_names", default=[], type=list)
    parser.add_argument("--enc_layers", default=4, type=int)
    parser.add_argument("--dec_layers", default=7, type=int)
    parser.add_argument("--dim_feedforward", default=3200, type=int)
    parser.add_argument("--hidden_dim", default=512, type=int)
    parser.add_argument("--dropout", default=0.1, type=float)
    parser.add_argument("--nheads", default=8, type=int)
    parser.add_argument("--num_queries", default=40, type=int)
    parser.add_argument("--pre_norm", action="store_true")
    parser.add_argument("--masks", action="store_true")
    parser.add_argument("--dilation", action="store_true")
    return parser


def build_ACT_model(args_override):
    parser = argparse.ArgumentParser("ACT", parents=[get_args_parser()])
    args, _ = parser.parse_known_args()
    for k, v in args_override.items():
        setattr(args, k, v)
    model = build_vae(args)
    model.cuda()
    return model, args
