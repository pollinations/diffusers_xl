# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Optional

import torch
import torch.nn.functional as F
from torch import nn

from ..utils import logging


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


class LoRALinearLayer(nn.Module):
    def __init__(self, in_features, out_features, rank=4, network_alpha=None, device=None, dtype=None):
        super().__init__()

        self.down = nn.Linear(in_features, rank, bias=False, device=device, dtype=dtype)
        self.up = nn.Linear(rank, out_features, bias=False, device=device, dtype=dtype)
        # This value has the same meaning as the `--network_alpha` option in the kohya-ss trainer script.
        # See https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning
        self.network_alpha = network_alpha
        self.rank = rank
        self.out_features = out_features
        self.in_features = in_features

        nn.init.normal_(self.down.weight, std=1 / rank)
        nn.init.zeros_(self.up.weight)

    def forward(self, hidden_states):
        orig_dtype = hidden_states.dtype
        dtype = self.down.weight.dtype

        down_hidden_states = self.down(hidden_states.to(dtype))
        up_hidden_states = self.up(down_hidden_states)

        if self.network_alpha is not None:
            up_hidden_states *= self.network_alpha / self.rank

        return up_hidden_states.to(orig_dtype)


class LoRAConv2dLayer(nn.Module):
    def __init__(
        self, in_features, out_features, rank=4, kernel_size=(1, 1), stride=(1, 1), padding=0, network_alpha=None
    ):
        super().__init__()

        self.down = nn.Conv2d(in_features, rank, kernel_size=kernel_size, stride=stride, padding=padding, bias=False)
        # according to the official kohya_ss trainer kernel_size are always fixed for the up layer
        # # see: https://github.com/bmaltais/kohya_ss/blob/2accb1305979ba62f5077a23aabac23b4c37e935/networks/lora_diffusers.py#L129
        self.up = nn.Conv2d(rank, out_features, kernel_size=(1, 1), stride=(1, 1), bias=False)

        # This value has the same meaning as the `--network_alpha` option in the kohya-ss trainer script.
        # See https://github.com/darkstorm2150/sd-scripts/blob/main/docs/train_network_README-en.md#execute-learning
        self.network_alpha = network_alpha
        self.rank = rank

        nn.init.normal_(self.down.weight, std=1 / rank)
        nn.init.zeros_(self.up.weight)

    def forward(self, hidden_states):
        orig_dtype = hidden_states.dtype
        dtype = self.down.weight.dtype

        down_hidden_states = self.down(hidden_states.to(dtype))
        up_hidden_states = self.up(down_hidden_states)

        if self.network_alpha is not None:
            up_hidden_states *= self.network_alpha / self.rank

        return up_hidden_states.to(orig_dtype)


class LoRACompatibleConv(nn.Conv2d):
    """
    A convolutional layer that can be used with LoRA.
    """

    def __init__(self, *args, lora_layer: Optional[LoRAConv2dLayer] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lora_layer = lora_layer

    def set_lora_layer(self, lora_layer: Optional[LoRAConv2dLayer]):
        self.lora_layer = lora_layer

    def _fuse_lora(self):
        if self.lora_layer is None:
            return

        dtype, device = self.weight.data.dtype, self.weight.data.device
        logger.info(f"Fusing LoRA weights for {self.__class__}")

        w_orig = self.weight.data.float()
        w_up = self.lora_layer.up.weight.data.float()
        w_down = self.lora_layer.down.weight.data.float()

        if self.lora_layer.network_alpha is not None:
            w_up = w_up * self.lora_layer.network_alpha / self.lora_layer.rank

        fusion = torch.mm(w_up.flatten(start_dim=1), w_down.flatten(start_dim=1))
        fusion = fusion.reshape((w_orig.shape))
        fused_weight = w_orig + fusion
        self.weight.data = fused_weight.to(device=device, dtype=dtype)

        # we can drop the lora layer now
        self.lora_layer = None

        # offload the up and down matrices to CPU to not blow the memory
        self.w_up = w_up.cpu()
        self.w_down = w_down.cpu()

    def _unfuse_lora(self):
        if not (hasattr(self, "w_up") and hasattr(self, "w_down")):
            return
        logger.info(f"Unfusing LoRA weights for {self.__class__}")

        fused_weight = self.weight.data
        dtype, device = fused_weight.data.dtype, fused_weight.data.device

        self.w_up = self.w_up.to(device=device, dtype=dtype)
        self.w_down = self.w_down.to(device, dtype=dtype)

        fusion = torch.mm(self.w_up.flatten(start_dim=1), self.w_down.flatten(start_dim=1))
        fusion = fusion.reshape((fused_weight.shape))
        unfused_weight = fused_weight - fusion
        self.weight.data = unfused_weight.to(device=device, dtype=dtype)

        self.w_up = None
        self.w_down = None

    def forward(self, x):
        if self.lora_layer is None:
            # make sure to the functional Conv2D function as otherwise torch.compile's graph will break
            # see: https://github.com/huggingface/diffusers/pull/4315
            return F.conv2d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)
        else:
            return super().forward(x) + self.lora_layer(x)


class LoRACompatibleLinear(nn.Linear):
    """
    A Linear layer that can be used with LoRA.
    """

    def __init__(self, *args, lora_layer: Optional[LoRALinearLayer] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lora_layer = lora_layer

    def set_lora_layer(self, lora_layer: Optional[LoRALinearLayer]):
        self.lora_layer = lora_layer

    def _fuse_lora(self):
        if self.lora_layer is None:
            return

        dtype, device = self.weight.data.dtype, self.weight.data.device
        logger.info(f"Fusing LoRA weights for {self.__class__}")

        w_orig = self.weight.data.float()
        w_up = self.lora_layer.up.weight.data.float()
        w_down = self.lora_layer.down.weight.data.float()

        if self.lora_layer.network_alpha is not None:
            w_up = w_up * self.lora_layer.network_alpha / self.lora_layer.rank

        fused_weight = w_orig + torch.bmm(w_up[None, :], w_down[None, :])[0]
        self.weight.data = fused_weight.to(device=device, dtype=dtype)

        # we can drop the lora layer now
        self.lora_layer = None

        # offload the up and down matrices to CPU to not blow the memory
        self.w_up = w_up.cpu()
        self.w_down = w_down.cpu()

    def _unfuse_lora(self):
        if not (hasattr(self, "w_up") and hasattr(self, "w_down")):
            return
        logger.info(f"Unfusing LoRA weights for {self.__class__}")

        fused_weight = self.weight.data
        dtype, device = fused_weight.dtype, fused_weight.device

        self.w_up = self.w_up.to(device=device, dtype=dtype)
        self.w_down = self.w_down.to(device, dtype=dtype)
        unfused_weight = fused_weight - torch.bmm(self.w_up[None, :], self.w_down[None, :])[0]
        self.weight.data = unfused_weight.to(device=device, dtype=dtype)

        self.w_up = None
        self.w_down = None

    def forward(self, hidden_states, lora_scale: int = 1):
        if self.lora_layer is None:
            return super().forward(hidden_states)
        else:
            return super().forward(hidden_states) + lora_scale * self.lora_layer(hidden_states)
