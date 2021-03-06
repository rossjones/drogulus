# -*- coding: utf-8 -*-
"""
Ensures code that represents a local node in the DHT network works as
expected
"""
from drogulus.dht.node import response_timeout, Lookup, Node
from drogulus.constants import (ERRORS, RPC_TIMEOUT, RESPONSE_TIMEOUT,
                                REPLICATE_INTERVAL)
from drogulus.dht.contact import Contact
from drogulus.version import get_version
from drogulus.net.protocol import DHTFactory
from drogulus.net.messages import (Error, Ping, Pong, Store, FindNode, Nodes,
                                   FindValue, Value)
from drogulus.crypto import construct_key
from twisted.trial import unittest
from twisted.test import proto_helpers
from twisted.python import log
from twisted.internet import defer, task, reactor
from twisted.python.failure import Failure
from mock import MagicMock, patch
from uuid import uuid4
import time


# Useful throw-away constants for testing purposes.
PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIICXgIBAAKBgQC+n3Au1cbSkjCVsrfnTbmA0SwQLN2RbbDIMHILA1i6wByXkqEa
mnEBvgsOkUUrsEXYtt0vb8Qill4LSs9RqTetSCjGb+oGVTKizfbMbGCKZ8fT64ZZ
gan9TvhItl7DAwbIXcyvQ+b1J7pHaytAZwkSwh+M6WixkMTbFM91fW0mUwIDAQAB
AoGBAJvBENvj5wH1W2dl0ShY9MLRpuxMjHogo3rfQr/G60AkavhaYfKn0MB4tPYh
MuCgtmF+ATqaWytbq9oUNVPnLUqqn5M9N86+Gb6z8ld+AcR2BD8oZ6tQaiEIGzmi
L9AWEZZnyluDSHMXDoVrvDLxPpKW0yPjvQfWN15QF+H79faJAkEA0hgdueFrZf3h
os59ukzNzQy4gjL5ea35azbQt2jTc+lDOu+yjUic2O7Os7oxnSArpujDiOkYgaih
Dny+/bIgLQJBAOhGKjhpafdpgpr/BjRlmUHXLaa+Zrp/S4RtkIEkE9XXkmQjvVZ3
EyN/h0IVNBv45lDK0Qztjic0L1GON62Z8H8CQAcRkqZ3ZCKpWRceNXK4NNBqVibj
SiuC4/psfLc/CqZCueVYvTwtrkFKP6Aiaprrwyw5dqK7nPx3zPtszQxCGv0CQQDK
51BGiz94VAE1qQYgi4g/zdshSD6xODYd7yBGz99L9M77D4V8nPRpFCRyA9fLf7ii
ZyoLYxHFCX80fUoCKvG9AkEAyX5iCi3aoLYd/CvOFYB2fcXzauKrhopS7/NruDk/
LluSlW3qpi1BGDHVTeWWj2sm30NAybTHjNOX7OxEZ1yVwg==
-----END RSA PRIVATE KEY-----"""


PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC+n3Au1cbSkjCVsrfnTbmA0SwQ
LN2RbbDIMHILA1i6wByXkqEamnEBvgsOkUUrsEXYtt0vb8Qill4LSs9RqTetSCjG
b+oGVTKizfbMbGCKZ8fT64ZZgan9TvhItl7DAwbIXcyvQ+b1J7pHaytAZwkSwh+M
6WixkMTbFM91fW0mUwIDAQAB
-----END PUBLIC KEY-----"""


class FakeClient(object):
    """
    A class that pretends to be a client endpoint returned by Twisted's
    clientFromString. To be used with the mocks.
    """

    def __init__(self, protocol, success=True, timeout=False,
                 replace_cancel=False):
        """
        The protocol instance is set up as a fake by the test class. The
        success flag indicates if the client is to be able to work
        successfully. The timeout flag indicates if the deferred is to fire
        as if a connection has been made.
        """
        self.protocol = protocol
        self.success = success
        self.timeout = timeout
        self.replace_cancel = replace_cancel
        if replace_cancel:
            self.cancel_function = MagicMock()

    def connect(self, factory):
        d = defer.Deferred()
        if self.timeout:
            # This is a hack to ensure the cancel method is within scope of the
            # test function (as an attribute of the FakeClient object to
            # ensure it has fired. :-(
            if self.replace_cancel:
                d.cancel = self.cancel_function
            return d
        else:
            if self.success:
                d.callback(self.protocol)
            else:
                d.errback(Exception("Error!"))
            return d


def fakeAbortConnection():
    """
    Fakes the abortConnection method to be attached to the StringTransport used
    in the tests below.
    """
    pass


