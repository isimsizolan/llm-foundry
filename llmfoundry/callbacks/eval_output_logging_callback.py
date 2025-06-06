# Copyright 2024 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

"""Log model outputs and expected outputs during ICL evaluation."""

import warnings
from copy import deepcopy
from typing import Any, Optional, Sequence, Union

import torch
from composer.core import Callback, State
from composer.loggers import ConsoleLogger, Logger
from composer.models import HuggingFaceModel
from composer.utils.dist import all_gather_object


class EvalOutputLogging(Callback):
    """Logs eval outputs for each sample of each ICL evaluation dataset.

    ICL metrics are required to support caching the model's responses including
    information on whether model was correct. Metrics are responsible for
    returning the results of individual data points in a dictionary of lists.
    The callback will log the metric name, the depadded and detokenized input,
    any data stored in state.metric_outputs, and any keys from the batch passed
    into `batch_keys_to_log`. It will do so after every eval batch.
    """

    def __init__(
        self,
        log_tokens: bool = False,
        log_output_text: Optional[bool] = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(self, *args, **kwargs)
        self.log_tokens = log_tokens
        self.columns = None
        self.name = None
        self.rows = []
        self.log_output_text = log_output_text

    def init(self, state: State, logger: Logger) -> None:
        if self.log_output_text is False:
            return

        has_output_text = (
            isinstance(state.model, HuggingFaceModel)
            and state.dataloader is not None
            and hasattr(
                state.dataloader.dataset,  # pyright: ignore[reportGeneralTypeIssues]
                'tokenizer',
            )
        )
        if self.log_output_text is True and has_output_text is False:
            raise ValueError(
                '`log_output_text=True` is only supported for HuggingFace models and datasets with tokenizers.',
            )
        elif self.log_output_text is None:
            self.log_output_text = has_output_text

    def eval_batch_end(self, state: State, logger: Logger) -> None:
        if not isinstance(state.batch, dict):
            warnings.warn(
                f"""EvalOutputLogging only supports batches that are dictionary. \
                Found batch for type {type(state.batch)}. \
                Not logging eval outputs.""",
            )
            return

        assert state.outputs is not None
        assert state.metric_outputs is not None
        logging_dict: dict[
            str,
            Union[list[Any], torch.Tensor, Sequence[torch.Tensor]],
        ] = deepcopy(
            state.metric_outputs,
        )

        if state.batch.get('mode') == 'generate':
            # Outputs are already detokenized
            logging_dict['outputs'] = state.outputs
        elif self.log_output_text and isinstance(state.outputs, torch.Tensor):
            # If batch mode is not generate, outputs will be logits
            logging_dict['outputs'] = state.outputs.argmax(dim=-1)

        input_ids = state.batch['input_ids']
        logged_input = []
        assert state.dataloader is not None
        dataset = state.dataloader.dataset  # pyright: ignore[reportGeneralTypeIssues]
        tokenizer = dataset.tokenizer  # pyright: ignore[reportGeneralTypeIssues]
        pad_token_id = getattr(
            dataset,
            'pad_tok_id',
            dataset.tokenizer.pad_token_id,
        )

        # Depad and decode input_ids
        for input_list in input_ids.tolist():
            depadded_input = [tok for tok in input_list if tok != pad_token_id]
            logged_input.append(tokenizer.decode(depadded_input))
        logging_dict['input'] = logged_input

        # Log token indices if toggled
        if self.log_tokens:
            logging_dict['input_tokens'] = input_ids.tolist()
            if not state.batch.get('mode') == 'generate':
                if isinstance(state.outputs, torch.Tensor):  # pyright
                    logging_dict['label_tokens'] = state.outputs.tolist()

        # Add run_name as a column
        run_name_list = [
            state.run_name for _ in range(0, len(logging_dict['input']))
        ]
        logging_dict['run_name'] = run_name_list

        # NOTE: This assumes _any_ tensor logged are tokens to be decoded.
        #       This might not be true if, for example, logits are logged.

        # Detokenize data in rows
        for key, value in logging_dict.items():
            # All types in list are the same
            if isinstance(value[0], torch.Tensor):
                logging_dict[key] = [tokenizer.decode(t) for t in value]
            elif isinstance(value[0], list):
                if isinstance(value[0][0], torch.Tensor):
                    logging_dict[key] = [[
                        tokenizer.decode(choice) for choice in t
                    ] for t in value]

        # Convert logging_dict from kv pairs of column name and column values to a list of rows
        # Example:
        # logging_dict = {"a": ["1a", "2a"], "b": ["1b", "2b"]}
        # will become
        # columns = {"a", "b"}, rows = [["1a", "1b"], ["2a", "2b"]]
        columns = list(logging_dict.keys())
        rows = [list(item) for item in zip(*logging_dict.values())]

        assert state.dataloader_label is not None
        if not self.name:
            # If only running eval, step will be 0
            # If running training, step will be current training step
            step = state.timestamp.batch.value
            self.name = f'{state.dataloader_label}_step_{step}'
            self.columns = columns
        self.rows.extend(rows)

    def eval_end(self, state: State, logger: Logger) -> None:
        list_of_rows = all_gather_object(self.rows)
        rows = [row for rows in list_of_rows for row in rows]
        # Only log if we have columns and a name
        if self.columns is not None and self.name is not None and rows:
            for dest_logger in logger.destinations:
                if not isinstance(dest_logger, ConsoleLogger):
                    dest_logger.log_table(
                        self.columns,
                        rows,
                        name=self.name,
                        step=state.timestamp.batch.value,
                    )

        self.rows = []
        self.name = None
        self.columns = None
