# coding=utf-8
from collections.abc import Iterable
from operator import attrgetter
from struct import pack, unpack

__doc__ = """
Pure python tools for inspecting unknown protobuf data. Written for py3.6+.

Author: Kent Ross
License: MIT

To parse a proto, pass a bytes-like object to ProtoMessage.parse(), with an
optional offset. This returns a ProtoMessage object with a fields attribute
that contains all the fields of the message in the exact order they appeared.

No assumptions are made about the actual schema of the message. Protobufs are
fully parseable without knowledge of the meaning of their contents, and that's
where this library comes in: allowing you to deserialize, inspect, modify, and
re-serialize proto values, fields, and messages at will with various partially-
and non-supported variations.


Parsing methods:

Whole messages can be parsed via ProtoMessage.parse(), which attempts to consume
the entire bytes-like object passed to it.

The method parse_field(data, offset=0) parses a single field or group from the
given data and offset, returning a 2-tuple of the resulting object and the
number of bytes consumed.

Field.parse(...) and Group.parse(...) are not useful for typical consumption;
use parse_field() instead.

Value type .parse(data, offset=0): Likewise to parse_field, returns a 2-tuple
of the parsed value and the number of bytes consumed from the given data and
offset.


Every message, field, value, and group has some variation of the following APIs:

    * repr_pretty(), pretty_print()
        Outputs the same valid code that reproduces the object like repr does,
        but broken into multiple lines with hierarchical indents. The number of
        spaces for the indentation is customizable with the indent argument.
        repr_pretty() returns the pretty as a string, and pretty_print sends it
        to print() for convenience.

    * operators ==, hash()
        Note: These objects are mutable, and hash() is not safe if you plan to
        change their values.

    * byte_size()
        Returns the exact number of bytes when serialized.

    * total_excess_bytes()
        Returns the number of bytes the message would be shortened by if all
        extraneous varint bytes are removed.

        Most protobuf varints have multiple valid representations because they
        are little-endian and trailing zeros are still interpreted as valid.
        Varints are used extensively: for tags, many integer value types, and
        the byte length of blob values. One of the unique features of this lib
        is that any proto value it manages to parse, it should re-serialize with
        the EXACT bytes it originated from, including extra bytes in varints.
        (It is not designed to be performant; if you need to strip these bytes,
        you should probably use the official protobuf libraries to de-and-re-
        serialize the message.)

    * strip_excess_bytes()
        Recursively removes all excess varint bytes from this value, field,
        group, or message.

    * serialize()
        Serializes the value, field, group, or message and returns it as a bytes
        object.

    * iter_serialize()
        Returns constituent chunks of the serialization as a generator. Used
        internally by serialize().


APIs unique to values (Varint, Blob, Fixed4Bytes, Fixed8Bytes):

    * excess_bytes
        If a value contains a varint, it will have this attribute. It can be set
        to arbitrary non-negative integers; however, values that result in a
        serialized varint length of over 10 bytes may not be valid or readable
        at all by other proto parsing libraries.

    * parse(data, offset=0)
        Returns an instance of the value read from the given data and offset,
        and the number of bytes consumed (as a 2-tuple).

    * parse_repeated(data, offset=0, limit=inf)
        Returns a generator that repeatedly consumes from the given data and
        offset, returning only the data each time. Terminates when it reads to
        the end of the data or the offset described by limit cleanly, whichever
        comes first. Used for reading the values from e.g. packed repeated
        fields, which are stored with variable-length (Blob) wire type and
        contain only the values concatenated together, omitting the tags.

    Typed access properties:
        Provides get/set views of the values translated for the given
        representation:

        Varint:
            unsigned, signed, bool,
            uint32, int32, sint32,
            uint64, int64, sint64
            value: un-translated int value

        Fixed4Bytes:
            float4 (alias single), fixed32, sfixed32
            value: 4-character bytes object

        Fixed8Bytes:
            float8 (alias double), fixed64, sfixed64
            value: 8-character bytes object

        Blob:
            bytes (plain bytes object), string (utf-8),
            message (nested protobuf message)
            value: plain bytes object


APIs unique to fields:
    * is_default()
        Returns a boolean value, true if this value is its proto3 default.

    * parse_submessage()
        TODO: document

    * parse_packed_repeated()
        TODO: document

    * unparsed()
        TODO: document


APIs common to fields, messages, and groups:
    * autoparse_recursive()
        Recursively parse Blob values if they look like valid messages.


APIs unique to messages and groups:
    * Access by index
        Accessing by index yields the field or fields with that id in the order
        they appear in the message (`msg[1]`). If there is only one, it is
        returned without wrapping in a list for convenience. If you always
        need a list, use the value_list(field_id) function instead.

    * Setting by index
        Likewise, iterables of values and groups can be assigned to existing or
        new field ids through setting by index (`msg[1] = Varint(2)`),
        overwriting any existing fields.

    * defaults_byte_size()
        Returns the total byte size of serialized values that could be omitted
        as defaults in proto3.

        Proto3 fields are typically not serialized at all when they manifest
        their default values, which are always the zero representation. Proto2
        differs from this in that defaults may not be zero and the presence or
        absence of a default- or zero-valued field conveys additional value by
        (controversial, deprecated) design.

    * strip_defaults()
        Removes all default-valued fields from the message or group (non-
        recursively: does not propagate to lower groups).

        Note that this will also remove zero values from e.g. repeated fields,
        zero-length serialized sub-message fields, and other values that are
        still serialized in proto3 even when they are default; exercise caution.
"""

NoneType = type(None)

