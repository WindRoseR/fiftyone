"""
FiftyOne operator execution.

| Copyright 2017-2023, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import asyncio
import traceback
import types as python_types

import fiftyone as fo
import fiftyone.core.dataset as fod
import fiftyone.core.utils as fou
import fiftyone.core.view as fov
import fiftyone.server.view as fosv
import fiftyone.operators.types as types
from fiftyone.plugins.secrets import PluginSecretsResolver

from .decorators import coroutine_timeout
from .registry import OperatorRegistry
from .message import GeneratedMessage, MessageType


class ExecutionRunState(object):
    """Enumeration of the available operator run states."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class InvocationRequest(object):
    """Represents a request to invoke an operator.

    Args:
        operator_uri: the URI of the operator to invoke
        params (None): an optional dictionary of parameters
    """

    def __init__(self, operator_uri, params=None):
        self.operator_uri = operator_uri
        self.params = params or {}

    def to_json(self):
        return {
            "operator_uri": self.operator_uri,
            "params": self.params,
        }


class Executor(object):
    """Handles the execution phase of the operator lifecycle.

    Args:
        requests (None): an optional list of InvocationRequest objects
        logs (None): an optional list of log messages
    """

    def __init__(self, requests=None, logs=None):
        self._requests = requests or []
        self._logs = logs or []

    def trigger(self, operator_name, params=None):
        """Triggers an invocation of the operator with the given name.

        Args:
            operator_name: the name of the operator
            params (None): a dictionary of parameters for the operator

        Returns:
            a :class:`fiftyone.operators.message.GeneratedMessage` containing
            the result of the invocation
        """
        inv_req = InvocationRequest(operator_name, params=params)
        self._requests.append(inv_req)
        return GeneratedMessage(
            MessageType.SUCCESS, cls=InvocationRequest, body=inv_req
        )

    def log(self, message):
        """Logs a message."""
        self._logs.append(message)

    def to_json(self):
        return {
            "requests": [t.to_json() for t in self._requests],
            "logs": self._logs,
        }


def execute_operator(operator_uri, ctx, params):
    """Executes the operator with the given name.

    Args:
        operator_uri: the URI of the operator
        ctx: a dictionary of parameters defining the execution context. The
            supported keys are:

            -   ``dataset``: a :class:`fiftyone.core.dataset.Dataset` or the
                name of a dataset to process. This is required unless a
                ``view`` is provided
            -   ``view``: an optional :class:`fiftyone.core.view.DatasetView`
                to process
            -   ``selected``: an optional list of selected sample IDs
            -   ``selected_labels``: an optional list of selected labels in the
                format returned by
                :attr:`fiftyone.core.session.Session.selected_labels`

        params: a dictionary of parameters for the operator. Consult the
            operator's documentation for details

    Returns:
        an :class:`ExecutionResult`
    """
    dataset_name, view_stages, selected, selected_labels = _parse_ctx(ctx)

    request_params = dict(
        operator_uri=operator_uri,
        dataset_name=dataset_name,
        view=view_stages,
        selected=selected,
        selected_labels=selected_labels,
        params=params,
    )

    return asyncio.run(
        execute_or_delegate_operator(operator_uri, request_params)
    )


def _parse_ctx(ctx):
    dataset = ctx.get("dataset", None)
    view = ctx.get("view", None)
    selected = ctx.get("selected", None)
    selected_labels = ctx.get("selected_labels", None)

    if dataset is None and isinstance(view, fov.DatasetView):
        dataset = view._root_dataset

    if view is None:
        if isinstance(dataset, str):
            dataset = fod.load_dataset(dataset)

        view = dataset.view()

    view_stages = view._serialize()

    if isinstance(dataset, fod.Dataset):
        dataset_name = dataset.name
    else:
        dataset_name = dataset

    return dataset_name, view_stages, selected, selected_labels


