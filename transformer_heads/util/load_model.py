"""
This module provides functions for loading and creating transformer models with additional heads.

Functions:
    patch_quantization_config: Modifies the quantization configuration to skip head modules during the quantization process.
    load_headed: Loads a transformer model with additional heads.
    load_lora_with_heads: Loads a LoRA (Low Rank Adaptation) transformer model with additional heads.
    create_headed_qlora: Creates a quantized LoRA (Low Rank Adaptation) transformer model with additional heads.

These functions are used to load and create transformer models with additional heads,
which can be useful for tasks such as multi-task learning or linear probes.
The models can be loaded with or without quantization, and with or without LoRA (Low Rank Adaptation).
"""

import json
import os
from typing import Type

import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import BitsAndBytesConfig, PretrainedConfig, PreTrainedModel

from transformer_heads.config import HeadConfig, create_headed_model_config
from transformer_heads.model.head import MLPHead
from transformer_heads.model.model import HeadedModel, get_multi_head_transformer

from .model import find_all_linear_names, patch_save_pretrained


def patch_quantization_config(quantization_config: BitsAndBytesConfig):
    """
    Modifies the quantization configuration to skip head modules during the quantization process.

    Args:
        quantization_config (BitsAndBytesConfig): The quantization configuration to modify.
    """
    if quantization_config.llm_int8_skip_modules is None:
        quantization_config.llm_int8_skip_modules = []
    quantization_config.llm_int8_skip_modules.extend(["MLPHead", "heads", "lm_head"])


def load_headed(
    base_model_class: Type[PreTrainedModel],
    model_name: str,
    head_configs=None,
    head_folder_path=None,
    only_inference: bool = False,
    device_map="auto",
    quantization_config: BitsAndBytesConfig = None,
    freeze_base_model: bool = True,
    **kwargs,
):
    """
    Loads a transformer model with additional heads.

    Args:
        base_model_class (Type[PreTrainedModel]): The class of the base transformer model.
        model_name (str): The huggingface name of the model to load.
        head_configs (list, optional): A list of head configurations.
        head_folder_path (str, optional): The path to the folder containing the saved heads and head configurations.
        only_inference (bool, optional): Whether to load the model for inference only.
        device_map (str, optional): The device map to use when loading the model.
        quantization_config (BitsAndBytesConfig, optional): The quantization configuration to use when loading the model.
        freeze_base_model (bool, optional): Whether to freeze the base model during training.
        **kwargs: Additional keyword arguments to pass to from_pretrained.
    """
    assert head_configs is not None or head_folder_path is not None
    assert head_configs is None or head_folder_path is None
    assert (
        quantization_config is None or only_inference or freeze_base_model
    ), "You can only use quantization in inference mode or if you freeze the base model. Use qlora to modify the base model with quantization."
    if head_folder_path is not None:
        with open(os.path.join(head_folder_path, "head_configs.json"), "r") as f:
            head_configs = list(json.load(f).values())
    if quantization_config is None:
        bits = 32
    else:
        patch_quantization_config(quantization_config)
        bits = (
            4
            if quantization_config.load_in_4bit
            else 8 if quantization_config.load_in_8bit else 32
        )
    base_model_config = base_model_class.config_class.from_pretrained(model_name)
    headed_config_class = create_headed_model_config(base_model_class.config_class)
    config = headed_config_class.from_base_class(base_model_config, head_configs)

    model = get_multi_head_transformer(base_model_class)
    model = model.from_pretrained(
        model_name,
        config=config,
        device_map=device_map,
        load_in_4bit=bits == 4,
        load_in_8bit=bits == 8,
        quantization_config=quantization_config,
        **kwargs,
    )
    if freeze_base_model and quantization_config is None:
        for name, param in model.named_parameters():
            param.requires_grad = False
    if quantization_config is not None and bits < 16:
        if not only_inference:
            model = prepare_model_for_kbit_training(model)
            model._hf_peft_config_loaded = (
                True  # Nasty hack to avoid hf Trainer assertion error
            )
        patch_save_pretrained(model, preserve_old=False)
    head: MLPHead
    for head in model.heads.values():
        if head_folder_path is not None:
            head.load_from_safetensors(head_folder_path)
        if not only_inference:
            if head.trainable:
                head.set_requires_grad(True)
            head.requires_individual_saving = True
    return model


