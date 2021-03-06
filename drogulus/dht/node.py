# -*- coding: utf-8 -*-
"""
Contains code that defines the behaviour of the local node in the DHT network.
"""

# Copyright (C) 2012-2013 Nicholas H.Tollervey.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from twisted.python import log
from twisted.internet import reactor, defer
from twisted.internet.endpoints import clientFromString
import time
from uuid import uuid4

from drogulus import constants
from drogulus.net.messages import (Error, Ping, Pong, Store, FindNode, Nodes,
                                   FindValue, Value)
from drogulus.net.protocol import DHTFactory
from routingtable import RoutingTable
from datastore import DictDataStore
from contact import Contact
from drogulus.crypto import validate_message, construct_key, generate_signature
from drogulus.version import get_version


def response_timeout(message, protocol, node):
    """
    Called when a pending message (identified with a uuid) awaiting a response
    via a given protocol object times-out. Closes the connection and removes
    the deferred from the "pending" dictionary.
    """
    uuid = message.uuid
    pending = node._pending
    if uuid in pending:
        pending[uuid].cancel()
        del pending[uuid]
        protocol.transport.abortConnection()
        node._routing_table.remove_contact(message.node)


class Lookup(defer.Deferred):
    """
    Encapsulates a lookup in the DHT given a particular key and message type.
    Will callback when a result is found or errback otherwise.
    """

    def __init__(self, key, message_type, local_node, timeout=None,
                 canceller=None):
        """
        Sets up the lookup to search for a certain key with a particular
        message_type using the DHT state found in the local_node. If defined,
        will cancel after timeout seconds. See the documentation for
        twisted.internet.defer.Deferred for explanation of canceller.
        """
        defer.Deferred.__init__(self, canceller)
        self.key = key
        self.message_type = message_type
        self.local_node = local_node
        # The list of active queries.
        self.active_probes = []
        # The set of peers that have already been contacted as part of this
        # lookup.
        self.contacted = set()
        # A set of active nodes that have been found during this lookup.
        self.active_candidates = set()
        # Will reference the deferred for the next iteration of the lookup if
        # another iteration is required.
        self.pending_iteration = None
        self.slow_node_count = 0
        if timeout:
            reactor.callLater(timeout, self.cancel)
        # To hold peers in the DHT that are known to the local node that are
        # possibly close to the target key.
        self.shortlist = self.local_node._routing_table.find_close_nodes(key)
        if self.key != self.local_node.id:
            # Update the last_accessed attribute of the affected k-bucket.
            self.local_node._routing_table.touch_kbucket(key)
        if not self.shortlist:
            # The node knows of no other nodes within the DHT.
            self.callback(None)

    def cancel(self):
        """
        Cancels this lookup in a clean fashion.
        """
        if self.pending_iteration:
            self.pending_iteration.cancel()
        defer.Deferred.cancel(self)


