"""This module contains custom field classes."""

import importlib

import orjson
from django.core.exceptions import ObjectDoesNotExist
from django.utils.functional import cached_property
from rest_framework import fields
from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.serializers import SerializerMethodField

from dynamic_rest.bases import (
    CacheableFieldMixin,
    DynamicSerializerBase,
    GetModelMixin,
    resettable_cached_property,
)
from dynamic_rest.conf import settings
from dynamic_rest.fields.common import WithRelationalFieldMixin
from dynamic_rest.meta import get_model_field, is_field_remote
from dynamic_rest.utils import (
    external_id_from_model_and_internal_id,
    internal_id_from_model_and_external_id,
)


class DynamicField(CacheableFieldMixin, fields.Field):
    """Generic field base to capture additional custom field attributes."""

    def __init__(
        self, requires=None, deferred=None, field_type=None, immutable=False, **kwargs
    ):
        """
        Initialize dynamic field.

        Arguments:
            requires: List of fields that this field depends on.
            deferred: Whether this field is deferred.
                Deferred fields are not included in the response,
                unless explicitly requested.
            field_type: Field data type, if not inferrable from model.
            immutable: Whether this field is immutable.
                Processed by the view layer during queryset build time.
        """
        self.requires = requires
        self.deferred = deferred
        self.field_type = field_type
        self.immutable = immutable
        self.kwargs = kwargs
        super().__init__(**kwargs)

    def to_representation(self, value):
        """Return the serialized value."""
        return value

    def to_internal_value(self, data):
        """Return the deserialized value."""
        return data


class DynamicComputedField(DynamicField):
    """Field that is computed from other fields."""

    pass


class DynamicMethodField(SerializerMethodField, DynamicField):
    """Field that is computed from a serializer method."""

    def reset(self):
        """Reset the field to its initial state."""
        super().reset()
        if self.method_name == f"get_{self.field_name}":
            self.method_name = None