@coroutine_timeout(seconds=fo.config.operator_timeout)
async def execute_or_delegate_operator(operator_uri, request_params):
    """Executes the operator with the given name.

    Args:
        operator_uri: the URI of the operator
        request_params: a dictionary of parameters for the operator

    Returns:
        an :class:`ExecutionResult`
    """
    prepared = await prepare_operator_executor(operator_uri, request_params)
    if isinstance(prepared, ExecutionResult):
        raise prepared.to_exception()
    else:
        operator, executor, ctx = prepared

    if operator.resolve_delegation(ctx):
        try:
            from .delegated import DelegatedOperationService

            op = DelegatedOperationService().queue_operation(
                operator=operator.uri,
                context=ctx.serialize(),
                delegation_target=operator.delegation_target,
            )

            execution = ExecutionResult(
                op.__dict__, executor, None, delegated=True
            )
            execution.result["context"] = (
                execution.result["context"].serialize()
                if execution.result["context"]
                else None
            )
            return execution
        except Exception as e:
            return ExecutionResult(
                executor=executor, error=traceback.format_exc()
            )
    else:
        try:
            raw_result = await (
                operator.execute(ctx)
                if asyncio.iscoroutinefunction(operator.execute)
                else fou.run_sync_task(operator.execute, ctx)
            )

        except Exception as e:
            return ExecutionResult(
                executor=executor, error=traceback.format_exc()
            )

        return ExecutionResult(result=raw_result, executor=executor)


async def prepare_operator_executor(operator_uri, request_params):
    registry = OperatorRegistry()
    if registry.operator_exists(operator_uri) is False:
        raise ValueError("Operator '%s' does not exist" % operator_uri)

    operator = registry.get_operator(operator_uri)
    executor = Executor()
    ctx = ExecutionContext(request_params, executor)
    await ctx.resolve_secret_values(operator._plugin_secrets)
    inputs = operator.resolve_input(ctx)
    validation_ctx = ValidationContext(ctx, inputs, operator)
    if validation_ctx.invalid:
        return ExecutionResult(
            error="Validation error", validation_ctx=validation_ctx
        )

    return operator, executor, ctx


def _is_generator(value):
    return isinstance(value, python_types.GeneratorType) or isinstance(
        value, python_types.AsyncGeneratorType
    )


def resolve_type(registry, operator_uri, request_params):
    """Resolves the inputs property type of the operator with the given name.

    Args:
        registry: an :class:`fiftyone.operators.registry.OperatorRegistry`
        operator_uri: the URI of the operator
        request_params: a dictionary of request parameters

    Returns:
        the type of the inputs :class:`fiftyone.operators.types.Property` of
        the operator, or None
    """
    if registry.operator_exists(operator_uri) is False:
        raise ValueError("Operator '%s' does not exist" % operator_uri)

    operator = registry.get_operator(operator_uri)
    ctx = ExecutionContext(request_params)
    try:
        return operator.resolve_type(
            ctx, request_params.get("target", "inputs")
        )
    except Exception as e:
        return ExecutionResult(error=traceback.format_exc())


def resolve_placement(operator, request_params):
    """Resolves the placement of the operator with the given name.

    Args:
        operator: the :class:`fiftyone.operators.operator.Operator`
        request_params: a dictionary of request parameters

    Returns:
        the placement of the operator or ``None``
    """
    ctx = ExecutionContext(request_params)
    try:
        return operator.resolve_placement(ctx)
    except Exception as e:
        return ExecutionResult(error=str(e))


