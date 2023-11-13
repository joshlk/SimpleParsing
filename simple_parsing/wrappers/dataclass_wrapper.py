from __future__ import annotations

import argparse
import dataclasses
import functools
import inspect
import sys
import textwrap
from dataclasses import MISSING
from logging import getLogger
from typing import Any, Callable, Generic, TypeVar, cast

from typing_extensions import Literal

from simple_parsing import docstring, utils
from simple_parsing.docstring import dp_parse, inspect_getdoc
from simple_parsing.utils import Dataclass, DataclassT, is_dataclass_instance, is_dataclass_type
from simple_parsing.wrappers.field_wrapper import FieldWrapper
from simple_parsing.wrappers.wrapper import Wrapper

logger = getLogger(__name__)

MAX_DOCSTRING_DESC_LINES_HEIGHT: int = 50
"""
Maximum number of lines of the class docstring to include in the autogenerated argument group
description. If fields don't have docstrings or help text, then this is not used, and the entire
docstring is used as the description of the argument group.
"""

DataclassWrapperType = TypeVar("DataclassWrapperType", bound="DataclassWrapper")


class DataclassWrapper(Wrapper, Generic[DataclassT]):
    def __init__(
        self,
        dataclass: type[DataclassT],
        name: str,
        default: DataclassT | dict | None = None,
        prefix: str = "",
        parent: DataclassWrapper | None = None,
        _field: dataclasses.Field | None = None,
        field_wrapper_class: type[FieldWrapper] = FieldWrapper,
        dataclass_fn: Callable[..., DataclassT] | None = None,
    ):
        super().__init__()
        self.dataclass = dataclass
        self._name = name
        assert is_dataclass_type(dataclass)  # FIXME: Remove
        if dataclass_fn:
            assert callable(dataclass_fn), dataclass_fn
        self.dataclass_fn = dataclass_fn or dataclass
        assert not is_dataclass_instance(self.dataclass_fn)  # FIXME: Remove

        self._default = default
        self.prefix = prefix
        self._parent = parent
        # the field of the parent, which contains this child dataclass.
        self._field = _field
        self.field_wrapper_class = field_wrapper_class

        self.fields: list[FieldWrapper] = []
        self.optional: bool = False

        self._destinations: list[str] = []
        self._required: bool = False
        self._explicit: bool = False
        self._dest: str = ""
        self._children: list[DataclassWrapper] = []
        # the default value(s).
        # NOTE: This is a list only because of the `ConflictResolution.ALWAYS_MERGE` option.
        self._defaults: list[DataclassT] = [default] if default else []

        dataclass_fields: tuple[dataclasses.Field, ...] = _get_dataclass_fields(dataclass)
        # Create an object for each field, which is used to compute (and hold) the arguments that
        # will then be passed to `argument_group.add_argument` later.
        # This could eventually be refactored into a stateless thing. But for now it isn't.
        for field in dataclass_fields:
            if not field.init or field.metadata.get("cmd", True) is False:
                # Don't add arguments for this field.
                continue

            if isinstance(field.type, str):
                # NOTE: Here we'd like to convert the fields type to an actual type, in case the
                # `from __future__ import annotations` feature is used.
                from simple_parsing.annotation_utils.get_field_annotations import (
                    get_field_type_from_annotations,
                )

                field_type = get_field_type_from_annotations(self.dataclass, field.name)
                # Modify the `type` of the Field object, in-place.
                field.type = field_type
            else:
                field_type = field.type

            # Manually overwrite the field default value with the corresponding attribute of the
            # default for the parent.
            field_default = dataclasses.MISSING
            if isinstance(dataclass_fn, functools.partial) and field.name in dataclass_fn.keywords:
                # NOTE: We need to override the default value of the field, because since the
                # dataclass_fn is a partial, and we always set the defaults for all fields in the
                # constructor arguments dict, those would be passed to the partial, and the value
                # for that argument in the partial (e.g. `dataclass_fn = partial(A, a=123)`) would
                # be unused when we call `dataclass_fn(**constructor_args[dataclass_dest])` later.
                field_default = dataclass_fn.keywords[field.name]
                # TODO: This is currently only really necessary in the case where the dataclass_fn
                # is a `functools.partial` (e.g. when using subgroups). But the idea of specifying
                # the default value and passing it here to the wrapper, rather than have the
                # wrappers "fetch" it from their field or their parent, makes sense!
                logger.debug(
                    f"Got a default value of {field_default} for field {field.name} from "
                    f"inspecting the dataclass function! ({dataclass_fn})"
                )
            elif isinstance(default, dict):
                if field.name in default:
                    field_default = default[field.name]
            elif default not in (None, argparse.SUPPRESS):
                field_default = getattr(default, field.name)

            if utils.is_tuple_or_list_of_dataclasses(field_type):
                raise NotImplementedError(
                    f"Field {field.name} is of type {field_type}, which isn't "
                    f"supported yet. (container of a dataclass type)"
                )

            if utils.is_subparser_field(field) or utils.is_choice(field):
                field_wrapper = self.field_wrapper_class(
                    field,
                    parent=self,
                    prefix=prefix,
                )
                if field_default is not dataclasses.MISSING:
                    field_wrapper.set_default(field_default)

                self.fields.append(field_wrapper)

            elif dataclasses.is_dataclass(field_type) and field.default is not None:
                # Non-optional dataclass field.
                # handle a nested dataclass attribute
                dataclass, name = field_type, field.name
                # todo: Figure out if this is still necessary, or if `field_default` can be handled
                # the same way as above.
                if field_default is dataclasses.MISSING:
                    field_default = None
                child_wrapper = DataclassWrapper(
                    dataclass,
                    name,
                    parent=self,
                    _field=field,
                    default=field_default,
                )
                self._children.append(child_wrapper)

            elif utils.contains_dataclass_type_arg(field_type):
                # Extract the dataclass type from the annotation of the field.
                field_dataclass = utils.get_dataclass_type_arg(field_type)
                # todo: Figure out if this is still necessary, or if `field_default` can be handled
                # the same way as above.
                if field_default is dataclasses.MISSING:
                    field_default = None
                child_wrapper = DataclassWrapper(
                    field_dataclass,
                    name=field.name,
                    parent=self,
                    _field=field,
                    default=field_default,
                )
                child_wrapper.required = False
                child_wrapper.optional = True
                self._children.append(child_wrapper)

            else:
                # a "normal" attribute
                field_wrapper = self.field_wrapper_class(field, parent=self, prefix=self.prefix)
                logger.debug(
                    f"wrapped field at {field_wrapper.dest} has a default value of {field_wrapper.default}"
                )
                if field_default is not dataclasses.MISSING:
                    field_wrapper.set_default(field_default)

                self.fields.append(field_wrapper)

        logger.debug(f"The dataclass at attribute {self.dest} has default values: {self.defaults}")

    def add_arguments(self, parser: argparse.ArgumentParser):
        from ..parsing import ArgumentParser

        parser = cast(ArgumentParser, parser)

        group = parser.add_argument_group(title=self.title, description=self.description)

        for wrapped_field in self.fields:
            # Note: This should be true since we don't create a FieldWrapper for fields with
            # `cmd=False`.
            assert wrapped_field.field.metadata.get("cmd", True)

            if wrapped_field.is_subparser:
                wrapped_field.add_subparsers(parser)
                continue

            arg_options = wrapped_field.arg_options

            if argparse.SUPPRESS in self.defaults:
                arg_options["default"] = argparse.SUPPRESS
            if wrapped_field.is_subgroup:
                # NOTE: Not skipping subgroup fields, because even though they will have been
                # resolved at this point, we still want them to show up in the --help message!
                logger.debug(
                    f"Adding a subgroup field {wrapped_field.name} just so it shows up in the "
                    f"--help text."
                )

            logger.info(f"group.add_argument(*{wrapped_field.option_strings}, **{arg_options})")
            # TODO: Perhaps we could hook into the `action` that is returned here to know if the
            # flag was passed or not for a given field.
            _ = group.add_argument(*wrapped_field.option_strings, **arg_options)

    def equivalent_argparse_code(self, leading="group") -> str:
        code = ""
        code += textwrap.dedent(
            f"""
        group = parser.add_argument_group(title="{self.title.strip()}", description="{self.description.strip()}")
        """
        )
        for wrapped_field in self.fields:
            if wrapped_field.is_subparser:
                # TODO:
                raise NotImplementedError("Subparsers equivalent is TODO.")
                code += textwrap.dedent(
                    f"""\
                # add subparsers for each dataclass type in the field.
                subparsers = parser.add_subparsers(
                    title={wrapped_field.name},
                    description={wrapped_field.help},
                    dest={wrapped_field.dest},
                )
                subparsers.required = True

                for subcommand, dataclass_type in {self.subparsers_dict.items()}:
                    subparser = subparsers.add_parser(subcommand)
                    subparser = cast(ArgumentParser, subparser)
                    subparser.add_arguments(dataclass_type, dest=self.dest)
                """
                )
            elif wrapped_field.arg_options:
                code += textwrap.dedent(wrapped_field.equivalent_argparse_code()) + "\n"
        return code

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self) -> DataclassWrapper | None:
        return self._parent

    @property
    def defaults(self) -> list[DataclassT | dict[str, Any] | None | Literal[argparse.SUPPRESS]]:
        if self._defaults:
            return self._defaults
        if self._field is None:
            return []
        assert self.parent is not None
        if self.parent.defaults:
            self._defaults = []
            for default in self.parent.defaults:
                if default not in (None, argparse.SUPPRESS):
                    default = getattr(default, self.name)
                self._defaults.append(default)
        else:
            default_field_value = utils.default_value(self._field)
            if default_field_value is MISSING:
                self._defaults = []
            else:
                self._defaults = [default_field_value]
        return self._defaults

    @defaults.setter
    def defaults(self, value: list[DataclassT]):
        self._defaults = value

    @property
    def default(self) -> DataclassT | None:
        return self._default

    # @default.setter
    # def default(self, value: DataclassT) -> None:
    #     self._default = value

    def set_default(self, value: DataclassT | dict | None):
        """Sets the default values for the arguments of the fields of this dataclass."""
        if value is not None and not isinstance(value, dict):
            field_default_values = dataclasses.asdict(value)
        else:
            field_default_values = value
        self._default = value
        if field_default_values is None:
            return
        unknown_names = set(field_default_values)
        for field_wrapper in self.fields:
            if field_wrapper.name not in field_default_values:
                continue
            # Manually set the default value for this argument.
            field_default_value = field_default_values[field_wrapper.name]
            field_wrapper.set_default(field_default_value)
            unknown_names.remove(field_wrapper.name)
        for nested_dataclass_wrapper in self._children:
            if nested_dataclass_wrapper.name not in field_default_values:
                continue
            field_default_value = field_default_values[nested_dataclass_wrapper.name]
            nested_dataclass_wrapper.set_default(field_default_value)
            unknown_names.remove(nested_dataclass_wrapper.name)
        unknown_names.discard("_type_")
        if unknown_names:
            raise RuntimeError(f"{sorted(unknown_names)} are not fields of {self.dataclass} at path {self.dest!r}!")

    @property
    def title(self) -> str:
        names_string = f""" [{', '.join(f"'{dest}'" for dest in self.destinations)}]"""
        title = self.dataclass.__qualname__ + names_string
        return title

    @property
    def description(self) -> str:
        if self.parent and self._field:
            doc = docstring.get_attribute_docstring(self.parent.dataclass, self._field.name)
            if doc is not None:
                if doc.docstring_below:
                    return doc.docstring_below
                elif doc.comment_above:
                    return doc.comment_above
                elif doc.comment_inline:
                    return doc.comment_inline

        # NOTE: The class docstring may be EXTRELEMY LARGE.

        class_docstring = inspect_getdoc(self.dataclass) or ""
        if not class_docstring:
            return ""

        doc = dp_parse(class_docstring)

        from simple_parsing.decorators import _description_from_docstring

        description = _description_from_docstring(doc)

        num_lines = len(description.splitlines())
        shortened_description = (
            "\n".join(description.splitlines()[:MAX_DOCSTRING_DESC_LINES_HEIGHT]) + " ..."
        )

        fields_have_docstrings = any(f._docstring.help_string for f in self.fields)
        docstring_is_huge = num_lines > MAX_DOCSTRING_DESC_LINES_HEIGHT
        if not fields_have_docstrings:
            # The fields don't have docstrings. Return the entire docstring, regardless of its
            # size.
            return description
        # Fields have docstrings, so there's probably some duplication between the docstring and
        # the dataclass fields help. Shorten the docstring, if needed.
        if docstring_is_huge:
            return shortened_description
        return description

    # @property
    # def prefix(self) -> str:
    #     return self._prefix

    # @prefix.setter
    # def prefix(self, value: str):
    #     self._prefix = value
    #     for child_wrapper in self._children:
    #         child_wrapper.prefix = value

    @property
    def required(self) -> bool:
        return self._required

    @required.setter
    def required(self, value: bool):
        self._required = value
        for field in self.fields:
            field.required = value
        for child_wrapper in self._children:
            child_wrapper.required = value

    @property
    def multiple(self) -> bool:
        return len(self.destinations) > 1

    @property
    def descendants(self):
        for child in self._children:
            yield child
            yield from child.descendants

    @property
    def dest(self):
        lineage = []
        parent = self.parent
        while parent is not None:
            lineage.append(parent.name)
            parent = parent.parent
        lineage = list(reversed(lineage))
        lineage.append(self.name)
        _dest = ".".join(lineage)
        # logger.debug(f"getting dest, returning {_dest}")
        return _dest

    @property
    def destinations(self) -> list[str]:
        if not self._destinations:
            if self.parent:
                self._destinations = [f"{d}.{self.name}" for d in self.parent.destinations]
            else:
                self._destinations = [self.name]
        return self._destinations

    @destinations.setter
    def destinations(self, value: list[str]):
        self._destinations = value

    def merge(self, other: DataclassWrapper):
        """Absorb all the relevant attributes from another wrapper.
        Args:
            other (DataclassWrapper): Another instance to absorb into this one.
        """
        # logger.debug(f"merging \n{self}\n with \n{other}")
        logger.debug(f"self destinations: {self.destinations}")
        logger.debug(f"other destinations: {other.destinations}")
        # assert not set(self.destinations).intersection(set(other.destinations)), "shouldn't have overlap in destinations"
        # self.destinations.extend(other.destinations)
        for dest in other.destinations:
            if dest not in self.destinations:
                self.destinations.append(dest)
        logger.debug(f"destinations after merge: {self.destinations}")
        self.defaults.extend(other.defaults)

        # Unset the default value for all fields.
        # TODO: Shouldn't be needed anymore.
        for field_wrapper in self.fields:
            field_wrapper.set_default(None)

        for child, other_child in zip(self._children, other._children):
            child.merge(other_child)


def _get_dataclass_fields(dataclass: type[Dataclass]) -> tuple[dataclasses.Field, ...]:
    # NOTE: `dataclasses.fields` method retrieves only `dataclasses._FIELD`
    # NOTE: but we also want to know about `dataclasses._FIELD_INITVAR`
    # NOTE: therefore we partly copy-paste its implementation
    if sys.version_info[:2] < (3, 8):
        # Before 3.8 `InitVar[tp] is InitVar` so it's impossible to retrieve field type
        # therefore we should skip it just to be fully backward compatible
        return dataclasses.fields(dataclass)
    try:
        dataclass_fields_map = getattr(dataclass, dataclasses._FIELDS)
    except AttributeError:
        raise TypeError("must be called with a dataclass type or instance")
    return tuple(
        field
        for field in dataclass_fields_map.values()
        if field._field_type in (dataclasses._FIELD, dataclasses._FIELD_INITVAR)
    )