class DynamicRelationField(WithRelationalFieldMixin, DynamicField):
    """Field proxy for a nested serializer.

    Supports passing in the child serializer as a class or string,
    and resolves to the class after binding to the parent serializer.

    Will proxy certain arguments to the child serializer.

    Attributes:
        SERIALIZER_KWARGS: list of arguments that are passed
            to the child serializer.
    """

    SERIALIZER_KWARGS = {"many", "source"}

    def __init__(
        self,
        serializer_class,
        many=False,
        queryset=None,
        embed=False,
        sideloading=None,
        debug=False,
        **kwargs,
    ):
        """
        Initialize the field.

        Arguments:
            serializer_class: Serializer class (or string representation)
                to proxy.
            many: Boolean, if relation is to-many.
            queryset: Default queryset to apply when filtering for related
                objects.
            embed: If True, always embed related object(s). Will not sideload,
                and will include the full object unless specifically excluded.
            sideloading: if True, force sideloading all the way down.
                if False, force embedding all the way down.
                This overrides the "embed" option if set.
            debug: If True, include debug information in the response.
        """
        self._serializer_class = serializer_class
        self.bound = False
        self.queryset = queryset
        self.sideloading = sideloading
        self.debug = debug
        self.embed = embed if sideloading is None else not sideloading
        if "." in kwargs.get("source", ""):
            raise RuntimeError("Nested relationships are not supported")
        if "link" in kwargs:
            self.link = kwargs.pop("link")
        super().__init__(**kwargs)
        self.kwargs["many"] = self.many = many

    def get_model(self):
        """Get the child serializer's model."""
        return getattr(self.serializer_class.Meta, "model", None)

    def bind(self, *args, **kwargs):
        """Bind to the parent serializer."""
        if self.bound:  # Prevent double-binding
            return
        super().bind(*args, **kwargs)
        self.bound = True
        parent_model = getattr(self.parent.Meta, "model", None)

        remote = is_field_remote(parent_model, self.source)

        model_field = get_model_field(parent_model, self.source)

        field_null = getattr(model_field, "null", False)

        # Infer `required` and `allow_null`
        if "required" not in self.kwargs and (
            remote or (model_field and (model_field.has_default() or field_null))
        ):
            self.required = False
        if "allow_null" not in self.kwargs and field_null:
            self.allow_null = True

        self.model_field = model_field

    @resettable_cached_property
    def root_serializer(self):
        """Return the root serializer (serializer for the primary resource)."""
        if not self.parent:
            # Don't cache, so that we'd recompute if parent is set.
            return None

        node = self
        seen = set()
        while True:
            seen.add(node)
            if parent_node := getattr(node, "parent", None):
                node = parent_node
                if node in seen:
                    return None
            else:
                return node

    def _get_cached_serializer(self, args, init_args):
        """Get a cached instance of the child serializer."""
        enabled = settings.ENABLE_SERIALIZER_CACHE

        root = self.root_serializer
        field_name = self.field_name
        if not root or not field_name or not enabled:
            # Not enough info to use cache.
            return self.serializer_class(*args, **init_args)

        if not hasattr(root, "_descendant_serializer_cache"):
            # Initialize dict to use as cache on root serializer.
            # Arguably this is a Serializer concern, but we'll do it
            # here, so it's agnostic to the exact type of the root
            # serializer (i.e. it could be a DRF serializer).
            root._descendant_serializer_cache = {}  # pylint: disable=protected-access
        cache = root._descendant_serializer_cache  # pylint: disable=protected-access
        key_dict = {
            "parent": self.parent.__class__.__name__,
            "field": field_name,
            "args": args,
            "init_args": init_args,
        }

        cache_key = hash(orjson.dumps(key_dict))

        if cache_key not in cache:
            szr = self.serializer_class(*args, **init_args)
            cache[cache_key] = szr
        else:
            cache[cache_key].reset()

        return cache[cache_key]

    def _inherit_parent_kwargs(self, kwargs):
        """Inherit kwargs from parent serializer.

        Extract any necessary attributes from parent serializer to
        propagate down to child serializer.
        """
        parent = self.parent
        if not parent or not self._is_dynamic:
            return kwargs

        if "request_fields" not in kwargs:
            # If 'request_fields' isn't explicitly set, pull it from the
            # parent serializer.
            request_fields = self._get_request_fields_from_parent()
            if request_fields is None:
                # Default to 'id_only' for nested serializers.
                request_fields = True
            kwargs["request_fields"] = request_fields

        if self.embed and kwargs.get("request_fields") is True:
            # If 'embed' then make sure we fetch the full object.
            kwargs["request_fields"] = {}

        if hasattr(parent, "sideloading"):
            kwargs["sideloading"] = parent.sideloading

        if hasattr(parent, "debug"):
            kwargs["debug"] = parent.debug

        return kwargs

    def get_serializer(self, *args, **kwargs):
        """Get an instance of the child serializer."""
        init_args = {
            k: v for k, v in self.kwargs.items() if k in self.SERIALIZER_KWARGS
        }

        kwargs = self._inherit_parent_kwargs(kwargs)
        init_args.update(kwargs)

        if self.embed and self._is_dynamic:
            init_args["embed"] = True

        serializer = self._get_cached_serializer(args, init_args)
        serializer.parent = self
        return serializer

    @resettable_cached_property
    def serializer(self):
        """Get serializer."""
        return self.get_serializer()

    @cached_property
    def _is_dynamic(self):
        """Return True if the child serializer is dynamic."""
        return issubclass(self.serializer_class, DynamicSerializerBase)

    def get_attribute(self, instance):
        """Get the related object(s)."""
        serializer = self.serializer
        model = serializer.get_model()

        # attempt to optimize by reading the related ID directly
        # from the current instance rather than from the related object
        if not self.kwargs["many"] and serializer.id_only():
            return instance
        elif model is not None:
            try:
                return getattr(instance, self.source)
            except model.DoesNotExist:
                return None
        else:
            return instance

    def to_representation(self, instance):  # pylint: disable=arguments-renamed
        """Represent the relationship, either as an ID or object."""
        serializer = self.serializer
        model = serializer.get_model()
        source = self.source

        if not self.kwargs["many"] and serializer.id_only():
            # attempt to optimize by reading the related ID directly
            # from the current instance rather than from the related object
            source_id = f"{source}_id"
            # try the faster way first:
            if hasattr(instance, source_id):
                return getattr(instance, source_id)
            elif model is not None:
                # this is probably a one-to-one field, or a reverse related
                # lookup, so let's look it up the slow way and let the
                # serializer handle the id de-referencing
                try:
                    instance = getattr(instance, source)
                except model.DoesNotExist:
                    instance = None

        # dereference ephemeral objects
        if model is None:
            instance = getattr(instance, source)

        if instance is None:
            return None

        return serializer.to_representation(instance)

    def to_internal_value_single(self, data, serializer):
        """Return the underlying object, given the serialized form."""
        related_model = serializer.Meta.model
        if isinstance(data, related_model):
            return data
        try:
            instance = related_model.objects.get(pk=data)
        except related_model.DoesNotExist as exc:
            # TODO: This causes issues with 400 errors
            # Investigate usage and make it return a dict of errors instead.
            raise ValidationError(
                f"Invalid value for '{self.field_name}': {related_model.__name__}"
                f" object with ID={data} not found"
            ) from exc
        return instance

    def to_internal_value(self, data):
        """Return the underlying object(s), given the serialized form."""
        if self.kwargs["many"]:
            serializer = self.serializer.child
            if not isinstance(data, list):
                raise ParseError(f"'{self.field_name}' value must be a list")
            return [
                self.to_internal_value_single(instance, serializer) for instance in data
            ]
        return self.to_internal_value_single(data, self.serializer)

    @property
    def serializer_class(self):
        """Get the class of the child serializer.

        Resolves string imports.
        """
        serializer_class = self._serializer_class
        if not isinstance(serializer_class, str):
            return serializer_class

        parts = serializer_class.split(".")
        module_path = ".".join(parts[:-1])
        if not module_path:
            if getattr(self, "parent", None) is None:
                raise RuntimeError(
                    f"Can not load serializer '{serializer_class}'"
                    " before binding or without specifying full path"
                )

            # try the module of the parent class
            module_path = self.parent.__module__

        module = importlib.import_module(module_path)
        serializer_class = getattr(module, parts[-1])

        self._serializer_class = serializer_class
        return serializer_class


