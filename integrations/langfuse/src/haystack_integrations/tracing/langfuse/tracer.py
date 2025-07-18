# SPDX-FileCopyrightText: 2023-present deepset GmbH <info@deepset.ai>
#
# SPDX-License-Identifier: Apache-2.0

import contextlib
import os
from abc import ABC, abstractmethod
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Union

from haystack import default_from_dict, default_to_dict, logging
from haystack.dataclasses import ChatMessage
from haystack.tracing import Span, Tracer
from haystack.tracing import tracer as proxy_tracer
from haystack.tracing import utils as tracing_utils
from typing_extensions import TypeAlias

import langfuse
from langfuse.client import StatefulGenerationClient, StatefulSpanClient, StatefulTraceClient

# Type alias for Langfuse stateful clients
LangfuseStatefulClient: TypeAlias = Union[StatefulTraceClient, StatefulSpanClient, StatefulGenerationClient]


logger = logging.getLogger(__name__)

HAYSTACK_LANGFUSE_ENFORCE_FLUSH_ENV_VAR = "HAYSTACK_LANGFUSE_ENFORCE_FLUSH"
_SUPPORTED_GENERATORS = [
    "AzureOpenAIGenerator",
    "OpenAIGenerator",
    "AnthropicGenerator",
    "HuggingFaceAPIGenerator",
    "HuggingFaceLocalGenerator",
    "CohereGenerator",
    "OllamaGenerator",
]
_SUPPORTED_CHAT_GENERATORS = [
    "AzureOpenAIChatGenerator",
    "OpenAIChatGenerator",
    "AnthropicChatGenerator",
    "HuggingFaceAPIChatGenerator",
    "HuggingFaceLocalChatGenerator",
    "CohereChatGenerator",
    "OllamaChatGenerator",
    "GoogleGenAIChatGenerator",
]
_ALL_SUPPORTED_GENERATORS = _SUPPORTED_GENERATORS + _SUPPORTED_CHAT_GENERATORS

# These are the keys used by Haystack for traces and span.
# We keep them here to avoid making typos when using them.
_PIPELINE_INPUT_KEY = "haystack.pipeline.input_data"
_PIPELINE_OUTPUT_KEY = "haystack.pipeline.output_data"
_ASYNC_PIPELINE_RUN_KEY = "haystack.async_pipeline.run"
_PIPELINE_RUN_KEY = "haystack.pipeline.run"
_COMPONENT_NAME_KEY = "haystack.component.name"
_COMPONENT_TYPE_KEY = "haystack.component.type"
_COMPONENT_OUTPUT_KEY = "haystack.component.output"
_COMPONENT_INPUT_KEY = "haystack.component.input"

# Context var used to keep track of tracing related info.
# This mainly useful for parents spans.
tracing_context_var: ContextVar[Dict[Any, Any]] = ContextVar("tracing_context")


class LangfuseSpan(Span):
    """
    Internal class representing a bridge between the Haystack span tracing API and Langfuse.
    """

    def __init__(self, span: LangfuseStatefulClient) -> None:
        """
        Initialize a LangfuseSpan instance.

        :param span: The span instance managed by Langfuse.
        """
        self._span = span
        # locally cache tags
        self._data: Dict[str, Any] = {}

    def set_tag(self, key: str, value: Any) -> None:
        """
        Set a generic tag for this span.

        :param key: The tag key.
        :param value: The tag value.
        """
        coerced_value = tracing_utils.coerce_tag_value(value)
        self._span.update(metadata={key: coerced_value})
        self._data[key] = value

    def set_content_tag(self, key: str, value: Any) -> None:
        """
        Set a content-specific tag for this span.

        :param key: The content tag key.
        :param value: The content tag value.
        """
        if not proxy_tracer.is_content_tracing_enabled:
            return
        if key.endswith(".input"):
            if "messages" in value:
                messages = [m.to_openai_dict_format(require_tool_call_ids=False) for m in value["messages"]]
                self._span.update(input=messages)
            else:
                coerced_value = tracing_utils.coerce_tag_value(value)
                self._span.update(input=coerced_value)
        elif key.endswith(".output"):
            if "replies" in value:
                if all(isinstance(r, ChatMessage) for r in value["replies"]):
                    replies = [m.to_openai_dict_format(require_tool_call_ids=False) for m in value["replies"]]
                else:
                    replies = value["replies"]
                self._span.update(output=replies)
            else:
                coerced_value = tracing_utils.coerce_tag_value(value)
                self._span.update(output=coerced_value)

        self._data[key] = value

    def raw_span(self) -> LangfuseStatefulClient:
        """
        Return the underlying span instance.

        :return: The Langfuse span instance.
        """
        return self._span

    def get_data(self) -> Dict[str, Any]:
        """
        Return the data associated with the span.

        :return: The data associated with the span.
        """
        return self._data

    def get_correlation_data_for_logs(self) -> Dict[str, Any]:
        return {}


