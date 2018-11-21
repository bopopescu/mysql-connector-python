# Copyright (c) 2016, 2019, Oracle and/or its affiliates. All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2.0, as
# published by the Free Software Foundation.
#
# This program is also distributed with certain software (including
# but not limited to OpenSSL) that is licensed under separate terms,
# as designated in a particular file or component or in included license
# documentation.  The authors of MySQL hereby grant you an
# additional permission to link the program and your derivative works
# with the separately licensed software that they have included with
# MySQL.
#
# Without limiting anything contained in the foregoing, this file,
# which is part of MySQL Connector/Python, is also subject to the
# Universal FOSS Exception, version 1.0, a copy of which can be found at
# http://oss.oracle.com/licenses/universal-foss-exception.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License, version 2.0, for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA

"""Implementation of the X protocol for MySQL servers."""

import struct

from .compat import STRING_TYPES, INT_TYPES
from .errors import (InterfaceError, NotSupportedError, OperationalError,
                     ProgrammingError)
from .expr import (ExprParser, build_expr, build_scalar, build_bool_scalar,
                   build_int_scalar, build_unsigned_int_scalar)
from .helpers import encode_to_bytes, get_item_or_attr
from .result import Column
from .protobuf import (CRUD_PREPARE_MAPPING, SERVER_MESSAGES,
                       PROTOBUF_REPEATED_TYPES, Message, mysqlxpb_enum)


class MessageReaderWriter(object):
    """Implements a Message Reader/Writer.

    Args:
        socket_stream (mysqlx.connection.SocketStream): `SocketStream` object.
    """
    def __init__(self, socket_stream):
        self._stream = socket_stream
        self._msg = None

    def _read_message(self):
        """Read message.

        Raises:
            :class:`mysqlx.ProgrammingError`: If e connected server does not
                                              have the MySQL X protocol plugin
                                              enabled.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        hdr = self._stream.read(5)
        msg_len, msg_type = struct.unpack("<LB", hdr)
        if msg_type == 10:
            raise ProgrammingError("The connected server does not have the "
                                   "MySQL X protocol plugin enabled or protocol"
                                   "mismatch")
        payload = self._stream.read(msg_len - 1)
        msg_type_name = SERVER_MESSAGES.get(msg_type)
        if not msg_type_name:
            raise ValueError("Unknown msg_type: {0}".format(msg_type))
        # Do not parse empty notices, Message requires a type in payload.
        if msg_type == 11 and payload == b"":
            return self._read_message()
        else:
            try:
                msg = Message.from_server_message(msg_type, payload)
            except RuntimeError:
                return self._read_message()
        return msg

    def read_message(self):
        """Read message.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        if self._msg is not None:
            msg = self._msg
            self._msg = None
            return msg
        return self._read_message()

    def push_message(self, msg):
        """Push message.

        Args:
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.

        Raises:
            :class:`mysqlx.OperationalError`: If message push slot is full.
        """
        if self._msg is not None:
            raise OperationalError("Message push slot is full")
        self._msg = msg

    def write_message(self, msg_id, msg):
        """Write message.

        Args:
            msg_id (int): The message ID.
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.
        """
        msg_str = encode_to_bytes(msg.serialize_to_string())
        header = struct.pack("<LB", len(msg_str) + 1, msg_id)
        self._stream.sendall(b"".join([header, msg_str]))