class TestTimeout(unittest.TestCase):
    """
    Ensures the timeout function works correctly.
    """

    def setUp(self):
        self.node_id = '1234567890abc'
        self.node = Node(self.node_id)
        self.factory = DHTFactory(self.node)
        self.protocol = self.factory.buildProtocol(('127.0.0.1', 0))
        self.transport = proto_helpers.StringTransport()
        self.protocol.makeConnection(self.transport)
        self.uuid = str(uuid4())

    def test_response_timeout(self):
        """
        Test the good case.
        """
        self.protocol.transport.abortConnection = MagicMock()
        self.node._routing_table.remove_contact = MagicMock()
        deferred = defer.Deferred()
        self.node._pending[self.uuid] = deferred
        # Create a simple Ping message.
        version = get_version()
        msg = Ping(self.uuid, self.node_id, version)
        response_timeout(msg, self.protocol, self.node)
        # The record associated with the uuid has been removed from the pending
        # dictionary.
        self.assertEqual({}, self.node._pending)
        # The deferred has been cancelled.
        self.assertIsInstance(deferred.result.value, defer.CancelledError)
        # abortConnection() has been called once.
        self.assertEqual(1, self.protocol.transport.abortConnection.call_count)
        # The remove_contact method of the routing table has been called once.
        self.node._routing_table.remove_contact.\
            assert_called_once_with(msg.node)

    def test_message_timout_missing(self):
        """
        Ensure no state is changed if the message's uuid is missing from the
        pending dict.
        """
        # There is no change in the number of messages in the pending
        # dictionary.
        self.node._pending[self.uuid] = 'a deferred'
        another_uuid = str(uuid4())
        version = get_version()
        msg = Ping(another_uuid, self.node_id, version)
        response_timeout(msg, self.protocol, self.node)
        self.assertIn(self.uuid, self.node._pending)


class TestLookup(unittest.TestCase):
    """
    Ensures the Lookup class works as expected.
    """

    def setUp(self):
        """
        Following the pattern explained here:

        http://twistedmatrix.com/documents/current/core/howto/trial.html
        """
        self.node_id = '1234567890abc'
        self.node = Node(self.node_id)
        self.factory = DHTFactory(self.node)
        self.protocol = self.factory.buildProtocol(('127.0.0.1', 0))
        self.transport = proto_helpers.StringTransport()
        self.transport.abortConnection = fakeAbortConnection
        self.protocol.makeConnection(self.transport)
        self.clock = task.Clock()
        reactor.callLater = self.clock.callLater
        self.value = 'value'
        self.signature = ('\x882f\xf9A\xcd\xf9\xb1\xcc\xdbl\x1c\xb2\xdb' +
                          '\xa3UQ\x9a\x08\x96\x12\x83^d\xd8M\xc2`\x81Hz' +
                          '\x84~\xf4\x9d\x0e\xbd\x81\xc4/\x94\x9dfg\xb2aq' +
                          '\xa6\xf8!k\x94\x0c\x9b\xb5\x8e \xcd\xfb\x87' +
                          '\x83`wu\xeb\xf2\x19\xd6X\xdd\xb3\x98\xb5\xbc#B' +
                          '\xe3\n\x85G\xb4\x9c\x9b\xb0-\xd2B\x83W\xb8\xca' +
                          '\xecv\xa9\xc4\x9d\xd8\xd0\xf1&\x1a\xfaw\xa0\x99' +
                          '\x1b\x84\xdad$\xebO\x1a\x9e:w\x14d_\xe3\x03#\x95' +
                          '\x9d\x10B\xe7\x13')
        self.uuid = str(uuid4())
        self.timestamp = 1350544046.084875
        self.expires = 1352221970.14242
        self.name = 'name'
        self.meta = {'meta': 'value'}
        self.version = get_version()
        self.key = construct_key(PUBLIC_KEY, self.name)
        self.timeout = 1000

    def test_init(self):
        """
        The simplest case - ensure the object is set up correctly.
        """
        lookup = Lookup(self.key, FindNode, self.node)
        self.assertIsInstance(lookup, defer.Deferred)
        self.assertEqual(lookup.key, self.key)
        self.assertEqual(lookup.message_type, FindNode)
        self.assertEqual(lookup.local_node, self.node)
        self.assertEqual(lookup.active_probes, [])
        self.assertEqual(lookup.contacted, set())
        self.assertEqual(lookup.active_candidates, set())
        self.assertEqual(lookup.pending_iteration, None)
        self.assertEqual(lookup.slow_node_count, 0)
        self.assertEqual(lookup.shortlist, [])

    def test_init_timeout_called(self):
        """
        Ensure the cancel method is called after timeout seconds.
        """
        lookup = Lookup(self.key, FindNode, self.node, self.timeout)
        lookup.cancel = MagicMock()
        self.clock.advance(self.timeout)
        lookup.cancel.called_once_with(lookup)

    def test_init_finds_close_nodes(self):
        """
        Ensure that __init__ attempts to call find_close_nodes on the routing
        table.
        """
        self.node._routing_table.find_close_nodes = MagicMock()
        Lookup(self.key, FindNode, self.node, self.timeout)
        self.node._routing_table.find_close_nodes.\
            assert_called_once_with(self.key)

    def test_init_touches_kbucket(self):
        """
        If the target key is not the local node's id then touch_kbucket needs
        to be called to update the last_accessed attribute of the K-bucket
        containing the target key.
        """
        self.node._routing_table.touch_kbucket = MagicMock()
        Lookup(self.key, FindNode, self.node, self.timeout)
        self.node._routing_table.touch_kbucket.\
            assert_called_once_with(self.key)

    def test_init_skips_touch_kbucket_if_node_id_is_key(self):
        """
        The touch_kbucket operation only needs to happen if the target key is
        NOT the local node's id.
        """
        self.node._routing_table.touch_kbucket = MagicMock()
        Lookup(self.node.id, FindNode, self.node, self.timeout)
        self.assertEqual(0, self.node._routing_table.touch_kbucket.call_count)

    def test_init_no_known_nodes(self):
        """
        Checks that if the local node doesn't know of any other nodes then
        the resulting lookup calls back with None.
        """
        lookup = Lookup(self.key, FindNode, self.node, self.timeout)
        self.assertIsInstance(lookup, defer.Deferred)
        self.assertTrue(lookup.called)

        def callback_check(result):
            self.assertEqual(None, result)

        lookup.addCallback(callback_check)