@dataclass(frozen=True)
class SpanContext:
    """
    Context for creating spans in Langfuse.

    Encapsulates the information needed to create and configure a span in Langfuse tracing.
    Used by SpanHandler to determine the span type (trace, generation, or default) and its configuration.

    :param name: The name of the span to create. For components, this is typically the component name.
    :param operation_name: The operation being traced (e.g. "haystack.pipeline.run"). Used to determine
        if a new trace should be created without warning.
    :param component_type: The type of component creating the span (e.g. "OpenAIChatGenerator").
        Can be used to determine the type of span to create.
    :param tags: Additional metadata to attach to the span. Contains component input/output data
        and other trace information.
    :param parent_span: The parent span if this is a child span. If None, a new trace will be created.
    :param trace_name: The name to use for the trace when creating a parent span. Defaults to "Haystack".
    :param public: Whether traces should be publicly accessible. Defaults to False.
    """

    name: str
    operation_name: str
    component_type: Optional[str]
    tags: Dict[str, Any]
    parent_span: Optional[Span]
    trace_name: str = "Haystack"
    public: bool = False

    def __post_init__(self) -> None:
        """
        Validate the span context attributes.

        :raises ValueError: If name, operation_name or trace_name are empty
        :raises TypeError: If tags is not a dictionary
        """
        if not self.name:
            msg = "Span name cannot be empty"
            raise ValueError(msg)
        if not self.operation_name:
            msg = "Operation name cannot be empty"
            raise ValueError(msg)
        if not self.trace_name:
            msg = "Trace name cannot be empty"
            raise ValueError(msg)


class SpanHandler(ABC):
    """
    Abstract base class for customizing how Langfuse spans are created and processed.

    This class defines two key extension points:
    1. create_span: Controls what type of span to create (default or generation)
    2. handle: Processes the span after component execution (adding metadata, metrics, etc.)

    To implement a custom handler:
    - Extend this class or DefaultSpanHandler
    - Override create_span and handle methods. It is more common to override handle.
    - Pass your handler to LangfuseConnector init method
    """

    def __init__(self) -> None:
        self.tracer: Optional[langfuse.Langfuse] = None

    def init_tracer(self, tracer: langfuse.Langfuse) -> None:
        """
        Initialize with Langfuse tracer. Called internally by LangfuseTracer.

        :param tracer: The Langfuse client instance to use for creating spans
        """
        self.tracer = tracer

    @abstractmethod
    def create_span(self, context: SpanContext) -> LangfuseSpan:
        """
        Create a span of appropriate type based on the context.

        This method determines what kind of span to create:
        - A new trace if there's no parent span
        - A generation span for LLM components
        - A default span for other components

        :param context: The context containing all information needed to create the span
        :returns: A new LangfuseSpan instance configured according to the context
        """
        pass

    @abstractmethod
    def handle(self, span: LangfuseSpan, component_type: Optional[str]) -> None:
        """
        Process a span after component execution by attaching metadata and metrics.

        This method is called after the component or pipeline yields its span, allowing you to:
        - Extract and attach token usage statistics
        - Add model information
        - Record timing data (e.g., time-to-first-token)
        - Set log levels for quality monitoring
        - Add custom metrics and observations

        :param span: The span that was yielded by the component
        :param component_type: The type of component that created the span, used to determine
            what metadata to extract and how to process it
        """
        pass

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpanHandler":
        return default_from_dict(cls, data)

    def to_dict(self) -> Dict[str, Any]:
        return default_to_dict(self)