class CountField(DynamicComputedField):
    """Computed field that counts the number of elements in another field."""

    def __init__(self, serializer_source, *args, **kwargs):
        """
        Initialize field.

        Arguments:
            serializer_source: A serializer field.
            unique: Whether to perform a count of distinct elements.
        """
        self.field_type = int
        # Use `serializer_source`, which indicates a field at the API level,
        # instead of `source`, which indicates a field at the model level.
        self.serializer_source = serializer_source
        # Set `source` to an empty value rather than the field name to avoid
        # an attempt to look up this field.
        kwargs["source"] = ""
        self.unique = kwargs.pop("unique", True)
        super().__init__(*args, **kwargs)

    def get_attribute(self, instance):
        """Get the attribute from the parent serializer."""
        source = self.serializer_source
        if source not in self.parent.fields:
            return None
        value = self.parent.fields[source].get_attribute(instance)
        data = self.parent.fields[source].to_representation(value)

        # How to count None is undefined... let the consumer decide.
        if data is None:
            return None

        # Check data type. Technically len() works on dicts, strings, but
        # since this is a "count" field, we'll limit to list, set, tuple.
        if not isinstance(data, (list, set, tuple)):
            raise TypeError(
                f"'{source}' is {type(data)}. "
                "Must be list, set or tuple to be countable."
            )

        if self.unique:
            # Try to create unique set. This may fail if `data` contains
            # non-hashable elements (like dicts).
            try:
                data = set(data)
            except TypeError:
                pass

        return len(data)


class DynamicHashIdField(GetModelMixin, DynamicField):
    """
    Represents an external ID (computed with hashids).

    Requires the source of the field to be an internal ID, and to provide
    a "model" keyword argument. Together these will produce the external ID.

    Based on
    https://github.com/evenicoulddoit/django-rest-framework-serializer-extensions
    implementation of HashIdField.
    """

    default_error_messages = {
        "malformed_hash_id": "That is not a valid HashId",
    }

    def to_representation(self, value):
        """Serializer value."""
        return external_id_from_model_and_internal_id(self.get_model(), value)

    def to_internal_value(self, data):
        """Internal value."""
        model = self.get_model()
        try:
            return internal_id_from_model_and_external_id(model, data)
        except ObjectDoesNotExist:
            self.fail("malformed_hash_id")