class ExecutionContext(object):
    """Represents the execution context of an operator.

    Operators can use the execution context to access the view, dataset, and
    selected samples, as well as to trigger other operators.

    Args:
        request_params (None): a optional dictionary of request parameters
        executor (None): an optional :class:`Executor` instance
    """

    def __init__(self, request_params=None, executor=None):
        self.request_params = request_params or {}
        self.params = self.request_params.get("params", {})
        self.executor = executor
        self._secrets = {}
        self._secrets_client = PluginSecretsResolver()

    @property
    def results(self):
        """A ``dict`` of results for the current operation. This is only availble
        for methods that are invoked after an operator is executed, e.g. :meth:`resolve_output`."""
        return self.request_params.get("results", {})

    @property
    def delegated(self):
        """``True`` if the operator's execution was delegated to an orchestrator. This is only availble
        for methods that are invoked after an operator is executed, e.g. :meth:`resolve_output`."""
        return self.request_params.get("delegated", False)

    @property
    def view(self):
        """The :class:`fiftyone.core.view.DatasetView` to operate on.

        This property is only available when the operator is invoked via the
        FiftyOne App and the user has defined a view.
        """
        stages = self.request_params.get("view", None)
        extended = self.request_params.get("extended", None)
        dataset_name = self.request_params.get("dataset_name", None)
        filters = self.request_params.get("filters", None)
        return fosv.get_view(
            dataset_name,
            stages=stages,
            extended_stages=extended,
            filters=filters,
        )

    @property
    def selected(self):
        """The list of selected sample IDs or an empty list."""
        return self.request_params.get("selected", [])

    @property
    def selected_labels(self):
        """A list of labels currently selected in the App.
        Items are dictionaries with the following keys:
        -   ``label_id``: the ID of the label
        -   ``sample_id``: the ID of the sample containing the label
        -   ``field``: the field name containing the label
        -   ``frame_number``: the frame number containing the label (only
            applicable to video samples)
        """
        return self.request_params.get("selected_labels", [])

    @property
    def dataset(self):
        """The :class:`fiftyone.core.dataset.Dataset` to operate on."""
        dataset_name = self.request_params.get("dataset_name", None)
        d = fod.load_dataset(dataset_name)
        return d

    @property
    def dataset_name(self):
        """The name of the :class:`fiftyone.core.dataset.Dataset` to operate
        on.
        """
        return self.request_params.get("dataset_name", None)

    @property
    def dataset_id(self):
        """The name of the :class:`fiftyone.core.dataset.Dataset` to operate
        on.
        """
        return self.request_params.get("dataset_id", None)

    def trigger(self, operator_name, params=None):
        """Triggers an invocation of the operator with the given name.

        . note::

            This method is only available when the operator is invoked via the
            FiftyOne App. You can check this via ``ctx.executor``.

        Args:
            operator_name: the name of the operator
            params (None): a dictionary of parameters for the operator
        """
        if self.executor is None:
            raise ValueError("No executor available")

        return self.executor.trigger(operator_name, params)

    def log(self, message):
        """Logs a message to the browser console.

        Args:
            message: a message to log
        """
        self.trigger("console_log", {"message": message})

    def secret(self, key):
        """Returns the secret with the given key from the context."""
        return self._secrets.get(key, None)

    @property
    def secrets(self) -> dict:
        """
        Returns all the secrets in the context.
        """
        return self._secrets

    async def resolve_secret_values(self, keys, **kwargs):
        """
        Resolves the values of the given secrets keys.

        Args:
            keys: a list of secret keys
            kwargs: additional keyword arguments to pass to the secrets
            client for authentication if required
        """
        if None in (self._secrets_client, keys):
            return None
        for key in keys:
            secret = await self._secrets_client.get_secret(key, **kwargs)

            if secret:
                self._secrets[secret.key] = secret.value

    def serialize(self):
        """Serializes the execution context.

        Returns:
            a JSON dict
        """
        return {
            "request_params": self.request_params,
            "params": self.params,
        }

    def to_dict(self):
        """Returns the properties of the execution context as a dict."""
        return {
            k: v for k, v in self.__dict__.items() if not k.startswith("_")
        }


class ExecutionResult(object):
    """Represents the result of an operator execution.

    Args:
        result (None): the execution result
        executor (None): an :class:`Executor`
        error (None): an error message
        validation_ctx (None): a :class:`ValidationContext`
    """

    def __init__(
        self,
        result=None,
        executor=None,
        error=None,
        validation_ctx=None,
        delegated=False,
    ):
        self.result = result
        self.executor = executor
        self.error = error
        self.validation_ctx = validation_ctx
        self.delegated = delegated

    @property
    def is_generator(self):
        """Whether the result is a generator or an async generator."""
        return _is_generator(self.result)

    def to_exception(self):
        """Returns an :class:`ExecutionError` representing a failed execution
        result.

        Returns:
            a :class:`ExecutionError`
        """
        msg = self.error

        if self.validation_ctx.invalid:
            val_error = self.validation_ctx.errors[0]
            path = val_error.path.lstrip(".")
            reason = val_error.reason
            msg += f". Path: {path}. Reason: {reason}"

        return ExecutionError(msg)

    def to_json(self):
        """Returns a JSON dict representation of the result.

        Returns:
            a JSON dict
        """
        return {
            "result": self.result,
            "executor": self.executor.to_json() if self.executor else None,
            "error": self.error,
            "delegated": self.delegated,
            "validation_ctx": self.validation_ctx.to_json()
            if self.validation_ctx
            else None,
        }


class ExecutionError(Exception):
    """An error that occurs while executing an operator."""


class ValidationError(object):
    """A validation error.

    Args:
        reason: the reason
        property: the property
        path: the path
    """

    def __init__(self, reason, property, path, custom=False):
        self.reason = reason
        self.error_message = property.error_message
        self.path = path
        self.custom = custom

    def to_json(self):
        """Returns a JSON dict representation of the error.

        Returns:
            a JSON dict
        """
        return {
            "reason": self.reason,
            "error_message": self.error_message,
            "path": self.path,
            "custom": self.custom,
        }