class DefaultSpanHandler(SpanHandler):
    """DefaultSpanHandler provides the default Langfuse tracing behavior for Haystack."""

    def create_span(self, context: SpanContext) -> LangfuseSpan:
        if self.tracer is None:
            message = (
                "Tracer is not initialized. "
                "Make sure the environment variable HAYSTACK_CONTENT_TRACING_ENABLED is set to true before "
                "importing Haystack."
            )
            raise RuntimeError(message)

        tracing_ctx = tracing_context_var.get({})
        if not context.parent_span:
            # Create a new trace when there's no parent span
            return LangfuseSpan(
                self.tracer.trace(
                    name=context.trace_name,
                    public=context.public,
                    id=tracing_ctx.get("trace_id"),
                    user_id=tracing_ctx.get("user_id"),
                    session_id=tracing_ctx.get("session_id"),
                    tags=tracing_ctx.get("tags"),
                    version=tracing_ctx.get("version"),
                )
            )
        elif context.component_type in _ALL_SUPPORTED_GENERATORS:
            return LangfuseSpan(context.parent_span.raw_span().generation(name=context.name))
        else:
            return LangfuseSpan(context.parent_span.raw_span().span(name=context.name))

    def handle(self, span: LangfuseSpan, component_type: Optional[str]) -> None:
        # If the span is at the pipeline level, we add input and output keys to the span
        at_pipeline_level = span.get_data().get(_PIPELINE_INPUT_KEY) is not None
        if at_pipeline_level:
            coerced_input = tracing_utils.coerce_tag_value(span.get_data().get(_PIPELINE_INPUT_KEY))
            coerced_output = tracing_utils.coerce_tag_value(span.get_data().get(_PIPELINE_OUTPUT_KEY))
            span.raw_span().update(input=coerced_input, output=coerced_output)

        # special case for ToolInvoker (to update the span name to be: `original_component_name - [tool_names]`)
        if component_type == "ToolInvoker":
            tool_names: List[str] = []
            messages = span.get_data().get(_COMPONENT_INPUT_KEY, {}).get("messages", [])
            for message in messages:
                if isinstance(message, ChatMessage) and message.tool_calls:
                    tool_names.extend(call.tool_name for call in message.tool_calls)

            if tool_names:
                # Fallback to "ToolInvoker" if we can't retrieve component name
                tool_invoker_name = span.get_data().get(_COMPONENT_NAME_KEY, "ToolInvoker")
                tool_counts = Counter(tool_names)  # how many times each tool was called
                formatted_names = [f"{name} (x{count})" if count > 1 else name for name, count in tool_counts.items()]
                span.raw_span().update(name=f"{tool_invoker_name} - {sorted(formatted_names)}")

        if component_type in _SUPPORTED_GENERATORS:
            meta = span.get_data().get(_COMPONENT_OUTPUT_KEY, {}).get("meta")
            if meta:
                span.raw_span().update(usage=meta[0].get("usage") or None, model=meta[0].get("model"))

        if component_type in _SUPPORTED_CHAT_GENERATORS:
            replies = span.get_data().get(_COMPONENT_OUTPUT_KEY, {}).get("replies")
            if replies:
                meta = replies[0].meta
                completion_start_time = meta.get("completion_start_time")
                if completion_start_time:
                    try:
                        completion_start_time = datetime.fromisoformat(completion_start_time)
                    except ValueError:
                        logger.error(f"Failed to parse completion_start_time: {completion_start_time}")
                        completion_start_time = None
                span.raw_span().update(
                    usage=meta.get("usage") or None,
                    model=meta.get("model"),
                    completion_start_time=completion_start_time,
                )


