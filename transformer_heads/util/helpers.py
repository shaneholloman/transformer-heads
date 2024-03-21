"""
This module provides helper functions and classes for handling data and models in a language model training and evaluation pipeline.
It includes a data collator for padding sequences and a function for getting model parameters based on the model type.

Classes:
    DataCollatorWithPadding: A data collator that pads sequences to the same length.

Functions:
    get_model_params(model_path: str): Get the parameters of a model based on its type.
"""

from dataclasses import dataclass
from typing import Any, Dict, List

import torch
from torch.nn.utils.rnn import pad_sequence
from transformers import GPT2LMHeadModel, LlamaForCausalLM, MistralForCausalLM


@dataclass
class DataCollatorWithPadding:
    """
    A data collator that pads sequences to the same length.

    Attributes:
        feature_name_to_padding_value (dict[str, int]): A dictionary mapping feature names to their padding values.

    Methods:
        __call__(features: List[Dict[str, Any]]) -> Dict[str, Any]: Pad the sequences in the features to the same length.
    """

    feature_name_to_padding_value: dict[str, int]

    def __call__(self, features: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Pad the sequences in the features to the same length.

        Args:
            features (List[Dict[str, Any]]): A list of features, where each feature is a dictionary mapping feature names to sequences.

        Returns:
            Dict[str, Any]: A dictionary mapping feature names to padded sequences.
        """
        batch = dict()
        for key, value in self.feature_name_to_padding_value.items():
            batch[key] = pad_sequence(
                [feature[key].clone().detach() for feature in features],
                batch_first=True,
                padding_value=value,
            )
        for key in features[0].keys():
            if key not in self.feature_name_to_padding_value:
                batch[key] = torch.stack(
                    [feature[key].clone().detach() for feature in features]
                )
        return batch


def get_model_params(model_path: str):
    """
    Get the parameters of a model based on its type.

    Args:
        model_path (str): The name of the huggingface model.

    Returns:
        dict: A dictionary containing the model class, hidden size, and vocab size.

    Raises:
        ValueError: If the model type is unknown.
    """
    if "gpt2" in model_path.lower():
        return {
            "model_class": GPT2LMHeadModel,
            "hidden_size": 768,
            "vocab_size": 50257,
        }
    elif "mistral" in model_path.lower():
        return {
            "model_class": MistralForCausalLM,
            "hidden_size": 4096,
            "vocab_size": 32000,
        }
    elif "llama" in model_path.lower():
        return {
            "model_class": LlamaForCausalLM,
            "hidden_size": 4096,
            "vocab_size": 32000,
        }
    else:
        raise ValueError(
            "Unknown model type. Find the hidden size and vocab size manually by checking your models config.json."
        )