class Protocol(object):
    """Implements the MySQL X Protocol.

    Args:
        read_writer (mysqlx.protocol.MessageReaderWriter): A Message \
            Reader/Writer object.
    """
    def __init__(self, reader_writer):
        self._reader = reader_writer
        self._writer = reader_writer
        self._message = None

    def _apply_filter(self, msg, stmt):
        """Apply filter.

        Args:
            msg (mysqlx.protobuf.Message): The MySQL X Protobuf Message.
            stmt (Statement): A `Statement` based type object.
        """
        if stmt.has_where:
            msg["criteria"] = stmt.get_where_expr()
        if stmt.has_sort:
            msg["order"].extend(stmt.get_sort_expr())
        if stmt.has_group_by:
            msg["grouping"].extend(stmt.get_grouping())
        if stmt.has_having:
            msg["grouping_criteria"] = stmt.get_having()

    def _create_any(self, arg):
        """Create any.

        Args:
            arg (object): Arbitrary object.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        if isinstance(arg, STRING_TYPES):
            value = Message("Mysqlx.Datatypes.Scalar.String", value=arg)
            scalar = Message("Mysqlx.Datatypes.Scalar", type=8, v_string=value)
            return Message("Mysqlx.Datatypes.Any", type=1, scalar=scalar)
        elif isinstance(arg, bool):
            return Message("Mysqlx.Datatypes.Any", type=1,
                           scalar=build_bool_scalar(arg))
        elif isinstance(arg, INT_TYPES):
            if arg < 0:
                return Message("Mysqlx.Datatypes.Any", type=1,
                               scalar=build_int_scalar(arg))
            return Message("Mysqlx.Datatypes.Any", type=1,
                           scalar=build_unsigned_int_scalar(arg))

        elif isinstance(arg, tuple) and len(arg) == 2:
            arg_key, arg_value = arg
            obj_fld = Message("Mysqlx.Datatypes.Object.ObjectField",
                              key=arg_key, value=self._create_any(arg_value))
            obj = Message("Mysqlx.Datatypes.Object",
                          fld=[obj_fld.get_message()])
            return Message("Mysqlx.Datatypes.Any", type=2, obj=obj)

        elif isinstance(arg, dict) or (isinstance(arg, (list, tuple)) and
                                       isinstance(arg[0], dict)):
            array_values = []
            for items in arg:
                obj_flds = []
                for key, value in items.items():
                    # Array can only handle Any types, Mysqlx.Datatypes.Any.obj
                    obj_fld = Message("Mysqlx.Datatypes.Object.ObjectField",
                                      key=key, value=self._create_any(value))
                    obj_flds.append(obj_fld.get_message())
                msg_obj = Message("Mysqlx.Datatypes.Object", fld=obj_flds)
                msg_any = Message("Mysqlx.Datatypes.Any", type=2, obj=msg_obj)
                array_values.append(msg_any.get_message())

            msg = Message("Mysqlx.Datatypes.Array")
            msg["value"] = array_values
            return Message("Mysqlx.Datatypes.Any", type=3, array=msg)

        return None

    def _get_binding_args(self, stmt, is_scalar=True):
        """Returns the binding any/scalar.

        Args:
            stmt (Statement): A `Statement` based type object.
            is_scalar (bool): `True` to return scalar values.

        Raises:
            :class:`mysqlx.ProgrammingError`: If unable to find placeholder for
                                              parameter.

        Returns:
            list: A list of ``Any`` or ``Scalar`` objects.
        """
        build_value = lambda value: build_scalar(value).get_message() \
            if is_scalar else self._create_any(value).get_message()
        bindings = stmt.get_bindings()
        binding_map = stmt.get_binding_map()

        # If binding_map is None it's a SqlStatement object
        if binding_map is None:
            return [build_value(value) for value in bindings]

        count = len(binding_map)
        args = count * [None]
        if count != len(bindings):
            raise ProgrammingError("The number of bind parameters and "
                                   "placeholders do not match")
        for name, value in bindings.items():
            if name not in binding_map:
                raise ProgrammingError("Unable to find placeholder for "
                                       "parameter: {0}".format(name))
            pos = binding_map[name]
            args[pos] = build_value(value)
        return args

    def _process_frame(self, msg, result):
        """Process frame.

        Args:
            msg (mysqlx.protobuf.Message): A MySQL X Protobuf Message.
            result (Result): A `Result` based type object.
        """
        if msg["type"] == 1:
            warn_msg = Message.from_message("Mysqlx.Notice.Warning",
                                            msg["payload"])
            result.append_warning(warn_msg.level, warn_msg.code, warn_msg.msg)
        elif msg["type"] == 2:
            Message.from_message("Mysqlx.Notice.SessionVariableChanged",
                                 msg["payload"])
        elif msg["type"] == 3:
            sess_state_msg = Message.from_message(
                "Mysqlx.Notice.SessionStateChanged", msg["payload"])
            if sess_state_msg["param"] == mysqlxpb_enum(
                    "Mysqlx.Notice.SessionStateChanged.Parameter."
                    "GENERATED_DOCUMENT_IDS"):
                result.set_generated_ids(
                    [get_item_or_attr(
                        get_item_or_attr(value, 'v_octets'), 'value').decode()
                     for value in sess_state_msg["value"]])
            else:  # Following results are unitary and not a list
                sess_state_value = sess_state_msg["value"][0] \
                    if isinstance(sess_state_msg["value"],
                                  tuple(PROTOBUF_REPEATED_TYPES)) \
                    else sess_state_msg["value"]
                if sess_state_msg["param"] == mysqlxpb_enum(
                        "Mysqlx.Notice.SessionStateChanged.Parameter."
                        "ROWS_AFFECTED"):
                    result.set_rows_affected(
                        get_item_or_attr(sess_state_value, "v_unsigned_int"))
                elif sess_state_msg["param"] == mysqlxpb_enum(
                        "Mysqlx.Notice.SessionStateChanged.Parameter."
                        "GENERATED_INSERT_ID"):
                    result.set_generated_insert_id(get_item_or_attr(
                        sess_state_value, "v_unsigned_int"))

    def _read_message(self, result):
        """Read message.

        Args:
            result (Result): A `Result` based type object.
        """
        while True:
            msg = self._reader.read_message()
            if msg.type == "Mysqlx.Error":
                raise OperationalError(msg["msg"], msg["code"])
            elif msg.type == "Mysqlx.Notice.Frame":
                try:
                    self._process_frame(msg, result)
                except:
                    continue
            elif msg.type == "Mysqlx.Sql.StmtExecuteOk":
                return None
            elif msg.type == "Mysqlx.Resultset.FetchDone":
                result.set_closed(True)
            elif msg.type == "Mysqlx.Resultset.FetchDoneMoreResultsets":
                result.set_has_more_results(True)
            elif msg.type == "Mysqlx.Resultset.Row":
                result.set_has_data(True)
                break
            else:
                break
        return msg

    def get_capabilites(self):
        """Get capabilities.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        msg = Message("Mysqlx.Connection.CapabilitiesGet")
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CON_CAPABILITIES_GET"),
            msg)
        msg = self._reader.read_message()
        while msg.type == "Mysqlx.Notice.Frame":
            msg = self._reader.read_message()
        return msg

    def set_capabilities(self, **kwargs):
        """Set capabilities.

        Args:
            **kwargs: Arbitrary keyword arguments.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        capabilities = Message("Mysqlx.Connection.Capabilities")
        for key, value in kwargs.items():
            capability = Message("Mysqlx.Connection.Capability")
            capability["name"] = key
            capability["value"] = self._create_any(value)
            capabilities["capabilities"].extend([capability.get_message()])
        msg = Message("Mysqlx.Connection.CapabilitiesSet")
        msg["capabilities"] = capabilities
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CON_CAPABILITIES_SET"),
            msg)
        return self.read_ok()

    def send_auth_start(self, method, auth_data=None, initial_response=None):
        """Send authenticate start.

        Args:
            method (str): Message method.
            auth_data (Optional[str]): Authentication data.
            initial_response (Optional[str]): Initial response.
        """
        msg = Message("Mysqlx.Session.AuthenticateStart")
        msg["mech_name"] = method
        if auth_data is not None:
            msg["auth_data"] = auth_data
        if initial_response is not None:
            msg["initial_response"] = initial_response
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_AUTHENTICATE_START"), msg)

    def read_auth_continue(self):
        """Read authenticate continue.

        Raises:
            :class:`InterfaceError`: If the message type is not
                                     `Mysqlx.Session.AuthenticateContinue`

        Returns:
            str: The authentication data.
        """
        msg = self._reader.read_message()
        while msg.type == "Mysqlx.Notice.Frame":
            msg = self._reader.read_message()
        if msg.type != "Mysqlx.Session.AuthenticateContinue":
            raise InterfaceError("Unexpected message encountered during "
                                 "authentication handshake")
        return msg["auth_data"]

    def send_auth_continue(self, auth_data):
        """Send authenticate continue.

        Args:
            auth_data (str): Authentication data.
        """
        msg = Message("Mysqlx.Session.AuthenticateContinue",
                      auth_data=auth_data)
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_AUTHENTICATE_CONTINUE"), msg)

    def read_auth_ok(self):
        """Read authenticate OK.

        Raises:
            :class:`mysqlx.InterfaceError`: If message type is `Mysqlx.Error`.
        """
        while True:
            msg = self._reader.read_message()
            if msg.type == "Mysqlx.Session.AuthenticateOk":
                break
            if msg.type == "Mysqlx.Error":
                raise InterfaceError(msg.msg)

    def send_prepare_prepare(self, msg_type, msg, stmt):
        """
        Send prepare statement.

        Args:
            msg_type (str): Message ID string.
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.
            stmt (Statement): A `Statement` based type object.

        Raises:
            :class:`mysqlx.NotSupportedError`: If prepared statements are not
                                               supported.

        .. versionadded:: 8.0.16
        """
        if stmt.has_limit and msg.type != "Mysqlx.Crud.Insert":
            # Remove 'limit' from message by building a new one
            if msg.type == "Mysqlx.Crud.Find":
                _, msg = self.build_find(stmt)
            elif msg.type == "Mysqlx.Crud.Update":
                _, msg = self.build_update(stmt)
            elif msg.type == "Mysqlx.Crud.Delete":
                _, msg = self.build_delete(stmt)
            else:
                raise ValueError("Invalid message type: {}".format(msg_type))
            # Build 'limit_expr' message
            position = len(stmt.get_bindings())
            placeholder = mysqlxpb_enum("Mysqlx.Expr.Expr.Type.PLACEHOLDER")
            msg_limit_expr = Message("Mysqlx.Crud.LimitExpr")
            msg_limit_expr["row_count"] = Message("Mysqlx.Expr.Expr",
                                                  type=placeholder,
                                                  position=position)
            if msg.type == "Mysqlx.Crud.Find":
                msg_limit_expr["offset"] = Message("Mysqlx.Expr.Expr",
                                                   type=placeholder,
                                                   position=position + 1)
            msg["limit_expr"] = msg_limit_expr

        oneof_type, oneof_op = CRUD_PREPARE_MAPPING[msg_type]
        msg_oneof = Message("Mysqlx.Prepare.Prepare.OneOfMessage")
        msg_oneof["type"] = mysqlxpb_enum(oneof_type)
        msg_oneof[oneof_op] = msg
        msg_prepare = Message("Mysqlx.Prepare.Prepare")
        msg_prepare["stmt_id"] = stmt.stmt_id
        msg_prepare["stmt"] = msg_oneof

        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.PREPARE_PREPARE"),
            msg_prepare)

        try:
            self.read_ok()
        except InterfaceError:
            raise NotSupportedError

    def send_prepare_execute(self, msg_type, msg, stmt):
        """
        Send execute statement.

        Args:
            msg_type (str): Message ID string.
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.
            stmt (Statement): A `Statement` based type object.

        .. versionadded:: 8.0.16
        """
        oneof_type, oneof_op = CRUD_PREPARE_MAPPING[msg_type]
        msg_oneof = Message("Mysqlx.Prepare.Prepare.OneOfMessage")
        msg_oneof["type"] = mysqlxpb_enum(oneof_type)
        msg_oneof[oneof_op] = msg
        msg_execute = Message("Mysqlx.Prepare.Execute")
        msg_execute["stmt_id"] = stmt.stmt_id

        args = self._get_binding_args(stmt, is_scalar=False)
        if args:
            msg_execute["args"].extend(args)

        if stmt.has_limit:
            msg_execute["args"].extend([
                self._create_any(stmt.get_limit_row_count()).get_message(),
                self._create_any(stmt.get_limit_offset()).get_message()
            ])

        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.PREPARE_EXECUTE"),
            msg_execute)

    def send_prepare_deallocate(self, stmt_id):
        """
        Send prepare deallocate statement.

        Args:
            stmt_id (int): Statement ID.

        .. versionadded:: 8.0.16
        """
        msg_dealloc = Message("Mysqlx.Prepare.Deallocate")
        msg_dealloc["stmt_id"] = stmt_id
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.PREPARE_DEALLOCATE"),
            msg_dealloc)
        self.read_ok()

    def send_msg_without_ps(self, msg_type, msg, stmt):
        """
        Send a message without prepared statements support.

        Args:
            msg_type (str): Message ID string.
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.
            stmt (Statement): A `Statement` based type object.

        .. versionadded:: 8.0.16
        """
        if stmt.has_limit:
            msg_limit = Message("Mysqlx.Crud.Limit")
            msg_limit["row_count"] = stmt.get_limit_row_count()
            if msg.type == "Mysqlx.Crud.Find":
                msg_limit["offset"] = stmt.get_limit_offset()
            msg["limit"] = msg_limit
        is_scalar = False \
            if msg_type == "Mysqlx.ClientMessages.Type.SQL_STMT_EXECUTE" \
               else True
        args = self._get_binding_args(stmt, is_scalar=is_scalar)
        if args:
            msg["args"].extend(args)
        self.send_msg(msg_type, msg)

    def send_msg(self, msg_type, msg):
        """
        Send a message.

        Args:
            msg_type (str): Message ID string.
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.

        .. versionadded:: 8.0.16
        """
        self._writer.write_message(mysqlxpb_enum(msg_type), msg)

    def build_find(self, stmt):
        """Build find/read message.

        Args:
            stmt (Statement): A :class:`mysqlx.ReadStatement` or
                              :class:`mysqlx.FindStatement` object.

        Returns:
            (tuple): Tuple containing:

                * `str`: Message ID string.
                * :class:`mysqlx.protobuf.Message`: MySQL X Protobuf Message.

        .. versionadded:: 8.0.16
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection",
                             name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Find", data_model=data_model,
                      collection=collection)
        if stmt.has_projection:
            msg["projection"] = stmt.get_projection_expr()
        self._apply_filter(msg, stmt)

        if stmt.is_lock_exclusive():
            msg["locking"] = \
                mysqlxpb_enum("Mysqlx.Crud.Find.RowLock.EXCLUSIVE_LOCK")
        elif stmt.is_lock_shared():
            msg["locking"] = \
                mysqlxpb_enum("Mysqlx.Crud.Find.RowLock.SHARED_LOCK")

        if stmt.lock_contention > 0:
            msg["locking_options"] = stmt.lock_contention

        return "Mysqlx.ClientMessages.Type.CRUD_FIND", msg

    def build_update(self, stmt):
        """Build update message.

        Args:
            stmt (Statement): A :class:`mysqlx.ModifyStatement` or
                              :class:`mysqlx.UpdateStatement` object.

        Returns:
            (tuple): Tuple containing:

                * `str`: Message ID string.
                * :class:`mysqlx.protobuf.Message`: MySQL X Protobuf Message.

        .. versionadded:: 8.0.16
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection",
                             name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Update", data_model=data_model,
                      collection=collection)
        self._apply_filter(msg, stmt)
        for _, update_op in stmt.get_update_ops().items():
            operation = Message("Mysqlx.Crud.UpdateOperation")
            operation["operation"] = update_op.update_type
            operation["source"] = update_op.source
            if update_op.value is not None:
                operation["value"] = build_expr(update_op.value)
            msg["operation"].extend([operation.get_message()])

        return "Mysqlx.ClientMessages.Type.CRUD_UPDATE", msg

    def build_delete(self, stmt):
        """Build delete message.

        Args:
            stmt (Statement): A :class:`mysqlx.DeleteStatement` or
                              :class:`mysqlx.RemoveStatement` object.

        Returns:
            (tuple): Tuple containing:

                * `str`: Message ID string.
                * :class:`mysqlx.protobuf.Message`: MySQL X Protobuf Message.

        .. versionadded:: 8.0.16
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection", name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Delete", data_model=data_model,
                      collection=collection)
        self._apply_filter(msg, stmt)
        return "Mysqlx.ClientMessages.Type.CRUD_DELETE", msg

    def build_execute_statement(self, namespace, stmt, args):
        """Build execute statement.

        Args:
            namespace (str): The namespace.
            stmt (Statement): A `Statement` based type object.
            args (iterable): An iterable object.

        Returns:
            (tuple): Tuple containing:

                * `str`: Message ID string.
                * :class:`mysqlx.protobuf.Message`: MySQL X Protobuf Message.

        .. versionadded:: 8.0.16
        """
        msg = Message("Mysqlx.Sql.StmtExecute", namespace=namespace, stmt=stmt,
                      compact_metadata=False)

        if namespace == "mysqlx":
            # mysqlx namespace behavior: one object with a list of arguments
            items = args[0].items() if isinstance(args, (list, tuple)) else \
                    args.items()
            obj_flds = []
            for key, value in items:
                obj_fld = Message("Mysqlx.Datatypes.Object.ObjectField",
                                  key=key, value=self._create_any(value))
                obj_flds.append(obj_fld.get_message())
            msg_obj = Message("Mysqlx.Datatypes.Object", fld=obj_flds)
            msg_any = Message("Mysqlx.Datatypes.Any", type=2, obj=msg_obj)
            msg["args"] = [msg_any.get_message()]
        else:
            # xplugin namespace behavior: list of arguments
            for arg in args:
                value = self._create_any(arg)
                msg["args"].extend([value.get_message()])

        return "Mysqlx.ClientMessages.Type.SQL_STMT_EXECUTE", msg

    def build_insert(self, stmt):
        """Build insert statement.

        Args:
            stmt (Statement): A :class:`mysqlx.AddStatement` or
                              :class:`mysqlx.InsertStatement` object.

        Returns:
            (tuple): Tuple containing:

                * `str`: Message ID string.
                * :class:`mysqlx.protobuf.Message`: MySQL X Protobuf Message.

        .. versionadded:: 8.0.16
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection",
                             name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Insert", data_model=data_model,
                      collection=collection)

        if hasattr(stmt, "_fields"):
            for field in stmt._fields:
                expr = ExprParser(field, not stmt.is_doc_based()) \
                    .parse_table_insert_field()
                msg["projection"].extend([expr.get_message()])

        for value in stmt.get_values():
            row = Message("Mysqlx.Crud.Insert.TypedRow")
            if isinstance(value, list):
                for val in value:
                    row["field"].extend([build_expr(val).get_message()])
            else:
                row["field"].extend([build_expr(value).get_message()])
            msg["row"].extend([row.get_message()])

        if hasattr(stmt, "is_upsert"):
            msg["upsert"] = stmt.is_upsert()

        return "Mysqlx.ClientMessages.Type.CRUD_INSERT", msg

    def close_result(self, result):
        """Close the result.

        Args:
            result (Result): A `Result` based type object.

        Raises:
            :class:`mysqlx.OperationalError`: If message read is None.
        """
        msg = self._read_message(result)
        if msg is not None:
            raise OperationalError("Expected to close the result")

    def read_row(self, result):
        """Read row.

        Args:
            result (Result): A `Result` based type object.
        """
        msg = self._read_message(result)
        if msg is None:
            return None
        if msg.type == "Mysqlx.Resultset.Row":
            return msg
        self._reader.push_message(msg)
        return None

    def get_column_metadata(self, result):
        """Returns column metadata.

        Args:
            result (Result): A `Result` based type object.

        Raises:
            :class:`mysqlx.InterfaceError`: If unexpected message.
        """
        columns = []
        while True:
            msg = self._read_message(result)
            if msg is None:
                break
            if msg.type == "Mysqlx.Resultset.Row":
                self._reader.push_message(msg)
                break
            if msg.type != "Mysqlx.Resultset.ColumnMetaData":
                raise InterfaceError("Unexpected msg type")
            col = Column(msg["type"], msg["catalog"], msg["schema"],
                         msg["table"], msg["original_table"],
                         msg["name"], msg["original_name"],
                         msg.get("length", 21),
                         msg.get("collation", 0),
                         msg.get("fractional_digits", 0),
                         msg.get("flags", 16),
                         msg.get("content_type"))
            columns.append(col)
        return columns

    def read_ok(self):
        """Read OK.

        Raises:
            :class:`mysqlx.InterfaceError`: If unexpected message.
        """
        msg = self._reader.read_message()
        if msg.type == "Mysqlx.Error":
            raise InterfaceError("Mysqlx.Error: {}".format(msg["msg"]))
        if msg.type != "Mysqlx.Ok":
            raise InterfaceError("Unexpected message encountered: {}"
                                 "".format(msg["msg"]))

    def send_connection_close(self):
        """Send connection close."""
        msg = Message("Mysqlx.Connection.Close")
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.CON_CLOSE"), msg)

    def send_close(self):
        """Send close."""
        msg = Message("Mysqlx.Session.Close")
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_CLOSE"), msg)

    def send_reset(self):
        """Send reset."""
        msg = Message("Mysqlx.Session.Reset")
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_RESET"), msg)
