# Copyright 2024 Bytedance Ltd. and/or its affiliates
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

import os
from typing import Optional, Union

import torch
import torch.distributed as dist
from torch.distributed.checkpoint.state_dict import (
    StateDictOptions,
    get_model_state_dict,
    get_state_dict,
    set_state_dict,
    set_model_state_dict,
    set_optimizer_state_dict,
)
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from transformers import PreTrainedModel, PreTrainedTokenizer, ProcessorMixin

from .checkpoint_manager import BaseCheckpointManager
from torch.distributed.fsdp.api import StateDictType


class FSDPCheckpointManager(BaseCheckpointManager):
    """
    A checkpoint manager that saves and loads
    - model
    - optimizer
    - lr_scheduler
    - extra_states
    in a SPMD way.

    We save
    - sharded model states and optimizer states
    - full lr_scheduler states
    - huggingface tokenizer and config for ckpt merge
    """

    def __init__(
        self,
        model: FSDP,
        optimizer: torch.optim.Optimizer,
        lr_scheduler: torch.optim.lr_scheduler.LRScheduler,
        processing_class: Union[PreTrainedTokenizer, ProcessorMixin],
    ):
        super().__init__(model, optimizer, lr_scheduler, processing_class)

    def load_checkpoint(self, path: Optional[str] = None):
        if path is None:
            return

        # every rank download its own checkpoint
        model_path = os.path.join(path, f"model_world_size_{self.world_size}_rank_{self.rank}.pt")
        optim_path = os.path.join(path, f"optim_world_size_{self.world_size}_rank_{self.rank}.pt")
        extra_path = os.path.join(path, f"extra_state_world_size_{self.world_size}_rank_{self.rank}.pt")
        state_dict_options = StateDictOptions(cpu_offload=True)
        if not os.path.exists(optim_path):
            print("[rank-{self.rank}]: Optimizer checkpoint not found, skipping optimizer and extra_state loading.")
            print(f"[rank-{self.rank}]: Loading model from {os.path.abspath(model_path)}.")
            model_state_dict = torch.load(model_path, weights_only=False)
            set_model_state_dict(
                model=self.model,
                model_state_dict=model_state_dict,
                options=state_dict_options,
            )
        else:
            print(f"[rank-{self.rank}]: Loading optimizer from {os.path.abspath(optim_path)}.")
            print(f"[rank-{self.rank}]: Loading model from {os.path.abspath(model_path)}.")
            print(f"[rank-{self.rank}]: Loading extra_state from {os.path.abspath(extra_path)}.")
            optim_state_dict = torch.load(optim_path, weights_only=False)
            extra_state_dict = torch.load(extra_path, weights_only=False)
            model_state_dict = torch.load(model_path, weights_only=False)

            set_state_dict(
                model=self.model,
                optimizers=self.optimizer,
                model_state_dict=model_state_dict,
                optim_state_dict=optim_state_dict,
                options=state_dict_options,
            )
            self.lr_scheduler.load_state_dict(extra_state_dict["lr_scheduler"])

            # recover random state
            if "rng" in extra_state_dict:
                self.load_rng_state(extra_state_dict["rng"])

    def save_checkpoint(self, path: str, save_model_only: bool = False):
        path = self.local_mkdir(path)
        dist.barrier()

        # every rank will save its own model and optim shard
        model_path = os.path.join(path, f"model_world_size_{self.world_size}_rank_{self.rank}.pt")
        optim_path = os.path.join(path, f"optim_world_size_{self.world_size}_rank_{self.rank}.pt")
        extra_path = os.path.join(path, f"extra_state_world_size_{self.world_size}_rank_{self.rank}.pt")

        state_dict_options = StateDictOptions(cpu_offload=True)
        if save_model_only:
            model_state_dict = get_model_state_dict(self.model, options=state_dict_options)
            print(f"[rank-{self.rank}]: Saving model to {os.path.abspath(model_path)}.")
            torch.save(model_state_dict, model_path)
        else:
            model_state_dict, optim_state_dict = get_state_dict(self.model, self.optimizer, options=state_dict_options)
            extra_state_dict = {
                "lr_scheduler": self.lr_scheduler.state_dict(),
                "rng": self.get_rng_state(),
            }
            print(f"[rank-{self.rank}]: Saving model to {os.path.abspath(model_path)}.")
            print(f"[rank-{self.rank}]: Saving optimizer to {os.path.abspath(optim_path)}.")
            print(f"[rank-{self.rank}]: Saving extra_state to {os.path.abspath(extra_path)}.")
            torch.save(model_state_dict, model_path)
            torch.save(optim_state_dict, optim_path)
            torch.save(extra_state_dict, extra_path)

        # wait for everyone to dump to local
        dist.barrier()

        # if self.rank == 0:
        #     hf_path = os.path.join(path, "huggingface")
        #     os.makedirs(hf_path, exist_ok=True)
        #     assert isinstance(self.model._fsdp_wrapped_module, PreTrainedModel)
        #     self.model._fsdp_wrapped_module.config.save_pretrained(hf_path)
        #     self.model._fsdp_wrapped_module.generation_config.save_pretrained(hf_path)
        #     self.processing_class.save_pretrained(hf_path)
        # dist.barrier()

        print("try to save merged model to huggingface format...")
        try:
            # 1. Get the consolidated state_dict from the FSDP WRAPPER
            with FSDP.state_dict_type(self.model, StateDictType.FULL_STATE_DICT):
                cpu_state_dict = self.model.state_dict()
            # 2. On rank 0, pass this state_dict to the UNDERLYING module's save function
            if self.rank == 0:
                merged_model_path = os.path.join(path, "merged_model")
                os.makedirs(merged_model_path, exist_ok=True)
                self.model._fsdp_wrapped_module.save_pretrained(merged_model_path, state_dict=cpu_state_dict)
                assert isinstance(self.model._fsdp_wrapped_module, PreTrainedModel)
                self.model._fsdp_wrapped_module.config.save_pretrained(merged_model_path)
                self.model._fsdp_wrapped_module.generation_config.save_pretrained(merged_model_path)
                self.processing_class.save_pretrained(merged_model_path)

        except Exception as e:
            print(f"Failed to save merged model: {e}")
            print("Skipping saving merged model to huggingface format.")
        dist.barrier()
