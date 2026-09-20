import numpy as np
import torch


class TemporalEnsembling(object):
    def __init__(self, chunk_size, action_dim, max_timesteps):
        self.chunk_size = chunk_size
        self.action_dim = action_dim
        self.max_timesteps = max_timesteps
        self.reset()

    def reset(self):
        self.t = 0
        self.all_time_actions = torch.zeros(
            [self.max_timesteps, self.max_timesteps + self.chunk_size, self.action_dim]
        ).cuda()

    def update(self, raw_actions):
        self.all_time_actions[[self.t], self.t:self.t + self.chunk_size] = raw_actions
        bias = self.t - self.chunk_size + 1
        start = int(max(0, bias))
        end = int(start + self.chunk_size + min(0, bias))
        actions_for_curr_step = self.all_time_actions[start:end, self.t]
        k = 0.01
        exp_weights = np.exp(-k * np.arange(actions_for_curr_step.shape[0]))
        exp_weights = exp_weights / exp_weights.sum()
        exp_weights = torch.from_numpy(exp_weights).cuda().unsqueeze(dim=1)
        new_action = (actions_for_curr_step * exp_weights).sum(dim=0, keepdim=True)
        self.t += 1
        return new_action