class ValidationContext(object):
    """Represents the validation context of an operator.

    Args:
        ctx: the :class:`ExecutionContext`
        inputs_property: the :class:`fiftyone.operators.types.Property` of the
            operator inputs
    """

    def __init__(self, ctx, inputs_property, operator):
        self.ctx = ctx
        self.params = ctx.params
        self.inputs_property = inputs_property
        self.errors = []
        self.disable_schema_validation = (
            operator.config.disable_schema_validation
        )
        if self.inputs_property is None:
            self.invalid = False
        else:
            self.errors = self._validate()
            self.invalid = len(self.errors) > 0

    def to_json(self):
        """Returns a JSON dict representation of the context.

        Returns:
            a JSON dict
        """
        return {
            "invalid": self.invalid,
            "errors": [e.to_json() for e in self.errors],
        }

    def add_error(self, error):
        """Adds a validation error.

        Args:
            error: a :class:`ValidationError`
        """
        if self.disable_schema_validation and error.custom != True:
            return
        self.errors.append(error)

    def _validate(self):
        params = self.params
        validation_error = self.validate_property(
            "", self.inputs_property, params
        )
        if validation_error:
            self.add_error(validation_error)

        return self.errors

    def validate_enum(self, path, property, value):
        """Validates an enum value.

        Args:
            path: the path to the property
            property: the :class:`fiftyone.operators.types.Property`
            value: the value to validate

        Returns:
            a :class:`ValidationError`, if the value is invalid
        """
        enum = property.type
        if value not in enum.values:
            return ValidationError("Invalid enum value", property, path)

    def validate_list(self, path, property, value):
        """Validates a list value.

        Args:
            path: the path to the property
            property: the :class:`fiftyone.operators.types.Property`
            value: the value to validate

        Returns:
            a :class:`ValidationError`, if the value is invalid
        """
        if not isinstance(value, list):
            return ValidationError("Invalid list", property, path)

        element_type = property.type.element_type

        for i in range(len(value)):
            item = value[i]
            item_path = f"{path}[{i}]"
            item_property = types.Property(element_type)
            validation_error = self.validate_property(
                item_path, item_property, item
            )
            if validation_error is not None:
                self.add_error(validation_error)

    def validate_property(self, path, property, value):
        """Validates a property value.

        Args:
            path: the path to the property
            property: the :class:`fiftyone.operators.types.Property`
            value: the value to validate

        Returns:
            a :class:`ValidationError`, if the value is invalid
        """
        if property.invalid:
            return ValidationError(
                property.error_message, property, path, True
            )

        if property.required and value is None:
            return ValidationError("Required property", property, path)

        if value is not None:
            if isinstance(property.type, types.Enum):
                return self.validate_enum(path, property, value)

            if isinstance(property.type, types.Object):
                return self.validate_object(path, property, value)

            if isinstance(property.type, types.List):
                return self.validate_list(path, property, value)

            return self.validate_primitive(path, property, value)

    def validate_object(self, path, property, value):
        """Validates an object value.

        Args:
            path: the path to the property
            property: the :class:`fiftyone.operators.types.Property`
            value: the value to validate

        Returns:
            a :class:`ValidationError`, if the value is invalid
        """
        propertyType = property.type
        if value is None:
            return ValidationError("Invalid object", property, path)

        for name, property in propertyType.properties.items():
            propertyValue = value.get(name, None)
            validation_error = self.validate_property(
                path + "." + name, property, propertyValue
            )
            if validation_error is not None:
                self.add_error(validation_error)

    def validate_primitive(self, path, property, value):
        """Validates a primitive value.

        Args:
            path: the path to the property
            property: the :class:`fiftyone.operators.types.Property`
            value: the value to validate

        Returns:
            a :class:`ValidationError`, if the value is invalid
        """
        type_name = property.type.__class__.__name__
        value_type = type(value)
        if type_name == "String" and value_type != str:
            return ValidationError("Invalid value type", property, path)

        if type_name == "Number" and (
            value_type != int and value_type != float
        ):
            return ValidationError("Invalid value type", property, path)

        if type_name == "Boolean" and value_type != bool:
            return ValidationError("Invalid value type", property, path)
