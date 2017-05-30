import gzip
from datetime import timedelta
from threading import RLock

from .. import helpers as utils
from ..crypto import AES
from ..errors import (BadMessageError, FloodWaitError,
                      RPCError, InvalidDCError)
from ..tl.all_tlobjects import tlobjects
from ..tl.types import MsgsAck
from ..utils import BinaryReader, BinaryWriter

import logging
logging.getLogger(__name__).addHandler(logging.NullHandler())


class MtProtoSender:
    """MTProto Mobile Protocol sender (https://core.telegram.org/mtproto/description)"""

    def __init__(self, transport, session):
        self._transport = transport
        self.session = session
        self._logger = logging.getLogger(__name__)

        self._need_confirmation = []  # Message IDs that need confirmation

        # Store an RLock instance to make this class safely multi-threaded
        self._lock = RLock()

        # Used when logging out, the only request that seems to use 'ack' requests
        # TODO There might be a better way to handle msgs_ack requests
        self.logging_out = False

    def connect(self):
        """Connects to the server"""
        self._transport.connect()

    def disconnect(self):
        """Disconnects from the server"""
        self._transport.close()

    def _generate_sequence(self, confirmed):
        """Generates the next sequence number, based on whether it
           was confirmed yet or not"""
        if confirmed:
            result = self.session.sequence * 2 + 1
            self.session.sequence += 1
            return result
        else:
            return self.session.sequence * 2

    # region Send and receive

    def send(self, request):
        """Sends the specified MTProtoRequest, previously sending any message
           which needed confirmation."""

        # Now only us can be using this method
        with self._lock:
            self._logger.debug('send() acquired the lock')

            # If any message needs confirmation send an AckRequest first
            if self._need_confirmation:
                msgs_ack = MsgsAck(self._need_confirmation)
                with BinaryWriter() as writer:
                    msgs_ack.on_send(writer)
                    self._send_packet(writer.get_bytes(), msgs_ack)

                del self._need_confirmation[:]

            # Finally send our packed request
            with BinaryWriter() as writer:
                request.on_send(writer)
                self._send_packet(writer.get_bytes(), request)

            # And update the saved session
            self.session.save()

        self._logger.debug('send() released the lock')

    def receive(self, request=None, timeout=timedelta(seconds=5), updates=None):
        """Receives the specified MTProtoRequest ("fills in it"
           the received data). This also restores the updates thread.
           An optional timeout can be specified to cancel the operation
           if no data has been read after its time delta.

           If 'request' is None, a single item will be read into
           the 'updates' list (which cannot be None).

           If 'request' is not None, any update received before
           reading the request's result will be put there unless
           it's None, in which case updates will be ignored.
        """
        if request is None and updates is None:
            raise ValueError('Both the "request" and "updates"'
                             'parameters cannot be None at the same time.')

        with self._lock:
            self._logger.debug('receive() acquired the lock')
            # Don't stop trying to receive until we get the request we wanted
            # or, if there is no request, until we read an update
            while True:
                self._logger.info('Trying to .receive() the request result...')
                seq, body = self._transport.receive(timeout)
                message, remote_msg_id, remote_sequence = self._decode_msg(body)

                with BinaryReader(message) as reader:
                    self._process_msg(remote_msg_id, remote_sequence, reader,
                                      request, updates)

                if request is None:
                    if updates:
                        break  # No request but one update read, exit
                elif request.confirm_received:
                        break  # Request, and result read, exit

            self._logger.info('Request result received')
        self._logger.debug('receive() released the lock')

    def receive_update(self, timeout=timedelta(seconds=5)):
        """Receives an update object and returns its result"""
        updates = []
        self.receive(timeout=timeout, updates=updates)
        return updates[0]

    def cancel_receive(self):
        """Cancels any pending receive operation
           by raising a ReadCancelledError"""
        self._transport.cancel_receive()

    # endregion

    # region Low level processing

    def _send_packet(self, packet, request):
        """Sends the given packet bytes with the additional
           information of the original request. This does NOT lock the threads!"""
        request.msg_id = self.session.get_new_msg_id()

        # First calculate plain_text to encrypt it
        with BinaryWriter() as plain_writer:
            plain_writer.write_long(self.session.salt, signed=False)
            plain_writer.write_long(self.session.id, signed=False)
            plain_writer.write_long(request.msg_id)
            plain_writer.write_int(self._generate_sequence(request.confirmed))
            plain_writer.write_int(len(packet))
            plain_writer.write(packet)

            msg_key = utils.calc_msg_key(plain_writer.get_bytes())

            key, iv = utils.calc_key(self.session.auth_key.key, msg_key, True)
            cipher_text = AES.encrypt_ige(plain_writer.get_bytes(), key, iv)

        # And then finally send the encrypted packet
        with BinaryWriter() as cipher_writer:
            cipher_writer.write_long(
                self.session.auth_key.key_id, signed=False)
            cipher_writer.write(msg_key)
            cipher_writer.write(cipher_text)
            self._transport.send(cipher_writer.get_bytes())

    def _decode_msg(self, body):
        """Decodes an received encrypted message body bytes"""
        message = None
        remote_msg_id = None
        remote_sequence = None

        with BinaryReader(body) as reader:
            if len(body) < 8:
                raise BufferError("Can't decode packet ({})".format(body))

            # TODO Check for both auth key ID and msg_key correctness
            reader.read_long()  # remote_auth_key_id
            msg_key = reader.read(16)

            key, iv = utils.calc_key(self.session.auth_key.key, msg_key, False)
            plain_text = AES.decrypt_ige(
                reader.read(len(body) - reader.tell_position()), key, iv)

            with BinaryReader(plain_text) as plain_text_reader:
                plain_text_reader.read_long()  # remote_salt
                plain_text_reader.read_long()  # remote_session_id
                remote_msg_id = plain_text_reader.read_long()
                remote_sequence = plain_text_reader.read_int()
                msg_len = plain_text_reader.read_int()
                message = plain_text_reader.read(msg_len)

        return message, remote_msg_id, remote_sequence

    def _process_msg(self, msg_id, sequence, reader, request, updates):
        """Processes and handles a Telegram message"""

        # TODO Check salt, session_id and sequence_number
        self._need_confirmation.append(msg_id)

        code = reader.read_int(signed=False)
        reader.seek(-4)

        # The following codes are "parsed manually"
        if code == 0xf35c6d01:  # rpc_result, (response of an RPC call, i.e., we sent a request)
            return self._handle_rpc_result(
                msg_id, sequence, reader, request)

        if code == 0x347773c5:  # pong
            return self._handle_pong(
                msg_id, sequence, reader, request)

        if code == 0x73f1f8dc:  # msg_container
            return self._handle_container(
                msg_id, sequence, reader, request, updates)

        if code == 0x3072cfa1:  # gzip_packed
            return self._handle_gzip_packed(
                msg_id, sequence, reader, request, updates)

        if code == 0xedab447b:  # bad_server_salt
            return self._handle_bad_server_salt(
                msg_id, sequence, reader, request)

        if code == 0xa7eff811:  # bad_msg_notification
            return self._handle_bad_msg_notification(
                msg_id, sequence, reader)

        # msgs_ack, it may handle the request we wanted
        if code == 0x62d6b459:
            ack = reader.tgread_object()
            if request and request.msg_id in ack.msg_ids:
                self._logger.warning('Ack found for the current request ID')

                if self.logging_out:
                    self._logger.info('Message ack confirmed the logout request')
                    request.confirm_received = True

            return False

        # If the code is not parsed manually, then it was parsed by the code generator!
        # In this case, we will simply treat the incoming TLObject as an Update,
        # if we can first find a matching TLObject
        if code in tlobjects:
            result = reader.tgread_object()
            if updates is None:
                self._logger.debug('Ignored update for %s', repr(result))
            else:
                self._logger.debug('Read update for %s', repr(result))
                updates.append(result)

            return False

        self._logger.warning('Unknown message: {}'.format(hex(code)))
        return False

    # endregion

    # region Message handling

    def _handle_pong(self, msg_id, sequence, reader, request):
        self._logger.debug('Handling pong')
        reader.read_int(signed=False)  # code
        received_msg_id = reader.read_long(signed=False)

        if received_msg_id == request.msg_id:
            self._logger.warning('Pong confirmed a request')
            request.confirm_received = True

        return False

    def _handle_container(self, msg_id, sequence, reader, request, updates):
        self._logger.debug('Handling container')
        reader.read_int(signed=False)  # code
        size = reader.read_int()
        for _ in range(size):
            inner_msg_id = reader.read_long(signed=False)
            reader.read_int()  # inner_sequence
            inner_length = reader.read_int()
            begin_position = reader.tell_position()

            # Note that this code is IMPORTANT for skipping RPC results of
            # lost requests (i.e., ones from the previous connection session)
            if not self._process_msg(
                    inner_msg_id, sequence, reader, request, updates):
                reader.set_position(begin_position + inner_length)

        return False

    def _handle_bad_server_salt(self, msg_id, sequence, reader, request):
        self._logger.debug('Handling bad server salt')
        reader.read_int(signed=False)  # code
        reader.read_long(signed=False)  # bad_msg_id
        reader.read_int()  # bad_msg_seq_no
        reader.read_int()  # error_code
        new_salt = reader.read_long(signed=False)

        self.session.salt = new_salt

        if request is None:
            raise ValueError(
                'Tried to handle a bad server salt with no request specified')

        # Resend
        self.send(request)

        return True

    def _handle_bad_msg_notification(self, msg_id, sequence, reader):
        self._logger.debug('Handling bad message notification')
        reader.read_int(signed=False)  # code
        reader.read_long(signed=False)  # request_id
        reader.read_int()  # request_sequence

        error_code = reader.read_int()
        error = BadMessageError(error_code)
        if error_code in (16, 17):
            # sent msg_id too low or too high (respectively).
            # Use the current msg_id to determine the right time offset.
            self.session.update_time_offset(correct_msg_id=msg_id)
            self.session.save()
            self._logger.warning('Read Bad Message error: ' + str(error))
            self._logger.info('Attempting to use the correct time offset.')
        else:
            raise error

    def _handle_rpc_result(self, msg_id, sequence, reader, request):
        self._logger.debug('Handling RPC result, request is%s None', ' not' if request else '')
        reader.read_int(signed=False)  # code
        request_id = reader.read_long(signed=False)
        inner_code = reader.read_int(signed=False)

        if request and request_id == request.msg_id:
            request.confirm_received = True

        if inner_code == 0x2144ca19:  # RPC Error
            error = RPCError(
                code=reader.read_int(), message=reader.tgread_string())

            self._logger.warning('Read RPC error: %s', str(error))
            if error.must_resend:
                if not request:
                    raise ValueError(
                        'The previously sent request must be resent. '
                        'However, no request was previously sent '
                        '(possibly called from a different thread).')
                request.confirm_received = False

            if error.message.startswith('FLOOD_WAIT_'):
                self._updates_thread_sleep = error.additional_data
                raise FloodWaitError(seconds=error.additional_data)

            elif '_MIGRATE_' in error.message:
                raise InvalidDCError(error)

            else:
                raise error
        else:
            if not request:
                raise ValueError(
                    'The request needed to read this RPC result was not '
                    'found (possibly called receive() from another thread).')

            self._logger.debug('Reading request response')
            if inner_code == 0x3072cfa1:  # GZip packed
                unpacked_data = gzip.decompress(reader.tgread_bytes())
                with BinaryReader(unpacked_data) as compressed_reader:
                    request.on_response(compressed_reader)
            else:
                reader.seek(-4)
                if request_id == request.msg_id:
                    request.on_response(reader)
                else:
                    # note: if it's really a result for RPC from previous connection
                    # session, it will be skipped by the handle_container()
                    self._logger.warning('RPC result found for unknown request (maybe from previous connection session)')

    def _handle_gzip_packed(self, msg_id, sequence, reader, request, updates):
        self._logger.debug('Handling gzip packed data')
        reader.read_int(signed=False)  # code
        packed_data = reader.tgread_bytes()
        unpacked_data = gzip.decompress(packed_data)

        with BinaryReader(unpacked_data) as compressed_reader:
            return self._process_msg(msg_id, sequence, compressed_reader,
                                     request, updates)

    # endregion