def load_lora_with_heads(
    base_model_class: Type[PreTrainedModel],
    path: str,
    quantization_config: BitsAndBytesConfig = None,
    only_inference: bool = False,
    fully_trained_heads: bool = True,
    device_map="auto",
    torch_dtype=torch.float32,
    gradient_checkpointing: bool = False,
    **kwargs,
):
    """
    Loads a LoRA (Low Rank Adaptation) transformer model with additional heads.

    Args:
        base_model_class (Type[PreTrainedModel]): The class of the base transformer model.
        path (str): The path (saved or huggingface) to the model to load.
        quantization_config (BitsAndBytesConfig, optional): The quantization configuration to use when loading the model.
        only_inference (bool, optional): Whether to load the model for inference only.
        fully_trained_heads (bool, optional): Whether to fully train all the heads.
        device_map (str, optional): The device map to use when loading the model.
        torch_dtype (torch.dtype, optional): The torch processing data type for the model.
        gradient_checkpointing (bool, optional): Whether to prepare the model for gradient checkpointing.
        **kwargs: Additional keyword arguments to pass to from_pretrained.
    """

    if quantization_config is None:
        bits = 32
    else:
        patch_quantization_config(quantization_config)
        bits = (
            4
            if quantization_config.load_in_4bit
            else 8 if quantization_config.load_in_8bit else 32
        )
    adapt_config_path = os.path.join(path, "adapter_config.json")
    with open(adapt_config_path, "r") as f:
        base_model_path = json.load(f)["base_model_name_or_path"]
    with open(os.path.join(path, "head_configs.json"), "r") as f:
        head_configs = list(json.load(f).values())

    base_model_config: PretrainedConfig = base_model_class.config_class.from_pretrained(
        base_model_path
    )

    headed_config_class = create_headed_model_config(base_model_class.config_class)
    config = headed_config_class.from_base_class(base_model_config, head_configs)

    model = get_multi_head_transformer(base_model_class)
    model: HeadedModel = model.from_pretrained(
        base_model_path,
        load_in_4bit=bits == 4,
        load_in_8bit=bits == 8,
        config=config,
        device_map=device_map,
        quantization_config=quantization_config,
        torch_dtype=torch_dtype,
        **kwargs,
    )

    if not only_inference:
        model: HeadedModel = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=gradient_checkpointing
        )
    model.load_adapter(path, device_map=device_map)

    if not only_inference:
        for name, param in model.named_parameters():
            if "lora" in name:
                param.requires_grad = True
    head: MLPHead
    for head in model.heads.values():
        head.load_from_safetensors(path)
        if not only_inference and fully_trained_heads:
            head.set_requires_grad(True)
            head.requires_individual_saving = True
    patch_save_pretrained(model)
    return model


def create_headed_qlora(
    base_model_class: Type[PreTrainedModel],
    model_name: str,
    quantization_config: BitsAndBytesConfig,
    lora_config: LoraConfig,
    head_configs: list[HeadConfig],
    fully_trained_heads: bool = True,
    device_map="auto",
    gradient_checkpointing: bool = False,
    **kwargs,
):
    """
    Creates a quantized LoRA (Low Rank Adaptation) transformer model with additional heads.

    Args:
        base_model_class (Type[PreTrainedModel]): The class of the base transformer model.
        model_name (str): The name of the pretrained base model (e.g. it's huggingface name).
        quantization_config (BitsAndBytesConfig): The quantization configuration to use when creating the model.
        lora_config (LoraConfig): The LoRA configuration to adapt the model with.
        head_configs (list[HeadConfig]): A list of head configurations.
        fully_trained_heads (bool, optional): Whether the heads should be fully trained.
        device_map (str, optional): The device map to use when creating the model.
        gradient_checkpointing (bool, optional): Whether to prepare the model for gradient checkpointing.
        **kwargs: Additional keyword arguments to pass to from_pretrained.
    """
    patch_quantization_config(quantization_config)
    bits = (
        4
        if quantization_config.load_in_4bit
        else 8 if quantization_config.load_in_8bit else 32
    )
    base_model_config: PretrainedConfig = base_model_class.config_class.from_pretrained(
        model_name
    )
    headed_config_class = create_headed_model_config(base_model_class.config_class)
    config = headed_config_class.from_base_class(base_model_config, head_configs)

    model = get_multi_head_transformer(base_model_class)

    model: HeadedModel = model.from_pretrained(
        model_name,
        load_in_4bit=quantization_config.load_in_4bit,
        load_in_8bit=quantization_config.load_in_8bit,
        config=config,
        device_map=device_map,
        quantization_config=quantization_config,
        **kwargs,
    )

    if lora_config.target_modules is None:
        lora_config.target_modules = find_all_linear_names(bits, model, noadd=["heads"])

    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=gradient_checkpointing
    )

    model = get_peft_model(model, lora_config)

    if fully_trained_heads:
        head: MLPHead
        for head in model.heads.values():
            head.set_requires_grad(True)
            head.requires_individual_saving = True

    patch_save_pretrained(model)
    return model