class Node(object):
    """
    This class represents a single local node in the DHT encapsulating its
    presence in the network.

    All interactions with the DHT network by a client application are
    performed via this class (or a subclass).
    """

    def __init__(self, id, client_string='ssl:%s:%d'):
        """
        Initialises the object representing the node with the given id.
        """
        # The node's ID within the distributed hash table.
        self.id = id
        # The routing table stores information about other nodes on the DHT.
        self._routing_table = RoutingTable(id)
        # The local key/value store containing data held by this node.
        self._data_store = DictDataStore()
        # A dictionary of IDs for messages pending a response and associated
        # deferreds to be fired when a response is completed.
        self._pending = {}
        # The template string to use when initiating a connection to another
        # node on the network.
        self._client_string = client_string
        # The version of Drogulus that this node implements.
        self.version = get_version()
        log.msg('Initialised node with id: %r' % self.id)

    def join(self, seed_nodes=None):
        """
        Causes the Node to join the DHT network. This should be called before
        any other DHT operations. The seedNodes argument contains a list of
        tuples describing existing nodes on the network in the form of their
        IP address and port.
        """
        pass

    def message_received(self, message, protocol):
        """
        Handles incoming messages.
        """
        # Update the routing table.
        peer = protocol.transport.getPeer()
        other_node = Contact(message.node, peer.host, peer.port,
                             message.version, time.time())
        log.msg('Message received from %s' % other_node)
        log.msg(message)
        self._routing_table.add_contact(other_node)
        # Sort on message type and pass to handler method. Explicit > implicit.
        if isinstance(message, Ping):
            self.handle_ping(message, protocol)
        elif isinstance(message, Pong):
            self.handle_pong(message)
        elif isinstance(message, Store):
            self.handle_store(message, protocol, other_node)
        elif isinstance(message, FindNode):
            self.handle_find_node(message, protocol)
        elif isinstance(message, FindValue):
            self.handle_find_value(message, protocol)
        elif isinstance(message, Error):
            self.handle_error(message, protocol, other_node)
        elif isinstance(message, Value):
            self.handle_value(message, other_node)
        elif isinstance(message, Nodes):
            self.handle_nodes(message)

    def send_message(self, contact, message):
        """
        Sends a message to the specified contact, adds it to the _pending
        dictionary and ensures it times-out after the correct period. If an
        error occurs the deferred's errback is called.
        """
        d = defer.Deferred()
        # open network call.
        client_string = self._client_string % (contact.address, contact.port)
        client = clientFromString(reactor, client_string)
        connection = client.connect(DHTFactory(self))
        # Ensure the connection will potentially time out.
        connection_timeout = reactor.callLater(constants.RPC_TIMEOUT,
                                               connection.cancel)

        def on_connect(protocol):
            # Cancel pending connection_timeout if it's still active.
            if connection_timeout.active():
                connection_timeout.cancel()
            # Send the message and add a timeout for the response.
            protocol.sendMessage(message)
            self._pending[message.uuid] = d
            reactor.callLater(constants.RESPONSE_TIMEOUT, response_timeout,
                              message, protocol, self)

        def on_error(error):
            log.msg('***** ERROR ***** connecting to %s' % contact)
            log.msg(error)
            self._routing_table.remove_contact(message.node)
            d.errback(error)

        connection.addCallbacks(on_connect, on_error)
        return d

    def trigger_deferred(self, message, error=False):
        """
        Given a message, will attempt to retrieve the deferred and trigger it
        with the appropriate callback or errback.
        """
        if message.uuid in self._pending:
            deferred = self._pending[message.uuid]
            if error:
                error.message = message
                deferred.errback(error)
            else:
                deferred.callback(message)
            # Remove the called deferred from the _pending dictionary.
            del self._pending[message.uuid]

    def handle_ping(self, message, protocol):
        """
        Handles an incoming Ping message. Returns a Pong message using the
        referenced protocol object.
        """
        pong = Pong(message.uuid, self.id, self.version)
        protocol.sendMessage(pong, True)

    def handle_pong(self, message):
        """
        Handles an incoming Pong message.
        """
        self.trigger_deferred(message)

    def handle_store(self, message, protocol, sender):
        """
        Handles an incoming Store message. Checks the provenance and timeliness
        of the message before storing locally. If there is a problem, removes
        the untrustworthy peer from the routing table. Otherwise, at
        REPLICATE_INTERVAL minutes in the future, the local node will attempt
        to replicate the Store message elsewhere in the DHT if such time is
        <= the message's expiry time.

        Sends a Pong message if successful otherwise replies with an
        appropriate Error.
        """
        # Check provenance
        is_valid, err_code = validate_message(message)
        if is_valid:
            # Ensure the node doesn't already have a more up-to-date version
            # of the value.
            current = self._data_store.get(message.key, False)
            if current and (message.timestamp < current.timestamp):
                # The node already has a later version of the value so
                # return an error.
                details = {
                    'new_timestamp': '%d' % current.timestamp
                }
                raise ValueError(8, constants.ERRORS[8], details,
                                 message.uuid)
            # Good to go, so store value.
            self._data_store.set_item(message.key, message)
            # Reply with a pong so the other end updates its routing table.
            pong = Pong(message.uuid, self.id, self.version)
            protocol.sendMessage(pong, True)
            # At some future time attempt to replicate the Store message
            # around the network IF it is within the message's expiry time.
            reactor.callLater(constants.REPLICATE_INTERVAL,
                              self.send_replicate, message)
        else:
            # Remove from the routing table.
            log.msg('Problem with Store command: %d - %s' %
                    (err_code, constants.ERRORS[err_code]))
            self._routing_table.remove_contact(sender.id, True)
            # Return an error.
            details = {
                'message': 'You have been removed from remote routing table.'
            }
            raise ValueError(err_code, constants.ERRORS[err_code], details,
                             message.uuid)

    def handle_find_node(self, message, protocol):
        """
        Handles an incoming FindNode message. Finds the details of up to K
        other nodes closer to the target key that *this* node knows about.
        Responds with a "Nodes" message containing the list of matching
        nodes.
        """
        target_key = message.key
        # List containing tuples of information about the matching contacts.
        other_nodes = [(n.id, n.address, n.port, n.version) for n in
                       self._routing_table.find_close_nodes(target_key)]
        result = Nodes(message.uuid, self.id, other_nodes, self.version)
        protocol.sendMessage(result, True)

    def handle_find_value(self, message, protocol):
        """
        Handles an incoming FindValue message. If the local node contains the
        value associated with the requested key replies with an appropriate
        "Value" message. Otherwise, responds with details of up to K other
        nodes closer to the target key that the local node knows about. In
        this case a "Nodes" message containing the list of matching nodes is
        sent to the caller.
        """
        match = self._data_store.get(message.key, False)
        if match:
            result = Value(message.uuid, self.id, match.key, match.value,
                           match.timestamp, match.expires, match.public_key,
                           match.name, match.meta, match.sig, match.version)
            protocol.sendMessage(result, True)
        else:
            self.handle_find_node(message, protocol)

    def handle_error(self, message, protocol, sender):
        """
        Handles an incoming Error message. Currently, this simply logs the
        error and closes the connection. In future this *may* remove the
        sender from the routing table (depending on the error).
        """
        # TODO: Handle error 8 (out of date data)
        log.msg('***** ERROR ***** from %s' % sender)
        log.msg(message)

    def handle_value(self, message, sender):
        """
        Handles an incoming Value message containing a value retrieved from
        another node on the DHT. Ensures the message is valid and calls the
        referenced deferred to signal the arrival of the value.

        TODO: How to handle invalid messages and errback the deferred.
        """
        # Check provenance
        is_valid, err_code = validate_message(message)
        if is_valid:
            self.trigger_deferred(message)
        else:
            log.msg('Problem with incoming Value: %d - %s' %
                    (err_code, constants.ERRORS[err_code]))
            log.msg(message)
            # Remove the remote node from the routing table.
            self._routing_table.remove_contact(sender.id, True)
            error = ValueError(constants.ERRORS[err_code])
            self.trigger_deferred(message, error)

    def handle_nodes(self, message):
        """
        Handles an incoming Nodes message containing information about other
        nodes on the network that are close to a requested key.
        """
        self.trigger_deferred(message)

    def iterative_lookup(self, key, message_class):
        """
        A generic lookup function for finding nodes or values within the
        distributed hash table. Takes a key that either references a value or
        location in the hash-space. This function returns a deferred that will
        fire wth the found value or a set of peers in the DHT that are close to
        the key. The message class should be either FindNode or FindValue.
        """
        pass

    def send_ping(self, contact):
        """
        Sends a ping request to the given contact and returns a deferred
        that is fired when the reply arrives or an error occurs.
        """
        new_uuid = str(uuid4())
        ping = Ping(new_uuid, self.id, self.version)
        return self.send_message(contact, ping)

    def send_store(self, private_key, public_key, name, value,
                   timestamp, expires, meta):
        """
        Sends a Store message to the given contact. The value contained within
        the message is stored against a key derived from the public_key and
        name. Furthermore, the message is cryptographically signed using the
        value, timestamp, expires, name and meta values.
        """
        new_uuid = str(uuid4())
        signature = generate_signature(value, timestamp, expires, name, meta,
                                       private_key)
        compound_key = construct_key(public_key, name)
        new_store = Store(new_uuid, self.id, compound_key, value, timestamp,
                          expires, public_key, name, meta, signature,
                          self.version)
        return self.send_replicate(new_store)

    def send_replicate(self, store_message):
        """
        Sends an existing valid Store message (that will probably have
        originated from a third party) to another peer on the network for the
        purposes of replication / spreading popular values.
        """
        # Check for expiry time..?
        # Find closest node...
        """
        new_uuid = str(uuid4())
        store = Store(new_uuid, self.id, store_message.key,
                      store_message.value, store_message.timestamp,
                      store_message.expires, store_message.public_key,
                      store_message.name, store_message.meta,
                      store_message.sig, self.version)
        return self.send_message(contact, store)"""

    def send_find_node(self, contact, id):
        """
        Sends a FindNode message to the given contact with the intention of
        obtaining contact information about the node with the specified id.
        """
        pass

    def send_find_value(self, contact, key):
        """
        Sends a FindValue message to the given contact with the intention of
        obtaining the value associated with the specified key.
        """
        pass