class TestNode(unittest.TestCase):
    """
    Ensures the Node class works as expected.
    """

    def setUp(self):
        """
        Following the pattern explained here:

        http://twistedmatrix.com/documents/current/core/howto/trial.html
        """
        self.node_id = '1234567890abc'
        self.node = Node(self.node_id)
        self.factory = DHTFactory(self.node)
        self.protocol = self.factory.buildProtocol(('127.0.0.1', 0))
        self.transport = proto_helpers.StringTransport()
        self.transport.abortConnection = fakeAbortConnection
        self.protocol.makeConnection(self.transport)
        self.clock = task.Clock()
        reactor.callLater = self.clock.callLater
        self.value = 'value'
        self.signature = ('\x882f\xf9A\xcd\xf9\xb1\xcc\xdbl\x1c\xb2\xdb' +
                          '\xa3UQ\x9a\x08\x96\x12\x83^d\xd8M\xc2`\x81Hz' +
                          '\x84~\xf4\x9d\x0e\xbd\x81\xc4/\x94\x9dfg\xb2aq' +
                          '\xa6\xf8!k\x94\x0c\x9b\xb5\x8e \xcd\xfb\x87' +
                          '\x83`wu\xeb\xf2\x19\xd6X\xdd\xb3\x98\xb5\xbc#B' +
                          '\xe3\n\x85G\xb4\x9c\x9b\xb0-\xd2B\x83W\xb8\xca' +
                          '\xecv\xa9\xc4\x9d\xd8\xd0\xf1&\x1a\xfaw\xa0\x99' +
                          '\x1b\x84\xdad$\xebO\x1a\x9e:w\x14d_\xe3\x03#\x95' +
                          '\x9d\x10B\xe7\x13')
        self.uuid = str(uuid4())
        self.timestamp = 1350544046.084875
        self.expires = 1352221970.14242
        self.name = 'name'
        self.meta = {'meta': 'value'}
        self.version = get_version()
        self.key = construct_key(PUBLIC_KEY, self.name)

    def test_init(self):
        """
        Ensures the class is instantiated correctly.
        """
        node = Node(123)
        self.assertEqual(123, node.id)
        self.assertTrue(node._routing_table)
        self.assertEqual({}, node._data_store)
        self.assertEqual({}, node._pending)
        self.assertEqual('ssl:%s:%d', node._client_string)
        self.assertEqual(get_version(), node.version)

    def test_message_received_calls_routing_table(self):
        """
        Ensures an inbound message updates the routing table.
        """
        self.node._routing_table.add_contact = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Check it results in a call to the routing table's add_contact method.
        peer = self.protocol.transport.getPeer()
        self.assertEqual(1, self.node._routing_table.add_contact.call_count)
        arg1 = self.node._routing_table.add_contact.call_args[0][0]
        self.assertTrue(isinstance(arg1, Contact))
        self.assertEqual(msg.node, arg1.id)
        self.assertEqual(peer.host, arg1.address)
        self.assertEqual(peer.port, arg1.port)
        self.assertEqual(msg.version, arg1.version)
        self.assertTrue(isinstance(arg1.last_seen, float))

    def test_message_received_ping(self):
        """
        Ensures a Ping message is handled correctly.
        """
        self.node.handle_ping = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Check it results in a call to the node's handle_ping method.
        self.node.handle_ping.assert_called_once_with(msg, self.protocol)

    def test_message_received_pong(self):
        """
        Ensures a Pong message is handled correctly.
        """
        self.node.handle_pong = MagicMock()
        # Create a simple Pong message.
        uuid = str(uuid4())
        version = get_version()
        msg = Pong(uuid, self.node_id, version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Check it results in a call to the node's handle_pong method.
        self.node.handle_pong.assert_called_once_with(msg)

    def test_message_received_store(self):
        """
        Ensures a Store message is handled correctly.
        """
        self.node.handle_store = MagicMock()
        # Create a simple Store message.
        msg = Store(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Dummy contact.
        contact = Contact(self.node.id, '192.168.1.1', 54321, self.version)
        # Check it results in a call to the node's handle_store method.
        self.node.handle_store.assert_called_once_with(msg, self.protocol,
                                                       contact)

    def test_message_received_find_node(self):
        """
        Ensures a FindNode message is handled correctly.
        """
        self.node.handle_find_node = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        key = '12345abc'
        msg = FindNode(uuid, self.node_id, key, version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Check it results in a call to the node's handle_find_node method.
        self.node.handle_find_node.assert_called_once_with(msg, self.protocol)

    def test_message_received_find_value(self):
        """
        Ensures a FindValue message is handled correctly.
        """
        self.node.handle_find_value = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        key = '12345abc'
        msg = FindValue(uuid, self.node_id, key, version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Check it results in a call to the node's handle_find_value method.
        self.node.handle_find_value.assert_called_once_with(msg, self.protocol)

    def test_message_received_error(self):
        """
        Ensures an Error message is handled correctly.
        """
        self.node.handle_error = MagicMock()
        # Create an Error message.
        uuid = str(uuid4())
        version = get_version()
        code = 1
        title = ERRORS[code]
        details = {'foo': 'bar'}
        msg = Error(uuid, self.node_id, code, title, details, version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Dummy contact.
        contact = Contact(self.node.id, '192.168.1.1', 54321, self.version)
        # Check it results in a call to the node's handle_error method.
        self.node.handle_error.assert_called_once_with(msg, self.protocol,
                                                       contact)

    def test_message_received_value(self):
        """
        Ensures a Value message is handled correctly.
        """
        self.node.handle_value = MagicMock()
        # Create a Value message.
        uuid = str(uuid4())
        msg = Value(uuid, self.node.id, self.key, self.value, self.timestamp,
                    self.expires, PUBLIC_KEY, self.name, self.meta,
                    self.signature, self.node.version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Dummy contact.
        contact = Contact(self.node.id, '192.168.1.1', 54321, self.version)
        # Check it results in a call to the node's handle_value method.
        self.node.handle_value.assert_called_once_with(msg, contact)

    def test_message_received_nodes(self):
        """
        Ensures a Nodes message is handled correctly.
        """
        self.node.handle_nodes = MagicMock()
        # Create a nodes message.
        msg = Nodes(self.uuid, self.node.id,
                    ((self.node.id, '127.0.0.1', 1908, '0.1')),
                    self.node.version)
        # Receive it...
        self.node.message_received(msg, self.protocol)
        # Check it results in a call to the node's handle_nodes method.
        self.node.handle_nodes.assert_called_once_with(msg)

    def test_handle_ping(self):
        """
        Ensures the handle_ping method returns a Pong message.
        """
        # Mock
        self.protocol.sendMessage = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Handle it.
        self.node.handle_ping(msg, self.protocol)
        # Check the result.
        result = Pong(uuid, self.node.id, version)
        self.protocol.sendMessage.assert_called_once_with(result, True)

    def test_handle_ping_loses_connection(self):
        """
        Ensures the handle_ping method loses the connection after sending the
        Pong.
        """
        # Mock
        self.protocol.transport.loseConnection = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Handle it.
        self.node.handle_ping(msg, self.protocol)
        # Ensure the loseConnection method was also called.
        self.protocol.transport.loseConnection.assert_called_once_with()

    @patch('drogulus.dht.node.validate_message')
    def test_handle_store_checks_with_validate_message(self, mock_validator):
        """
        Ensure that the validate_message function is called as part of
        handle_store.
        """
        # Mock
        mock_validator.return_value = (1, 2)
        self.protocol.sendMessage = MagicMock()
        # Create a fake contact and valid message.
        msg = Store(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        self.node.handle_store(msg, self.protocol, other_node)
        mock_validator.assert_called_once_with(msg)

    @patch('drogulus.dht.node.reactor.callLater')
    def test_handle_store(self, mock_call_later):
        """
        Ensures a correct Store message is handled correctly.
        """
        # Mock
        self.protocol.sendMessage = MagicMock()
        # Incoming message and peer
        msg = Store(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        self.node.handle_store(msg, self.protocol, other_node)
        # Ensure the message is in local storage.
        self.assertIn(self.key, self.node._data_store)
        # Ensure call_later has been called to replicate the value.
        mock_call_later.assert_called_once_with(REPLICATE_INTERVAL,
                                                self.node.send_replicate,
                                                msg)
        # Ensure the response is a Pong message.
        result = Pong(self.uuid, self.node.id, self.version)
        self.protocol.sendMessage.assert_called_once_with(result, True)

    def test_handle_store_old_value(self):
        """
        Ensures that a Store message containing an out-of-date version of a
        value already known to the node is handled correctly:

        * The current up-to-date value is not overwritten.
        * The node responds with an appropriate error message.
        """
        # Create existing up-to-date value
        newer_msg = Store(self.uuid, self.node.id, self.key, self.value,
                          self.timestamp, self.expires, PUBLIC_KEY, self.name,
                          self.meta, self.signature, self.version)
        self.node._data_store.set_item(newer_msg.key, newer_msg)
        # Incoming message and peer
        old_timestamp = self.timestamp - 9999
        old_value = 'old value'
        old_sig = ('\t^#F:\x0c;\r{Z\xbd$\xe4\xffz}\xb6Q\xb3g6\xca,\xe8' +
                   '\xe4eY<g\x92tN\x8f\xbe\x8fs|\xdf\xe5O\xc6eZ\xef\xf5' +
                   '\xd8\xab?g\xd7y\x81\xbeB\\\xe0=\xd1{\xcc\x0f%#\x9ad' +
                   '\xcf\xea\xbd\x95\x0e\xed\xd7\x98\xfc\x85O\x81\x15' +
                   '\x18/\xcb\xa0\x01\x1f+\x12\x8e\xdc\xbf\x9a\r\xd6\xfb' +
                   '\xe0\xab\xc9\xff\xb5\xe5\x18\xb8\xe9\x8c\x13\xd1\xa5' +
                   '\xba\xeb\xfa\xce\xaaT\xc8\x8c:\xcd\xc7\x0c\xfdCD\x00' +
                   '\xd9\x93\xfeo><')
        old_msg = Store(self.uuid, self.node.id, self.key, old_value,
                        old_timestamp, self.expires, PUBLIC_KEY, self.name,
                        self.meta, old_sig, self.version)
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        # Check for the expected exception.
        ex = self.assertRaises(ValueError, self.node.handle_store, old_msg,
                               self.protocol, other_node)
        details = {
            'new_timestamp': '%d' % self.timestamp
        }
        self.assertEqual(ex.args[0], 8)
        self.assertEqual(ex.args[1], ERRORS[8])
        self.assertEqual(ex.args[2], details)
        self.assertEqual(ex.args[3], self.uuid)
        # Ensure the original message is in local storage.
        self.assertIn(self.key, self.node._data_store)
        self.assertEqual(newer_msg, self.node._data_store[self.key])

    def test_handle_store_new_value(self):
        """
        Ensures that a Store message containing a new version of a
        value already known to the node is handled correctly.
        """
        # Mock
        self.protocol.sendMessage = MagicMock()
        # Create existing up-to-date value
        old_timestamp = self.timestamp - 9999
        old_value = 'old value'
        old_sig = ('\t^#F:\x0c;\r{Z\xbd$\xe4\xffz}\xb6Q\xb3g6\xca,\xe8' +
                   '\xe4eY<g\x92tN\x8f\xbe\x8fs|\xdf\xe5O\xc6eZ\xef\xf5' +
                   '\xd8\xab?g\xd7y\x81\xbeB\\\xe0=\xd1{\xcc\x0f%#\x9ad' +
                   '\xcf\xea\xbd\x95\x0e\xed\xd7\x98\xfc\x85O\x81\x15' +
                   '\x18/\xcb\xa0\x01\x1f+\x12\x8e\xdc\xbf\x9a\r\xd6\xfb' +
                   '\xe0\xab\xc9\xff\xb5\xe5\x18\xb8\xe9\x8c\x13\xd1\xa5' +
                   '\xba\xeb\xfa\xce\xaaT\xc8\x8c:\xcd\xc7\x0c\xfdCD\x00' +
                   '\xd9\x93\xfeo><')
        old_msg = Store(self.uuid, self.node.id, self.key, old_value,
                        old_timestamp, self.expires, PUBLIC_KEY, self.name,
                        self.meta, old_sig, self.version)
        self.node._data_store.set_item(old_msg.key, old_msg)
        self.assertIn(self.key, self.node._data_store)
        self.assertEqual(old_msg, self.node._data_store[self.key])
        # Incoming message and peer
        new_msg = Store(self.uuid, self.node.id, self.key, self.value,
                        self.timestamp, self.expires, PUBLIC_KEY, self.name,
                        self.meta, self.signature, self.version)
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        # Store the new version of the message.
        self.node.handle_store(new_msg, self.protocol, other_node)
        # Ensure the message is in local storage.
        self.assertIn(self.key, self.node._data_store)
        self.assertEqual(new_msg, self.node._data_store[self.key])
        # Ensure the response is a Pong message.
        result = Pong(self.uuid, self.node.id, self.version)
        self.protocol.sendMessage.assert_called_once_with(result, True)

    def test_handle_store_bad_message(self):
        """
        Ensures an invalid Store message is handled correctly.
        """
        # Incoming message and peer
        msg = Store(self.uuid, self.node.id, self.key, 'wrong value',
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        other_node = Contact('12345678abc', '127.0.0.1', 1908,
                             self.version, time.time())
        self.node._routing_table.add_contact(other_node)
        # Sanity check for expected routing table start state.
        self.assertEqual(1, len(self.node._routing_table._buckets[0]))
        # Handle faulty message.
        ex = self.assertRaises(ValueError, self.node.handle_store, msg,
                               self.protocol, other_node)
        # Check the exception
        self.assertEqual(ex.args[0], 6)
        self.assertEqual(ex.args[1], ERRORS[6])
        details = {
            'message': 'You have been removed from remote routing table.'
        }
        self.assertEqual(ex.args[2], details)
        self.assertEqual(ex.args[3], self.uuid)
        # Ensure the message is not in local storage.
        self.assertNotIn(self.key, self.node._data_store)
        # Ensure the contact is not in the routing table
        self.assertEqual(0, len(self.node._routing_table._buckets[0]))

    def test_handle_store_loses_connection(self):
        """
        Ensures the handle_store method with a good Store message loses the
        connection after sending the Pong message.
        """
        # Mock
        self.protocol.transport.loseConnection = MagicMock()
        # Incoming message and peer
        msg = Store(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        self.node.handle_store(msg, self.protocol, other_node)
        # Ensure the loseConnection method was also called.
        self.protocol.transport.loseConnection.assert_called_once_with()

    def test_handle_find_nodes(self):
        """
        Ensure a valid FindNodes message is handled correctly.
        """
        # Mock
        self.protocol.sendMessage = MagicMock()
        # Populate the routing table with contacts.
        for i in range(512):
            contact = Contact(2 ** i, "192.168.0.%d" % i, self.version, 0)
            self.node._routing_table.add_contact(contact)
        # Incoming FindNode message
        msg = FindNode(self.uuid, self.node.id, self.key, self.version)
        self.node.handle_find_node(msg, self.protocol)
        # Check the response sent back
        other_nodes = [(n.id, n.address, n.port, n.version) for n in
                       self.node._routing_table.find_close_nodes(self.key)]
        result = Nodes(msg.uuid, self.node.id, other_nodes, self.version)
        self.protocol.sendMessage.assert_called_once_with(result, True)

    def test_handle_find_nodes_loses_connection(self):
        """
        Ensures the handle_find_nodes method loses the connection after
        sending the Nodes message.
        """
        # Mock
        self.protocol.transport.loseConnection = MagicMock()
        # Populate the routing table with contacts.
        for i in range(512):
            contact = Contact(2 ** i, "192.168.0.%d" % i, self.version, 0)
            self.node._routing_table.add_contact(contact)
        # Incoming FindNode message
        msg = FindNode(self.uuid, self.node.id, self.key, self.version)
        self.node.handle_find_node(msg, self.protocol)
        # Ensure the loseConnection method was also called.
        self.protocol.transport.loseConnection.assert_called_once_with()

    def test_handle_find_value_with_match(self):
        """
        Ensures the handle_find_value method responds with a matching Value
        message if the value exists in the datastore.
        """
        # Store value.
        val = Store(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        self.node._data_store.set_item(val.key, val)
        # Mock
        self.protocol.sendMessage = MagicMock()
        # Incoming FindValue message
        msg = FindValue(self.uuid, self.node.id, self.key, self.version)
        self.node.handle_find_value(msg, self.protocol)
        # Check the response sent back
        result = Value(msg.uuid, self.node.id, val.key, val.value,
                       val.timestamp, val.expires, val.public_key, val.name,
                       val.meta, val.sig, val.version)
        self.protocol.sendMessage.assert_called_once_with(result, True)

    def test_handle_find_value_no_match(self):
        """
        Ensures the handle_find_value method calls the handle_find_nodes
        method with the correct values if no matching value exists in the
        local datastore.
        """
        # Mock
        self.node.handle_find_node = MagicMock()
        # Incoming FindValue message
        msg = FindValue(self.uuid, self.node.id, self.key, self.version)
        self.node.handle_find_value(msg, self.protocol)
        # Check the response sent back
        self.node.handle_find_node.assert_called_once_with(msg, self.protocol)

    def test_handle_find_value_loses_connection(self):
        """
        Ensures the handle_find_value method loses the connection after
        sending the a matched value.
        """
        # Store value.
        val = Store(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.version)
        self.node._data_store.set_item(val.key, val)
        # Mock
        self.protocol.transport.loseConnection = MagicMock()
        # Incoming FindValue message
        msg = FindValue(self.uuid, self.node.id, self.key, self.version)
        self.node.handle_find_value(msg, self.protocol)
        # Ensure the loseConnection method was also called.
        self.protocol.transport.loseConnection.assert_called_once_with()

    def test_handle_error_writes_to_log(self):
        """
        Ensures the handle_error method writes details about the error to the
        log.
        """
        log.msg = MagicMock()
        # Create an Error message.
        uuid = str(uuid4())
        version = get_version()
        code = 1
        title = ERRORS[code]
        details = {'foo': 'bar'}
        msg = Error(uuid, self.node_id, code, title, details, version)
        # Dummy contact.
        contact = Contact(self.node.id, '192.168.1.1', 54321, self.version)
        # Receive it...
        self.node.handle_error(msg, self.protocol, contact)
        # Check it results in two calls to the log.msg method (one to signify
        # an error has happened, the other the actual error message).
        self.assertEqual(2, log.msg.call_count)

    @patch('drogulus.dht.node.validate_message')
    def test_handle_value_checks_with_validate_message(self, mock_validator):
        """
        Ensure that the validate_message function is called as part of
        handle_value.
        """
        mock_validator.return_value = (1, 2)
        # Create a fake contact and valid message.
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        msg = Value(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.node.version)
        # Handle it.
        self.node.handle_value(msg, other_node)
        mock_validator.assert_called_once_with(msg)

    def test_handle_value_with_valid_message(self):
        """
        Ensure a valid Value is checked and results in the expected call to
        trigger_deferred.
        """
        # Mock
        self.node.trigger_deferred = MagicMock()
        # Create a fake contact and valid message.
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        msg = Value(self.uuid, self.node.id, self.key, self.value,
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.node.version)
        # Handle it.
        self.node.handle_value(msg, other_node)
        self.node.trigger_deferred.assert_called_once_with(msg)

    def test_handle_value_with_bad_message(self):
        """
        Ensure a bad message results in an error sent to trigger_deferred along
        with expected logging and removal or the other node from the local
        node's routing table.
        """
        # Mocks
        self.node._routing_table.remove_contact = MagicMock()
        self.node.trigger_deferred = MagicMock()
        patcher = patch('drogulus.dht.node.log.msg')
        mockLog = patcher.start()
        # Create a fake contact and valid message.
        other_node = Contact(self.node.id, '127.0.0.1', 1908,
                             self.version, time.time())
        msg = Value(self.uuid, self.node.id, self.key, 'bad_value',
                    self.timestamp, self.expires, PUBLIC_KEY, self.name,
                    self.meta, self.signature, self.node.version)
        # Handle it.
        self.node.handle_value(msg, other_node)
        # Logger was called twice.
        self.assertEqual(2, mockLog.call_count)
        # other node was removed from the routing table.
        self.node._routing_table.remove_contact.\
            assert_called_once_with(other_node.id, True)
        # trigger_deferred called as expected.
        self.assertEqual(1, self.node.trigger_deferred.call_count)
        self.assertEqual(self.node.trigger_deferred.call_args[0][0], msg)
        self.assertIsInstance(self.node.trigger_deferred.call_args[0][1],
                              ValueError)
        # Tidy up.
        patcher.stop()

    def test_handle_nodes(self):
        """
        Ensure a Nodes message merely results in the expected call to
        trigger_deferred.
        """
        self.node.trigger_deferred = MagicMock()
        msg = Nodes(self.uuid, self.node.id,
                    ((self.node.id, '127.0.0.1', 1908, '0.1')),
                    self.node.version)
        self.node.handle_nodes(msg)
        self.node.trigger_deferred.assert_called_once_with(msg)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message(self, mock_client):
        """
        Ensure send_message returns a deferred.
        """
        # Mock, mock, glorious mock; nothing quite like it to test a code
        # block. (To the tune of "Mud, mud, glorious mud!")
        mock_client.return_value = FakeClient(self.protocol)
        # Mock the callLater function
        patcher = patch('drogulus.dht.node.reactor.callLater')
        mockCallLater = patcher.start()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        # Check for the deferred.
        result = self.node.send_message(contact, msg)
        self.assertTrue(isinstance(result, defer.Deferred))
        # Ensure the timeout function was called
        call_count = mockCallLater.call_count
        # Tidy up.
        patcher.stop()
        # Check callLater was called twice - once each for connection timeout
        # and message timeout.
        self.assertEqual(2, call_count)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message_on_connect_adds_message_to_pending(self,
                                                             mock_client):
        """
        Ensure that when a connection is made the on_connect function wrapped
        inside send_message adds the message and deferred to the pending
        messages dictionary.
        """
        # Mock.
        mock_client.return_value = FakeClient(self.protocol)
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        deferred = self.node.send_message(contact, msg)
        self.assertIn(uuid, self.node._pending)
        self.assertEqual(self.node._pending[uuid], deferred)
        # Tidies up.
        self.clock.advance(RPC_TIMEOUT)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message_timeout_connection_cancel_called(self, mock_client):
        """
        If attempting to connect times out before the connection is eventuially
        made, ensure the connection's deferred is cancelled.
        """
        mock_client.return_value = FakeClient(self.protocol, True, True, True)
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        self.node.send_message(contact, msg)
        self.clock.advance(RPC_TIMEOUT)
        self.assertNotIn(uuid, self.node._pending)
        self.assertEqual(1,
                         mock_client.return_value.cancel_function.call_count)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message_timeout_remove_contact(self, mock_client):
        """
        If the connection deferred is cancelled ensure that the node's
        _routing_table.remove_contact is called once.
        """
        mock_client.return_value = FakeClient(self.protocol, True, True, False)
        self.node._routing_table.remove_contact = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        self.node.send_message(contact, msg)
        self.clock.advance(RPC_TIMEOUT)
        self.assertNotIn(uuid, self.node._pending)
        self.node._routing_table.remove_contact.\
            assert_called_once_with(self.node.id)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message_response_timeout_call_later(self, mock_client):
        """
        Ensure that when a connection is made the on_connect function wrapped
        inside send_message calls callLater with the response_timeout function.
        """
        mock_client.return_value = FakeClient(self.protocol)
        # Mock the timeout function
        patcher = patch('drogulus.dht.node.response_timeout')
        mockTimeout = patcher.start()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        deferred = self.node.send_message(contact, msg)
        self.assertIn(uuid, self.node._pending)
        self.assertEqual(self.node._pending[uuid], deferred)
        self.clock.advance(RESPONSE_TIMEOUT)
        # Ensure the timeout function was called
        self.assertEqual(1, mockTimeout.call_count)
        # Tidy up.
        patcher.stop()

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message_sends_message(self, mock_client):
        """
        Ensure that the message passed in to send_message gets sent down the
        wire to the recipient.
        """
        mock_client.return_value = FakeClient(self.protocol)
        self.protocol.sendMessage = MagicMock()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        self.node.send_message(contact, msg)
        self.protocol.sendMessage.assert_called_once_with(msg)
        # Tidy up.
        self.clock.advance(RPC_TIMEOUT)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_message_fires_errback_in_case_of_errors(self, mock_client):
        """
        Ensure that if there's an error during connection or sending of the
        message then the errback is fired.
        """
        mock_client.return_value = FakeClient(self.protocol, success=False)
        errback = MagicMock()
        patcher = patch('drogulus.dht.node.log.msg')
        mockLog = patcher.start()
        # Create a simple Ping message.
        uuid = str(uuid4())
        version = get_version()
        msg = Ping(uuid, self.node_id, version)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        deferred = self.node.send_message(contact, msg)
        deferred.addErrback(errback)
        # The errback is called and the error is logged automatically.
        self.assertEqual(1, errback.call_count)
        self.assertEqual(2, mockLog.call_count)
        # Tidy up.
        patcher.stop()
        self.clock.advance(RPC_TIMEOUT)

    def test_trigger_deferred_no_match(self):
        """
        Ensures that there are no changes to the _pending dict if there are
        no matches for the incoming message's uuid.
        """
        to_not_match = str(uuid4())
        self.node._pending[to_not_match] = defer.Deferred()
        # Create a simple Pong message.
        uuid = str(uuid4())
        version = get_version()
        msg = Pong(uuid, self.node_id, version)
        # Trigger.
        self.node.trigger_deferred(msg)
        # Check.
        self.assertEqual(1, len(self.node._pending))
        self.assertIn(to_not_match, self.node._pending)

    def test_trigger_deferred_with_error(self):
        """
        Ensures that an errback is called on the correct deferred given the
        incoming message's uuid if the error flag is passed in.
        """
        uuid = str(uuid4())
        deferred = defer.Deferred()
        self.node._pending[uuid] = deferred
        handler = MagicMock()
        deferred.addErrback(handler)
        # Create an Error message.
        version = get_version()
        code = 1
        title = ERRORS[code]
        details = {'foo': 'bar'}
        msg = Error(uuid, self.node_id, code, title, details, version)
        # Sanity check.
        self.assertEqual(1, len(self.node._pending))
        # Trigger.
        error = ValueError('Information about the erroneous message')
        self.node.trigger_deferred(msg, error)
        # The deferred has fired with an errback.
        self.assertTrue(deferred.called)
        self.assertEqual(1, handler.call_count)
        self.assertEqual(handler.call_args[0][0].value, error)
        self.assertEqual(handler.call_args[0][0].value.message, msg)
        self.assertEqual(handler.call_args[0][0].__class__, Failure)

    def test_trigger_deferred_with_ok_message(self):
        """
        Ensures that a callback is triggered on the correct deferred given the
        incoming message's uuid.
        """
        # Set up a simple Pong message.
        uuid = str(uuid4())
        deferred = defer.Deferred()
        self.node._pending[uuid] = deferred
        handler = MagicMock()
        deferred.addCallback(handler)
        version = get_version()
        msg = Pong(uuid, self.node_id, version)
        # Sanity check.
        self.assertEqual(1, len(self.node._pending))
        # Trigger.
        self.node.trigger_deferred(msg)
        # The deferred has fired with a callback.
        self.assertTrue(deferred.called)
        self.assertEqual(1, handler.call_count)
        self.assertEqual(handler.call_args[0][0], msg)
        # The deferred is removed from pending.
        self.assertEqual(0, len(self.node._pending))

    def test_trigger_deferred_cleans_up(self):
        """
        Ensures that once the deferred is triggered it is cleaned from the
        node's _pending dict.
        """
        # Set up a simple Pong message.
        uuid = str(uuid4())
        deferred = defer.Deferred()
        self.node._pending[uuid] = deferred
        handler = MagicMock()
        deferred.addCallback(handler)
        version = get_version()
        msg = Pong(uuid, self.node_id, version)
        # Sanity check.
        self.assertEqual(1, len(self.node._pending))
        # Trigger.
        self.node.trigger_deferred(msg)
        # The deferred is removed from pending.
        self.assertEqual(0, len(self.node._pending))

    def test_handle_pong(self):
        """
        Ensures that a pong message triggers the correct deferred that was
        originally created by an outgoing (ping) message.
        """
        # Mock
        self.node.trigger_deferred = MagicMock()
        # Create a simple Pong message.
        uuid = str(uuid4())
        version = get_version()
        msg = Pong(uuid, self.node_id, version)
        # Handle it.
        self.node.handle_pong(msg)
        # Check the result.
        result = Pong(uuid, self.node.id, version)
        self.node.trigger_deferred.assert_called_once_with(result)

    @patch('drogulus.dht.node.clientFromString')
    def test_send_ping_returns_deferred(self, mock_client):
        """
        Ensures that sending a ping returns a deferred.
        """
        mock_client.return_value = FakeClient(self.protocol)
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        deferred = self.node.send_ping(contact)
        self.assertIsInstance(deferred, defer.Deferred)
        # Tidy up.
        self.clock.advance(RPC_TIMEOUT)

    def test_send_ping_calls_send_message(self):
        """
        Ensures that sending a ping calls the node's send_message method with
        the ping message.
        """
        # Mock
        self.node.send_message = MagicMock()
        # Dummy contact.
        contact = Contact(self.node.id, '127.0.0.1', 54321, self.version)
        self.node.send_ping(contact)
        self.assertEqual(1, self.node.send_message.call_count)
        called_contact = self.node.send_message.call_args[0][0]
        self.assertEqual(contact, called_contact)
        message_to_send = self.node.send_message.call_args[0][1]
        self.assertIsInstance(message_to_send, Ping)

    @patch('drogulus.dht.node.generate_signature')
    def test_send_store_generates_signature(self, mock):
        """
        Ensure the generate_signature function is called with the expected
        arguments as part of send_store.
        """
        mock.return_value = 'test'
        self.node.send_store(PRIVATE_KEY, PUBLIC_KEY, self.name,
                             self.value, self.timestamp, self.expires,
                             self.meta)
        mock.assert_called_once_with(self.value, self.timestamp, self.expires,
                                     self.name, self.meta, PRIVATE_KEY)

    @patch('drogulus.dht.node.construct_key')
    def test_send_store_makes_compound_key(self, mock):
        """
        Ensure the construct_key function is called with the expected arguments
        as part of send_store.
        """
        mock.return_value = 'test'
        self.node.send_store(PRIVATE_KEY, PUBLIC_KEY, self.name,
                             self.value, self.timestamp, self.expires,
                             self.meta)
        mock.assert_called_once_with(PUBLIC_KEY, self.name)

    def test_send_store_calls_send_replicate(self):
        """
        Ensure send_replicate is called as part of send_store.
        """
        self.node.send_replicate = MagicMock()
        self.node.send_store(PRIVATE_KEY, PUBLIC_KEY, self.name,
                             self.value, self.timestamp, self.expires,
                             self.meta)
        self.assertEqual(1, self.node.send_replicate.call_count)

    def test_send_store_creates_expected_store_message(self):
        """
        Ensure the message passed in to send_replicate looks correct.
        """
        self.node.send_replicate = MagicMock()
        self.node.send_store(PRIVATE_KEY, PUBLIC_KEY, self.name,
                             self.value, self.timestamp, self.expires,
                             self.meta)
        self.assertEqual(1, self.node.send_replicate.call_count)
        message_to_send = self.node.send_replicate.call_args[0][0]
        self.assertIsInstance(message_to_send, Store)
        self.assertTrue(message_to_send.uuid)
        self.assertEqual(message_to_send.node, self.node.id)
        self.assertEqual(message_to_send.key, self.key)
        self.assertEqual(message_to_send.value, self.value)
        self.assertEqual(message_to_send.timestamp, self.timestamp)
        self.assertEqual(message_to_send.expires, self.expires)
        self.assertEqual(message_to_send.public_key, PUBLIC_KEY)
        self.assertEqual(message_to_send.name, self.name)
        self.assertEqual(message_to_send.meta, self.meta)
        self.assertEqual(message_to_send.sig, self.signature)
        self.assertEqual(message_to_send.version, self.node.version)