class LangfuseTracer(Tracer):
    """
    Internal class representing a bridge between the Haystack tracer and Langfuse.
    """

    def __init__(
        self,
        tracer: langfuse.Langfuse,
        name: str = "Haystack",
        public: bool = False,
        span_handler: Optional[SpanHandler] = None,
    ) -> None:
        """
        Initialize a LangfuseTracer instance.

        :param tracer: The Langfuse tracer instance.
        :param name: The name of the pipeline or component. This name will be used to identify the tracing run on the
            Langfuse dashboard.
        :param public: Whether the tracing data should be public or private. If set to `True`, the tracing data will
            be publicly accessible to anyone with the tracing URL. If set to `False`, the tracing data will be private
            and only accessible to the Langfuse account owner.
        :param span_handler: Custom handler for processing spans. If None, uses DefaultSpanHandler.
        """
        if not proxy_tracer.is_content_tracing_enabled:
            logger.warning(
                "Traces will not be logged to Langfuse because Haystack tracing is disabled. "
                "To enable, set the HAYSTACK_CONTENT_TRACING_ENABLED environment variable to true "
                "before importing Haystack."
            )
        self._tracer = tracer
        self._context: List[LangfuseSpan] = []
        self._name = name
        self._public = public
        self.enforce_flush = os.getenv(HAYSTACK_LANGFUSE_ENFORCE_FLUSH_ENV_VAR, "true").lower() == "true"
        self._span_handler = span_handler or DefaultSpanHandler()
        self._span_handler.init_tracer(tracer)

    @contextlib.contextmanager
    def trace(
        self, operation_name: str, tags: Optional[Dict[str, Any]] = None, parent_span: Optional[Span] = None
    ) -> Iterator[Span]:
        tags = tags or {}
        span_name = tags.get(_COMPONENT_NAME_KEY, operation_name)
        component_type = tags.get(_COMPONENT_TYPE_KEY)

        # Create a new span context
        span_context = SpanContext(
            name=span_name,
            operation_name=operation_name,
            component_type=component_type,
            tags=tags,
            # We use the current active span as the parent span if not provided to handle nested pipelines
            # The nested pipeline (or sub-pipeline) will be a child of the current active span
            parent_span=parent_span or self.current_span(),
            trace_name=self._name,
            public=self._public,
        )

        # Create span using the handler
        span = self._span_handler.create_span(span_context)

        self._context.append(span)
        span.set_tags(tags)

        try:
            yield span
        finally:
            # Always clean up context, even if nested operations fail
            try:
                # Process span data (may fail with nested pipeline exceptions)
                self._span_handler.handle(span, component_type)

                # End span (may fail if span data is corrupted)
                raw_span = span.raw_span()
                if isinstance(raw_span, (StatefulSpanClient, StatefulGenerationClient)):
                    raw_span.end()
            except Exception as cleanup_error:
                # Log cleanup errors but don't let them corrupt context
                logger.warning(
                    "Error during span cleanup for {operation_name}: {cleanup_error}",
                    operation_name=operation_name,
                    cleanup_error=cleanup_error,
                )
            finally:
                # CRITICAL: Always pop context to prevent corruption
                # This is especially important for nested pipeline scenarios
                if self._context and self._context[-1] == span:
                    self._context.pop()

            if self.enforce_flush:
                self.flush()

    def flush(self) -> None:
        self._tracer.flush()

    def current_span(self) -> Optional[Span]:
        """
        Return the current active span.

        :return: The current span if available, else None.
        """
        return self._context[-1] if self._context else None

    def get_trace_url(self) -> str:
        """
        Return the URL to the tracing data.
        :return: The URL to the tracing data.
        """
        return self._tracer.get_trace_url()

    def get_trace_id(self) -> str:
        """
        Return the trace ID.
        :return: The trace ID.
        """
        return self._tracer.get_trace_id()