UNSIGNED_64_BIT_RANGE = range(0x1_0000_0000_0000_0000)
SIGNED_64_BIT_RANGE = range(-0x8000_0000_0000_0000, 0x8000_0000_0000_0000)
UNSIGNED_32_BIT_RANGE = range(0x1_0000_0000)
SIGNED_32_BIT_RANGE = range(-0x8000_0000, 0x8000_0000)

# ProtoValue klasses by name of proto type (e.g., int64, string, double etc.)
VALUE_TYPE_KLASSES = {}


def _register_value_types(*types):
    global VALUE_TYPE_KLASSES

    def dec(klass):
        for type_name in types:
            # ensure the klass has an accessor for that name
            assert hasattr(klass, type_name), f'{klass} missing {type_name}'
            VALUE_TYPE_KLASSES[type_name] = klass
        return klass

    return dec


def uint_to_signed(n):
    """
    Convert a non-negative integer to the signed value with zig-zag decoding.
    """
    return (n >> 1) ^ (0 - (n & 1))


def signed_to_uint(n):
    """
    Convert a signed integer to the non-negative value with zig-zag encoding.
    """
    if n < 0:
        return ((n ^ -1) << 1) | 1
    else:
        return n << 1


def write_varint(value, excess_bytes=0):
    """Converts an unsigned varint to bytes."""

    def varint_bytes(n):
        while n:
            more_bytes = (n > 0x7f) or (excess_bytes > 0)
            yield (0x80 * more_bytes) | (n & 0x7f)
            n >>= 7
        if excess_bytes > 0:
            for _ in range(excess_bytes - 1):
                yield 0x80
            yield 0x00

    if value < 0:
        raise ValueError('Encoded varint must be non-negative')
    elif value == 0:
        return b'\0'
    else:
        return bytes(varint_bytes(value))


def read_varint(data, offset=0):
    """
    Read a varint from the given offset in the given byte data.

    Returns a tuple containing the numeric value of the varint and
    the number of bytes consumed.

    If the varint representation does not end before the end of the data,
    a ValueError is raised.
    """
    result = 0
    bytes_read = 0
    try:
        while True:
            byte = data[offset + bytes_read]
            result |= (byte & 0x7f) << (7 * bytes_read)
            bytes_read += 1
            if byte & 0x80 == 0:
                break
    except IndexError:
        raise ValueError(f'Data truncated in varint at position {offset}')
    return result, bytes_read


