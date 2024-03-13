import json
import os
from os import PathLike
from transformers.models.mistral.modeling_mistral import (
    MistralModel,
    MistralPreTrainedModel,
)
from transformers.modeling_outputs import BaseModelOutputWithPast
from transformers.utils import ModelOutput
from headed_config import HeadConfig
from headed_output import HeadedModelOutput
from mlp_head import MLPHead
import torch.nn as nn
import torch
from typing import Optional, List, Union, Tuple, Dict, Type, Any, Callable
from types import MethodType
from abc import ABC, abstractmethod

from transformers import PreTrainedModel, PretrainedConfig
from headed_config import create_headed_model_config


def get_headed_pretrained_model_class(base_model_class: Type[PreTrainedModel]):
    class HeadedPreTrainedModel(base_model_class):
        config_class = create_headed_model_config(base_model_class.config_class)

    return HeadedPreTrainedModel


loss_fct_map = {
    "mse": nn.MSELoss(),
    "cross_entropy": nn.CrossEntropyLoss(),
}

model_type_map = {
    "mistral": MistralModel,
}


def patch_save_pretrained(model, preserve_old: bool = True):
    def save_pretrained(
        self,
        save_directory: str | PathLike,
        is_main_process: bool = True,
        state_dict: Dict | None = None,
        save_function: Callable[..., Any] = torch.save,
        push_to_hub: bool = False,
        max_shard_size: int | str = "5GB",
        safe_serialization: bool = True,
        variant: str | None = None,
        token: str | bool | None = None,
        save_peft_format: bool = True,
        **kwargs
    ):
        os.makedirs(save_directory, exist_ok=True)
        self.old_save_pretrained(
            save_directory=save_directory,
            is_main_process=is_main_process,
            state_dict=state_dict,
            save_function=save_function,
            push_to_hub=push_to_hub,
            max_shard_size=max_shard_size,
            safe_serialization=safe_serialization,
            variant=variant,
            token=token,
            save_peft_format=save_peft_format,
            **kwargs
        )
        head: MLPHead
        for head in self.heads.values():
            if head.requires_individual_saving:
                head.save_to_safetensors(save_directory)
        with open(os.path.join(save_directory, "head_configs.json"), "w") as f:
            json.dump(self.head_configs, f)

    if preserve_old:
        model.old_save_pretrained = model.save_pretrained
    else:
        model.old_save_pretrained = MethodType(lambda *args, **kwargs: None, model)
    model.save_pretrained = MethodType(save_pretrained, model)


class HeadedModel(ABC, PreTrainedModel):
    head_configs: List[HeadConfig]
    vocab_size: int
    heads: nn.ModuleDict
    lm_head_config: Optional[HeadConfig]
    lm_head: Optional[MLPHead]


def get_multi_head_transformer(base_model_class: Type[PreTrainedModel]):
    class TransformerWithHeads(
        get_headed_pretrained_model_class(base_model_class), HeadedModel
    ):
        def __init__(self, config: PretrainedConfig):
            super().__init__(config)
            self.model = model_type_map[config.model_type](config.to_base_class())
            self.vocab_size: int = config.vocab_size
            self.head_configs: dict[str:HeadConfig] = {
                cfg.name: cfg for cfg in config.output_heads
            }
            self.heads = nn.ModuleDict(
                {
                    name: MLPHead.from_head_config(head_config)
                    for name, head_config in self.head_configs.items()
                }
            )

            # Make pretrained loading of lm_head work
            self.lm_head = None
            self.lm_head_config = None
            print(type(self.heads))
            head: MLPHead
            for name, head in self.heads.items():
                if name == "lm_head":
                    self.lm_head = head.lins[0]
                    self.lm_head_config = self.head_configs[name]
                    del self.heads[name]
                    break

        def get_input_embeddings(self):
            return self.model.embed_tokens

        def set_input_embeddings(self, value):
            self.model.embed_tokens = value

        def save_pretrained(
            self,
            save_directory: str | PathLike,
            is_main_process: bool = True,
            state_dict: Dict | None = None,
            save_function: Callable[..., Any] = torch.save,
            push_to_hub: bool = False,
            max_shard_size: int | str = "5GB",
            safe_serialization: bool = True,
            variant: str | None = None,
            token: str | bool | None = None,
            save_peft_format: bool = True,
            **kwargs
        ):
            super().save_pretrained(
                save_directory,
                is_main_process,
                state_dict,
                save_function,
                push_to_hub,
                max_shard_size,
                safe_serialization,
                variant,
                token,
                save_peft_format,
                **kwargs
            )
            head: MLPHead
            for head in self.heads:
                if head.requires_individual_saving:
                    head.save_to_safetensors(save_directory)

        def set_decoder(self, decoder):
            self.model = decoder

        def get_decoder(self):
            return self.model

        def forward(
            self,
            input_ids: torch.LongTensor = None,
            attention_mask: Optional[torch.Tensor] = None,
            position_ids: Optional[torch.LongTensor] = None,
            past_key_values: Optional[List[torch.FloatTensor]] = None,
            inputs_embeds: Optional[torch.FloatTensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = False,
            **labels
        ) -> HeadedModelOutput:
            assert not return_dict
            output_attentions = (
                output_attentions
                if output_attentions is not None
                else self.config.output_attentions
            )
            output_hidden_states = (
                output_hidden_states
                if output_hidden_states is not None
                else self.config.output_hidden_states
            )
            print("In id shape", input_ids.shape)

            # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
            outputs: BaseModelOutputWithPast = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                inputs_embeds=inputs_embeds,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=True,
            )

            out_logits = {}
            out_preds = {}

            hidden_states = outputs.hidden_states
            loss = 0
            loss_by_head = {}
            for key in list(self.heads.keys()) + ["lm_head"]:
                if key == "lm_head":
                    if self.lm_head is None:
                        continue
                    head = self.lm_head
                    head_config = self.lm_head_config
                else:
                    head = self.heads[key]
                    head_config = self.head_configs[key]
                selected_hidden_states = hidden_states[head_config.layer_hook]
                logits: torch.FloatTensor = head(selected_hidden_states)
                if head_config.is_regression:
                    out_preds[head_config.name] = logits
                else:
                    out_logits[head_config.name] = logits
                if (
                    labels is not None
                    and head_config.name in labels
                    and head_config.loss_fct is not None
                ):
                    loss_fct = loss_fct_map[head_config.loss_fct]
                    if head_config.is_causal_lm:
                        use_logits = logits[..., :-1, :].contiguous()
                        use_labels = labels[head_config.name][..., 1:].contiguous()
                    else:
                        use_logits = logits
                        use_labels = labels[head_config.name]
                    if head_config.is_regression:
                        use_logits = use_logits.view(-1)
                    else:
                        use_logits = use_logits.view(
                            -1, head_config.num_outputs or self.config.vocab_size
                        )
                    use_labels = use_labels.view(-1)
                    use_labels = use_labels.to(use_logits.device)
                    loss_by_head[head_config.name] = loss_fct(
                        use_logits, use_labels[head_config.name]
                    )
                    loss += loss_by_head[head_config.name]

            return HeadedModelOutput(
                loss=loss,
                loss_by_head=loss_by_head,
                logits_by_head=out_logits,
                preds_by_head=out_preds,
                past_key_values=outputs.past_key_values,
                hidden_states=outputs.hidden_states if output_hidden_states else None,
                attentions=outputs.attentions,
            )

    return TransformerWithHeads