def bytes_to_encode_varint(n):
    """
    Return the minimum number of bytes needed to represent a number in varint
    encoding.
    """
    if n < 0:
        raise ValueError('Encoded varint must be non-negative')
    return max(1, (n.bit_length() + 6) // 7)


def bytes_to_encode_tag(tag_id):
    """
    Return the minimum number of bytes needed to represent a tag with a given
    id.
    """
    return (tag_id.bit_length() + 9) // 7


def _recursive_autoparse(fields, parse_empty):
    """
    Auto-parses a field into submessages recursively, returning the number
    of submessages successfully parsed.
    """
    num_parsed = 0
    for field in fields:
        try:
            if field.parse_submessage(parse_empty):
                num_parsed += 1 + _recursive_autoparse(
                    field.value.fields,
                    parse_empty
                )
        except (TypeError, ValueError):
            pass
    return num_parsed


class _Serializable:
    __slots__ = ()

    def iter_pretty(self, indent, depth):
        raise NotImplementedError

    def repr_pretty(self, indent=4):
        return ''.join(self.iter_pretty(' ' * indent, 0))

    def pretty_print(self, *args, **kwargs):
        print(self.repr_pretty(*args, **kwargs))

    def byte_size(self):
        raise NotImplementedError

    def total_excess_bytes(self):
        return 0

    def strip_excess_bytes(self):
        pass

    def iter_serialize(self):
        raise NotImplementedError

    def serialize(self):
        return b''.join(self.iter_serialize())


class _FieldSet(_Serializable):
    __slots__ = ('fields',)

    # TODO: implement a fast mode where 'fields' is a dict instead for more
    #  efficient access; allow converting to and from that representation

    def __init__(self, fields):
        if not isinstance(fields, Iterable):
            raise TypeError(f'Cannot create fieldset with non-iterable type '
                            f'{repr(type(fields).__name__)}')
        self.fields = list(fields)

    def __eq__(self, other):
        """
        Calculate equality, ignoring excess varint bytes and un-sorted
        fields.
        """
        if type(other) is not type(self):
            return NotImplemented
        return (
                sorted(other.fields, key=attrgetter('id')) ==
                sorted(self.fields, key=attrgetter('id'))
        )

    def __iter__(self):
        yield from self.fields

    def __repr__(self):
        return (
            f'{type(self).__name__}('
            f'{repr(self.fields)}'
            f')'
        )

    def iter_pretty(self, indent, depth):
        if self.fields:
            yield f'{type(self).__name__}('
            yield from self._pretty_extra_pre()
            yield '[\n'
            for field in self:
                yield indent * (depth + 1)
                yield from field.iter_pretty(indent, depth + 1)
                yield ',\n'
            yield f'{indent * depth}]'
            yield from self._pretty_extra_post()
            yield ')'
        else:
            yield repr(self)

    def _pretty_extra_pre(self):
        return ()

    def _pretty_extra_post(self):
        return ()

    def __getitem__(self, field_id):
        result = self.value_list(field_id)
        if not len(result):
            raise KeyError(f'Field not found: {repr(field_id)}')
        if len(result) == 1:
            return result[0]
        else:
            return result

    def value_list(self, field_id):
        return [field.value for field in self if field.id == field_id]

    def __setitem__(self, field_id, values):
        def to_field(value):
            """Wrap the value in a field only if it isn't a group."""
            if isinstance(value, Group):
                return Group(
                    field_id,
                    list(value.fields),
                    value.excess_tag_bytes,
                    value.excess_end_tag_bytes
                )
            else:
                return Field(field_id, value)

        if not isinstance(values, Iterable):
            fields_to_add = [to_field(values)]
        else:
            fields_to_add = [to_field(value) for value in values]
        new_fields = []
        for field in self:
            # Replace the existing fields with this id at the position it's
            # first encountered
            if field.id == field_id:
                new_fields.extend(fields_to_add)
                fields_to_add = ()
            else:
                new_fields.append(field)
        if fields_to_add:
            # If no fields with this id existed yet, add them to the end
            new_fields.extend(fields_to_add)
        self.fields = new_fields

    def __delitem__(self, field_id):
        self.fields = [field for field in self if field.id != field_id]

    def sort(self):
        """Order the fields in this message by id"""
        self.fields.sort(key=attrgetter('id'))

    # TODO: parse_multi_packed_repeated

    def parse_multi_submessages(
            self,
            field_ids=(),
            auto=False,
            auto_parse_empty=False
    ):
        """
        Parse Blob values in the given field ids into messages and return the
        number of values parsed thus. If every parse is successful, replaces the
        fields with the parsed version.

        If auto is set to True, blob values in field ids that are NOT specified
        will also be converted to SubMessage type if and only if they appear to
        be valid protobuf messages. In this case, field ids that ARE specified
        are interpreted as required, and if any are not valid an error will
        be raised.
        """
        # TODO: document up top
        if not isinstance(field_ids, Iterable):
            field_ids = (field_ids,)
        num_parsed = 0
        new_fields = []
        for field in self:
            if field.id in field_ids:
                if not isinstance(field.value, (Blob, SubMessage)):
                    raise ValueError(
                        f'Encountered field at specified id {field.id} with '
                        f'non-Blob type {type(field.value).__name__}'
                    )
                if isinstance(field.value, Blob):
                    new_fields.append(Field(
                        field.id,
                        SubMessage(
                            field.value.message,
                            excess_bytes=field.value.excess_bytes
                        ),
                        excess_tag_bytes=field.excess_tag_bytes
                    ))
                    num_parsed += 1
                else:
                    new_fields.append(field)
            elif auto:
                if (
                        isinstance(field.value, Blob) and
                        (len(field.value.value) > 0 or auto_parse_empty)
                ):
                    try:
                        new_fields.append(Field(
                            field.id,
                            SubMessage(
                                field.value.message,
                                excess_bytes=field.value.excess_bytes
                            ),
                            excess_tag_bytes=field.excess_tag_bytes
                        ))
                        num_parsed += 1
                    except ValueError:
                        new_fields.append(field)
                else:
                    new_fields.append(field)

        self.fields = new_fields
        return num_parsed

    def autoparse_recursive(self, parse_empty=False):
        """
        Recursively parse submessages whenever possible, returning the total
        number of submessages parsed thusly.
        """
        # These calls mutate as they go rather than building a speculative list
        # of altered fields (like unparse_fields does) because if we are only
        # trying to parse anything that *can* be parsed and there is no failure
        # mode.
        return _recursive_autoparse(self.fields, parse_empty)

    def unparse_fields(self, field_ids=None):
        """
        Replace SubMessage and PackedRepeated fields at the specified ids with
        their serialized Blob values.

        If field_ids is None, performs this action on all fields.
        """
        if field_ids is not None and not isinstance(field_ids, Iterable):
            field_ids = (field_ids,)
        new_fields = []
        num_unparsed = 0
        for field in self:
            if field_ids is None or field.id in field_ids:
                unparsed = field.unparsed()
                if unparsed is not field:
                    new_fields.append(unparsed)
                    num_unparsed += 1
            else:
                new_fields.append(field)

        self.fields = new_fields
        return num_unparsed

    def byte_size(self):
        """
        Return the total length this message will occupy when serialized in
        bytes.
        """
        return sum(field.byte_size() for field in self)

    def defaults_byte_size(self):
        """
        Return the total number of bytes used to serialize fields that are
        assigned default values.
        """
        return sum(
            field.byte_size()
            for field in self
            if field.is_default()
        )

    def strip_defaults(self):
        """
        Strip all fields from the message that are assigned default values,
        returning the number of fields so removed.

        Note: This will also strip submessages, even though empty submessages
        may be represented intentionally.
        """
        old_len = len(self.fields)
        self.fields = [field for field in self if not field.is_default()]
        return old_len - len(self.fields)

    def total_excess_bytes(self):
        """
        Return the total number of excess bytes used to encode varints (tags,
        varint values, and lengths).
        """
        return sum(field.total_excess_bytes() for field in self)

    def strip_excess_bytes(self):
        """Strip all excess bytes from this message's fields and values."""
        for field in self:
            field.strip_excess_bytes()

    def iter_serialize(self):
        for field in self:
            yield from field.iter_serialize()

    # TODO: implement and document map accessors (maybe as a view?); delegate to
    #  PackedRepeated if that's what's found


class ProtoMessage(_FieldSet):
    __slots__ = ()

    def __init__(self, fields=()):
        """
        Create a new ProtoMessage with the given iterable of protobuf Fields.
        """
        super().__init__(fields)

    def __repr__(self):
        return f'{type(self).__name__}({repr(self.fields)})'

    @classmethod
    def parse(
            cls,
            data,
            offset=0,
            limit=float('inf'),
            allow_orphan_group_ends=False
    ):
        """
        Parse a complete ProtoMessage from a bytes-like object.

        Starts parsing from the given offset and consumes until the end of the
        data or the given limit (another greater or equal offset) is reached,
        whichever comes first. Fails if the end of the message does not fit
        exactly to the end of the data (or the limit).
        """

        def get_fields():
            current_offset = offset
            while current_offset < len(data) and current_offset < limit:
                field, bytes_read = parse_field(data, current_offset)
                if (
                        isinstance(field.value, GroupEnd) and
                        not allow_orphan_group_ends
                ):
                    raise ValueError(f'Orphaned group end with id {field.id} '
                                     f'at position {current_offset}')
                yield field
                current_offset += bytes_read
            if current_offset > limit:
                raise ValueError(
                    f'Message truncated (overran limit at position {limit}) '
                    f'in message starting at position {offset}'
                )

        return cls(get_fields())

    @property
    def message(self):
        return self


def parse_field(data, offset=0):
    try:
        tag, tag_bytes = read_varint(data, offset)
    except ValueError as ex:
        raise ValueError(f'{ex.args[0]} while parsing field tag')
    field_id = tag >> 3
    wire_type = tag & 0b111
    excess_tag_bytes = tag_bytes - bytes_to_encode_tag(field_id)
    value_klass = WIRE_TYPE_KLASSES.get(wire_type)
    if not value_klass:
        raise ValueError(f'Invalid or unsupported field wire type '
                         f'{wire_type} in tag at position {offset}')
    field, field_bytes = FIELD_TYPES.get(wire_type, Field).parse(
        field_id, wire_type, excess_tag_bytes,
        data,
        offset + tag_bytes
    )
    return field, field_bytes + tag_bytes


class Field(_Serializable):
    __slots__ = ('id', 'value', 'excess_tag_bytes',)

    def __init__(self, field_id, value, excess_tag_bytes=0):
        self.id = field_id
        self.value = value
        self.excess_tag_bytes = excess_tag_bytes

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return other.id == self.id and other.value == self.value

    def __repr__(self):
        if self.excess_tag_bytes:
            return (
                f'{type(self).__name__}('
                f'{repr(self.id)}, '
                f'{repr(self.value)}, '
                f'excess_tag_bytes={repr(self.excess_tag_bytes)}'
                f')'
            )
        else:
            return (
                f'{type(self).__name__}('
                f'{repr(self.id)}, '
                f'{repr(self.value)}'
                f')'
            )

    def iter_pretty(self, indent, depth):
        yield f'{type(self).__name__}({repr(self.id)}, '
        yield from self.value.iter_pretty(indent, depth)
        if self.excess_tag_bytes:
            yield f', excess_tag_bytes={repr(self.excess_tag_bytes)}'
        yield ')'

    @classmethod
    def parse(cls, field_id, wire_type, excess_tag_bytes, data, offset):
        value_klass = WIRE_TYPE_KLASSES.get(wire_type)
        if not value_klass:
            raise ValueError(f'Invalid or unsupported field wire type '
                             f'{wire_type} in tag at position {offset}')
        value, value_bytes = value_klass.parse(data, offset)
        return cls(field_id, value, excess_tag_bytes), value_bytes

    def parse_packed_repeated(self, repeated_value_type):
        """
        Parse the value of this field as a packed repeated field, changing its
        value from a Blob to a PackedRepeated type.

        This method changes the representation of the field, but not its
        serialized value -- only the form of its interpretation.

        Raises a TypeError if the value is not a Blob, or a ValueError if it
        does not parse cleanly.
        """
        # TODO: document up top
        if not isinstance(self.value, Blob):
            raise TypeError(
                'Cannot parse non-Blob field value as a packed repeated value.'
            )

        def producer():
            data = self.value.bytes
            offset = 0
            while offset < len(data):
                value, value_bytes = repeated_value_type.parse(data, offset)
                yield value
                offset += value_bytes

        self.value = PackedRepeated(
            producer(),
            excess_bytes=self.value.excess_bytes
        )

    def parse_submessage(self, parse_empty=False):
        """
        Parse the value of this field as a submessage, changeing its value from
        a Blob to a SubMessage type. Does nothing if the field is already a
        parsed submessage. Returns True if the value is now parsed as a
        submessage, or False if the value was empty.

        This method changes the representation of the field, but not its
        serialized value -- only the form of its interpretation.

        Raises a TypeError if the field is of the wrong type (not a Blob), or a
        ValueError if it does not parse cleanly.
        """
        # TODO: document up top
        if isinstance(self.value, Blob):
            if len(self.value.value) > 0 or parse_empty:
                self.value = SubMessage(
                    self.value.message.fields,
                    self.value.excess_bytes
                )
                return True
            else:
                return False  # not parsing empty value
        elif isinstance(self.value, SubMessage):
            return True  # already parsed
        else:
            raise TypeError('Cannot parse non-Blob field value')

    def autoparse_recursive(self, parse_empty=False):
        """
        Recursively parse submessages whenever possible, returning the total
        number of submessages parsed thusly.
        """
        return _recursive_autoparse((self,), parse_empty)

    def unparsed(self):
        """
        Return a new field with the same value as this field, converted from a
        parsed SubMessage or PackedRepeated to an opaque Blob.

        Returns this object if this field already has a Blob type.
        """
        # TODO: document up top
        if type(self.value) is Blob:
            return self
        elif self.value.wire_type == Blob.wire_type:
            return Field(
                self.id,
                Blob(self.value.bytes, self.value.excess_bytes),
                excess_tag_bytes=self.excess_tag_bytes
            )
        else:
            raise TypeError(
                f'Cannot unparse a value of type {type(self.value).__name__}'
            )

    def is_default(self):
        return self.value.value == self.value.default_value

    def total_excess_bytes(self):
        return self.excess_tag_bytes + self.value.total_excess_bytes()

    def strip_excess_bytes(self):
        self.excess_tag_bytes = 0
        self.value.strip_excess_bytes()

    def byte_size(self):
        return (
                bytes_to_encode_tag(self.id) +
                self.excess_tag_bytes +
                self.value.byte_size()
        )

    def iter_serialize(self):
        yield write_varint(
            (self.id << 3) | self.value.wire_type,
            self.excess_tag_bytes
        )
        yield from self.value.iter_serialize()


class Group(_FieldSet):
    __slots__ = ('id', 'excess_tag_bytes', 'excess_end_tag_bytes')

    def __init__(
            self, group_id, fields=(),
            excess_tag_bytes=0, excess_end_tag_bytes=0
    ):
        super().__init__(fields)
        self.id = group_id
        self.excess_tag_bytes = excess_tag_bytes
        self.excess_end_tag_bytes = excess_end_tag_bytes

    def __repr__(self):
        if self.excess_tag_bytes + self.excess_end_tag_bytes:
            return (
                f'{type(self).__name__}('
                f'{repr(self.id)}, {repr(self.fields)}, '
                f'excess_tag_bytes={repr(self.excess_tag_bytes)}, '
                f'excess_end_tag_bytes={repr(self.excess_end_tag_bytes)}'
                f')'
            )
        else:
            return (
                f'{type(self).__name__}('
                f'{repr(self.id)}, {repr(self.fields)}'
                f')'
            )

    def _pretty_extra_pre(self):
        yield f'{repr(self.id)}, '

    def _pretty_extra_post(self):
        if self.excess_tag_bytes:
            yield f', excess_tag_bytes={repr(self.excess_tag_bytes)}'
        if self.excess_end_tag_bytes:
            yield f', excess_end_tag_bytes={repr(self.excess_end_tag_bytes)}'

    @classmethod
    def parse(cls, _wire_type, field_id, excess_tag_bytes, data, offset):
        excess_end_tag_bytes = 0
        total_bytes_read = 0

        def get_fields(offset_):
            nonlocal excess_end_tag_bytes, total_bytes_read
            while offset_ < len(data):
                field, bytes_read = parse_field(data, offset_)
                offset_ += bytes_read
                total_bytes_read += bytes_read
                if isinstance(field.value, GroupEnd):
                    if field.id != field_id:
                        raise ValueError(f'Non-matching group end tag with id '
                                         f'{field.id} at position {offset}')
                    excess_end_tag_bytes = field.excess_tag_bytes
                    break
                else:
                    yield field
            else:
                # Reached the end of the data without closing the group
                raise ValueError(
                    f'Missing group end tag while parsing group '
                    f'with id {field_id} which began at position {offset}'
                )

        try:
            fields = list(get_fields(offset))
        except ValueError as ex:
            # Append info about this group context to parsing errors
            raise ValueError(
                f'{ex.args[0]} in group with id {field_id} '
                f'which began at position {offset}'
            )
        return cls(
            field_id,
            fields,
            excess_tag_bytes=excess_tag_bytes,
            excess_end_tag_bytes=excess_end_tag_bytes
        ), total_bytes_read

    def parse_submessage(self):
        raise TypeError('Groups cannot be parsed further')

    def unparse(self):
        raise TypeError('Groups cannot be unparsed')

    @property
    def value(self):
        """
        This property is used when fields are gotten by id with indexing.
        It makes the most sense to work with an entire group, since it
        manages its own 'value' in the fields attribute.
        """
        return self

    def is_default(self):
        return not self.fields

    def byte_size(self):
        return (
                bytes_to_encode_tag(self.id) * 2 +
                self.excess_tag_bytes + self.excess_end_tag_bytes +
                super().byte_size()
        )

    def total_excess_bytes(self):
        return (
                super().total_excess_bytes() +
                self.excess_tag_bytes +
                self.excess_end_tag_bytes
        )

    def strip_excess_bytes(self):
        super().strip_excess_bytes()
        self.excess_tag_bytes = 0
        self.excess_end_tag_bytes = 0

    def iter_serialize(self):
        yield write_varint(
            (self.id << 3) | GroupStart.wire_type,
            self.excess_tag_bytes
        )
        yield from super().iter_serialize()
        yield write_varint(
            (self.id << 3) | GroupEnd.wire_type,
            self.excess_end_tag_bytes
        )


class _ParseableValue:
    """Mixin for values which can be parsed once or repeatedly."""
    __slots__ = ()

    @classmethod
    def parse(cls, data, offset=0):
        raise NotImplementedError

    @classmethod
    def parse_repeated(cls, data, offset=0, limit=float('inf')):
        """
        Parses a repeated proto value from a bytes-like object.

        Starts parsing from the given offset and consumes until the end of the
        data or the given limit (another greater or equal offset) is reached,
        whichever comes first. Fails if the end of the message does not fit
        exactly to the end of the data (or the limit).
        """
        current_offset = offset
        while current_offset < len(data) and current_offset < limit:
            value, value_bytes = cls.parse(data, current_offset)
            yield value
            current_offset += value_bytes
        if current_offset > limit:
            raise ValueError(
                f'Data truncated (overran limit at position {limit}) '
                f'in repeated {cls.__name__} '
                f'starting at position {offset}'
            )


# noinspection PyAbstractClass
class _ProtoValue(_Serializable, _ParseableValue):
    __slots__ = ('value',)

    def __init__(self, value=None):
        if value is None:
            self.value = self.default_value
        else:
            self.value = value

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return other.value == self.value

    def __repr__(self):
        excess_bytes = getattr(self, 'excess_bytes', None)
        if excess_bytes:
            return (
                f'{type(self).__name__}({repr(self.value)}, '
                f'excess_bytes={excess_bytes})'
            )
        else:
            return f'{type(self).__name__}({repr(self.value)})'

    def iter_pretty(self, indent, depth):
        yield repr(self)

    @property
    def default_value(self):
        raise NotImplementedError

    @property
    def wire_type(self):
        raise NotImplementedError


@_register_value_types(
    'varint',
    'unsigned',
    'signed',
    'bool',
    'int32',
    'int64',
    'uint32',
    'uint64',
    'sint32',
    'sint64',
)
class Varint(_ProtoValue):
    __slots__ = ('excess_bytes',)
    wire_type = 0
    default_value = 0

    def __init__(self, value=None, excess_bytes=0):
        super().__init__(value)
        self.excess_bytes = excess_bytes

    @classmethod
    def parse(cls, data, offset=0):
        value, value_bytes = read_varint(data, offset)
        excess_bytes = value_bytes - bytes_to_encode_varint(value)
        return cls(value, excess_bytes=excess_bytes), value_bytes

    def byte_size(self):
        return bytes_to_encode_varint(self.value) + self.excess_bytes

    def total_excess_bytes(self):
        return self.excess_bytes

    def strip_excess_bytes(self):
        self.excess_bytes = 0

    def iter_serialize(self):
        yield write_varint(self.value, self.excess_bytes)

    @property
    def varint(self):
        return self.value

    @varint.setter
    def varint(self, value):
        self.value = value

    unsigned = varint

    @property
    def signed(self):
        return uint_to_signed(self.value)

    @signed.setter
    def signed(self, value):
        self.value = signed_to_uint(value)

    @property
    def bool(self):
        return bool(self.value)

    @bool.setter
    def bool(self, value):
        self.value = int(bool(value))

    @property
    def uint32(self):
        if self.value not in UNSIGNED_32_BIT_RANGE:
            raise ValueError('Varint out of range for uint32')
        return self.value

    @uint32.setter
    def uint32(self, value):
        if value not in UNSIGNED_32_BIT_RANGE:
            raise ValueError('Value out of range for uint32')
        self.value = value

    @property
    def int32(self):
        if self.value not in UNSIGNED_32_BIT_RANGE:
            raise ValueError('Varint out of range for int32')
        if self.value & 0x8000_0000:
            return self.value - 0x1_0000_0000
        else:
            return self.value

    @int32.setter
    def int32(self, value):
        if value not in SIGNED_32_BIT_RANGE:
            raise ValueError('Value out of range for int32')
        self.value = value & 0xffff_ffff

    @property
    def sint32(self):
        if self.value not in UNSIGNED_32_BIT_RANGE:
            raise ValueError('Varint out of range for sint32')
        return uint_to_signed(self.value)

    @sint32.setter
    def sint32(self, value):
        if value not in SIGNED_32_BIT_RANGE:
            raise ValueError('Value out of range for sint32')
        self.value = signed_to_uint(value)

    @property
    def uint64(self):
        if self.value not in UNSIGNED_64_BIT_RANGE:
            raise ValueError('Varint out of range for uint64')
        return self.value

    @uint64.setter
    def uint64(self, value):
        if value not in UNSIGNED_64_BIT_RANGE:
            raise ValueError('Value out of range for uint64')
        self.value = value

    @property
    def int64(self):
        if self.value not in UNSIGNED_64_BIT_RANGE:
            raise ValueError('Varint out of range for int64')
        if self.value & 0x8000_0000_0000_0000:
            return self.value - 0x1_0000_0000_0000_0000
        else:
            return self.value

    @int64.setter
    def int64(self, value):
        if value not in SIGNED_64_BIT_RANGE:
            raise ValueError('Value out of range for int64')
        self.value = value & 0xffff_ffff_ffff_ffff

    @property
    def sint64(self):
        if self.value not in UNSIGNED_64_BIT_RANGE:
            raise ValueError('Varint out of range for sint64')
        return uint_to_signed(self.value)

    @sint64.setter
    def sint64(self, value):
        if value not in SIGNED_64_BIT_RANGE:
            raise ValueError('Value out of range for sint64')
        self.value = signed_to_uint(value)


@_register_value_types(
    'fixed4bytes',
    'float4',
    'fixed32',
    'sfixed32',
)
class Fixed4Bytes(_ProtoValue):
    __slots__ = ()
    wire_type = 5
    default_value = b'\0' * 4

    @classmethod
    def parse(cls, data, offset=0):
        value = data[offset:offset + 4]
        if len(value) < 4:
            raise ValueError(f'Data truncated in Fixed4Bytes value '
                             f'beginning at position {offset}')
        return cls(value), 4

    def byte_size(self):
        return 4

    def iter_serialize(self):
        yield self.value

    @property
    def fixed4bytes(self):
        return self.value

    @fixed4bytes.setter
    def fixed4bytes(self, value):
        if len(value) != 4:
            raise ValueError('Fixed4Bytes value must have length 4')
        self.value = value

    @property
    def float4(self):
        result, = unpack('<f', self.value)
        return result

    @float4.setter
    def float4(self, value):
        self.value = pack('<f', value)

    single = float4

    @property
    def fixed32(self):
        result, = unpack('<L', self.value)
        return result

    @fixed32.setter
    def fixed32(self, value):
        self.value = pack('<L', value)

    @property
    def sfixed32(self):
        result, = unpack('<l', self.value)
        return result

    @sfixed32.setter
    def sfixed32(self, value):
        self.value = pack('<l', value)


@_register_value_types(
    'fixed8bytes',
    'float8',
    'double',
    'fixed64',
    'sfixed64',
)
class Fixed8Bytes(_ProtoValue):
    __slots__ = ()
    wire_type = 1
    default_value = b'\0' * 8

    @classmethod
    def parse(cls, data, offset=0):
        value = data[offset:offset + 8]
        if len(value) < 8:
            raise ValueError(f'Data truncated in Fixed8Bytes value '
                             f'beginning at position {offset}')
        return cls(value), 8

    def byte_size(self):
        return 8

    def iter_serialize(self):
        yield self.value

    @property
    def fixed8bytes(self):
        return self.value

    @fixed8bytes.setter
    def fixed8bytes(self, value):
        if len(value) != 8:
            raise ValueError('Fixed4Bytes value must have length 8')
        self.value = value

    @property
    def float8(self):
        result, = unpack('<d', self.value)
        return result

    @float8.setter
    def float8(self, value):
        self.value = pack('<d', value)

    double = float8

    @property
    def fixed64(self):
        result, = unpack('<Q', self.value)
        return result

    @fixed64.setter
    def fixed64(self, value):
        self.value = pack('<Q', value)

    @property
    def sfixed64(self):
        result, = unpack('<q', self.value)
        return result

    @sfixed64.setter
    def sfixed64(self, value):
        self.value = pack('<q', value)


@_register_value_types(
    'blob',
    'string',
    'bytes',
)
class Blob(_ProtoValue):
    __slots__ = ('excess_bytes',)
    wire_type = 2
    default_value = b''

    def __init__(self, value=None, excess_bytes=0):
        super().__init__(value)
        self.excess_bytes = excess_bytes

    @classmethod
    def parse(cls, data, offset=0):
        try:
            length, length_bytes = read_varint(data, offset)
        except ValueError as ex:
            raise ValueError(
                f'{ex.args[0]} while parsing length of {type(cls).__name__}'
            )
        excess_bytes = length_bytes - bytes_to_encode_varint(length)
        start = offset + length_bytes
        value = data[start:start + length]
        if len(value) < length:
            raise ValueError(f'Data truncated in length-delimited data '
                             f'beginning at position {start} '
                             f'(was {length} long)')
        return cls(value, excess_bytes=excess_bytes), length_bytes + length

    def byte_size(self):
        length = len(self.value)
        return bytes_to_encode_varint(length) + self.excess_bytes + length

    def total_excess_bytes(self):
        return self.excess_bytes

    def strip_excess_bytes(self):
        self.excess_bytes = 0

    def iter_serialize(self):
        yield write_varint(len(self.value), self.excess_bytes)
        yield self.value

    @property
    def blob(self):
        return self.value

    @blob.setter
    def blob(self, value):
        self.value = value

    @property
    def bytes(self):
        return self.value

    @bytes.setter
    def bytes(self, value):
        self.value = value

    @property
    def string(self):
        return self.value.decode('utf-8')

    @string.setter
    def string(self, value):
        self.value = value.encode('utf-8')

    @property
    def message(self):
        return ProtoMessage.parse(self.value)

    @message.setter
    def message(self, value):
        self.value = value.serialize()

    # TODO: implement and document map accessors (maybe as a view?)


class PackedRepeated(_Serializable):
    """Represents a Blob field interpreted as a valid packed repeated field."""
    __slots__ = ('values', 'excess_bytes',)
    wire_type = Blob.wire_type

    def __init__(self, values=(), value_type=None, excess_bytes=0):
        self.values = None  # suppress 'set outside __init__' warning
        self.set_as(values, value_type=value_type)
        self.excess_bytes = excess_bytes

    @property
    def default_value(self):
        return []

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return other.values == self.values

    def __iter__(self):
        yield from self.values

    def __repr__(self):
        if self.excess_bytes:
            return (
                f'{type(self).__name__}('
                f'{repr(self.values)}, '
                f'excess_bytes={self.excess_bytes}'
                f')'
            )
        else:
            return f'{type(self).__name__}({repr(self.values)})'

    def iter_pretty(self, indent, depth):
        if self.values:
            yield f'{type(self).__name__}([\n'
            for value in self.values:
                yield indent * (depth + 1)
                yield from value.iter_pretty(indent, depth + 1)
                yield ',\n'
            yield f'{indent * depth}]'
            if self.excess_bytes:
                yield f', excess_tag_bytes={self.excess_bytes}'
            yield ')'

    def byte_size(self):
        length = sum(value.byte_size() for value in self.values)
        return bytes_to_encode_varint(length) + self.excess_bytes + length

    def total_excess_bytes(self):
        return (
                sum(value.total_excess_bytes() for value in self.values) +
                self.excess_bytes
        )

    def strip_excess_bytes(self):
        self.excess_bytes = 0
        for value in self.values:
            value.strip_excess_bytes()

    def iter_serialize(self):
        # Not exactly efficient, but we're not prescient.
        length = sum(value.byte_size() for value in self.values)
        yield write_varint(length, self.excess_bytes)
        for value in self.values:
            yield from value.iter_serialize()

    @classmethod
    def parse(cls, repeated_value_type, data, offset=0):
        try:
            length, length_bytes = read_varint(data, offset)
        except ValueError as ex:
            raise ValueError(
                f'{ex.args[0]} while parsing length of {type(cls).__name__}'
            )
        return cls(
            repeated_value_type.parse_repeated(
                data,
                offset=offset + length_bytes,
                limit=offset + length_bytes + length
            ),
            excess_bytes=length_bytes - bytes_to_encode_varint(length)
        )

    @property
    def bytes(self):
        return b''.join(
            part
            for value in self.values
            for part in value.iter_serialize()
        )

    @bytes.setter
    def bytes(self, value):
        if self.values:
            repeated_type = type(self.values[0])
            self.values = list(repeated_type.parse_repeated(value))

    def get_as(self, interpretation):
        def emitter():
            for value in self.values:
                try:
                    yield getattr(value, interpretation)
                except AttributeError:
                    raise TypeError(
                        f'Invalid interpretation {repr(interpretation)} '
                        f'for value klass {type(value).__name__}'
                    )

        return list(emitter())

    def set_as(self, values, value_type=None):
        """
        Set the repeated values of this field.

        The provided value_type should be the format that the values are in.
        For example, say values is a list of ints: if value_type is "varint" or
        "unsigned" they will be wrapped in Varint as raw values, but if
        value_type is "float4" they will be converted to single-precision
        floating point values and wrapped in Fixed4Bytes values.

        If value_type is not provided, the values will be used as-is.
        """

        def ingester():
            if value_type is None:
                yield from values
            else:
                try:
                    value_klass = VALUE_TYPE_KLASSES[value_type]
                except KeyError:
                    raise ValueError(f'Unknown value type {repr(value_type)}')
                for value in values:
                    proto_value = value_klass()
                    setattr(proto_value, value_type, value)
                    yield proto_value

        self.values = list(ingester())


@_register_value_types(
    'message',
)
class SubMessage(ProtoMessage, _ParseableValue):
    """Represents a Blob field interpreted as a valid sub-message."""
    __slots__ = ('excess_bytes',)
    wire_type = Blob.wire_type

    def __init__(self, fields=(), excess_bytes=0):
        super().__init__(fields)
        self.excess_bytes = excess_bytes

    def _pretty_extra_post(self):
        if self.excess_bytes:
            yield f', excess_bytes={self.excess_bytes}',

    @property
    def default_value(self):
        return ProtoMessage()

    # This method intentionally has a different signature than super's.
    # noinspection PyMethodOverriding
    @classmethod
    def parse(cls, data, offset=0):
        try:
            length, length_bytes = read_varint(data, offset)
        except ValueError as ex:
            raise ValueError(
                f'{ex.args[0]} while parsing length of {type(cls).__name__}'
            )
        excess_bytes = length_bytes - bytes_to_encode_varint(length)
        start = offset + length_bytes
        value, length = ProtoMessage.parse(
            data, offset=start, limit=start + length
        )
        return (
            cls(value.fields, excess_bytes=excess_bytes),
            length_bytes + length
        )

    def byte_size(self):
        length = super().byte_size()
        return bytes_to_encode_varint(length) + self.excess_bytes + length

    def total_excess_bytes(self):
        return self.excess_bytes + super().total_excess_bytes()

    def strip_excess_bytes(self):
        self.excess_bytes = 0

    def iter_serialize(self):
        yield write_varint(super().byte_size(), self.excess_bytes)
        yield from super().iter_serialize()

    @property
    def bytes(self):
        return b''.join(super().iter_serialize())

    @bytes.setter
    def bytes(self, value):
        self.fields = ProtoMessage.parse(value).fields

    @property
    def message(self):
        return self

    @message.setter
    def message(self, value):
        self.fields = list(value)

    @property
    def string(self):
        return self.bytes.decode('utf-8')


# noinspection PyMethodMayBeStatic
class _TagOnlyValue(_Serializable):
    __slots__ = ()
    value = None
    default_value = NotImplemented

    def __repr__(self):
        return f'{type(self).__name__}()'

    def iter_pretty(self, indent, depth):
        yield repr(self)

    @classmethod
    def parse(cls, _data, _offset=0):
        return cls(), 0

    def byte_size(self):
        return 0

    def iter_serialize(self):
        return ()  # nothing


class GroupStart(_TagOnlyValue):
    __slots__ = ()
    wire_type = 3


class GroupEnd(_TagOnlyValue):
    __slots__ = ()
    wire_type = 4


# Mapping from wire type to value klass.
WIRE_TYPE_KLASSES = {
    klass.wire_type: klass
    for klass in [
        Varint,
        Fixed8Bytes,
        Blob,
        GroupStart,
        GroupEnd,
        Fixed4Bytes,
    ]
}

# These are overrides for the klass of field that parses a given wiretype.
# If a wiretype is not present, defaults to Field.
# Currently only applies to groups. Setting this to an empty dict and passing
# allow_orphan_group_ends=True to ProtoMessage.parse() will return messages
# parsed with explicit GroupStart/GroupEnd fields instead of actual groups,
# allowing the inspection of messages with mismatching or missing group
# delimiters.
FIELD_TYPES = {
    GroupStart.wire_type: Group
}

# These are all the types that will appear in repr strings. We don't need to
# include any other utility functions, but it's important that the user can
# import * and not get repr values that can't eval back to an equivalent object.
__all__ = (
    'ProtoMessage',
    'Field',
    'Group',
    'Varint',
    'Blob',
    'Fixed4Bytes',
    'Fixed8Bytes',
    'PackedRepeated',
    'SubMessage',
    'GroupStart',
    'GroupEnd',
)
